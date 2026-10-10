#!/usr/bin/env python3
"""Where did the rows in this installation come from?  (read-only)

Run it when a count somewhere does not match what you expect - "137 offers",
"237 mails" - and you want to know which table holds them and which adapter put
them there.  It reads the database the app would read (``DREAMJOB_DB_PATH``,
honouring ``.env``), counts the tables that matter, and breaks the two that
carry provenance down by source.

It never writes, and it prints no message bodies, no addresses in full and no
credentials - only counts and domains, so the output is safe to paste.

    python scripts/where_is_my_data.py
    python scripts/where_is_my_data.py --db /path/to/dreamjob.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Run from a clone without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from dreamjob.config import get_settings  # noqa: E402
from dreamjob.db.connection import query_all  # noqa: E402

#: Tables worth counting, with what a row in each one actually means.  Keeping
#: the explanation next to the table is the whole point: a bare "vacancy: 137"
#: is what sends someone looking for mail that was never there.
TABLES: dict[str, str] = {
    "vacancy": "job adverts collected from a board, an ATS or a company site",
    "opportunity": "scored rows for one campaign (a vacancy, or a speculative target)",
    "company": "employers discovered or enriched",
    "contact": "people found at those employers",
    "dispatch": "messages Dream Job sent",
    "incoming_reply": "messages read back from a connected mailbox",
    "mail_account": "connected mailboxes",
    "raw_document": "pages and files kept as fetched (FR-182 provenance)",
}


def _count(table: str) -> int:
    rows = query_all(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608 - fixed names above
    return int(rows[0]["n"]) if rows else 0


def _print_block(title: str, rows: list[dict], key: str, empty: str) -> None:
    print(f"\n{title}")
    if not rows:
        print(f"  {empty}")
        return
    width = max(len(str(r[key] or "(unset)")) for r in rows)
    for row in rows:
        print(f"  {str(row[key] or '(unset)'):<{width}}  {row['n']:>6}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="override the database path")
    args = parser.parse_args()

    db_path = args.db or get_settings().abs_db_path
    print(f"database: {db_path}")
    if not Path(db_path).exists():
        print("\nThat file does not exist, so nothing has been collected into it yet.")
        print("Check DREAMJOB_DB_PATH in your .env, or run the app once to create it.")
        return 1
    print(f"size:     {Path(db_path).stat().st_size / 1_048_576:.1f} MB")

    print("\n--- row counts ------------------------------------------------")
    for table, meaning in TABLES.items():
        try:
            print(f"  {table:<16} {_count(table):>7}   {meaning}")
        except Exception as exc:  # noqa: BLE001 - an older schema may lack a table
            print(f"  {table:<16} {'n/a':>7}   ({exc})")

    # Where the adverts came from.  source_adapter is the honest answer to
    # "did this arrive by mail?" - there is no mail adapter, so a mailbox can
    # never appear here.
    _print_block(
        "--- vacancies by source adapter -------------------------------",
        query_all(
            "SELECT source_adapter AS source_adapter, COUNT(*) AS n FROM vacancy "
            "GROUP BY source_adapter ORDER BY n DESC"
        ),
        "source_adapter",
        "no vacancies collected",
    )
    _print_block(
        "--- vacancies by access method (http / browser / bulk) --------",
        query_all(
            "SELECT access_method AS access_method, COUNT(*) AS n FROM vacancy "
            "GROUP BY access_method ORDER BY n DESC"
        ),
        "access_method",
        "no vacancies collected",
    )

    # What the mailbox poller has read, by sender domain only.
    _print_block(
        "--- replies read from a mailbox, by sender domain -------------",
        query_all(
            "SELECT lower(substr(from_address, instr(from_address, '@') + 1)) AS domain, "
            "COUNT(*) AS n FROM incoming_reply WHERE from_address IS NOT NULL "
            "GROUP BY domain ORDER BY n DESC LIMIT 25"
        ),
        "domain",
        "nothing read from a mailbox yet",
    )
    # A reply with no dispatch_id matched nothing Dream Job sent.  That is the
    # bucket a job-alert mail would land in - recorded, classified, and not a
    # source of opportunities.
    unmatched = query_all("SELECT COUNT(*) AS n FROM incoming_reply WHERE dispatch_id IS NULL")
    if unmatched and unmatched[0]["n"]:
        print(
            f"\n  of which {unmatched[0]['n']} matched no message Dream Job sent "
            "(recorded and classified; never turned into an opportunity)"
        )

    print("\n--- connected mailboxes ---------------------------------------")
    accounts = query_all(
        "SELECT a.backend, a.address, a.is_active, "
        "(a.credentials_enc IS NOT NULL) AS has_credential, "
        "p.folder, p.last_uid, p.last_polled_at, p.messages_seen, p.last_error "
        "FROM mail_account a LEFT JOIN mail_poll_state p ON p.mail_account_id = a.id "
        "ORDER BY a.backend"
    )
    if not accounts:
        print("  none connected")
    for a in accounts:
        print(f"  {a['backend']} - {a['address']}")
        print(
            f"      active={bool(a['is_active'])} credential={bool(a['has_credential'])} "
            f"folder={a['folder'] or '-'} read={a['messages_seen'] or 0} "
            f"last_poll={a['last_polled_at'] or 'never'}"
        )
        if a["last_error"]:
            print(f"      last error: {a['last_error'][:120]}")

    print(
        "\nNote: Dream Job has no mail adapter - a mailbox is read for replies and "
        "bounces only (FR-326).\nAdverts come from the board, ATS, registry and "
        "browser adapters listed above."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
