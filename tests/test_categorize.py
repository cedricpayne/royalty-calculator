from royaltycalc.categorize import (
    MASTERS,
    NEIGHBOURING,
    OTHER,
    PRODUCER,
    PUBLISHING,
    UNCATEGORIZED,
    categorize_row,
    categorize_text,
    resolve_category_name,
)


def test_income_type_beats_source():
    cat, reason = categorize_row("Producer Royalty", "Spotify", None)
    assert cat == PRODUCER
    assert "income type" in reason


def test_neighbouring_beats_masters_keywords():
    cat, _ = categorize_text("Neighbouring rights - master recording")
    assert cat == NEIGHBOURING


def test_ppl_word_boundary_does_not_match_apple():
    cat, _ = categorize_text("Apple Music streams")
    assert cat == MASTERS  # via "apple music"/"streams", never PPL
    cat, _ = categorize_text("PPL distribution UK")
    assert cat == NEIGHBOURING


def test_pro_names_are_publishing():
    for text in ("ASCAP", "BMI performance royalties", "PRS for Music", "The MLC"):
        assert categorize_text(text)[0] == PUBLISHING


def test_distributors_and_dsps_are_masters():
    for text in ("DistroKid", "TuneCore payout", "Spotify", "physical sales"):
        assert categorize_text(text)[0] == MASTERS


def test_other_bucket():
    assert categorize_text("Merchandise")[0] == OTHER
    assert categorize_text("advance recoupment adjustment")[0] == OTHER


def test_unknown_is_uncategorized():
    cat, reason = categorize_row("Blanket License ZZ", "Mystery Collective", None)
    assert cat == UNCATEGORIZED


def test_filename_fallback():
    cat, reason = categorize_row(None, None, None, filename="soundexchange_2024.csv")
    assert cat == NEIGHBOURING
    assert "filename" in reason


def test_resolve_category_name():
    assert resolve_category_name("masters") == MASTERS
    assert resolve_category_name("Neighbouring") == NEIGHBOURING
    assert resolve_category_name("producer royalties") == PRODUCER
    assert resolve_category_name("pub") == PUBLISHING
    assert resolve_category_name("bogus") is None
