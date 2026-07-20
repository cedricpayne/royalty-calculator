import zipfile
from pathlib import Path

import pytest

from royaltycalc.ingest import ingest_upload
from royaltycalc.store import Store

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
CHAT = "zipchat"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_zip_ingests_all_supported_members(store, tmp_path):
    z = make_zip(
        tmp_path / "statements.zip",
        {
            "distrokid_2025.csv": (SAMPLES / "distrokid_2025.csv").read_bytes(),
            "nested/soundexchange_2024.csv": (SAMPLES / "soundexchange_2024.csv").read_bytes(),
            "__MACOSX/._junk.csv": b"junk",
            "readme.pdf": b"not a statement",
            ".hidden.csv": b"a,b\n1,2\n",
        },
    )
    results = ingest_upload(store, CHAT, z)
    assert len(results) == 2  # only the two real statements
    assert any("distrokid_2025.csv" in r for r in results)
    assert any("soundexchange_2024.csv" in r for r in results)
    assert len(store.transactions(CHAT)) == 8  # 6 + 2 rows
    # Member filenames keep the zip name for traceability.
    filenames = {f["filename"] for f in store.files(CHAT)}
    assert filenames == {
        "statements.zip/distrokid_2025.csv",
        "statements.zip/soundexchange_2024.csv",
    }


def test_zip_duplicate_members_reported_not_raised(store, tmp_path):
    data = (SAMPLES / "distrokid_2025.csv").read_bytes()
    z = make_zip(tmp_path / "dupes.zip", {"a.csv": data, "b.csv": data})
    results = ingest_upload(store, CHAT, z)
    assert len(results) == 2
    assert sum("Duplicate file skipped" in r for r in results) == 1
    assert len(store.transactions(CHAT)) == 6


def test_zip_with_no_statements(store, tmp_path):
    z = make_zip(tmp_path / "empty.zip", {"notes.pdf": b"x"})
    results = ingest_upload(store, CHAT, z)
    assert len(results) == 1
    assert "no statement files found" in results[0]


def test_bad_zip(store, tmp_path):
    fake = tmp_path / "broken.zip"
    fake.write_bytes(b"this is not a zip")
    results = ingest_upload(store, CHAT, fake)
    assert "not a valid zip" in results[0]


def test_single_file_upload_still_works(store):
    results = ingest_upload(store, CHAT, SAMPLES / "producer_statement.csv")
    assert len(results) == 1
    assert "3 transactions added" in results[0]


def test_unreadable_single_file_becomes_text_not_exception(store, tmp_path):
    p = tmp_path / "junk.csv"
    p.write_text("a,b,c\n1,2,3\n")
    results = ingest_upload(store, CHAT, p)
    assert len(results) == 1
    assert "Could not read" in results[0]
