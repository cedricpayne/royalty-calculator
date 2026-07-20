"""Download statements from URLs, resolving share pages to direct file links.

`/fetch` accepts more than direct file URLs: if a link serves an HTML page
(Hightail, Dropbox, Drive and similar share pages), the resolver scans the
page for likely download URLs - "downloadUrl"-style JSON keys, hrefs that
point at supported file types, links containing "download" - and follows the
best candidates a couple of hops deep. Files are validated by BOTH filename
extension and Content-Type, so extension-less presigned URLs work too.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx

from .ingest import UPLOAD_EXTENSIONS

log = logging.getLogger(__name__)

CONTENT_TYPE_EXT = {
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "text/csv": ".csv",
    "application/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}
HTML_TYPES = {"text/html", "application/xhtml+xml"}

MAX_PAGE_BYTES = 2 * 1024 * 1024   # how much of a share page we read
MAX_RESOLVE_DEPTH = 2              # share page -> link -> file
MAX_CANDIDATES = 6                 # candidate links tried per page

# Extensions that are clearly page assets, never statements.
_JUNK_EXTENSIONS = {
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff",
    ".woff2", ".ttf", ".eot", ".map", ".webp", ".mp4", ".html", ".htm",
}

_DOWNLOAD_KEY_RE = re.compile(
    r'"(?:download_?url|directdownloadurl|file_?url|contenturl|href)"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)
_HREF_RE = re.compile(r"""(?:href|src|data-url|data-href)=["']([^"'<>]+)""", re.IGNORECASE)
_BARE_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")

_BROWSER_HEADERS = {
    # Some share hosts refuse non-browser clients outright.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


class ShareResolveError(ValueError):
    """The URL serves a web page and no downloadable statement was found on it."""


def filename_from_response(url: str, content_disposition: str | None) -> str | None:
    if content_disposition:
        m = re.search(r"filename\*=(?:UTF-8'')?\"?([^\";]+)\"?", content_disposition,
                      re.IGNORECASE)
        if not m:
            m = re.search(r'filename="?([^";]+)"?', content_disposition, re.IGNORECASE)
        if m:
            name = Path(unquote(m.group(1).strip())).name
            if name:
                return name
    name = Path(unquote(urlparse(url).path)).name
    return name or None


def _dropbox_direct(url: str) -> str:
    """Dropbox share links serve a page unless dl=1 is set - fix that silently."""
    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname.endswith("dropbox.com") and "dl=1" not in url:
        sep = "&" if parsed.query else "?"
        return re.sub(r"([?&])dl=0", r"\g<1>dl=1", url) if "dl=0" in url else url + sep + "dl=1"
    return url


def _hightail_candidates(url: str) -> list[str]:
    """Hightail Spaces receive links: the 'Download all' endpoint lives under
    the share URL. Try it before scraping the page."""
    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname.endswith("hightail.com") \
            and "/receive/" in parsed.path:
        base = url.split("?")[0].rstrip("/")
        return [base + "/download", base + "/download/all"]
    return []


def _score_candidate(base_url: str, candidate: str) -> int:
    score = 0
    lower = candidate.lower()
    path = urlparse(candidate).path.lower()
    suffix = Path(path).suffix
    if suffix in UPLOAD_EXTENSIONS:
        score += 5
    if "download" in lower:
        score += 3
    if urlparse(candidate).hostname == urlparse(base_url).hostname:
        score += 1
    return score


def _extract_candidates(base_url: str, html: str) -> list[str]:
    """Pull likely download URLs out of a share page, best first."""
    # JSON often escapes slashes; unescape a copy for URL scanning.
    unescaped = html.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    found: dict[str, int] = {}

    def add(raw: str, bonus: int = 0) -> None:
        raw = raw.strip().rstrip(".,);\"'")
        if not raw or raw.startswith(("javascript:", "mailto:", "#", "data:")):
            return
        absolute = urljoin(base_url, raw)
        if not absolute.startswith(("http://", "https://")):
            return
        if Path(urlparse(absolute).path).suffix.lower() in _JUNK_EXTENSIONS:
            return
        if absolute.split("#")[0] == base_url.split("#")[0]:
            return
        score = _score_candidate(base_url, absolute) + bonus
        if score <= 0:
            return
        found[absolute] = max(found.get(absolute, 0), score)

    for m in _DOWNLOAD_KEY_RE.finditer(unescaped):
        add(m.group(1), bonus=6)
    for m in _HREF_RE.finditer(html):
        add(m.group(1))
    for m in _BARE_URL_RE.finditer(unescaped):
        add(m.group(0))

    ranked = sorted(found.items(), key=lambda kv: kv[1], reverse=True)
    return [u for u, _ in ranked[:MAX_CANDIDATES]]


def _resolve_name_and_ext(url: str, resp: httpx.Response) -> tuple[str | None, str | None]:
    """Decide whether a response is a statement file; return (filename, extension)."""
    content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    filename = filename_from_response(str(resp.url), resp.headers.get("content-disposition"))
    ext = Path(filename).suffix.lower() if filename else ""
    if ext in UPLOAD_EXTENSIONS:
        return filename, ext
    type_ext = CONTENT_TYPE_EXT.get(content_type)
    if type_ext:
        return (filename or "download") if ext else f"download{type_ext}", type_ext
    return filename, None


def _stream_to_disk(resp: httpx.Response, target: Path, max_bytes: int) -> None:
    length = resp.headers.get("content-length")
    if length and int(length) > max_bytes:
        raise ValueError(
            f"File is {int(length) / (1024*1024):,.0f} MB, above the "
            f"{max_bytes >> 20} MB /fetch limit."
        )
    written = 0
    with open(target, "wb") as fh:
        for chunk in resp.iter_bytes(1 << 20):
            written += len(chunk)
            if written > max_bytes:
                raise ValueError(f"Download exceeded the {max_bytes >> 20} MB /fetch limit.")
            fh.write(chunk)


def download_statement(url: str, dest_dir: Path, max_bytes: int) -> tuple[Path, str]:
    """Download a statement (or zip of statements) from `url`, resolving share
    pages when needed. Returns (local_path, filename). Raises ValueError with a
    user-facing message when nothing downloadable is found."""
    url = _dropbox_direct(url)
    with httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, read=300.0),
        headers=_BROWSER_HEADERS,
    ) as client:

        def attempt(target_url: str, depth: int) -> tuple[Path, str]:
            with client.stream("GET", target_url) as resp:
                resp.raise_for_status()
                filename, ext = _resolve_name_and_ext(target_url, resp)
                if ext:
                    name = filename or f"download{ext}"
                    if not name.lower().endswith(ext):
                        name += ext
                    target = dest_dir / Path(name).name
                    _stream_to_disk(resp, target, max_bytes)
                    return target, Path(name).name

                content_type = (resp.headers.get("content-type") or "").split(";")[0].strip()
                if content_type.lower() not in HTML_TYPES or depth >= MAX_RESOLVE_DEPTH:
                    raise ShareResolveError(
                        f"The link serves '{content_type or 'unknown content'}', "
                        "not a statement file."
                    )
                page = b""
                for chunk in resp.iter_bytes(64 * 1024):
                    page += chunk
                    if len(page) >= MAX_PAGE_BYTES:
                        break
            html = page.decode("utf-8", errors="replace")
            candidates = _extract_candidates(str(resp.url), html)
            log.info("Share page %s: trying %d candidate link(s)", target_url, len(candidates))
            for candidate in candidates:
                try:
                    return attempt(candidate, depth + 1)
                except (httpx.HTTPError, ValueError) as e:
                    log.info("Candidate %s failed: %s", candidate, e)
            raise ShareResolveError(
                "This looks like a share page, and I couldn't find a direct "
                "download link on it."
            )

        # Host-specific fast paths tried before the generic page scrape.
        for candidate in _hightail_candidates(url):
            try:
                return attempt(candidate, 1)
            except (httpx.HTTPError, ValueError) as e:
                log.info("Hightail fast path %s failed: %s", candidate, e)
        return attempt(url, 0)
