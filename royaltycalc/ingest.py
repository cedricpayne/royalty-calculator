"""Shared ingest pipeline used by both the Telegram bot and the CLI."""

from __future__ import annotations

import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

from . import ai
from .parsing import StatementReader, file_sha256, layout_signature, preview_rows
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
    """Ingest a single statement, streaming rows straight into the store.

    When neither header aliases nor content inference can decode the layout
    and the AI layer is enabled, Claude maps the columns from a preview of
    the file; mappings are cached by layout signature so a batch of
    same-format statements costs a single API call."""
    path = Path(path)
    filename = filename or path.name
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type '{path.suffix}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    sha256 = file_sha256(path)
    try:
        reader = StatementReader(path, filename=filename)
    except ValueError as parse_error:
        reader = _ai_reader(store, path, filename, parse_error)
    return store.ingest(chat_id, filename, sha256, reader)


def _ai_reader(store: Store, path: Path, filename: str,
               parse_error: ValueError) -> StatementReader:
    """Last-resort layout mapping via Claude; re-raises the original parse
    error whenever AI is unavailable or can't produce a working mapping."""
    if not ai.ai_enabled():
        raise parse_error
    preview = preview_rows(path)
    if not preview:
        raise parse_error
    signature = layout_signature(preview, path.suffix)

    cached = store.get_layout(signature)
    if cached is not None:
        try:
            return StatementReader(path, filename=filename, column_overrides=cached)
        except ValueError:
            log.warning("Cached layout %s no longer fits %s; re-mapping", signature, filename)

    try:
        mapping = ai.map_columns(preview, filename)
    except Exception:
        log.exception("AI column mapping failed for %s", filename)
        raise parse_error from None
    if mapping is None:
        raise parse_error
    try:
        reader = StatementReader(path, filename=filename, column_overrides=mapping)
    except ValueError:
        raise parse_error from None
    store.save_layout(signature, mapping)
    log.info("AI mapped layout %s for %s", signature, filename)
    return reader


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
