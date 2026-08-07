# Music Catalog Earnings Bot

A Telegram bot (with a matching CLI) that organizes music royalty income.
Upload royalty statements — CSV, TSV or Excel — from producers, artists,
publishers, distributors or labels, and it combines them into one catalog view:

```
LTM Total: $3,315.66

Masters: $353.67
Publishing: $847.62
Producer Royalties: $2,875.42
Neighbouring Rights: $599.56
Other: $55.00
Uncategorized: $99.99 (1 transactions - send /uncategorized to review)

2026: $2,335.54
2025: $1,896.16
2024: $599.56
```

This first version only organizes the data and computes annual + LTM earnings.
It does not value the catalog or apply a multiple.

## What it does

- **Bulk uploads.** Send one file, several files in a single message (Telegram
  album — the bot replies once with a combined summary), or a `.zip` containing
  any number of statements. Files inside a zip are ingested individually and
  traced as `archive.zip/statement.csv`.
- **Reads bulk files with different layouts.** Column names are mapped through an
  alias table (`Net Amount`, `Earnings (USD)`, `Royalty`, `Amount Payable`, ... all
  become the amount; `Sale Month`, `Distribution Period`, `Statement Date`, `Q1 2025`,
  `Mar 2025`, ... all become the date). Preamble rows before the header are skipped,
  and total/subtotal rows are excluded.
- **Infers unknown layouts from the data itself.** When column names aren't
  recognized - or there is no header row at all (e.g. PRS 052 exports) - the
  parser classifies columns by content: decimal/currency-shaped values become
  the amount (bare-integer quantity and ID columns are rejected), date-shaped
  values become the date, and text columns feed the categorizer. Workbooks are
  scanned sheet by sheet, so data behind a cover sheet is found. Every inferred
  mapping is flagged in the ingest summary so it can be spot-checked with
  `/trace`; anything still unreadable errors with a preview of the file's first
  rows.
- **Categorizes every transaction** into Masters, Publishing, Producer Royalties,
  Neighbouring Rights or Other, using keyword rules over the stated royalty type,
  the payor/store, the description, and finally the filename. PROs (ASCAP, BMI,
  PRS, ...) map to Publishing; SoundExchange/PPL/GVL to Neighbouring Rights;
  DSPs and distributors (Spotify, DistroKid, TuneCore, ...) to Masters.
- **Catches duplicates.**
  - Whole files: an upload with byte-identical content (even under a new filename)
    is rejected.
  - Transactions: rows whose original content is identical to rows already ingested
    from *other* files are stored but flagged as duplicates and excluded from every
    total. Identical rows *within* one statement are kept (statements are trusted).
- **Uncategorized bucket.** Anything it cannot classify — unknown royalty type or a
  missing/unparseable date — lands in Uncategorized for manual review and can be
  assigned with one command.
- **Full traceability.** Every transaction stores its source file (name + SHA-256),
  row number, the complete raw row, and the reason it was categorized the way it
  was. `/trace <id>` shows all of it.

## Definitions

- **LTM** = the trailing 12 months ending today (transactions dated after
  `today - 12 months`). Statement *periods* resolve to their period end, so a
  "March 2025" line counts as 2025-03-31.
- **Category totals** are all-time; yearly lines are calendar years.
- Amounts are summed as reported. There is **no currency conversion**; if mixed
  currencies are detected the report says so.

## Run the Telegram bot

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. ```bash
   pip install -r requirements.txt
   export TELEGRAM_BOT_TOKEN=123456:ABC-your-token
   python -m royaltycalc.bot
   ```
3. Open a chat with your bot, send it statement files, then `/report`.

Each Telegram chat has its own isolated catalog. Data is stored in SQLite
(`data/royalties.db` by default; override with `ROYALTY_DB`).

### Bot commands

| Command | Purpose |
| --- | --- |
| *(send files)* | Ingest statements (`.csv`, `.tsv`, `.txt`, `.xlsx`, `.xls`, or a `.zip` of them); multiple files per message supported |
| `/report` | LTM total, category breakdown, per-year earnings |
| `/upload` | Private browser page for big uploads (no Telegram size limit) |
| `/fetch <url>` | Ingest from a link — direct files or share pages (Hightail/Dropbox/Drive) |
| `/uncategorized` | List transactions needing manual review |
| `/categorize <id> <category>` | Assign masters / publishing / producer / neighbouring / other |
| `/trace <id>` | Show source file, row number and raw data for a transaction |
| `/files` | List ingested statements with row/duplicate counts |
| `/deletefile <id>` | Remove a statement and its transactions |
| `/reset` | Delete everything for this chat (requires `/reset confirm`) |

## Plug in Claude (optional AI layer)

Add one variable and the bot gets a brain:

```
ANTHROPIC_API_KEY=sk-ant-...        (from console.anthropic.com)
```

That switches on three capabilities, all built on the Claude API
(`claude-opus-5` by default; override with `ROYALTY_AI_MODEL`):

- **Automatic layout mapping.** When neither column-name matching nor content
  inference can decode a statement, Claude reads the file's first rows and
  returns the column mapping. Mappings are cached by layout fingerprint, so a
  batch of 30 same-format statements costs one API call — and the mapping is
  flagged in the ingest summary for `/trace` spot-checking.
- **`/autocategorize`.** Claude classifies whatever sits in the Uncategorized
  bucket into Masters / Publishing / Producer Royalties / Neighbouring Rights /
  Other, marking each transaction as "categorized by Claude" for auditability.
  Rows without enough information stay uncategorized.
- **`/ask` and plain-text questions.** Ask anything about the catalog in
  natural language ("which track earned the most in 2025?", "how did Q1
  compare to last year?"). Answers are grounded in SQL aggregates computed
  from your data — Claude never sees raw statements, only the summary tables.

Without the key, everything falls back to the deterministic pipeline —
no AI calls are ever made.

## Deploy on Railway

The repo ships with `railway.json` (start command + restart policy), a `Procfile`
(worker process, no public port needed — the bot uses polling) and
`.python-version`, so Railway's builder picks everything up automatically.

1. **Create the service** — on [railway.com](https://railway.com): *New Project →
   Deploy from GitHub repo* and select this repository. Railway detects Python
   from `requirements.txt` and uses `python -m royaltycalc.bot` as the start
   command.
2. **Set the token** — in the service's *Variables* tab add:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-your-token
   ```
3. **Add a volume (important).** Railway's container filesystem is ephemeral —
   without a volume, every deploy/restart wipes the SQLite database and all
   ingested statements. In the service: *right-click → Attach Volume*, mount it at
   `/data`, then add a second variable:
   ```
   ROYALTY_DB=/data/royalties.db
   ```
4. **Deploy.** Watch the deploy logs for `Bot starting (db=/data/royalties.db)`,
   then message your bot on Telegram.

Notes:
- Run **exactly one instance** (Railway's default). Two replicas polling the same
  bot token will fight over updates, and SQLite on a volume is single-writer.
- Pushes to your default branch auto-deploy; the volume keeps your data across
  deploys.

## Large catalogs (hundreds of MB of statements)

The pipeline is built for bulk: files stream from disk row-by-row (constant
~30 MB of memory regardless of file size), inserts are batched, and duplicate
detection and reporting run inside SQLite. Measured throughput is roughly
20,000 rows/second — a 750 MB catalog (~13M rows) ingests in about 10-15
minutes, and the bot stays responsive while it works.

**Getting big files into Telegram.** Telegram limits what a bot can *download*
to 20 MB per file. Ways around it, easiest first:

1. **`/upload` (recommended)** — the bot serves its own drag-and-drop upload
   page. Send `/upload` in the chat to get a private link (expires after 2
   hours, bound to your chat), open it in any browser, and drop in statements
   or zips of any size — up to 2 GB per file, 100 files per upload. Results
   arrive back in the Telegram chat as each file finishes.

   *Railway setup (one-time):* the page needs a public domain. In the service:
   **Settings → Networking → Generate Domain**. Railway then injects
   `RAILWAY_PUBLIC_DOMAIN` and `PORT` automatically and it works on the next
   deploy. On other hosts, set `PUBLIC_BASE_URL` (e.g. `https://mybot.example.com`).
2. **Zip and split** — CSVs compress ~10x, so 750 MB of statements is usually
   4-8 zips under 20 MB. Send them all in one message; the bot processes each
   archive's contents individually.
3. **`/fetch <url>`** — put the file (or one big zip) anywhere reachable by
   link and the bot downloads it itself. Direct links (S3 presigned URLs, raw
   file URLs) always work; share pages (Hightail Spaces, Dropbox, Drive) are
   resolved automatically — the bot scans the page for the real download link
   and follows it. Dropbox links get `dl=1` added for you. Files are accepted
   by content type as well as extension, so extension-less download endpoints
   work. Default cap 1 GB, configurable with `ROYALTY_MAX_FETCH_MB`. Share
   pages that require a login or build their download links entirely in
   JavaScript can't be resolved — download locally and send the files, or use
   a direct link.
4. **Self-hosted Bot API server** (advanced) — run
   [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) alongside the
   bot and set `TELEGRAM_API_BASE_URL` / `TELEGRAM_API_BASE_FILE_URL`; the
   download limit rises to 2 GB per file.

**Volume sizing.** The database keeps the full raw row for every transaction
(that's what makes every number traceable), which costs roughly 8-10x the
input CSV size. For a 750 MB catalog, size the Railway volume at ~10 GB.

## CLI (same engine, no Telegram needed)

```bash
python -m royaltycalc.cli ingest samples/*.csv   # ingest + print report
python -m royaltycalc.cli report
python -m royaltycalc.cli uncategorized
python -m royaltycalc.cli trace 7
python -m royaltycalc.cli categorize 7 publishing
```

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest tests/
```

`samples/` contains statements in five different real-world-style layouts
(distributor, PRO, SoundExchange, producer, label) used by the tests and handy
for a demo.

## Known limitations (v1)

- No currency conversion — mixed-currency catalogs are flagged, not converted.
- Duplicate detection is content-based: the *same* income reported with different
  formatting/wording across two statements will not be caught automatically
  (review `/files` and use `/deletefile` for overlapping statements).
- PDF statements are not parsed yet — export CSV where possible.
