"""Tests for the AI layer's pure logic and the ingest fallback wiring.

No Anthropic API calls are made: network-dependent functions are stubbed."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from royaltycalc import ai
from royaltycalc.ingest import ingest_file
from royaltycalc.parsing import StatementReader, layout_signature, preview_rows
from royaltycalc.report import catalog_context
from royaltycalc.store import Store

CHAT = "aichat"

# A layout neither aliases nor inference can decode: amounts are bare integers
# (pence) so the money-shape heuristic rejects them.
WEIRD = (
    "REF001,DAVIDGE,UNFINISHED SYMPATHY,20220715,12345\n"
    "REF002,DAVIDGE,ANGEL,20220715,6789\n"
    "REF003,DAVIDGE,TEARDROP,20220715,4500\n"
)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_ai_disabled_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert ai.ai_enabled() is False


def test_validate_mapping():
    good = {"header_row": None, "columns": {"amount": 4, "date": 3, "track": 2,
                                            "income_type": None, "source": None,
                                            "artist": 1, "description": None,
                                            "currency": None}}
    result = ai.validate_mapping(good, ncols=5)
    assert result == {"header_row": None,
                      "columns": {"amount": 4, "date": 3, "track": 2, "artist": 1}}
    # Out-of-range indices are dropped; missing amount kills the mapping.
    assert ai.validate_mapping(
        {"header_row": 0, "columns": {"amount": 99}}, ncols=5) is None
    assert ai.validate_mapping({"columns": {"date": 1}}, ncols=5) is None
    assert ai.validate_mapping("nonsense", ncols=5) is None


def test_parse_assignments():
    text = json.dumps({"assignments": [
        {"id": 1, "category": "Publishing"},
        {"id": 2, "category": "Uncategorized"},   # kept uncategorized -> dropped
        {"id": 99, "category": "Masters"},        # unknown id -> dropped
        {"id": 3, "category": "Bogus"},           # invalid category -> dropped
    ]})
    assert ai.parse_assignments(text, {1, 2, 3}) == {1: "Publishing"}
    assert ai.parse_assignments("not json", {1}) == {}


def test_layout_signature_stable_across_same_format_files(tmp_path):
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    a.write_text(WEIRD)
    b.write_text(
        "REF900,SOMEONE ELSE,OTHER SONG,20250101,777\n"
        "REF901,SOMEONE ELSE,B SIDE,20250201,888\n"
    )
    sig_a = layout_signature(preview_rows(a), ".csv")
    sig_b = layout_signature(preview_rows(b), ".csv")
    assert sig_a == sig_b
    c = tmp_path / "c.csv"
    c.write_text("Date,Store,Earnings\n2025-01-01,Spotify,1.23\n")
    assert layout_signature(preview_rows(c), ".csv") != sig_a


def test_reader_with_overrides_headerless(tmp_path):
    p = tmp_path / "weird.csv"
    p.write_text(WEIRD)
    reader = StatementReader(p, column_overrides={
        "header_row": None,
        "columns": {"amount": 4, "date": 3, "track": 2},
    })
    rows = list(reader)
    assert len(rows) == 3
    assert rows[0].amount == Decimal("12345")
    assert rows[0].txn_date.isoformat() == "2022-07-15"
    assert rows[0].track == "UNFINISHED SYMPATHY"
    assert any("identified by Claude" in w for w in reader.warnings)


def test_ingest_ai_fallback_and_layout_cache(store, tmp_path, monkeypatch):
    calls = []

    def fake_map_columns(preview, filename):
        calls.append(filename)
        return {"header_row": None, "columns": {"amount": 4, "date": 3, "track": 2}}

    monkeypatch.setattr(ai, "ai_enabled", lambda: True)
    monkeypatch.setattr(ai, "map_columns", fake_map_columns)

    a = tmp_path / "prs_a.csv"
    a.write_text(WEIRD)
    summary = ingest_file(store, CHAT, a)
    assert summary.rows_ingested == 3
    assert summary.total_added == Decimal("23634")
    assert any("identified by Claude" in w for w in summary.warnings)
    assert calls == ["prs_a.csv"]

    # Second file, same layout: served from the cache, no second AI call.
    b = tmp_path / "prs_b.csv"
    b.write_text("REF900,OTHER,PIECE,20250101,100\n")
    ingest_file(store, CHAT, b)
    assert calls == ["prs_a.csv"]


def test_ingest_without_ai_still_raises(store, tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "ai_enabled", lambda: False)
    p = tmp_path / "weird.csv"
    p.write_text(WEIRD)
    with pytest.raises(ValueError):
        ingest_file(store, CHAT, p)


def test_ai_mapping_failure_reraises_original_error(store, tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "ai_enabled", lambda: True)
    monkeypatch.setattr(ai, "map_columns", lambda *a: None)
    p = tmp_path / "weird.csv"
    p.write_text(WEIRD)
    with pytest.raises(ValueError) as exc:
        ingest_file(store, CHAT, p)
    assert "REF001" in str(exc.value)  # the original preview-bearing error


def test_catalog_context_contains_aggregates(store):
    samples = Path(__file__).resolve().parent.parent / "samples"
    ingest_file(store, CHAT, samples / "distrokid_2025.csv")
    ingest_file(store, CHAT, samples / "soundexchange_2024.csv")
    ctx = catalog_context(store, CHAT)
    assert "LTM Total" in ctx
    assert "Masters" in ctx
    assert "Top sources" in ctx
    assert "Spotify" in ctx
    assert "distrokid_2025.csv" in ctx


def test_layout_cache_roundtrip(store):
    mapping = {"header_row": 1, "columns": {"amount": 3}}
    store.save_layout("csv:5:aaadm", mapping)
    assert store.get_layout("csv:5:aaadm") == mapping
    assert store.get_layout("missing") is None
