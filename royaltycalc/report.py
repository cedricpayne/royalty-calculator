"""Build the earnings report: LTM total, category breakdown, per-year totals."""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from .categorize import MASTERS, NEIGHBOURING, OTHER, PRODUCER, PUBLISHING, UNCATEGORIZED

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


def build_report(rows, as_of: date | None = None) -> Report:
    """Aggregate transaction rows (sqlite Rows or dicts).

    LTM = the trailing 12 months ending on `as_of` (default: today), i.e.
    transactions dated after (as_of - 12 months) up to and including as_of.
    Category totals are all-time. Duplicate rows must already be filtered out.
    """
    as_of = as_of or date.today()
    ltm_start = as_of - relativedelta(months=12)
    rep = Report(as_of=as_of, ltm_start=ltm_start)
    rep.by_category = {c: Decimal("0") for c in MAIN_CATEGORIES}

    for row in rows:
        amount = Decimal(str(row["amount"]))
        category = row["category"]
        txn_date = date.fromisoformat(row["txn_date"]) if row["txn_date"] else None

        rep.txn_count += 1
        rep.total += amount
        if row["currency"]:
            rep.currencies.add(row["currency"])

        if category == UNCATEGORIZED:
            rep.uncategorized_total += amount
            rep.uncategorized_count += 1
        else:
            rep.by_category[category] = rep.by_category.get(category, Decimal("0")) + amount

        if txn_date is None:
            rep.undated_count += 1
            continue
        year = txn_date.year
        rep.by_year[year] = rep.by_year.get(year, Decimal("0")) + amount
        if ltm_start < txn_date <= as_of:
            rep.ltm_total += amount

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
