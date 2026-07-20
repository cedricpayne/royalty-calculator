"""Shared ingest pipeline used by both the Telegram bot and the CLI."""

from __future__ import annotations

import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

from .parsing import StatementReader, file_sha256
from .store import DuplicateFileError, IngestSummary, Store

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls"}
ARCHIVE_EXTENSIONS = {".zip"}
UPLOAD_EXTENSIONS = SUPPORTED_EXTENSIONS | ARCHIVE_EXTENSIONS

# Safety limits for zip uploads. Members are extracted, ingested and deleted
# one at a time, so disk usage peaks at one member, not the whole archive.
MAX_ZIP_MEMBERS = 500
MAX_MEMBER_BYTES = 1024 * 1024 * 1024        # 1 GB per statement
MAX_TOTAL_EXTRACTED = 4 * 1024 * 1024 * 1024  # 4 GB per archive


def ingest_file(store: Store, chat_id: str | int, path: str | Path,
                filename: str | None = None) -> IngestSummary:
    """Ingest a single statement, streaming rows straight into the store."""
    path = Path(path)
    filename = filename or path.name
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type '{path.suffix}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    sha256 = file_sha256(path)
    reader = StatementReader(path, filename=filename)
    return store.ingest(chat_id, filename, sha256, reader)


def _is_statement_member(info: zipfile.ZipInfo) -> bool:
    if info.is_dir() or info.filename.startswith("__MACOSX"):
        return False
    name = Path(info.filename).name
    if not name or name.startswith("."):
        return False
    return Path(name).suffix.lower() in SUPPORTED_EXTENSIONS


def _ingest_one_to_text(store: Store, chat_id: str | int, path: Path, filename: str) -> str:
    """Ingest a single statement and return a human-readable result line."""
    try:
        return summary_text(ingest_file(store, chat_id, path, filename=filename))
    except DuplicateFileError as e:
        return (
            f"Duplicate file skipped: {filename} is identical to "
            f"'{e.existing_filename}' ({e.uploaded_at}). Nothing was double-counted."
        )
    except ValueError as e:
        return f"Could not read {filename}: {e}"
    except Exception:
        log.exception("Failed to ingest %s", filename)
        return f"Something went wrong reading {filename}. Check that it is a valid statement export."


def ingest_upload(store: Store, chat_id: str | int, path: str | Path,
                  filename: str | None = None) -> list[str]:
    """Ingest an uploaded file - a single statement or a .zip of statements.

    Returns one result text per statement processed; never raises for
    per-file problems (they become result lines instead).
    """
    path = Path(path)
    filename = filename or path.name
    if path.suffix.lower() not in ARCHIVE_EXTENSIONS:
        return [_ingest_one_to_text(store, chat_id, path, filename)]

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return [f"{filename} is not a valid zip archive."]

    results: list[str] = []
    with zf:
        members = [i for i in zf.infolist() if _is_statement_member(i)]
        if not members:
            return [
                f"{filename}: no statement files found inside "
                f"(supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))})."
            ]
        if len(members) > MAX_ZIP_MEMBERS:
            results.append(
                f"Zip contains {len(members)} statements; processing the first "
                f"{MAX_ZIP_MEMBERS}. Split the rest into another archive."
            )
            members = members[:MAX_ZIP_MEMBERS]

        total_extracted = 0
        with tempfile.TemporaryDirectory() as tmp:
            for k, info in enumerate(members):
                name = Path(info.filename).name
                if info.file_size > MAX_MEMBER_BYTES:
                    results.append(
                        f"Skipped {name}: {info.file_size >> 20} MB uncompressed "
                        f"exceeds the {MAX_MEMBER_BYTES >> 20} MB per-file limit."
                    )
                    continue
                total_extracted += info.file_size
                if total_extracted > MAX_TOTAL_EXTRACTED:
                    results.append(
                        f"Stopped at {name}: archive expands beyond "
                        f"{MAX_TOTAL_EXTRACTED >> 30} GB. Split it into smaller archives."
                    )
                    break
                target = Path(tmp) / f"{k}_{name}"
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                results.append(
                    _ingest_one_to_text(store, chat_id, target, f"{filename}/{name}")
                )
                target.unlink(missing_ok=True)
    return results


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
