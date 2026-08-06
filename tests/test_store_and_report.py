from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from royaltycalc.ingest import ingest_file
from royaltycalc.report import build_report, render_report
from royaltycalc.store import DuplicateFileError, Store, decode_raw, encode_raw

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
CHAT = "testchat"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_duplicate_file_rejected(store, tmp_path):
    ingest_file(store, CHAT, SAMPLES / "distrokid_2025.csv")
    # Same bytes under a different name is still a duplicate.
    copy = tmp_path / "renamed.csv"
    copy.write_bytes((SAMPLES / "distrokid_2025.csv").read_bytes())
    with pytest.raises(DuplicateFileError):
        ingest_file(store, CHAT, copy)
    assert len(store.files(CHAT)) == 1


def test_duplicate_transactions_skipped(store, tmp_path):
    ingest_file(store, CHAT, SAMPLES / "distrokid_2025.csv")
    # A second file overlapping 2 rows with the first + 1 new row.
    overlap = tmp_path / "aggregator_export.csv"
    overlap.write_text(
        "Sale Month,Store,Artist,Title,Quantity,Earnings (USD)\n"
        "2025-01,Spotify,Nova Kane,Midnight Drive,15302,42.18\n"
        "2025-02,Spotify,Nova Kane,Midnight Drive,14876,40.55\n"
        "2025-04,Deezer,Nova Kane,City Lights,300,1.99\n"
    )
    summary = ingest_file(store, CHAT, overlap)
    assert summary.rows_duplicate == 2
    assert summary.rows_ingested == 1
    assert summary.total_added == Decimal("1.99")

    rows = store.transactions(CHAT)
    total = sum(Decimal(r["amount"]) for r in rows)
    # 6 original rows + 1 new one; the 2 overlapping rows counted once.
    assert len(rows) == 7
    assert total == Decimal("42.18") + Decimal("18.94") + Decimal("40.55") + \
        Decimal("31.02") + Decimal("6.87") + Decimal("4.11") + Decimal("1.99")

    # Duplicates are stored (for traceability) but flagged.
    all_rows = store.transactions(CHAT, include_duplicates=True)
    dups = [r for r in all_rows if r["is_duplicate"]]
    assert len(dups) == 2
    assert all(r["duplicate_of"] is not None for r in dups)


def test_identical_rows_within_one_file_are_kept(store, tmp_path):
    f = tmp_path / "twice.csv"
    f.write_text(
        "Date,Store,Earnings\n"
        "2025-05-01,Spotify,10.00\n"
        "2025-05-01,Spotify,10.00\n"
    )
    summary = ingest_file(store, CHAT, f)
    assert summary.rows_ingested == 2
    assert summary.rows_duplicate == 0


def test_full_report(store):
    for name in (
        "distrokid_2025.csv",
        "pro_statement_2025.csv",
        "soundexchange_2024.csv",
        "producer_statement.csv",
        "label_mixed.csv",
    ):
        ingest_file(store, CHAT, SAMPLES / name)

    rep = build_report(store, CHAT, as_of=date(2026, 7, 20))

    # Category totals (all-time)
    assert rep.by_category["Masters"] == Decimal("42.18") + Decimal("18.94") + \
        Decimal("40.55") + Decimal("31.02") + Decimal("6.87") + Decimal("4.11") + \
        Decimal("210.00")
    assert rep.by_category["Publishing"] == Decimal("120.50") + Decimal("88.10") + \
        Decimal("44.02") + Decimal("19.75") + Decimal("500.00") + Decimal("75.25")
    assert rep.by_category["Producer Royalties"] == \
        Decimal("1240.00") + Decimal("655.30") + Decimal("980.12")
    assert rep.by_category["Neighbouring Rights"] == Decimal("310.44") + Decimal("289.12")
    assert rep.by_category["Other"] == Decimal("55.00")
    assert rep.uncategorized_total == Decimal("99.99")
    assert rep.uncategorized_count == 1

    # Yearly buckets
    assert rep.by_year[2024] == Decimal("599.56")
    assert rep.by_year[2025] == Decimal("1896.16")
    assert rep.by_year[2026] == Decimal("2335.54")

    # LTM = after 2025-07-20 through 2026-07-20: producer 2025-07-31 + 980.12,
    # producer 2026-01-31 rows, and the Mar 2026 label rows.
    expected_ltm = Decimal("980.12") + Decimal("1240.00") + Decimal("655.30") + \
        Decimal("210.00") + Decimal("75.25") + Decimal("55.00") + Decimal("99.99")
    assert rep.ltm_total == expected_ltm

    text = render_report(rep)
    assert text.splitlines()[0] == f"LTM Total: ${expected_ltm:,.2f}"
    assert "Masters: $353.67" in text
    assert "Uncategorized: $99.99" in text
    assert "2026: $2,335.54" in text
    assert "2025: $1,896.16" in text
    assert "2024: $599.56" in text


def test_traceability(store):
    ingest_file(store, CHAT, SAMPLES / "distrokid_2025.csv")
    rows = store.transactions(CHAT)
    txn = store.get_transaction(CHAT, rows[0]["id"])
    assert txn["filename"] == "distrokid_2025.csv"
    assert txn["row_number"] == 2  # first data row after the header
    raw = decode_raw(txn["raw_json"])
    assert raw["Store"] == "Spotify"
    assert raw["Sale Month"] == "2025-01"
    assert txn["sha256"]


def test_encode_raw_roundtrip_and_compression():
    small = {"a": "1"}
    assert decode_raw(encode_raw(small)) == small
    assert isinstance(encode_raw(small), str)  # tiny rows stay as plain text
    big = {f"Column {i}": f"value {i}" for i in range(20)}
    encoded = encode_raw(big)
    assert isinstance(encoded, bytes)          # larger rows are compressed
    assert decode_raw(encoded) == big


def test_report_currencies_from_file_summaries(store, tmp_path):
    mixed = tmp_path / "mixed.csv"
    mixed.write_text(
        "Date,Store,Currency,Earnings\n"
        "2025-01-15,Spotify,USD,10.00\n"
        "2025-02-15,Deezer,EUR,8.00\n"
    )
    ingest_file(store, CHAT, mixed)
    rep = build_report(store, CHAT, as_of=date(2026, 7, 20))
    assert rep.currencies == {"USD", "EUR"}


def test_manual_categorize_updates_report(store):
    ingest_file(store, CHAT, SAMPLES / "label_mixed.csv")
    unc = store.uncategorized(CHAT)
    assert len(unc) == 1
    store.set_category(CHAT, unc[0]["id"], "Publishing")
    assert store.uncategorized(CHAT) == []
    rep = build_report(store, CHAT, as_of=date(2026, 7, 20))
    assert rep.uncategorized_count == 0
    assert rep.by_category["Publishing"] == Decimal("75.25") + Decimal("99.99")


def test_delete_file_removes_transactions(store):
    s1 = ingest_file(store, CHAT, SAMPLES / "distrokid_2025.csv")
    ingest_file(store, CHAT, SAMPLES / "soundexchange_2024.csv")
    store.delete_file(CHAT, s1.file_id)
    rows = store.transactions(CHAT)
    assert all(r["file_id"] != s1.file_id for r in rows)
    assert len(store.files(CHAT)) == 1


def test_chats_are_isolated(store):
    ingest_file(store, "chat_a", SAMPLES / "distrokid_2025.csv")
    assert store.transactions("chat_b") == []
    # Same file in another chat is not a duplicate there.
    summary = ingest_file(store, "chat_b", SAMPLES / "distrokid_2025.csv")
    assert summary.rows_ingested == 6
    assert summary.rows_duplicate == 0
