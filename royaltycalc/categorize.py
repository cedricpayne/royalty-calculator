"""Categorize royalty transactions into income buckets.

Categories:
    MASTERS             - recorded-music / master-side income (distribution, streaming, sales)
    PUBLISHING          - composition-side income (performance, mechanical, sync via publisher)
    PRODUCER            - producer royalties / points
    NEIGHBOURING_RIGHTS - neighbouring rights (PPL, SoundExchange, GVL, etc.)
    OTHER               - recognized income that fits none of the four main buckets
    UNCATEGORIZED       - could not be determined; flagged for manual review
"""

from __future__ import annotations

import re
from functools import lru_cache

MASTERS = "Masters"
PUBLISHING = "Publishing"
PRODUCER = "Producer Royalties"
NEIGHBOURING = "Neighbouring Rights"
OTHER = "Other"
UNCATEGORIZED = "Uncategorized"

CATEGORIES = [MASTERS, PUBLISHING, PRODUCER, NEIGHBOURING, OTHER, UNCATEGORIZED]

# Keyword rules are checked in order; the first category with a match wins.
# All matching is whole-word (so "ppl" does not match "apple").
_RULES: list[tuple[str, list[str]]] = [
    (
        NEIGHBOURING,
        [
            "neighbouring rights", "neighboring rights", "neighbouring", "neighboring",
            "soundexchange", "sound exchange", "ppl", "gvl", "adami", "spedidam",
            "sena", "gramex", "ifpi", "re:sound", "resound", "lsg", "playright",
            "digital performance", "equitable remuneration", "phonographic performance",
        ],
    ),
    (
        PRODUCER,
        [
            "producer royalty", "producer royalties", "producer points", "producer",
            "prod points", "production royalty", "beat lease", "beat license",
        ],
    ),
    (
        PUBLISHING,
        [
            "publishing", "publisher", "mechanical", "mechanicals", "composition",
            "songwriter", "songwriting", "writer share", "writer's share", "writer",
            "performance royalty", "performance royalties", "performance income",
            "performing rights", "performance", "micro-sync", "microsync",
            "ascap", "bmi", "sesac", "gmr", "prs", "prs for music", "gema", "sacem",
            "socan", "apra", "amcos", "apra amcos", "buma", "stemra", "buma/stemra",
            "sabam", "suisa", "stim", "teosto", "koda", "tono", "imro", "spa",
            "zaiks", "sgae", "siae", "jasrac", "cmrra", "the mlc", "mlc",
            "harry fox", "hfa", "songtrust", "sync fee", "sync license",
            "synchronization", "synchronisation", "sync",
        ],
    ),
    (
        MASTERS,
        [
            "master", "masters", "master recording", "sound recording", "recording",
            "distribution", "streaming", "stream", "streams", "download", "downloads",
            "digital sales", "physical sales", "sales", "sale", "album sales",
            "track sales", "content id", "youtube content id", "art track", "ugc",
            "distrokid", "tunecore", "cd baby", "cdbaby", "believe", "awal",
            "the orchard", "orchard", "empire", "symphonic", "unitedmasters",
            "united masters", "ditto", "stem", "vydia", "repost", "amuse",
            "label engine", "fuga", "ingrooves", "virgin music", "ada",
            "spotify", "apple music", "itunes", "amazon music", "amazon",
            "youtube music", "youtube", "deezer", "tidal", "pandora", "napster",
            "soundcloud", "audiomack", "bandcamp", "beatport", "traxsource",
            "tiktok", "instagram", "facebook", "meta", "snapchat", "peloton",
            "vinyl", "cassette", "airplay",
        ],
    ),
    (
        OTHER,
        [
            "merch", "merchandise", "touring", "live", "show", "gig",
            "advance", "adjustment", "adjustments", "reimbursement", "bonus",
            "grant", "interest", "fee", "fees", "misc", "miscellaneous", "other",
        ],
    ),
]

_COMPILED: list[tuple[str, re.Pattern]] = []
for _cat, _keywords in _RULES:
    # Sort longer phrases first so "producer royalty" is tried before "producer".
    parts = sorted((re.escape(k) for k in _keywords), key=len, reverse=True)
    pattern = re.compile(r"(?<![a-z0-9])(" + "|".join(parts) + r")(?![a-z0-9])")
    _COMPILED.append((_cat, pattern))


def categorize_text(text: str) -> tuple[str, str | None]:
    """Return (category, matched_keyword) for a blob of text. UNCATEGORIZED if no match."""
    lowered = (text or "").lower()
    for cat, pattern in _COMPILED:
        m = pattern.search(lowered)
        if m:
            return cat, m.group(1)
    return UNCATEGORIZED, None


@lru_cache(maxsize=65536)  # the same type/source strings repeat across rows
def categorize_row(
    income_type: str | None,
    source: str | None,
    description: str | None,
    filename: str | None = None,
) -> tuple[str, str]:
    """Categorize a transaction.

    Fields are tried most-specific first: the stated income/royalty type, then the
    payor/source/store, then free-text description, and finally the statement's
    filename as a last resort. Returns (category, reason) where reason records
    which field and keyword decided the category, for traceability.
    """
    for field_name, value in (
        ("income type", income_type),
        ("source", source),
        ("description", description),
        ("filename", filename),
    ):
        if not value:
            continue
        cat, keyword = categorize_text(value)
        if cat != UNCATEGORIZED:
            return cat, f'matched "{keyword}" in {field_name}'
    return UNCATEGORIZED, "no keyword matched"


def resolve_category_name(name: str) -> str | None:
    """Map user input like 'masters' or 'neighbouring' to a canonical category name."""
    n = re.sub(r"[^a-z]", "", (name or "").lower())
    lookup = {
        "masters": MASTERS,
        "master": MASTERS,
        "recording": MASTERS,
        "publishing": PUBLISHING,
        "pub": PUBLISHING,
        "producer": PRODUCER,
        "producerroyalties": PRODUCER,
        "producerroyalty": PRODUCER,
        "neighbouring": NEIGHBOURING,
        "neighboring": NEIGHBOURING,
        "neighbouringrights": NEIGHBOURING,
        "neighboringrights": NEIGHBOURING,
        "other": OTHER,
        "uncategorized": UNCATEGORIZED,
        "uncategorised": UNCATEGORIZED,
    }
    return lookup.get(n)
