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
