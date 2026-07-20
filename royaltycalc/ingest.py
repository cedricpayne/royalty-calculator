"""Shared ingest pipeline used by both the Telegram bot and the CLI."""

from __future__ import annotations

from pathlib import Path

from .parsing import file_sha256, parse_file
from .store import IngestSummary, Store

SUPPORTED_EXTENSIONS = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls"}


def ingest_file(store: Store, chat_id: str | int, path: str | Path,
                filename: str | None = None) -> IngestSummary:
    path = Path(path)
    filename = filename or path.name
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type '{path.suffix}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    sha256 = file_sha256(path)
    result = parse_file(path, filename=filename)
    return store.ingest(chat_id, filename, sha256, result)


def summary_text(s: IngestSummary) -> str:
    lines = [
        f"Ingested {s.filename} (file #{s.file_id}):",
        f"  {s.rows_ingested} transactions added "
        f"(total {s.total_added:,.2f})",
    ]
    if s.rows_duplicate:
        lines.append(
            f"  {s.rows_duplicate} duplicate transactions skipped "
            "(already ingested from another file)"
        )
    if s.rows_skipped:
        lines.append(f"  {s.rows_skipped} rows skipped (totals/unparseable)")
    if s.uncategorized:
        lines.append(
            f"  {s.uncategorized} transactions need manual review - /uncategorized"
        )
    for w in s.warnings:
        lines.append(f"  Warning: {w}")
    return "\n".join(lines)
