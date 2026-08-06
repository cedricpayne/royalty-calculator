"""Build the earnings report: LTM total, category breakdown, per-year totals.

All aggregation runs as SQL against the store, so reports stay fast and
memory-flat even with millions of ingested transactions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from .categorize import MASTERS, NEIGHBOURING, OTHER, PRODUCER, PUBLISHING, UNCATEGORIZED
from .store import Store, from_micros

MAIN_CATEGORIES = [MASTERS, PUBLISHING, PRODUCER, NEIGHBOURING, OTHER]


@dataclass
class Report:
    as_of: date
    ltm_start: date              # exclusive lower bound: txn_date > ltm_start
    ltm_total: Decimal = Decimal("0")
    by_category: dict = dc_field(default_factory=dict)      # all-time, per category
    by_year: dict = dc_field(default_factory=dict)          # calendar year -> total
    total: Decimal = Decimal("0")                           # all-time total
    uncategorized_total: Decimal = Decimal("0")
    uncategorized_count: int = 0
    undated_count: int = 0
    currencies: set = dc_field(default_factory=set)
    txn_count: int = 0


def build_report(store: Store, chat_id: str | int, as_of: date | None = None) -> Report:
    """Aggregate a chat's non-duplicate transactions.

    LTM = the trailing 12 months ending on `as_of` (default: today), i.e.
    transactions dated after (as_of - 12 months) up to and including as_of.
    Category totals are all-time.
    """
    as_of = as_of or date.today()
    ltm_start = as_of - relativedelta(months=12)
    rep = Report(as_of=as_of, ltm_start=ltm_start)
    rep.by_category = {c: Decimal("0") for c in MAIN_CATEGORIES}

    chat = str(chat_id)
    conn = store.conn
    base = "FROM transactions WHERE chat_id=? AND is_duplicate=0"

    row = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(amount_micros),0) AS t, "
        f"COUNT(*) FILTER (WHERE txn_date IS NULL) AS undated {base}",
        (chat,),
    ).fetchone()
    rep.txn_count = row["n"]
    rep.total = from_micros(row["t"])
    rep.undated_count = row["undated"]

    for r in conn.execute(
        f"SELECT category, COALESCE(SUM(amount_micros),0) AS t, COUNT(*) AS n "
        f"{base} GROUP BY category",
        (chat,),
    ):
        if r["category"] == UNCATEGORIZED:
            rep.uncategorized_total = from_micros(r["t"])
            rep.uncategorized_count = r["n"]
        else:
            rep.by_category[r["category"]] = from_micros(r["t"])

    # Year from the txn_date prefix so the covering index (chat_id,
    # is_duplicate, txn_date, amount_micros) satisfies the whole query.
    for r in conn.execute(
        f"SELECT substr(txn_date, 1, 4) AS y, COALESCE(SUM(amount_micros),0) AS t "
        f"{base} AND txn_date IS NOT NULL GROUP BY y",
        (chat,),
    ):
        rep.by_year[int(r["y"])] = from_micros(r["t"])

    ltm = conn.execute(
        f"SELECT COALESCE(SUM(amount_micros),0) AS t {base} "
        f"AND txn_date IS NOT NULL AND txn_date > ? AND txn_date <= ?",
        (chat, ltm_start.isoformat(), as_of.isoformat()),
    ).fetchone()
    rep.ltm_total = from_micros(ltm["t"])

    # Currencies come from per-file summaries recorded at ingest (a handful of
    # rows) instead of scanning millions of transactions; fall back to a scan
    # only for files ingested before the summary column existed.
    need_scan = False
    for r in conn.execute("SELECT currencies FROM files WHERE chat_id=?", (chat,)):
        if r["currencies"] is None:
            need_scan = True
        else:
            rep.currencies.update(json.loads(r["currencies"]))
    if need_scan:
        for r in conn.execute(
            f"SELECT DISTINCT currency {base} AND currency IS NOT NULL", (chat,)
        ):
            rep.currencies.add(r["currency"])

    return rep


def fmt_money(value: Decimal, symbol: str = "$") -> str:
    q = value.quantize(Decimal("0.01"))
    if q < 0:
        return f"-{symbol}{-q:,.2f}"
    return f"{symbol}{q:,.2f}"


def render_report(rep: Report) -> str:
    """Render the simple main output requested for the tool."""
    if rep.txn_count == 0:
        return (
            "No transactions ingested yet.\n"
            "Upload royalty statement files (CSV, TSV or Excel) to get started."
        )

    lines = [f"LTM Total: {fmt_money(rep.ltm_total)}", ""]
    for cat in MAIN_CATEGORIES:
        lines.append(f"{cat}: {fmt_money(rep.by_category.get(cat, Decimal('0')))}")
    if rep.uncategorized_count:
        lines.append(
            f"Uncategorized: {fmt_money(rep.uncategorized_total)} "
            f"({rep.uncategorized_count} transactions - send /uncategorized to review)"
        )
    lines.append("")
    for year in sorted(rep.by_year, reverse=True):
        lines.append(f"{year}: {fmt_money(rep.by_year[year])}")

    footnotes = []
    if rep.undated_count:
        footnotes.append(
            f"{rep.undated_count} transaction(s) have no usable date and are "
            "excluded from LTM and yearly totals (they appear under Uncategorized)."
        )
    if len(rep.currencies) > 1:
        footnotes.append(
            "Mixed currencies detected (" + ", ".join(sorted(rep.currencies)) +
            "); amounts are summed as-is without conversion."
        )
    footnotes.append(
        f"LTM window: {rep.ltm_start.isoformat()} (exclusive) to {rep.as_of.isoformat()}. "
        f"All-time total: {fmt_money(rep.total)} across {rep.txn_count} transactions."
    )
    if footnotes:
        lines.append("")
        lines.extend(f"* {f}" for f in footnotes)
    return "\n".join(lines)
