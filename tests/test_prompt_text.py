"""Tests for the asset creation strategy prompt (dual-mode workflow)."""

from cadpilot.prompt_text import ASSET_CREATION_STRATEGY


def test_prompt_covers_dual_mode_workflow():
    for keyword in (
        "assembly_session",
        "hull",
        "sketch",
        "pad",
        "mate",
        "session_start",
        "recall_patterns",
    ):
        assert keyword in ASSET_CREATION_STRATEGY, keyword


def test_prompt_steers_away_from_blind_placement():
    assert "Placement" in ASSET_CREATION_STRATEGY
    assert "anchor" in ASSET_CREATION_STRATEGY.lower()
