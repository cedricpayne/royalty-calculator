from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from royaltycalc.parsing import parse_amount, parse_file, parse_statement_date

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


# ---------------------------------------------------------------- amounts

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("42.18", Decimal("42.18")),
        ("$1,234.56", Decimal("1234.56")),
        ("(12.50)", Decimal("-12.50")),
        ("-7.25", Decimal("-7.25")),
        ("1.234,56", Decimal("1234.56")),
        ("€99,50", Decimal("99.50")),
        ("0.003", Decimal("0.003")),
        ("1,240.00", Decimal("1240.00")),
        ("", None),
        ("N/A", None),
        ("notanumber", None),
    ],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


# ---------------------------------------------------------------- dates

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2025-03-15", date(2025, 3, 15)),
        ("2025-03", date(2025, 3, 31)),        # month period -> period end
        ("Mar 2025", date(2025, 3, 31)),
        ("March 2025", date(2025, 3, 31)),
        ("Mar-25", date(2025, 3, 31)),
        ("Q1 2025", date(2025, 3, 31)),
        ("2025 Q4", date(2025, 12, 31)),
        ("2024", date(2024, 12, 31)),
        ("202502", date(2025, 2, 28)),
        ("03/2025", date(2025, 3, 31)),
        ("06/15/2024", date(2024, 6, 15)),
        ("garbage", None),
        ("", None),
    ],
)
def test_parse_statement_date(raw, expected):
    assert parse_statement_date(raw) == expected


# ---------------------------------------------------------------- files

def test_parse_distrokid_style():
    result = parse_file(SAMPLES / "distrokid_2025.csv")
    assert len(result.rows) == 6
    assert result.column_map["amount"] == "Earnings (USD)"
    row = result.rows[0]
    assert row.amount == Decimal("42.18")
    assert row.txn_date == date(2025, 1, 31)  # sale month -> end of month
    assert row.currency == "USD"              # from the header
    assert row.category == "Masters"          # Spotify store


def test_parse_semicolon_pro_statement():
    result = parse_file(SAMPLES / "pro_statement_2025.csv")
    assert len(result.rows) == 5
    cats = {r.income_type: r.category for r in result.rows}
    assert cats["Performance"] == "Publishing"
    assert cats["Mechanical"] == "Publishing"
    assert cats["Sync License"] == "Publishing"
    assert result.rows[0].txn_date == date(2025, 3, 31)  # Q1 2025 -> period end


def test_parse_soundexchange():
    result = parse_file(SAMPLES / "soundexchange_2024.csv")
    assert all(r.category == "Neighbouring Rights" for r in result.rows)


def test_parse_producer_statement():
    result = parse_file(SAMPLES / "producer_statement.csv")
    assert all(r.category == "Producer Royalties" for r in result.rows)
    assert result.rows[0].amount == Decimal("1240.00")  # quoted thousands


def test_parse_label_mixed_skips_total_and_flags_unknown():
    result = parse_file(SAMPLES / "label_mixed.csv")
    assert len(result.rows) == 4
    assert any("total" in reason.lower() for _, reason in result.skipped)
    by_type = {r.income_type: r.category for r in result.rows}
    assert by_type["Master Recording"] == "Masters"
    assert by_type["Publishing"] == "Publishing"
    assert by_type["Merchandise"] == "Other"
    assert by_type["Blanket License ZZ"] == "Uncategorized"


def test_missing_date_forces_uncategorized(tmp_path):
    p = tmp_path / "nodate.csv"
    p.write_text("Store,Earnings\nSpotify,10.00\n")
    result = parse_file(p)
    assert result.rows[0].category == "Uncategorized"
    assert "date missing" in result.rows[0].category_reason


def test_header_not_on_first_line(tmp_path):
    p = tmp_path / "preamble.csv"
    p.write_text(
        "Royalty statement for Nova Kane,,,\n"
        "Generated 2026-01-05,,,\n"
        ",,,\n"
        "Date,Track,Royalty Type,Net Amount\n"
        "2025-11-30,Midnight Drive,Streaming,12.34\n"
    )
    result = parse_file(p)
    assert len(result.rows) == 1
    assert result.rows[0].amount == Decimal("12.34")
    assert result.rows[0].category == "Masters"


def test_unrecognizable_file_raises_with_preview(tmp_path):
    p = tmp_path / "junk.csv"
    p.write_text("a,b,c\n1,2,3\n")
    with pytest.raises(ValueError) as exc:
        parse_file(p)
    # The error shows the file's first rows so unknown formats can be mapped.
    assert "a | b | c" in str(exc.value)


def test_currency_qualified_amount_headers(tmp_path):
    p = tmp_path / "gbp.csv"
    p.write_text("Date,Track,Amount GBP\n2025-03-01,Song,12.50\n")
    result = parse_file(p)
    assert result.column_map["amount"] == "Amount GBP"
    assert result.rows[0].amount == Decimal("12.50")


def test_deep_header_found(tmp_path):
    preamble = "".join(f"Info line {i},,,\n" for i in range(30))
    p = tmp_path / "deep.csv"
    p.write_text(preamble + "Date,Track,Net Amount\n2025-03-01,Song,9.99\n")
    result = parse_file(p)
    assert len(result.rows) == 1
    assert result.rows[0].amount == Decimal("9.99")


def test_fingerprint_stable_and_distinct():
    result = parse_file(SAMPLES / "distrokid_2025.csv")
    fps = [r.fingerprint for r in result.rows]
    assert len(set(fps)) == len(fps)
    again = parse_file(SAMPLES / "distrokid_2025.csv")
    assert [r.fingerprint for r in again.rows] == fps
