"""Tests for ServerState.resolve_screenshot precedence rules."""

from cadpilot.server_state import ServerState


def test_default_is_no_screenshot():
    state = ServerState()
    assert state.resolve_screenshot(None) is False


def test_per_call_true_enables_screenshot():
    state = ServerState()
    assert state.resolve_screenshot(True) is True


def test_per_call_false_disables_even_with_global_flag():
    state = ServerState(with_screenshots=True)
    assert state.resolve_screenshot(False) is False


def test_global_flag_applies_when_no_per_call_value():
    state = ServerState(with_screenshots=True)
    assert state.resolve_screenshot(None) is True


def test_only_text_feedback_wins_over_everything():
    state = ServerState(only_text_feedback=True, with_screenshots=True)
    assert state.resolve_screenshot(True) is False
    assert state.resolve_screenshot(None) is False
