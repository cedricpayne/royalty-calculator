"""Command-line interface for local use and testing (same engine as the bot).

Examples:
    python -m royaltycalc.cli ingest statements/*.csv
    python -m royaltycalc.cli report
    python -m royaltycalc.cli uncategorized
    python -m royaltycalc.cli trace 42
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal

from .ingest import ingest_file, summary_text
from .report import build_report, fmt_money, render_report
from .store import DuplicateFileError, Store, decode_raw


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="royaltycalc", description=__doc__)
    ap.add_argument("--db", default="data/royalties.db", help="SQLite database path")
    ap.add_argument("--catalog", default="cli", help="Catalog id (bot uses the chat id)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ingest = sub.add_parser("ingest", help="Ingest one or more statement files")
    p_ingest.add_argument("files", nargs="+")

    sub.add_parser("report", help="Print the earnings report")
    sub.add_parser("uncategorized", help="List transactions needing manual review")
    sub.add_parser("files", help="List ingested files")

    p_trace = sub.add_parser("trace", help="Show the origin of a transaction")
    p_trace.add_argument("txn_id", type=int)

    p_cat = sub.add_parser("categorize", help="Manually set a transaction's category")
    p_cat.add_argument("txn_id", type=int)
    p_cat.add_argument("category")

    args = ap.parse_args(argv)
    store = Store(args.db)
    cid = args.catalog

    if args.cmd == "ingest":
        for f in args.files:
            try:
                print(summary_text(ingest_file(store, cid, f)))
            except DuplicateFileError as e:
                print(f"Duplicate file skipped ({f}): {e}")
            except Exception as e:
                print(f"Failed to ingest {f}: {e}", file=sys.stderr)
        print()
        print(render_report(build_report(store, cid)))
    elif args.cmd == "report":
        print(render_report(build_report(store, cid)))
    elif args.cmd == "uncategorized":
        rows = store.uncategorized(cid)
        if not rows:
            print("No uncategorized transactions.")
        for r in rows:
            desc = r["income_type"] or r["description"] or r["track"] or r["source"] or "?"
            print(
                f"#{r['id']}  {r['txn_date'] or 'no date'}  "
                f"{fmt_money(Decimal(r['amount']))}  {desc[:40]}  "
                f"[{r['filename']}:{r['row_number']}]"
            )
    elif args.cmd == "files":
        for f in store.files(cid):
            print(
                f"#{f['id']}  {f['filename']}  {f['uploaded_at']}  "
                f"{f['rows_ingested']} rows / {f['rows_duplicate']} dup / "
                f"{f['rows_skipped']} skipped"
            )
    elif args.cmd == "trace":
        r = store.get_transaction(cid, args.txn_id)
        if r is None:
            print(f"No transaction #{args.txn_id}")
            return 1
        print(f"Transaction #{r['id']}")
        print(f"File: {r['filename']} (file #{r['file_id']}, sha256 {r['sha256']})")
        print(f"Row in file: {r['row_number']}")
        print(f"Date: {r['txn_date']}   Amount: {r['amount']} {r['currency'] or ''}")
        print(f"Category: {r['category']} ({r['category_reason']})")
        if r["is_duplicate"]:
            print(f"DUPLICATE of transaction #{r['duplicate_of']} - excluded from totals")
        print("Original row:")
        print(json.dumps(decode_raw(r["raw_json"]), indent=2, ensure_ascii=False))
    elif args.cmd == "categorize":
        from .categorize import resolve_category_name

        cat = resolve_category_name(args.category)
        if cat is None:
            print("Unknown category. Use: masters, publishing, producer, neighbouring, other")
            return 1
        ok = store.set_category(cid, args.txn_id, cat)
        print(f"Transaction #{args.txn_id} set to {cat}." if ok else "Not found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
