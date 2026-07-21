"""Tests for the browser upload page (aiohttp app), end to end through ingest."""

import asyncio
from pathlib import Path

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from royaltycalc.ingest import ingest_upload
from royaltycalc.store import Store
from royaltycalc.webupload import UploadServer

CSV_BODY = b"Date,Store,Earnings\n2025-05-01,Spotify,10.00\n2025-06-01,Spotify,12.50\n"


def _ingest(db_path, chat_id, path, filename):
    store = Store(db_path)
    try:
        return ingest_upload(store, chat_id, path, filename=filename)
    finally:
        store.close()


def run(coro):
    return asyncio.run(coro)


def make_server(tmp_path, sent):
    async def send_results(chat_id, text):
        sent.append((chat_id, text))

    return UploadServer(
        db_path=str(tmp_path / "web.db"),
        ingest_func=_ingest,
        send_results=send_results,
        base_url="https://bot.example.com",
    )


def test_create_link_and_token_binding(tmp_path):
    server = make_server(tmp_path, [])
    link = server.create_link("chat42")
    assert link.startswith("https://bot.example.com/u/")
    token = link.rsplit("/", 1)[1]
    assert server._chat_for(token) == "chat42"
    assert server._chat_for("bogus") is None


def test_no_base_url_returns_none(tmp_path):
    server = UploadServer(
        db_path=str(tmp_path / "x.db"),
        ingest_func=_ingest,
        send_results=lambda c, t: None,
        base_url=None,
    )
    assert server.create_link("chat") is None


def test_upload_page_and_ingest_flow(tmp_path):
    sent = []

    async def main():
        server = make_server(tmp_path, sent)
        client = TestClient(TestServer(server.build_app()))
        await client.start_server()
        try:
            token = server.create_token("chat1")

            resp = await client.get(f"/u/{token}")
            assert resp.status == 200
            assert "Upload royalty statements" in await resp.text()

            data = aiohttp.FormData()
            data.add_field("files", CSV_BODY, filename="may.csv",
                           content_type="text/csv")
            data.add_field("files", b"junk", filename="notes.pdf",
                           content_type="application/pdf")
            resp = await client.post(f"/u/{token}", data=data)
            assert resp.status == 200
            body = await resp.text()
            assert "1 file(s) accepted" in body
            assert "notes.pdf: unsupported type" in body

            await server.drain()
        finally:
            await client.close()

    run(main())
    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == "chat1"
    assert "2 transactions added" in text

    store = Store(tmp_path / "web.db")
    assert len(store.transactions("chat1")) == 2
    store.close()


def test_invalid_token_rejected(tmp_path):
    async def main():
        server = make_server(tmp_path, [])
        client = TestClient(TestServer(server.build_app()))
        await client.start_server()
        try:
            resp = await client.get("/u/nope")
            assert resp.status == 404
            data = aiohttp.FormData()
            data.add_field("files", CSV_BODY, filename="x.csv", content_type="text/csv")
            resp = await client.post("/u/nope", data=data)
            assert resp.status == 404
        finally:
            await client.close()

    run(main())


def test_upload_with_no_valid_files_is_400(tmp_path):
    async def main():
        server = make_server(tmp_path, [])
        client = TestClient(TestServer(server.build_app()))
        await client.start_server()
        try:
            token = server.create_token("chat9")
            data = aiohttp.FormData()
            data.add_field("files", b"x", filename="report.pdf",
                           content_type="application/pdf")
            resp = await client.post(f"/u/{token}", data=data)
            assert resp.status == 400
        finally:
            await client.close()

    run(main())


def test_expired_token(tmp_path, monkeypatch):
    server = make_server(tmp_path, [])
    token = server.create_token("chat1")
    chat, _ = server._tokens[token]
    server._tokens[token] = (chat, 1.0)  # long expired
    assert server._chat_for(token) is None
