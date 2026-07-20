"""Shared ingest pipeline used by both the Telegram bot and the CLI."""

from __future__ import annotations

import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

from .parsing import file_sha256, parse_file
from .store import DuplicateFileError, IngestSummary, Store

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls"}
ARCHIVE_EXTENSIONS = {".zip"}
UPLOAD_EXTENSIONS = SUPPORTED_EXTENSIONS | ARCHIVE_EXTENSIONS

# Safety limits for zip uploads.
MAX_ZIP_MEMBERS = 200
MAX_MEMBER_BYTES = 100 * 1024 * 1024


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


def _expand_zip(path: Path, dest: Path) -> list[tuple[Path, str]]:
    """Extract supported statement files from a zip into `dest`.

    Returns (extracted_path, display_name) pairs. Directories, hidden files,
    macOS metadata and unsupported types are skipped.
    """
    out: list[tuple[Path, str]] = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if info.filename.startswith("__MACOSX"):
                continue
            name = Path(info.filename).name
            if not name or name.startswith("."):
                continue
            if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                log.warning("Skipping oversized zip member %s", info.filename)
                continue
            if len(out) >= MAX_ZIP_MEMBERS:
                log.warning("Zip has more than %d members; extras skipped", MAX_ZIP_MEMBERS)
                break
            target = dest / f"{len(out)}_{name}"
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            out.append((target, name))
    return out


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
    if path.suffix.lower() in ARCHIVE_EXTENSIONS:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                members = _expand_zip(path, Path(tmp))
            except zipfile.BadZipFile:
                return [f"{filename} is not a valid zip archive."]
            if not members:
                return [
                    f"{filename}: no statement files found inside "
                    f"(supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))})."
                ]
            return [
                _ingest_one_to_text(store, chat_id, member_path, f"{filename}/{member_name}")
                for member_path, member_name in members
            ]
    return [_ingest_one_to_text(store, chat_id, path, filename)]


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
