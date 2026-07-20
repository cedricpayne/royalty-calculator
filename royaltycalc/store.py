"""SQLite persistence for statements and transactions.

Every transaction keeps: the file it came from, its row number in that file,
the full raw row (JSON) and the reason it was categorized the way it was -
so every reported number is traceable back to its origin.

Duplicate handling:
  * whole-file duplicates: rejected by content hash (SHA-256), regardless of filename;
  * transaction duplicates: when a newly uploaded file contains rows whose raw
    content is identical to rows already ingested from *other* files, those rows
    are stored but flagged `is_duplicate=1` and excluded from all totals.
    Matching is occurrence-aware: if an earlier file legitimately contains the
    same line twice and the new file has it three times, only two are considered
    already-counted... (min of the two counts is preserved as non-duplicate).
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .parsing import ParseResult, ParsedRow

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
"""


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
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ ingest

    def ingest(
        self,
        chat_id: str | int,
        filename: str,
        sha256: str,
        result: ParseResult,
    ) -> IngestSummary:
        chat_id = str(chat_id)
        cur = self.conn.execute(
            "SELECT filename, uploaded_at FROM files WHERE chat_id=? AND sha256=?",
            (chat_id, sha256),
        )
        existing = cur.fetchone()
        if existing:
            raise DuplicateFileError(existing["filename"], existing["uploaded_at"])

        # Occurrence counts of each fingerprint already ingested (non-duplicate)
        # in this chat, so duplicate detection is multiset-aware.
        existing_counts: Counter[str] = Counter()
        first_seen: dict[str, int] = {}
        for row in self.conn.execute(
            "SELECT fingerprint, MIN(id) AS first_id, COUNT(*) AS n FROM transactions "
            "WHERE chat_id=? AND is_duplicate=0 GROUP BY fingerprint",
            (chat_id,),
        ):
            existing_counts[row["fingerprint"]] = row["n"]
            first_seen[row["fingerprint"]] = row["first_id"]

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = self.conn.execute(
            "INSERT INTO files (chat_id, filename, sha256, uploaded_at, column_map) "
            "VALUES (?,?,?,?,?)",
            (chat_id, filename, sha256, now, json.dumps(result.column_map)),
        )
        file_id = cur.lastrowid

        ingested = duplicates = uncategorized = 0
        total_added = Decimal("0")
        for r in result.rows:
            fp = r.fingerprint
            is_dup = 0
            dup_of = None
            if existing_counts.get(fp, 0) > 0:
                existing_counts[fp] -= 1
                is_dup = 1
                dup_of = first_seen.get(fp)
            self._insert_txn(chat_id, file_id, r, is_dup, dup_of)
            if is_dup:
                duplicates += 1
            else:
                ingested += 1
                total_added += r.amount
                if r.category == "Uncategorized":
                    uncategorized += 1

        self.conn.execute(
            "UPDATE files SET rows_ingested=?, rows_duplicate=?, rows_skipped=? WHERE id=?",
            (ingested, duplicates, len(result.skipped), file_id),
        )
        self.conn.commit()
        return IngestSummary(
            file_id=file_id,
            filename=filename,
            rows_ingested=ingested,
            rows_duplicate=duplicates,
            rows_skipped=len(result.skipped),
            uncategorized=uncategorized,
            total_added=total_added,
            warnings=list(result.warnings),
            skipped_details=list(result.skipped),
        )

    def _insert_txn(
        self, chat_id: str, file_id: int, r: ParsedRow, is_dup: int, dup_of: int | None
    ) -> None:
        self.conn.execute(
            "INSERT INTO transactions (chat_id, file_id, row_number, txn_date, year, "
            "amount, currency, category, category_reason, income_type, source, track, "
            "artist, description, fingerprint, is_duplicate, duplicate_of, raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                chat_id, file_id, r.row_number,
                r.txn_date.isoformat() if r.txn_date else None,
                r.txn_date.year if r.txn_date else None,
                str(r.amount), r.currency, r.category, r.category_reason,
                r.income_type, r.source, r.track, r.artist, r.description,
                r.fingerprint, is_dup, dup_of,
                json.dumps(r.raw, ensure_ascii=False),
            ),
        )

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
