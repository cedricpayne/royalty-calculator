"""Browser upload page served by the bot itself.

`/upload` in Telegram mints a private, expiring link to a drag-and-drop page
hosted on the bot's own web server. Files stream to disk (no Telegram size
limits), ingest in the background, and results are delivered back to the
Telegram chat. Tokens are unguessable, bound to the chat that requested them,
and expire after two hours.
"""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

from .ingest import UPLOAD_EXTENSIONS

log = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 2 * 3600
MAX_FILES_PER_UPLOAD = 100
MAX_BYTES_PER_FILE = 2 * 1024 * 1024 * 1024  # 2 GB

_ACCEPT = ",".join(sorted(UPLOAD_EXTENSIONS))

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Royalty statement upload</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:640px;margin:3rem auto;padding:0 1rem;color:#222}}
 .box{{border:2px dashed #999;border-radius:12px;padding:2.5rem;text-align:center}}
 button{{font-size:1.1rem;padding:.6rem 1.6rem;border-radius:8px;border:0;background:#2563eb;color:#fff;cursor:pointer}}
 input[type=file]{{margin:1rem 0}}
 .note{{color:#666;font-size:.9rem;margin-top:1.5rem}}
</style></head><body>
<h2>Upload royalty statements</h2>
<form class="box" method="post" enctype="multipart/form-data">
  <p>Choose statement files or zips ({accept})</p>
  <input type="file" name="files" multiple accept="{accept}" required>
  <br><button type="submit">Upload &amp; ingest</button>
</form>
<p class="note">Large uploads can take a while - leave the page open until it
confirms. Processing happens in the background afterwards; results are sent to
your Telegram chat. This link expires two hours after it was created.</p>
</body></html>"""

_DONE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Upload received</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:3rem auto;padding:0 1rem;color:#222}}</style>
</head><body><h2>Upload received</h2><p>{message}</p>
<p>Results will appear in your Telegram chat as each file finishes.
You can close this page, or <a href="">upload more files</a>.</p></body></html>"""


class UploadServer:
    """aiohttp application handling /u/<token> upload pages.

    Decoupled from the Telegram layer: `ingest_func(db_path, chat_id, path,
    filename) -> list[str]` does the work (on a thread) and
    `send_results(chat_id, text)` delivers outcomes, so tests can stub both.
    """

    def __init__(
        self,
        db_path: str,
        ingest_func: Callable[[str, str, str, str], list[str]],
        send_results: Callable[[str, str], Awaitable[None]],
        base_url: str | None = None,
    ):
        self.db_path = db_path
        self.ingest_func = ingest_func
        self.send_results = send_results
        self.base_url = base_url.rstrip("/") if base_url else None
        self._tokens: dict[str, tuple[str, float]] = {}
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------- tokens

    def create_token(self, chat_id: str | int) -> str:
        token = secrets.token_urlsafe(16)
        self._tokens[token] = (str(chat_id), time.time() + TOKEN_TTL_SECONDS)
        self._prune()
        return token

    def create_link(self, chat_id: str | int) -> str | None:
        if not self.base_url:
            return None
        return f"{self.base_url}/u/{self.create_token(chat_id)}"

    def _chat_for(self, token: str) -> str | None:
        entry = self._tokens.get(token)
        if not entry:
            return None
        chat_id, expires = entry
        if time.time() > expires:
            del self._tokens[token]
            return None
        return chat_id

    def _prune(self) -> None:
        now = time.time()
        for tok in [t for t, (_, exp) in self._tokens.items() if now > exp]:
            del self._tokens[tok]

    # ------------------------------------------------------------- handlers

    async def handle_page(self, request: web.Request) -> web.Response:
        if self._chat_for(request.match_info["token"]) is None:
            return web.Response(
                status=404,
                text="This upload link is invalid or has expired. "
                     "Send /upload to the bot to get a fresh one.",
            )
        return web.Response(text=_PAGE.format(accept=_ACCEPT), content_type="text/html")

    async def handle_upload(self, request: web.Request) -> web.Response:
        chat_id = self._chat_for(request.match_info["token"])
        if chat_id is None:
            return web.Response(
                status=404,
                text="This upload link is invalid or has expired. "
                     "Send /upload to the bot to get a fresh one.",
            )

        tmpdir = Path(tempfile.mkdtemp(prefix="royalty_upload_"))
        saved: list[tuple[Path, str]] = []
        rejected: list[str] = []
        try:
            reader = await request.multipart()
            while (part := await reader.next()) is not None:
                if part.name != "files" or not part.filename:
                    continue
                filename = Path(part.filename).name
                if len(saved) >= MAX_FILES_PER_UPLOAD:
                    rejected.append(f"{filename}: over the {MAX_FILES_PER_UPLOAD}-file limit")
                    continue
                if Path(filename).suffix.lower() not in UPLOAD_EXTENSIONS:
                    rejected.append(f"{filename}: unsupported type")
                    continue
                target = tmpdir / f"{len(saved)}_{filename}"
                size = 0
                too_big = False
                with open(target, "wb") as fh:
                    while chunk := await part.read_chunk(1 << 20):
                        size += len(chunk)
                        if size > MAX_BYTES_PER_FILE:
                            too_big = True
                            break
                        fh.write(chunk)
                if too_big:
                    target.unlink(missing_ok=True)
                    rejected.append(
                        f"{filename}: exceeds {MAX_BYTES_PER_FILE >> 30} GB per-file limit"
                    )
                    continue
                saved.append((target, filename))
        except Exception:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise

        if not saved:
            shutil.rmtree(tmpdir, ignore_errors=True)
            detail = "; ".join(rejected) if rejected else "no files were included"
            return web.Response(
                status=400, text=f"Nothing to ingest: {html.escape(detail)}."
            )

        task = asyncio.get_running_loop().create_task(
            self._process(chat_id, saved, rejected, tmpdir)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        message = f"{len(saved)} file(s) accepted and queued for processing."
        if rejected:
            message += " Skipped: " + html.escape("; ".join(rejected)) + "."
        return web.Response(text=_DONE.format(message=message), content_type="text/html")

    async def _process(
        self,
        chat_id: str,
        files: list[tuple[Path, str]],
        rejected: list[str],
        tmpdir: Path,
    ) -> None:
        try:
            results: list[str] = list(rejected)
            for path, filename in files:
                try:
                    results.extend(
                        await asyncio.to_thread(
                            self.ingest_func, self.db_path, chat_id, str(path), filename
                        )
                    )
                except Exception:
                    log.exception("Web upload ingest failed for %s", filename)
                    results.append(f"Something went wrong reading {filename}.")
                path.unlink(missing_ok=True)
            text = "\n\n".join(results)
            text += f"\n\nProcessed {len(files)} uploaded file(s). Send /report for the updated totals."
            await self.send_results(chat_id, text)
        except Exception:
            log.exception("Web upload processing failed for chat %s", chat_id)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def drain(self) -> None:
        """Wait for background ingest tasks (used by tests and shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ------------------------------------------------------------- app

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.handle_health)
        app.router.add_get("/u/{token}", self.handle_page)
        app.router.add_post("/u/{token}", self.handle_upload)
        return app

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.Response(text="Music Catalog Earnings Bot is running.")
