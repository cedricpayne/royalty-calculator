"""Optional Claude-powered intelligence layer.

Enabled by setting ANTHROPIC_API_KEY (plug in an Anthropic API key and the bot
gets smarter; without it, everything falls back to the deterministic pipeline).

Three capabilities:
  * map_columns    - when heuristics can't decode a statement layout, Claude
                     reads a preview of the file and returns the column mapping;
  * categorize_rows - classify Uncategorized transactions into income buckets;
  * answer_question - natural-language questions about the catalog (/ask).

All Claude calls use structured outputs where a machine-readable answer is
needed, and every failure degrades gracefully to the non-AI behavior.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("ROYALTY_AI_MODEL", "claude-opus-5")

CATEGORIES = [
    "Masters", "Publishing", "Producer Royalties", "Neighbouring Rights",
    "Other", "Uncategorized",
]

_MAPPING_FIELDS = [
    "amount", "date", "income_type", "source", "track", "artist",
    "description", "currency",
]

MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "header_row": {
            "type": ["integer", "null"],
            "description": "0-based index of the header row in the numbered "
                           "preview, or null if the file has no header row",
        },
        "columns": {
            "type": "object",
            "properties": {f: {"type": ["integer", "null"]} for f in _MAPPING_FIELDS},
            "required": _MAPPING_FIELDS,
            "additionalProperties": False,
        },
    },
    "required": ["header_row", "columns"],
    "additionalProperties": False,
}

CATEGORIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "category": {"type": "string", "enum": CATEGORIES},
                },
                "required": ["id", "category"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["assignments"],
    "additionalProperties": False,
}


class AIUnavailable(RuntimeError):
    """The AI layer could not serve this request."""


def ai_enabled() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def _request(prompt: str, *, schema: dict | None = None, system: str | None = None,
             effort: str = "low", max_tokens: int = 4096) -> str:
    """One Claude call; returns the response text. Raises AIUnavailable on refusal."""
    import anthropic

    client = anthropic.Anthropic()
    kwargs: dict = {
        "model": DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"effort": effort},
    }
    if system:
        kwargs["system"] = system
    if schema:
        kwargs["output_config"]["format"] = {"type": "json_schema", "schema": schema}

    try:
        # Server-side refusal fallback (rare, but keeps the feature working if
        # a request trips the model's safety classifiers).
        response = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            **kwargs,
        )
    except (TypeError, anthropic.BadRequestError):
        # Older SDK/API surface without the fallbacks parameter.
        response = client.messages.create(**kwargs)

    if response.stop_reason == "refusal":
        raise AIUnavailable("The model declined this request.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        raise AIUnavailable("The model returned no text.")
    return text


# ---------------------------------------------------------------- column mapping

def validate_mapping(obj: dict, ncols: int) -> dict | None:
    """Sanitize a raw mapping response into StatementReader overrides."""
    if not isinstance(obj, dict) or not isinstance(obj.get("columns"), dict):
        return None
    columns: dict[str, int] = {}
    for field, idx in obj["columns"].items():
        if field in _MAPPING_FIELDS and isinstance(idx, int) and 0 <= idx < ncols:
            columns[field] = idx
    if "amount" not in columns:
        return None
    header_row = obj.get("header_row")
    if not isinstance(header_row, int) or header_row < 0:
        header_row = None
    return {"header_row": header_row, "columns": columns}


def map_columns(preview_rows: list[list[str]], filename: str) -> dict | None:
    """Ask Claude to identify the column layout of an unrecognized statement.

    Returns StatementReader-compatible overrides, or None if no usable
    mapping was produced.
    """
    ncols = max((len(r) for r in preview_rows), default=0)
    numbered = "\n".join(
        f"{i}: " + " | ".join(c if c else "(empty)" for c in row)
        for i, row in enumerate(preview_rows)
        if any(row)
    )
    prompt = (
        f'Below is the beginning of a music royalty statement file named '
        f'"{filename}". Each line is prefixed with its 0-based row index; '
        f'cells are separated by " | ".\n\n{numbered}\n\n'
        "Identify the column layout. Column indices are 0-based positions "
        "within a row. \"amount\" is the net payable royalty amount - prefer "
        "net over gross, and never pick a quantity, unit rate, ID, or year "
        "column. \"date\" is the transaction date or royalty period. "
        "\"income_type\" is the royalty/right type if present, \"source\" the "
        "payor/platform/society. Set header_row to the row index of the "
        "column-name row, or null if the file starts directly with data."
    )
    text = _request(prompt, schema=MAPPING_SCHEMA, effort="low", max_tokens=2048)
    try:
        return validate_mapping(json.loads(text), ncols)
    except json.JSONDecodeError:
        log.warning("AI mapping response was not valid JSON")
        return None


# ---------------------------------------------------------------- categorization

def parse_assignments(text: str, valid_ids: set[int]) -> dict[int, str]:
    """Parse a categorize response into {txn_id: category}."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return {}
    out: dict[int, str] = {}
    for item in obj.get("assignments", []):
        if (
            isinstance(item, dict)
            and item.get("id") in valid_ids
            and item.get("category") in CATEGORIES
            and item["category"] != "Uncategorized"
        ):
            out[item["id"]] = item["category"]
    return out


def categorize_rows(rows: list[dict]) -> dict[int, str]:
    """Classify uncategorized transactions.

    `rows` is a list of {"id": int, "text": str} where text carries whatever
    is known about the transaction. Returns {id: category} for rows Claude
    could confidently classify; leaves the rest out.
    """
    listing = "\n".join(f'{r["id"]}: {r["text"]}' for r in rows)
    prompt = (
        "These are music royalty transactions that automatic rules could not "
        "classify. Assign each to one of these income categories:\n"
        "- Masters: recorded-music/master-side income (distribution, streaming, "
        "sales, labels, DSPs)\n"
        "- Publishing: composition-side income (performance, mechanical, sync, "
        "PROs like ASCAP/BMI/PRS, publishers)\n"
        "- Producer Royalties: producer points/royalties\n"
        "- Neighbouring Rights: performer/master remuneration via SoundExchange, "
        "PPL, GVL and similar societies\n"
        "- Other: real income that fits none of the above (merch, live, advances)\n"
        "- Uncategorized: keep only if there is genuinely not enough information\n\n"
        f"Transactions (id: details):\n{listing}\n\n"
        "Assign a category to every id."
    )
    text = _request(prompt, schema=CATEGORIZE_SCHEMA, effort="low", max_tokens=8192)
    return parse_assignments(text, {r["id"] for r in rows})


# ---------------------------------------------------------------- Q&A

def answer_question(question: str, catalog_context: str) -> str:
    """Answer a natural-language question about the user's royalty catalog."""
    system = (
        "You are the analyst inside a music catalog earnings tool. Answer the "
        "user's question using ONLY the catalog data provided. Amounts are in "
        "the catalog's reporting currency. Be concise and concrete - lead with "
        "the number or answer, then one or two sentences of context. If the "
        "data provided cannot answer the question, say so and suggest what "
        "would help (e.g. /report, /uncategorized, /trace). Plain text only, "
        "no markdown headers."
    )
    prompt = f"Catalog data:\n\n{catalog_context}\n\nQuestion: {question}"
    return _request(prompt, system=system, effort="high", max_tokens=1500).strip()
