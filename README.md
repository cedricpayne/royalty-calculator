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

- **Reads bulk files with different layouts.** Column names are mapped through an
  alias table (`Net Amount`, `Earnings (USD)`, `Royalty`, `Amount Payable`, ... all
  become the amount; `Sale Month`, `Distribution Period`, `Statement Date`, `Q1 2025`,
  `Mar 2025`, ... all become the date). Preamble rows before the header are skipped,
  and total/subtotal rows are excluded.
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
| *(send a file)* | Ingest a statement (`.csv`, `.tsv`, `.txt`, `.xlsx`, `.xls`) |
| `/report` | LTM total, category breakdown, per-year earnings |
| `/uncategorized` | List transactions needing manual review |
| `/categorize <id> <category>` | Assign masters / publishing / producer / neighbouring / other |
| `/trace <id>` | Show source file, row number and raw data for a transaction |
| `/files` | List ingested statements with row/duplicate counts |
| `/deletefile <id>` | Remove a statement and its transactions |
| `/reset` | Delete everything for this chat (requires `/reset confirm`) |

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
