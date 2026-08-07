"""Tests for content-based column inference: unknown headers, headerless
files, and multi-sheet workbooks."""

from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook

from royaltycalc.parsing import parse_file


def test_headerless_statement_inferred(tmp_path):
    """PRS-052-style export: no header row at all."""
    p = tmp_path / "prs052.csv"
    p.write_text(
        "00121710524,DAVIDGE NEIL JAMES,UNFINISHED SYMPATHY,PRS,2022-07-15,123.45\n"
        "00121710524,DAVIDGE NEIL JAMES,ANGEL,PRS,2022-07-15,67.89\n"
        "00121710524,DAVIDGE NEIL JAMES,TEARDROP,PRS,2022-07-15,45.00\n"
    )
    result = parse_file(p)
    assert len(result.rows) == 3
    assert result.rows[0].amount == Decimal("123.45")
    assert result.rows[0].txn_date == date(2022, 7, 15)
    # 'PRS' in the text columns categorizes the row as Publishing.
    assert result.rows[0].category == "Publishing"
    assert any("inferred from the data" in w for w in result.warnings)


def test_unrecognized_amount_header_gap_filled(tmp_path):
    """Alias-known date/track columns, but the money column has an odd name."""
    p = tmp_path / "odd.csv"
    p.write_text(
        "Period,Work,Society,Value Due\n"
        "2025-03,Song A,ASCAP,10.50\n"
        "2025-04,Song B,ASCAP,20.25\n"
    )
    result = parse_file(p)
    assert len(result.rows) == 2
    assert result.column_map["amount"] == "Value Due"
    assert result.rows[0].amount == Decimal("10.50")
    assert result.rows[0].txn_date == date(2025, 3, 31)
    assert result.rows[0].category == "Publishing"  # ASCAP in Society column
    assert any("Amount column inferred" in w for w in result.warnings)


def test_quantity_column_not_mistaken_for_earnings(tmp_path):
    """Bare-integer columns (quantities, IDs) must lose to decimal money."""
    p = tmp_path / "qty.csv"
    p.write_text(
        "Month,Item,Units,Payable Sum\n"
        "2025-01,Song A,15302,42.18\n"
        "2025-02,Song A,14876,40.55\n"
    )
    result = parse_file(p)
    assert result.column_map["amount"] == "Payable Sum"
    assert result.rows[0].amount == Decimal("42.18")


def test_multisheet_workbook_finds_data_sheet(tmp_path):
    """Cover sheet first, real table on the second worksheet."""
    wb = Workbook()
    cover = wb.active
    cover.title = "Cover"
    cover["A1"] = "Royalty statement"
    cover["A2"] = "Prepared for Nova Kane"
    data = wb.create_sheet("Detail")
    data.append(["Date", "Track", "Royalty Type", "Net Amount"])
    data.append(["2025-11-30", "Midnight Drive", "Streaming", 12.34])
    data.append(["2025-12-31", "City Lights", "Streaming", 8.66])
    path = tmp_path / "multi.xlsx"
    wb.save(path)

    result = parse_file(path)
    assert len(result.rows) == 2
    assert result.rows[0].amount == Decimal("12.34")
    assert result.rows[0].category == "Masters"
    assert any("Using worksheet 'Detail'" in w for w in result.warnings)


def test_headerless_with_preamble(tmp_path):
    """Metadata lines, then an unrecognizable header, then data rows."""
    p = tmp_path / "pre.csv"
    p.write_text(
        "Member statement,,,,\n"
        "Ref,Opus,Src,Paid on,Sum owed\n"
        "A1,Track One,SoundExchange,2024-06-15,11.50\n"
        "A2,Track Two,SoundExchange,2024-12-15,3.25\n"
    )
    result = parse_file(p)
    assert len(result.rows) == 2
    assert result.rows[0].amount == Decimal("11.50")
    assert result.rows[0].txn_date == date(2024, 6, 15)
    assert result.rows[0].category == "Neighbouring Rights"
    # Raw rows keep the file's own header names for traceability.
    assert result.rows[0].raw.get("Sum owed") == "11.50"


def test_hopeless_file_still_errors_with_preview(tmp_path):
    p = tmp_path / "hopeless.csv"
    p.write_text("alpha,beta\nfoo,bar\nbaz,qux\n")
    with pytest.raises(ValueError) as exc:
        parse_file(p)
    assert "alpha | beta" in str(exc.value)
