"""
migrate_history.py — one-time import of legacy flat-file history into Postgres.

Before this branch, daily reports lived at data/reports/<date>.json on local
disk (never durable on Render's free tier — wiped on every redeploy/restart).
This branch moves history to Postgres (store.py) instead. If a still-running
instance has accumulated flat files before this deploy lands, this script
copies them into radar_history so they aren't silently lost the moment the
new code starts reading from Postgres only.

Safe to run more than once: every write is the same upsert store.save() uses
(INSERT ... ON CONFLICT DO UPDATE), so re-running just re-writes identical
rows. It never deletes or modifies data/reports/ — the flat files are left
alone so a rollback still has them.

Run manually, once, from the deployed instance (e.g. Render Shell):
    python migrate_history.py
Requires DATABASE_URL to be set — refuses to run without it.
"""
import datetime as _dt
import json
import os
import re
import sys

import store

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_REPORTS = os.path.join(_DATA, "reports")
_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


def main():
    if not store._URL:
        print("migrate_history: DATABASE_URL is not set — nothing to migrate into, stopping.")
        return 1
    if store.psycopg is None:
        print("migrate_history: psycopg is not installed — stopping.")
        return 1

    try:
        names = sorted(os.listdir(_REPORTS))
    except OSError:
        print("migrate_history: no %s directory — nothing to migrate (imported=0 skipped=0 failed=0)." % _REPORTS)
        return 0

    imported = skipped = failed = 0
    for name in names:
        m = _NAME_RE.match(name)
        if not m:
            skipped += 1
            print("migrate_history: SKIP %s (not a YYYY-MM-DD.json report file)" % name)
            continue
        date_str = m.group(1)
        try:
            _dt.date.fromisoformat(date_str)
        except ValueError:
            skipped += 1
            print("migrate_history: SKIP %s (invalid date in filename)" % name)
            continue

        path = os.path.join(_REPORTS, name)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            failed += 1
            print("migrate_history: FAILED %s (unreadable/invalid JSON: %s)" % (name, e))
            continue

        topic = data.get("topic") or ""
        geo = data.get("geo") or ""
        if not topic or not geo:
            failed += 1
            print("migrate_history: FAILED %s (report has no topic/geo)" % name)
            continue

        try:
            with store._connect() as conn:
                store._ensure_table(conn)
                conn.execute("""
                    INSERT INTO radar_history (date, topic, geo, data)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (date, topic, geo) DO UPDATE SET data = EXCLUDED.data
                """, (date_str, topic, geo, json.dumps(data)))
        except Exception as e:
            failed += 1
            print("migrate_history: FAILED %s (database write error: %s)" % (name, e))
            continue

        imported += 1
        print("migrate_history: imported %s (topic=%s geo=%s)" % (date_str, topic, geo))

    print("migrate_history: done — imported=%d skipped=%d failed=%d. "
          "Original files in %s were left untouched." % (imported, skipped, failed, _REPORTS))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
