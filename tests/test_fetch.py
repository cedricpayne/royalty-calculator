"""Tests for URL fetching and share-page resolution, using a local HTTP server."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from royaltycalc.fetch import (
    ShareResolveError,
    _confirm_form_url,
    _gdrive_candidates,
    _gdrive_file_id,
    download_statement,
)

CSV_BODY = b"Date,Store,Earnings\n2025-05-01,Spotify,10.00\n"

ROUTES: dict[str, tuple[int, dict, bytes]] = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = ROUTES.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        status, headers, body = route
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture(autouse=True)
def clear_routes():
    ROUTES.clear()


def test_direct_csv(server, tmp_path):
    ROUTES["/statements.csv"] = (200, {"Content-Type": "text/csv"}, CSV_BODY)
    path, name = download_statement(f"{server}/statements.csv", tmp_path, 10**6)
    assert name == "statements.csv"
    assert path.read_bytes() == CSV_BODY


def test_extensionless_url_with_content_type(server, tmp_path):
    ROUTES["/dl"] = (200, {"Content-Type": "application/zip"}, b"PK\x03\x04fake")
    path, name = download_statement(f"{server}/dl", tmp_path, 10**6)
    assert name.endswith(".zip")


def test_content_disposition_filename(server, tmp_path):
    ROUTES["/get"] = (
        200,
        {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": 'attachment; filename="q1_royalties.csv"',
        },
        CSV_BODY,
    )
    path, name = download_statement(f"{server}/get", tmp_path, 10**6)
    assert name == "q1_royalties.csv"


def test_share_page_resolves_href(server, tmp_path):
    ROUTES["/share/abc"] = (
        200,
        {"Content-Type": "text/html"},
        b'<html><body><a class="btn" href="/files/statement.csv">Download</a></body></html>',
    )
    ROUTES["/files/statement.csv"] = (200, {"Content-Type": "text/csv"}, CSV_BODY)
    path, name = download_statement(f"{server}/share/abc", tmp_path, 10**6)
    assert name == "statement.csv"
    assert path.read_bytes() == CSV_BODY


def test_share_page_resolves_json_download_url(server, tmp_path):
    page = (
        '<html><script>window.__STATE__={"file":{"name":"all.zip",'
        f'"downloadUrl":"{server}/api/download/all"'
        "}}</script></html>"
    ).encode()
    ROUTES["/share/xyz"] = (200, {"Content-Type": "text/html"}, page)
    ROUTES["/api/download/all"] = (
        200,
        {
            "Content-Type": "application/zip",
            "Content-Disposition": 'attachment; filename="all.zip"',
        },
        b"PK\x03\x04fake",
    )
    path, name = download_statement(f"{server}/share/xyz", tmp_path, 10**6)
    assert name == "all.zip"


def test_share_page_without_downloads_raises(server, tmp_path):
    ROUTES["/share/empty"] = (
        200,
        {"Content-Type": "text/html"},
        b"<html><body><p>Login required</p></body></html>",
    )
    with pytest.raises(ShareResolveError):
        download_statement(f"{server}/share/empty", tmp_path, 10**6)


def test_unsupported_content_raises(server, tmp_path):
    ROUTES["/thing.pdf"] = (200, {"Content-Type": "application/pdf"}, b"%PDF")
    with pytest.raises(ValueError):
        download_statement(f"{server}/thing.pdf", tmp_path, 10**6)


def test_size_cap_enforced(server, tmp_path):
    ROUTES["/big.csv"] = (200, {"Content-Type": "text/csv"}, b"x" * 5000)
    with pytest.raises(ValueError):
        download_statement(f"{server}/big.csv", tmp_path, 1000)


# ------------------------------------------------------------- Google Drive

def test_gdrive_file_id_extraction():
    fid = "1OA8WrTAbt-SbWRMAAG6HjvhEiLzepuuA"
    assert _gdrive_file_id(
        f"https://drive.google.com/file/d/{fid}/view?usp=drivesdk"
    ) == fid
    assert _gdrive_file_id(
        f"https://drive.google.com/uc?export=download&id={fid}"
    ) == fid
    assert _gdrive_file_id("https://example.com/file/d/abcdefghijkl/view") is None


def test_gdrive_candidates_hit_download_endpoints():
    fid = "1OA8WrTAbt-SbWRMAAG6HjvhEiLzepuuA"
    cands = _gdrive_candidates(f"https://drive.google.com/file/d/{fid}/view?usp=drivesdk")
    assert len(cands) == 2
    assert all(fid in c and "confirm=t" in c for c in cands)
    assert "drive.usercontent.google.com/download" in cands[0]


def test_confirm_form_url_rebuilds_hidden_inputs():
    html = (
        '<html><body><form id="download-form" action="https://drive.usercontent'
        '.google.com/download" method="get">'
        '<input type="hidden" name="id" value="FILEID">'
        '<input type="hidden" name="confirm" value="t">'
        '<input type="hidden" name="uuid" value="u-1">'
        "<input type='submit' value='Download anyway'></form></body></html>"
    )
    url = _confirm_form_url("https://drive.google.com/uc?id=FILEID", html)
    assert url.startswith("https://drive.usercontent.google.com/download?")
    assert "id=FILEID" in url and "confirm=t" in url and "uuid=u-1" in url


def test_interstitial_confirm_flow(server, tmp_path):
    # A share URL that serves a virus-scan-style confirm form, whose action
    # (with hidden inputs) serves the real file.
    form = (
        '<html><body><p>This file cannot be scanned.</p>'
        f'<form action="{server}/real-download" method="get">'
        '<input type="hidden" name="id" value="F123">'
        '<input type="hidden" name="confirm" value="t">'
        "<input type='submit'></form></body></html>"
    ).encode()
    ROUTES["/share/scan"] = (200, {"Content-Type": "text/html"}, form)
    ROUTES["/real-download?id=F123&confirm=t"] = (
        200,
        {
            "Content-Type": "text/csv",
            "Content-Disposition": 'attachment; filename="royalties.csv"',
        },
        CSV_BODY,
    )
    path, name = download_statement(f"{server}/share/scan", tmp_path, 10**6)
    assert name == "royalties.csv"
    assert path.read_bytes() == CSV_BODY
