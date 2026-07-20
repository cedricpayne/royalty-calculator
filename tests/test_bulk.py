"""Tests for the large-catalog paths: streamed ingest, SQL dedupe, migration."""

import csv
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from royaltycalc.ingest import ingest_file
from royaltycalc.report import build_report
from royaltycalc.store import Store

CHAT = "bulkchat"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def write_big_csv(path: Path, n_rows: int, start_id: int = 0) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Date", "Store", "Track", "Earnings (USD)"])
        for i in range(start_id, start_id + n_rows):
            month = (i % 12) + 1
            w.writerow([f"2025-{month:02d}-15", "Spotify", f"Track {i}", "0.010000"])


def test_streamed_ingest_of_large_file(store, tmp_path):
    n = 60_000  # spans many insert batches
    big = tmp_path / "big.csv"
    write_big_csv(big, n)
    summary = ingest_file(store, CHAT, big)
    assert summary.rows_ingested == n
    assert summary.rows_duplicate == 0
    assert summary.total_added == Decimal("600.00")

    rep = build_report(store, CHAT, as_of=date(2026, 7, 20))
    assert rep.txn_count == n
    assert rep.total == Decimal("600.00")
    assert rep.by_year[2025] == Decimal("600.00")
    assert rep.by_category["Masters"] == Decimal("600.00")


def test_sql_dedupe_on_overlapping_large_files(store, tmp_path):
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    write_big_csv(a, 5_000, start_id=0)
    write_big_csv(b, 5_000, start_id=2_500)  # rows 2500-4999 overlap file a
    ingest_file(store, CHAT, a)
    summary = ingest_file(store, CHAT, b)
    assert summary.rows_duplicate == 2_500
    assert summary.rows_ingested == 2_500
    rep = build_report(store, CHAT, as_of=date(2026, 7, 20))
    assert rep.txn_count == 7_500
    assert rep.total == Decimal("75.00")


def test_occurrence_aware_sql_dedupe(store, tmp_path):
    twice = tmp_path / "twice.csv"
    twice.write_text(
        "Date,Store,Earnings\n"
        "2025-05-01,Spotify,10.00\n"
        "2025-05-01,Spotify,10.00\n"
    )
    thrice = tmp_path / "thrice.csv"
    thrice.write_text(
        "Date,Store,Earnings\n"
        "2025-05-01,Spotify,10.00\n"
        "2025-05-01,Spotify,10.00\n"
        "2025-05-01,Spotify,10.00\n"
    )
    ingest_file(store, CHAT, twice)
    summary = ingest_file(store, CHAT, thrice)
    # Two copies already exist, so two of the three are flagged; the third counts.
    assert summary.rows_duplicate == 2
    assert summary.rows_ingested == 1
    rep = build_report(store, CHAT, as_of=date(2026, 7, 20))
    assert rep.total == Decimal("30.00")


def test_failed_ingest_leaves_nothing_behind(store, tmp_path):
    class ExplodingReader:
        column_map = {}
        skipped_count = 0
        skipped_samples = []

        def __iter__(self):
            raise RuntimeError("boom mid-stream")

    with pytest.raises(RuntimeError):
        store.ingest(CHAT, "broken.csv", "deadbeef", ExplodingReader())
    assert store.files(CHAT) == []
    assert store.transactions(CHAT) == []


OLD_SCHEMA = """
CREATE TABLE files (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
    filename TEXT NOT NULL, sha256 TEXT NOT NULL, uploaded_at TEXT NOT NULL,
    rows_ingested INTEGER NOT NULL DEFAULT 0, rows_duplicate INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0, column_map TEXT, UNIQUE (chat_id, sha256)
);
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL, txn_date TEXT, year INTEGER, amount TEXT NOT NULL,
    currency TEXT, category TEXT NOT NULL, category_reason TEXT,
    manual_category INTEGER NOT NULL DEFAULT 0, income_type TEXT, source TEXT,
    track TEXT, artist TEXT, description TEXT, fingerprint TEXT NOT NULL,
    is_duplicate INTEGER NOT NULL DEFAULT 0, duplicate_of INTEGER, raw_json TEXT NOT NULL
);
"""


def test_migration_backfills_amount_micros(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO files (chat_id, filename, sha256, uploaded_at) VALUES ('c','f.csv','x','t')"
    )
    conn.execute(
        "INSERT INTO transactions (chat_id, file_id, row_number, txn_date, year, amount, "
        "category, fingerprint, raw_json) "
        "VALUES ('c', 1, 2, '2025-01-31', 2025, '12.34', 'Masters', 'fp', '{}')"
    )
    conn.commit()
    conn.close()

    store = Store(db)
    row = store.conn.execute("SELECT amount_micros FROM transactions").fetchone()
    assert row["amount_micros"] == 12_340_000
    rep = build_report(store, "c", as_of=date(2026, 7, 20))
    assert rep.total == Decimal("12.34")
    store.close()
