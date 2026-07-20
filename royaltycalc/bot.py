"""Telegram bot for the music catalog earnings tool.

Usage:
    export TELEGRAM_BOT_TOKEN=123456:ABC...   (from @BotFather)
    python -m royaltycalc.bot

Each Telegram chat gets its own isolated catalog (keyed by chat id).
"""

from __future__ import annotations

import html
import json
import logging
import os
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
from .ingest import SUPPORTED_EXTENSIONS, ingest_file, summary_text
from .report import build_report, fmt_money, render_report
from .store import DuplicateFileError, Store

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("royaltycalc.bot")

DB_PATH = os.environ.get("ROYALTY_DB", "data/royalties.db")

HELP_TEXT = """\
Music Catalog Earnings Bot

Send me royalty statement files (CSV, TSV or Excel) from any producer, artist, \
publisher, distributor or label. I combine them - even with different column \
names - skip duplicate files/transactions, and report your earnings.

Commands:
/report - LTM total, category breakdown and per-year earnings
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


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    chat_id = update.effective_chat.id
    filename = doc.file_name or "statement"
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        await update.message.reply_text(
            f"Unsupported file type '{suffix or '(none)'}'. "
            "Send a CSV, TSV or Excel (.xlsx/.xls) statement."
        )
        return

    tg_file = await doc.get_file()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / filename
        await tg_file.download_to_drive(custom_path=str(local))
        store = get_store(context)
        try:
            summary = ingest_file(store, chat_id, local, filename=filename)
        except DuplicateFileError as e:
            await update.message.reply_text(
                f"Duplicate file skipped: identical content was already ingested as "
                f"'{e.existing_filename}' ({e.uploaded_at}). Nothing was double-counted."
            )
            return
        except ValueError as e:
            await update.message.reply_text(f"Could not read {filename}: {e}")
            return
        except Exception:
            log.exception("Failed to ingest %s", filename)
            await update.message.reply_text(
                f"Something went wrong reading {filename}. "
                "Check that it is a valid statement export."
            )
            return

    await update.message.reply_text(
        summary_text(summary) + "\n\nSend /report for the updated totals."
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = get_store(context)
    rows = store.transactions(update.effective_chat.id)
    rep = build_report(rows)
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
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
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
    app = build_application(token)
    log.info("Bot starting (db=%s)", DB_PATH)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
