"""Read royalty statement files (CSV/TSV/XLSX) with heterogeneous layouts.

The parser:
  * detects the header row (statements often have preamble/title rows),
  * maps varied column names onto canonical fields via an alias table,
  * parses flexible date/period formats (full dates, "2025-03", "Mar 2025", "Q1 2025"),
  * parses amounts with currency symbols, thousands separators and parentheses,
  * keeps every raw row so each transaction is traceable to its origin.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
from dateutil import parser as dateutil_parser

from .categorize import categorize_row

# ---------------------------------------------------------------------------
# Column alias table
# ---------------------------------------------------------------------------

def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h).lower())


# field -> list of aliases, ordered by preference (earlier = preferred when
# several columns of the same field exist, e.g. "net amount" beats "gross amount").
_FIELD_ALIASES: dict[str, list[str]] = {
    "amount": [
        "netamount", "netroyalty", "netroyalties", "netrevenue", "netearnings",
        "netincome", "netpayable", "netamountpayable", "netpayout", "amountdue",
        "amountpayable", "royaltyamount", "royaltiesearned", "royaltyearned",
        "yourshare", "payableamount", "earnings", "earningsusd", "royalty",
        "royalties", "payout", "revenue", "income", "amount", "total",
        "totalamount", "totalearnings", "value", "netdue",
    ],
    "date": [
        "transactiondate", "statementdate", "paymentdate", "saledate",
        "activitydate", "date", "statementperiod", "accountingperiod",
        "royaltyperiod", "salesperiod", "reportingperiod", "distributionperiod",
        "performanceperiod", "period", "periodend", "periodending",
        "salemonth", "reportingmonth", "activitymonth", "month", "quarter",
        "year",
    ],
    "income_type": [
        "incometype", "royaltytype", "righttype", "rightstype", "revenuetype",
        "incomecategory", "royaltycategory", "revenuecategory", "earningstype",
        "usetype", "usagetype", "type", "category", "incomesource",
        "revenuesource", "royaltysource", "configuration",
    ],
    "source": [
        "source", "payor", "payer", "society", "collectionsociety", "platform",
        "store", "storename", "service", "dsp", "retailer", "distributor",
        "channel", "saletype", "outlet", "partner", "licensee", "territorysource",
    ],
    "track": [
        "tracktitle", "songtitle", "worktitle", "recordingtitle", "releasetitle",
        "title", "track", "song", "work", "recording", "release", "trackname",
        "songname", "assettitle", "project", "isrctitle",
    ],
    "artist": [
        "artistname", "artist", "band", "performer", "actname",
    ],
    "description": [
        "description", "details", "notes", "memo", "usage", "lineitem",
        "transactiondescription", "comment", "comments",
    ],
    "currency": [
        "currency", "currencycode", "curr", "ccy",
    ],
}

# normalized alias -> (field, preference_rank)
_ALIAS_LOOKUP: dict[str, tuple[str, int]] = {}
for _field, _aliases in _FIELD_ALIASES.items():
    for _rank, _alias in enumerate(_aliases):
        _ALIAS_LOOKUP.setdefault(_alias, (_field, _rank))


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_AMOUNT_CLEAN_RE = re.compile(r"[^\d.,\-()]")


def parse_amount(raw: str | float | int | None) -> Decimal | None:
    """Parse '$1,234.56', '(12.50)', '1.234,56', '0.003' etc. into a Decimal."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in {"nan", "none", "n/a", "-", "--"}:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    s = _AMOUNT_CLEAN_RE.sub("", s).strip()
    if s.startswith("-"):
        negative = True
        s = s[1:]
    s = s.replace("(", "").replace(")", "")
    if not s:
        return None
    # Decide decimal separator: if both present, the right-most one is decimal.
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # "1,234" -> thousands; "12,5" -> decimal comma
        tail = s.split(",")[-1]
        if len(tail) == 3 and len(s.split(",")[0]) > 0:
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return -value if negative else value


def detect_currency(raw_amount: str | None, header: str | None) -> str | None:
    """Pull a currency hint from the amount cell ('$12') or header ('Earnings (USD)')."""
    for text in (raw_amount, header):
        if not text:
            continue
        text = str(text)
        m = re.search(r"\b(USD|EUR|GBP|CAD|AUD|JPY|SEK|NOK|DKK|CHF|NZD|BRL|MXN)\b",
                      text.upper())
        if m:
            return m.group(1)
        for sym, code in _CURRENCY_SYMBOLS.items():
            if sym in text:
                return code
    return None


_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})


def _end_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def parse_statement_date(raw: str | None) -> date | None:
    """Parse a transaction date or statement period into a date.

    Periods (months, quarters, years) resolve to the period's last day, so a
    'March 2025' line lands in calendar year 2025 and in any LTM window that
    includes end of March 2025.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in {"nan", "none", "n/a"}:
        return None

    # Quarter: "Q1 2025", "2025 Q1", "2025-Q1"
    m = re.fullmatch(r"(?i)q([1-4])[\s\-/]*(\d{4})", s) or \
        re.fullmatch(r"(?i)(\d{4})[\s\-/]*q([1-4])", s)
    if m:
        a, b = m.group(1), m.group(2)
        year, q = (int(b), int(a)) if len(a) == 1 else (int(a), int(b))
        return _end_of_month(year, q * 3)

    # Year only: "2025"
    if re.fullmatch(r"(19|20)\d{2}", s):
        return date(int(s), 12, 31)

    # Numeric year-month: "2025-03", "2025/03", "202503", "03/2025"
    m = re.fullmatch(r"(\d{4})[\-/.](\d{1,2})", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return _end_of_month(int(m.group(1)), int(m.group(2)))
    m = re.fullmatch(r"(\d{1,2})[\-/.](\d{4})", s)
    if m and 1 <= int(m.group(1)) <= 12:
        return _end_of_month(int(m.group(2)), int(m.group(1)))
    m = re.fullmatch(r"(\d{4})(\d{2})", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return _end_of_month(int(m.group(1)), int(m.group(2)))

    # Month-name periods: "Mar 2025", "March 2025", "Mar-25", "2025 March"
    m = re.fullmatch(r"(?i)([a-z]{3,9})[\s\-/,]+(\d{2,4})", s)
    if m and m.group(1).lower() in _MONTHS:
        year = int(m.group(2))
        if year < 100:
            year += 2000
        return _end_of_month(year, _MONTHS[m.group(1).lower()])
    m = re.fullmatch(r"(?i)(\d{4})[\s\-/,]+([a-z]{3,9})", s)
    if m and m.group(2).lower() in _MONTHS:
        return _end_of_month(int(m.group(1)), _MONTHS[m.group(2).lower()])

    # Full dates - dateutil handles ISO, US, and most exports.
    try:
        return dateutil_parser.parse(s, dayfirst=False).date()
    except (ValueError, OverflowError):
        pass
    try:
        return dateutil_parser.parse(s, dayfirst=True).date()
    except (ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

@dataclass
class ParsedRow:
    row_number: int              # 1-based data row number in the original file
    txn_date: date | None
    amount: Decimal
    currency: str | None
    category: str
    category_reason: str
    income_type: str | None
    source: str | None
    track: str | None
    artist: str | None
    description: str | None
    raw: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Content hash used for cross-file duplicate detection.

        Built from the raw original row values (not the interpreted fields), so
        two different files carrying the identical statement line collide, while
        rows that differ in any original column do not.
        """
        payload = json.dumps(
            {k: v for k, v in self.raw.items()}, sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ParseResult:
    rows: list[ParsedRow]
    skipped: list[tuple[int, str]]   # (row_number, reason) for rows we could not use
    column_map: dict[str, str]       # canonical field -> original column name
    warnings: list[str]


_TOTAL_ROW_RE = re.compile(r"(?i)^\s*(grand\s+)?(sub)?total\b")


def _read_table(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm", ".xls"}:
        return pd.read_excel(path, header=None, dtype=str)
    # CSV/TSV/TXT: sniff the delimiter.
    return pd.read_csv(
        path, header=None, dtype=str, sep=None, engine="python",
        skip_blank_lines=False, encoding_errors="replace",
    )


def _find_header_row(df: pd.DataFrame, max_scan: int = 15) -> tuple[int, dict[int, tuple[str, int]]]:
    """Locate the header row: the first row where >=2 cells map to known fields
    (at minimum an amount column). Returns (row_index, {col_index: (field, rank)}).
    """
    best: tuple[int, dict[int, tuple[str, int]]] | None = None
    for i in range(min(max_scan, len(df))):
        mapping: dict[int, tuple[str, int]] = {}
        for col_idx, cell in enumerate(df.iloc[i]):
            if pd.isna(cell):
                continue
            hit = _ALIAS_LOOKUP.get(_norm_header(str(cell)))
            if hit:
                mapping[col_idx] = hit
        fields_found = {f for f, _ in mapping.values()}
        if "amount" in fields_found and len(fields_found) >= 2:
            return i, mapping
        if mapping and best is None:
            best = (i, mapping)
    if best:
        return best
    raise ValueError(
        "Could not find a header row with recognizable columns "
        "(need at least an amount column such as 'Net Amount', 'Earnings' or 'Royalty')."
    )


def parse_file(path: str | Path, filename: str | None = None) -> ParseResult:
    """Parse a statement file into normalized transaction rows."""
    path = Path(path)
    filename = filename or path.name
    df = _read_table(path)
    header_idx, col_hits = _find_header_row(df)

    headers = [("" if pd.isna(c) else str(c).strip()) for c in df.iloc[header_idx]]

    # Choose the best column for each canonical field (lowest alias rank wins).
    chosen: dict[str, int] = {}
    chosen_rank: dict[str, int] = {}
    for col_idx, (field_name, rank) in col_hits.items():
        if field_name not in chosen or rank < chosen_rank[field_name]:
            chosen[field_name] = col_idx
            chosen_rank[field_name] = rank

    if "amount" not in chosen:
        raise ValueError("No amount column recognized in this file.")

    column_map = {f: headers[idx] for f, idx in chosen.items()}
    warnings: list[str] = []
    if "date" not in chosen:
        warnings.append(
            "No date/period column recognized - rows will need manual review."
        )

    header_currency = detect_currency(None, headers[chosen["amount"]])

    def cell(row, field_name: str) -> str | None:
        idx = chosen.get(field_name)
        if idx is None:
            return None
        v = row.iloc[idx]
        if pd.isna(v):
            return None
        v = str(v).strip()
        return v or None

    rows: list[ParsedRow] = []
    skipped: list[tuple[int, str]] = []

    for data_i in range(header_idx + 1, len(df)):
        row = df.iloc[data_i]
        row_number = data_i + 1  # 1-based position in the original file
        raw = {
            headers[j] or f"col{j+1}": ("" if pd.isna(row.iloc[j]) else str(row.iloc[j]).strip())
            for j in range(len(headers))
        }
        if all(v == "" for v in raw.values()):
            continue

        first_cell = next((v for v in raw.values() if v), "")
        amount = parse_amount(cell(row, "amount"))
        if amount is None:
            skipped.append((row_number, "no parseable amount"))
            continue
        if _TOTAL_ROW_RE.match(first_cell) or any(
            _TOTAL_ROW_RE.match(v) for v in raw.values() if v
        ):
            skipped.append((row_number, "looks like a total/subtotal row"))
            continue

        txn_date = parse_statement_date(cell(row, "date"))
        income_type = cell(row, "income_type")
        source = cell(row, "source")
        track = cell(row, "track")
        artist = cell(row, "artist")
        description = cell(row, "description")
        currency = (
            cell(row, "currency")
            or detect_currency(cell(row, "amount"), None)
            or header_currency
        )
        if currency:
            currency = currency.upper()

        category, reason = categorize_row(income_type, source, description, filename)
        if txn_date is None:
            # Without a date the transaction cannot be placed in a year or the
            # LTM window, so force manual review regardless of keyword matches.
            from .categorize import UNCATEGORIZED
            if category != UNCATEGORIZED:
                reason = f"date missing/unparseable (would be {category}: {reason})"
            else:
                reason = "date missing/unparseable; " + reason
            category = UNCATEGORIZED

        rows.append(
            ParsedRow(
                row_number=row_number,
                txn_date=txn_date,
                amount=amount,
                currency=currency,
                category=category,
                category_reason=reason,
                income_type=income_type,
                source=source,
                track=track,
                artist=artist,
                description=description,
                raw=raw,
            )
        )

    currencies = {r.currency for r in rows if r.currency}
    if len(currencies) > 1:
        warnings.append(
            "Multiple currencies detected in this file: "
            + ", ".join(sorted(currencies))
            + ". Totals do not convert between currencies."
        )
    return ParseResult(rows=rows, skipped=skipped, column_map=column_map, warnings=warnings)


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
