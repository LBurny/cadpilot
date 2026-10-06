"""Tests for pattern_store: persistence and keyword retrieval."""

from cadpilot.pattern_store import add_pattern, get_pattern, list_patterns, search_patterns


def test_add_and_get(isolated_home):
    entry = add_pattern("flanged pipe", "Loft two circles then shell", code="Part.makeLoft(...)")
    found = get_pattern(entry["pattern_id"])
    assert found is not None
    assert found["name"] == "flanged pipe"
    assert found["source"] == "manual"


def test_search_ranks_by_token_overlap(isolated_home):
    add_pattern("box with holes", "Cut cylinders out of a box", tags=["boolean", "cut"])
    add_pattern("pipe flange", "Revolve a profile to make a flange")
    add_pattern("gear", "Involute gear via script")

    hits = search_patterns("boolean cut box")
    assert hits[0]["name"] == "box with holes"
    # unrelated pattern should not appear
    assert all(h["name"] != "gear" for h in hits)


def test_search_no_match(isolated_home):
    add_pattern("gear", "Involute gear via script")
    assert search_patterns("zzz qqq") == []


def test_search_empty_query(isolated_home):
    add_pattern("gear", "x")
    assert search_patterns("") == []


def test_search_unicode_tokens(isolated_home):
    add_pattern("法兰盘", "旋转成型法兰")
    hits = search_patterns("法兰")
    assert hits and hits[0]["name"] == "法兰盘"


def test_list_patterns_returns_recent(isolated_home):
    for i in range(5):
        add_pattern(f"p{i}", f"desc {i}")
    patterns = list_patterns(limit=3)
    assert len(patterns) == 3
    assert patterns[-1]["name"] == "p4"


def test_patterns_persist_across_loads(isolated_home):
    add_pattern("durable", "survives reload")
    # a second search goes through a fresh _load() from disk
    assert search_patterns("durable")[0]["name"] == "durable"


def test_a_corrupt_store_is_refused_not_overwritten(isolated_home):
    """_load() used to return [] for an unreadable file as well as for a missing
    one, so one corrupt patterns.json made the next add_pattern write a store
    holding only the new entry: every previously saved pattern gone, no error
    reported. An unreadable store now reads as None and add_pattern refuses."""
    import pytest

    import cadpilot.pattern_store as store

    add_pattern("keeper", "a pattern that must survive")
    path = store._store_path()
    before = path.read_text(encoding="utf-8")
    path.write_text('{"truncated": ', encoding="utf-8")

    with pytest.raises(RuntimeError) as err:
        add_pattern("newcomer", "must not clobber the store")
    assert "cannot be read" in str(err.value)

    # The tool layer keeps the failure inside the result.
    from cadpilot.operations import save_pattern_operation

    text = " ".join(c.text for c in save_pattern_operation("x", "y") if hasattr(c, "text"))
    assert "Could not store the pattern" in text

    # Read-only paths still answer (an unreadable store reads as empty).
    assert list_patterns() == []
    assert search_patterns("keeper") == []
    # ...and the corrupt file was left for the user to repair, not replaced.
    assert path.read_text(encoding="utf-8") == '{"truncated": '
    assert before  # the keeper really was written first


def test_a_non_list_store_is_corrupt_too(isolated_home):
    import pytest

    import cadpilot.pattern_store as store

    store._store_path().write_text('{"patterns": []}', encoding="utf-8")
    with pytest.raises(RuntimeError):
        add_pattern("x", "y")


def test_store_of_non_dict_entries_reads_as_unwritable(isolated_home, monkeypatch):
    """Valid JSON holding non-dict entries used to crash search_patterns'
    entry.get as AttributeError; treat it like any other corrupt store."""
    from cadpilot import pattern_store

    monkeypatch.setenv("CADPILOT_HOME", str(isolated_home))
    path = pattern_store._store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('["a string", 42]', encoding="utf-8")
    assert pattern_store._load() is None
