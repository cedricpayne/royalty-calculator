"""Telegram bot for the music catalog earnings tool.

Usage:
    export TELEGRAM_BOT_TOKEN=123456:ABC...   (from @BotFather)
    python -m royaltycalc.bot

Each Telegram chat gets its own isolated catalog (keyed by chat id).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import tempfile
from decimal import Decimal
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .categorize import CATEGORIES, resolve_category_name
from .ingest import UPLOAD_EXTENSIONS, ingest_upload
from .report import build_report, fmt_money, render_report
from .store import Store

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("royaltycalc.bot")

DB_PATH = os.environ.get("ROYALTY_DB", "data/royalties.db")

# Optional local Bot API server (https://github.com/tdlib/telegram-bot-api):
# lifts Telegram's 20 MB bot-download limit to 2 GB. e.g.
#   TELEGRAM_API_BASE_URL=http://bot-api:8081/bot
#   TELEGRAM_API_BASE_FILE_URL=http://bot-api:8081/file/bot
API_BASE_URL = os.environ.get("TELEGRAM_API_BASE_URL")
API_BASE_FILE_URL = os.environ.get("TELEGRAM_API_BASE_FILE_URL")
MAX_TELEGRAM_FILE = (2000 if API_BASE_URL else 20) * 1024 * 1024
MAX_FETCH_BYTES = int(os.environ.get("ROYALTY_MAX_FETCH_MB", "1024")) * 1024 * 1024
LARGE_FILE_ACK_BYTES = 5 * 1024 * 1024

HELP_TEXT = """\
Music Catalog Earnings Bot

Send me royalty statement files (CSV, TSV or Excel) from any producer, artist, \
publisher, distributor or label. You can send several files in one message, or \
a .zip containing any number of statements. I combine them - even with \
different column names - skip duplicate files/transactions, and report your \
earnings.

For large catalogs, zip your statements (CSVs compress ~10x). Telegram limits \
bot downloads to 20 MB per file, so split big collections into several zips - \
or use /fetch with a direct download link (Dropbox/Drive/S3), which has no \
20 MB limit.

Commands:
/report - LTM total, category breakdown and per-year earnings
/fetch <url> - ingest a statement or zip from a direct download link
/uncategorized - transactions needing manual review
/categorize <id> <category> - assign a category (masters, publishing, producer, neighbouring, other)
/trace <id> - show the original file, row and raw data for a transaction
/files - list ingested statements
/deletefile <id> - remove a statement and its transactions
/reset - delete ALL data for this chat (asks for confirmation)
/help - this message
"""


def get_store(context: ContextTypes.DEFAULT_TYPE) -> Store:
    store = context.application.bot_data.get("store")
    if store is None:
        store = Store(DB_PATH)
        context.application.bot_data["store"] = store
    return store


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


TELEGRAM_MESSAGE_LIMIT = 4000  # keep headroom under Telegram's 4096-char cap
BATCH_FLUSH_SECONDS = 2.5      # quiet time before replying to a multi-file album


def _chunk_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split a long reply into Telegram-sized chunks on blank-line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for block in text.split("\n\n"):
        if current and length + len(block) + 2 > limit:
            chunks.append("\n\n".join(current))
            current, length = [], 0
        current.append(block)
        length += len(block) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


async def _send_chunked(bot, chat_id: int, text: str) -> None:
    for chunk in _chunk_text(text):
        await bot.send_message(chat_id, chunk)


async def _flush_batch(context: ContextTypes.DEFAULT_TYPE, chat_id: int, group_id: str) -> None:
    """After a quiet period, send one combined reply for a multi-file album."""
    try:
        await asyncio.sleep(BATCH_FLUSH_SECONDS)
    except asyncio.CancelledError:
        return  # another file from the same album arrived; a new flush is armed
    batches = context.chat_data.get("upload_batches", {})
    entry = batches.pop(group_id, None)
    if not entry or not entry["lines"]:
        return
    n = entry["count"]
    text = "\n\n".join(entry["lines"]) + (
        f"\n\nProcessed {n} file(s). Send /report for the updated totals."
    )
    await _send_chunked(context.bot, chat_id, text)


def _ingest_blocking(db_path: str, chat_id: int, path: str, filename: str) -> list[str]:
    """Runs on a worker thread: own DB connection so the event loop stays free."""
    store = Store(db_path)
    try:
        return ingest_upload(store, chat_id, path, filename=filename)
    finally:
        store.close()


def _too_big_message(filename: str, size: int) -> str:
    mb = size / (1024 * 1024)
    if API_BASE_URL:
        return (
            f"{filename} is {mb:,.0f} MB, above the 2 GB local Bot API limit. "
            "Split it into smaller archives."
        )
    return (
        f"{filename} is {mb:,.0f} MB, but Telegram only lets bots download files "
        "up to 20 MB. Options:\n"
        "1) Zip your CSVs (they compress ~10x) and split into archives under 20 MB - "
        "you can send several zips in one message;\n"
        "2) Put the file anywhere with a direct download link (Dropbox, Drive, S3) "
        "and send /fetch <link> - no 20 MB limit;\n"
        "3) Self-host a Telegram Bot API server for a 2 GB limit (see README)."
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    chat_id = update.effective_chat.id
    filename = doc.file_name or "statement"
    suffix = Path(filename).suffix.lower()
    if suffix not in UPLOAD_EXTENSIONS:
        results = [
            f"Unsupported file type '{suffix or '(none)'}' ({filename}). "
            "Send CSV, TSV, Excel (.xlsx/.xls) or a .zip of statements."
        ]
    elif doc.file_size and doc.file_size > MAX_TELEGRAM_FILE:
        results = [_too_big_message(filename, doc.file_size)]
    else:
        if (
            doc.file_size
            and doc.file_size > LARGE_FILE_ACK_BYTES
            and update.message.media_group_id is None
        ):
            await update.message.reply_text(
                f"Received {filename} ({doc.file_size / (1024*1024):,.0f} MB) - "
                "processing. Large statements can take a few minutes."
            )
        tg_file = await doc.get_file()
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / filename
            await tg_file.download_to_drive(custom_path=str(local))
            # Parsing/inserting big files is CPU-bound; keep the event loop free.
            results = await asyncio.to_thread(
                _ingest_blocking, DB_PATH, chat_id, str(local), filename
            )

    group_id = update.message.media_group_id
    if group_id is None:
        # Single file (or a zip): reply immediately.
        text = "\n\n".join(results)
        if len(results) > 1:
            text += f"\n\nProcessed {len(results)} file(s)."
        text += "\n\nSend /report for the updated totals."
        await _send_chunked(context.bot, chat_id, text)
        return

    # Part of an album (multiple files sent together): each file arrives as its
    # own message with the same media_group_id and no end marker, so buffer the
    # results and reply once after a short quiet period.
    batches = context.chat_data.setdefault("upload_batches", {})
    entry = batches.setdefault(group_id, {"lines": [], "count": 0, "task": None})
    entry["lines"].extend(results)
    entry["count"] += len(results)
    if entry["task"] is not None:
        entry["task"].cancel()
    entry["task"] = context.application.create_task(
        _flush_batch(context, chat_id, group_id), update=update
    )


def _filename_from_response(url: str, content_disposition: str | None) -> str:
    if content_disposition:
        m = re.search(r'filename\*?="?([^";]+)"?', content_disposition)
        if m:
            return Path(m.group(1).strip()).name
    from urllib.parse import unquote, urlparse

    return Path(unquote(urlparse(url).path)).name or "statement"


def _download_blocking(url: str, dest_dir: Path) -> tuple[Path, str]:
    """Stream a URL to disk with a size cap. Runs on a worker thread."""
    import httpx

    with httpx.Client(
        follow_redirects=True, timeout=httpx.Timeout(30.0, read=300.0)
    ) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            filename = _filename_from_response(str(resp.url), resp.headers.get("content-disposition"))
            if Path(filename).suffix.lower() not in UPLOAD_EXTENSIONS:
                raise ValueError(
                    f"The link serves '{filename}', which is not a supported type. "
                    "Use a direct link to a CSV/TSV/Excel file or a .zip of statements."
                )
            length = resp.headers.get("content-length")
            if length and int(length) > MAX_FETCH_BYTES:
                raise ValueError(
                    f"File is {int(length) / (1024*1024):,.0f} MB, above the "
                    f"{MAX_FETCH_BYTES >> 20} MB /fetch limit."
                )
            target = dest_dir / filename
            written = 0
            with open(target, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    written += len(chunk)
                    if written > MAX_FETCH_BYTES:
                        raise ValueError(
                            f"Download exceeded the {MAX_FETCH_BYTES >> 20} MB /fetch limit."
                        )
                    fh.write(chunk)
    return target, filename


async def cmd_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args or not args[0].lower().startswith(("http://", "https://")):
        await update.message.reply_text(
            "Usage: /fetch <direct download link>\n"
            "The link must point straight at a CSV/TSV/Excel file or a .zip of "
            "statements (for Dropbox use dl=1, for Google Drive a direct-download link)."
        )
        return
    url = args[0]
    chat_id = update.effective_chat.id
    await update.message.reply_text("Downloading... large files can take a few minutes.")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            local, filename = await asyncio.to_thread(_download_blocking, url, Path(tmp))
        except ValueError as e:
            await update.message.reply_text(str(e))
            return
        except Exception as e:
            log.warning("Fetch failed for %s: %s", url, e)
            await update.message.reply_text(
                f"Download failed: {e}\nCheck that the link is a direct, public download."
            )
            return
        results = await asyncio.to_thread(
            _ingest_blocking, DB_PATH, chat_id, str(local), filename
        )
    text = "\n\n".join(results)
    if len(results) > 1:
        text += f"\n\nProcessed {len(results)} file(s)."
    text += "\n\nSend /report for the updated totals."
    await _send_chunked(context.bot, chat_id, text)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rep = build_report(get_store(context), update.effective_chat.id)
    await update.message.reply_text(render_report(rep))


async def cmd_uncategorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = get_store(context)
    rows = store.uncategorized(update.effective_chat.id, limit=25)
    if not rows:
        await update.message.reply_text("No uncategorized transactions. All income is classified.")
        return
    lines = ["Uncategorized transactions (up to 25):", ""]
    for r in rows:
        desc = r["income_type"] or r["description"] or r["track"] or r["source"] or "?"
        lines.append(
            f"#{r['id']}  {r['txn_date'] or 'no date'}  "
            f"{fmt_money(Decimal(r['amount']))}  "
            f"{desc[:40]}  [{r['filename']}:{r['row_number']}]"
        )
    lines.append("")
    lines.append("Assign with: /categorize <id> <masters|publishing|producer|neighbouring|other>")
    lines.append("Inspect with: /trace <id>")
    await update.message.reply_text("\n".join(lines))


async def cmd_categorize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /categorize <transaction id> <category>\n"
            "Categories: masters, publishing, producer, neighbouring, other"
        )
        return
    try:
        txn_id = int(args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("Transaction id must be a number, e.g. /categorize 42 masters")
        return
    category = resolve_category_name(" ".join(args[1:]))
    if category is None:
        await update.message.reply_text(
            "Unknown category. Use one of: masters, publishing, producer, neighbouring, other"
        )
        return
    store = get_store(context)
    if store.set_category(update.effective_chat.id, txn_id, category):
        await update.message.reply_text(f"Transaction #{txn_id} set to {category}.")
    else:
        await update.message.reply_text(f"No transaction #{txn_id} found in this chat.")


async def cmd_trace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /trace <transaction id>")
        return
    try:
        txn_id = int(args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("Transaction id must be a number, e.g. /trace 42")
        return
    store = get_store(context)
    r = store.get_transaction(update.effective_chat.id, txn_id)
    if r is None:
        await update.message.reply_text(f"No transaction #{txn_id} found in this chat.")
        return
    raw = json.dumps(json.loads(r["raw_json"]), indent=2, ensure_ascii=False)
    dup = ""
    if r["is_duplicate"]:
        dup = f"\nDUPLICATE - excluded from totals (same as transaction #{r['duplicate_of']})"
    text = (
        f"Transaction #{r['id']}\n"
        f"File: {r['filename']} (file #{r['file_id']}, sha256 {r['sha256'][:12]}...)\n"
        f"Row in file: {r['row_number']}\n"
        f"Date: {r['txn_date'] or 'none'}   Amount: {r['amount']} {r['currency'] or ''}\n"
        f"Category: {r['category']} ({r['category_reason']})"
        f"{dup}\n\nOriginal row:\n<pre>{html.escape(raw)}</pre>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = get_store(context)
    rows = store.files(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("No files ingested yet.")
        return
    lines = ["Ingested statements:", ""]
    for f in rows:
        lines.append(
            f"#{f['id']}  {f['filename']}  ({f['uploaded_at']})\n"
            f"    {f['rows_ingested']} rows, {f['rows_duplicate']} duplicates, "
            f"{f['rows_skipped']} skipped  sha256 {f['sha256'][:12]}..."
        )
    lines.append("\nRemove one with /deletefile <id>")
    await update.message.reply_text("\n".join(lines))


async def cmd_deletefile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /deletefile <file id> (see /files)")
        return
    try:
        file_id = int(args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("File id must be a number, e.g. /deletefile 3")
        return
    store = get_store(context)
    if store.delete_file(update.effective_chat.id, file_id):
        await update.message.reply_text(
            f"File #{file_id} and its transactions were removed. Send /report for updated totals."
        )
    else:
        await update.message.reply_text(f"No file #{file_id} found in this chat.")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if args and args[0].lower() == "confirm":
        get_store(context).reset(update.effective_chat.id)
        await update.message.reply_text("All data for this chat has been deleted.")
    else:
        await update.message.reply_text(
            "This deletes ALL ingested statements and transactions for this chat.\n"
            "Send /reset confirm to proceed."
        )


def build_application(token: str) -> Application:
    builder = (
        Application.builder()
        .token(token)
        # Generous timeouts: downloading a 20 MB statement (or 2 GB via a
        # local Bot API server) takes longer than the 5 s defaults.
        .connect_timeout(30)
        .read_timeout(300)
        .write_timeout(300)
        .pool_timeout(60)
    )
    if API_BASE_URL:
        builder = builder.base_url(API_BASE_URL)
    if API_BASE_FILE_URL:
        builder = builder.base_file_url(API_BASE_FILE_URL)
    app = builder.build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("fetch", cmd_fetch))
    app.add_handler(CommandHandler("uncategorized", cmd_uncategorized))
    app.add_handler(CommandHandler("categorize", cmd_categorize))
    app.add_handler(CommandHandler("trace", cmd_trace))
    app.add_handler(CommandHandler("files", cmd_files))
    app.add_handler(CommandHandler("deletefile", cmd_deletefile))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    return app


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN (get one from @BotFather on Telegram) and retry."
        )
    if "ROYALTY_DB" not in os.environ:
        log.warning(
            "ROYALTY_DB is not set - using local %s. On ephemeral hosts "
            "(e.g. Railway) attach a persistent volume and point ROYALTY_DB "
            "at it, or all data is lost on every redeploy.",
            DB_PATH,
        )
    app = build_application(token)
    log.info("Bot starting (db=%s)", DB_PATH)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
