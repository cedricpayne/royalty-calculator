"""SQLite persistence for statements and transactions.

Every transaction keeps: the file it came from, its row number in that file,
the full raw row (JSON) and the reason it was categorized the way it was -
so every reported number is traceable back to its origin.

Built for bulk: rows stream from the parser and are inserted in batches, so
memory stays flat for very large statements. Amounts are stored both as exact
decimal text (source of truth for display/trace) and as integer micro-units
(`amount_micros`) so totals and duplicate math run inside SQLite instead of
Python.

Duplicate handling:
  * whole-file duplicates: rejected by content hash (SHA-256), regardless of filename;
  * transaction duplicates: when a newly uploaded file contains rows whose raw
    content is identical to rows already ingested from *other* files, those rows
    are stored but flagged `is_duplicate=1` and excluded from all totals.
    Matching is occurrence-aware (done with a window function in SQL): if an
    earlier file legitimately contains the same line twice and the new file has
    it three times, two are flagged and one still counts.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Iterable

from .parsing import ParsedRow, StatementReader

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    rows_ingested INTEGER NOT NULL DEFAULT 0,
    rows_duplicate INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0,
    column_map TEXT,
    UNIQUE (chat_id, sha256)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL,
    txn_date TEXT,
    year INTEGER,
    amount TEXT NOT NULL,
    amount_micros INTEGER NOT NULL DEFAULT 0,
    currency TEXT,
    category TEXT NOT NULL,
    category_reason TEXT,
    manual_category INTEGER NOT NULL DEFAULT 0,
    income_type TEXT,
    source TEXT,
    track TEXT,
    artist TEXT,
    description TEXT,
    fingerprint TEXT NOT NULL,
    is_duplicate INTEGER NOT NULL DEFAULT 0,
    duplicate_of INTEGER,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_txn_chat ON transactions (chat_id, is_duplicate);
CREATE INDEX IF NOT EXISTS idx_txn_fingerprint ON transactions (chat_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_txn_year ON transactions (chat_id, year);
CREATE INDEX IF NOT EXISTS idx_txn_file ON transactions (file_id);
"""

INSERT_BATCH_SIZE = 2000
COMMIT_EVERY_ROWS = 100_000   # fsync cadence during bulk ingest


def to_micros(amount: Decimal) -> int:
    """Exact-integer representation at 6 decimal places (streaming royalties
    routinely carry sub-cent amounts)."""
    return int(amount.scaleb(6).to_integral_value(rounding=ROUND_HALF_UP))


def from_micros(micros: int | None) -> Decimal:
    return Decimal(micros or 0).scaleb(-6)


class DuplicateFileError(Exception):
    def __init__(self, existing_filename: str, uploaded_at: str):
        self.existing_filename = existing_filename
        self.uploaded_at = uploaded_at
        super().__init__(
            f"Identical file already ingested as '{existing_filename}' at {uploaded_at}"
        )


@dataclass
class IngestSummary:
    file_id: int
    filename: str
    rows_ingested: int
    rows_duplicate: int
    rows_skipped: int
    uncategorized: int
    total_added: Decimal
    warnings: list[str]
    skipped_details: list[tuple[int, str]]


class Store:
    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        if db_path.parent and str(db_path.parent) not in ("", "."):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False lets ingestion run on a worker thread while
        # quick queries stay on the event loop; callers serialize writes.
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(transactions)")}
        if cols and "amount_micros" not in cols:
            self.conn.execute(
                "ALTER TABLE transactions ADD COLUMN amount_micros INTEGER NOT NULL DEFAULT 0"
            )
            self.conn.execute(
                "UPDATE transactions SET amount_micros = "
                "CAST(ROUND(CAST(amount AS REAL) * 1000000) AS INTEGER)"
            )
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ ingest

    def ingest(
        self,
        chat_id: str | int,
        filename: str,
        sha256: str,
        reader: StatementReader | Iterable[ParsedRow],
    ) -> IngestSummary:
        """Stream rows from `reader` into the database.

        The reader is consumed once, in batches; duplicate marking and all
        counting happen in SQL afterwards, so ingestion memory is flat no
        matter how large the statement is.
        """
        chat_id = str(chat_id)
        existing = self.conn.execute(
            "SELECT filename, uploaded_at FROM files WHERE chat_id=? AND sha256=?",
            (chat_id, sha256),
        ).fetchone()
        if existing:
            raise DuplicateFileError(existing["filename"], existing["uploaded_at"])

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        column_map = getattr(reader, "column_map", None)
        cur = self.conn.execute(
            "INSERT INTO files (chat_id, filename, sha256, uploaded_at, column_map) "
            "VALUES (?,?,?,?,?)",
            (chat_id, filename, sha256, now, json.dumps(column_map or {})),
        )
        file_id = cur.lastrowid
        self.conn.commit()

        try:
            batch: list[tuple] = []
            since_commit = 0
            for r in reader:
                batch.append(self._txn_tuple(chat_id, file_id, r))
                if len(batch) >= INSERT_BATCH_SIZE:
                    self._flush(batch)
                    since_commit += len(batch)
                    batch = []
                    if since_commit >= COMMIT_EVERY_ROWS:
                        self.conn.commit()
                        since_commit = 0
            if batch:
                self._flush(batch)
            self.conn.commit()

            self._mark_duplicates(chat_id, file_id)

            counts = self.conn.execute(
                "SELECT "
                "COUNT(*) FILTER (WHERE is_duplicate=0) AS ingested, "
                "COUNT(*) FILTER (WHERE is_duplicate=1) AS dups, "
                "COALESCE(SUM(amount_micros) FILTER (WHERE is_duplicate=0), 0) AS total, "
                "COUNT(*) FILTER (WHERE is_duplicate=0 AND category='Uncategorized') AS unc "
                "FROM transactions WHERE file_id=?",
                (file_id,),
            ).fetchone()

            skipped_count = getattr(reader, "skipped_count", 0)
            self.conn.execute(
                "UPDATE files SET rows_ingested=?, rows_duplicate=?, rows_skipped=? WHERE id=?",
                (counts["ingested"], counts["dups"], skipped_count, file_id),
            )
            self.conn.commit()
        except Exception:
            # Remove the partial file (transactions cascade) so a failed upload
            # never leaves half-counted income behind.
            self.conn.rollback()
            self.conn.execute("DELETE FROM files WHERE id=?", (file_id,))
            self.conn.commit()
            raise

        finalize = getattr(reader, "finalize_warnings", None)
        warnings = finalize() if callable(finalize) else []
        return IngestSummary(
            file_id=file_id,
            filename=filename,
            rows_ingested=counts["ingested"],
            rows_duplicate=counts["dups"],
            rows_skipped=getattr(reader, "skipped_count", 0),
            uncategorized=counts["unc"],
            total_added=from_micros(counts["total"]),
            warnings=list(warnings),
            skipped_details=list(getattr(reader, "skipped_samples", [])),
        )

    def _txn_tuple(self, chat_id: str, file_id: int, r: ParsedRow) -> tuple:
        return (
            chat_id, file_id, r.row_number,
            r.txn_date.isoformat() if r.txn_date else None,
            r.txn_date.year if r.txn_date else None,
            str(r.amount), to_micros(r.amount), r.currency, r.category,
            r.category_reason, r.income_type, r.source, r.track, r.artist,
            r.description, r.fingerprint, json.dumps(r.raw, ensure_ascii=False),
        )

    def _flush(self, batch: list[tuple]) -> None:
        # No commit here: the caller batches commits (COMMIT_EVERY_ROWS) so a
        # bulk ingest is not bound by fsync frequency.
        self.conn.executemany(
            "INSERT INTO transactions (chat_id, file_id, row_number, txn_date, year, "
            "amount, amount_micros, currency, category, category_reason, income_type, "
            "source, track, artist, description, fingerprint, raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            batch,
        )

    def _mark_duplicates(self, chat_id: str, file_id: int) -> None:
        """Flag rows of `file_id` that repeat rows already ingested from other
        files, occurrence-aware: with N prior copies of a fingerprint, only the
        first N matching rows in this file are flagged."""
        self.conn.execute(
            """
            WITH existing AS (
                SELECT fingerprint, COUNT(*) AS n, MIN(id) AS first_id
                FROM transactions
                WHERE chat_id = :chat AND is_duplicate = 0 AND file_id <> :fid
                GROUP BY fingerprint
            ),
            ranked AS (
                SELECT id, fingerprint,
                       ROW_NUMBER() OVER (PARTITION BY fingerprint ORDER BY id) AS rn
                FROM transactions WHERE file_id = :fid
            )
            UPDATE transactions
            SET is_duplicate = 1,
                duplicate_of = (
                    SELECT e.first_id FROM existing e
                    WHERE e.fingerprint = transactions.fingerprint
                )
            WHERE id IN (
                SELECT r.id FROM ranked r
                JOIN existing e ON e.fingerprint = r.fingerprint
                WHERE r.rn <= e.n
            )
            """,
            {"chat": chat_id, "fid": file_id},
        )
        self.conn.commit()

    # ------------------------------------------------------------------ queries

    def transactions(self, chat_id: str | int, include_duplicates: bool = False):
        q = "SELECT * FROM transactions WHERE chat_id=?"
        if not include_duplicates:
            q += " AND is_duplicate=0"
        return self.conn.execute(q, (str(chat_id),)).fetchall()

    def uncategorized(self, chat_id: str | int, limit: int = 50):
        return self.conn.execute(
            "SELECT t.*, f.filename FROM transactions t JOIN files f ON f.id=t.file_id "
            "WHERE t.chat_id=? AND t.is_duplicate=0 AND t.category='Uncategorized' "
            "ORDER BY t.id LIMIT ?",
            (str(chat_id), limit),
        ).fetchall()

    def get_transaction(self, chat_id: str | int, txn_id: int):
        return self.conn.execute(
            "SELECT t.*, f.filename, f.sha256 FROM transactions t "
            "JOIN files f ON f.id=t.file_id WHERE t.chat_id=? AND t.id=?",
            (str(chat_id), txn_id),
        ).fetchone()

    def set_category(self, chat_id: str | int, txn_id: int, category: str) -> bool:
        cur = self.conn.execute(
            "UPDATE transactions SET category=?, manual_category=1, "
            "category_reason='manually set' WHERE chat_id=? AND id=?",
            (category, str(chat_id), txn_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def files(self, chat_id: str | int):
        return self.conn.execute(
            "SELECT * FROM files WHERE chat_id=? ORDER BY id", (str(chat_id),)
        ).fetchall()

    def delete_file(self, chat_id: str | int, file_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM files WHERE chat_id=? AND id=?", (str(chat_id), file_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def reset(self, chat_id: str | int) -> None:
        self.conn.execute("DELETE FROM files WHERE chat_id=?", (str(chat_id),))
        self.conn.commit()
