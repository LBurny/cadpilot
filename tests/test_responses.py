"""Tests for cadpilot.responses helpers."""

import json
from unittest.mock import MagicMock

import pytest
from mcp.types import ImageContent, TextContent

from cadpilot.responses import (
    add_screenshot_if_available,
    get_screenshot_mode,
    json_response,
    set_screenshot_mode,
    text_response,
)


def test_text_response_returns_single_text_content():
    resp = text_response("hello")
    assert len(resp) == 1
    assert isinstance(resp[0], TextContent)
    assert resp[0].text == "hello"


def test_json_response_is_compact():
    resp = json_response({"a": 1, "b": [1, 2]})
    text = resp[0].text
    # compact separators: no indentation, no spaces after : or ,
    assert text == '{"a":1,"b":[1,2]}'
    assert json.loads(text) == {"a": 1, "b": [1, 2]}


def test_json_response_keeps_unicode():
    resp = json_response({"name": "部件"})
    assert "部件" in resp[0].text


def test_json_response_falls_back_to_str_for_unknown_types():
    class Weird:
        def __str__(self):
            return "weird!"

    resp = json_response({"x": Weird()})
    assert json.loads(resp[0].text) == {"x": "weird!"}


def test_add_screenshot_appends_image_content():
    resp = add_screenshot_if_available(
        text_response("ok"), "aGVsbG8=", False, screenshot_mode="image"
    )
    assert len(resp) == 2
    assert isinstance(resp[1], ImageContent)
    assert resp[1].data == "aGVsbG8="
    assert resp[1].mimeType == "image/png"


def test_add_screenshot_skipped_in_text_only_mode():
    resp = add_screenshot_if_available(text_response("ok"), "aGVsbG8=", True)
    assert len(resp) == 1


def test_add_screenshot_skipped_when_none():
    resp = add_screenshot_if_available(text_response("ok"), None, False)
    assert len(resp) == 1


PNG_1X1 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


class StubFreeCAD:
    def get_active_screenshot(self, *args, **kwargs):
        return PNG_1X1


@pytest.fixture
def file_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("CADPILOT_HOME", str(tmp_path))
    previous = get_screenshot_mode()
    set_screenshot_mode("file")
    yield tmp_path
    set_screenshot_mode(previous)


def test_set_screenshot_mode_rejects_unknown():
    with pytest.raises(ValueError):
        set_screenshot_mode("bogus")


def test_file_mode_appends_path_text_not_image(file_mode):
    resp = add_screenshot_if_available(text_response("ok"), PNG_1X1, False)
    assert len(resp) == 2
    assert isinstance(resp[1], TextContent)
    saved = list((file_mode / "screenshots").glob("view-*.png"))
    assert len(saved) == 1
    assert str(saved[0]) in resp[1].text
    assert "1x1" in resp[1].text


def test_file_mode_falls_back_to_inline_on_write_error(monkeypatch):
    previous = get_screenshot_mode()
    set_screenshot_mode("file")
    monkeypatch.setattr(
        "cadpilot.responses.save_screenshot", MagicMock(side_effect=OSError("disk full"))
    )
    try:
        resp = add_screenshot_if_available(text_response("ok"), PNG_1X1, False)
    finally:
        set_screenshot_mode(previous)
    assert isinstance(resp[1], ImageContent)


def test_get_view_file_mode_returns_path_text(file_mode):
    from cadpilot.operations.core import get_view_operation

    resp = get_view_operation(StubFreeCAD(), "Isometric")
    assert isinstance(resp[0], TextContent)
    assert "Screenshot saved to" in resp[0].text


def test_get_view_mode_param_overrides_server_default(tmp_path, monkeypatch):
    from cadpilot.operations.core import get_view_operation

    monkeypatch.setenv("CADPILOT_HOME", str(tmp_path))
    previous = get_screenshot_mode()
    set_screenshot_mode("image")
    try:
        # global default is "image"; the per-call mode wins
        resp = get_view_operation(StubFreeCAD(), "Isometric", screenshot_mode="file")
    finally:
        set_screenshot_mode(previous)
    assert isinstance(resp[0], TextContent)
    assert "Screenshot saved to" in resp[0].text


def test_get_view_mode_image_overrides_file_default(file_mode):
    from cadpilot.operations.core import get_view_operation

    resp = get_view_operation(StubFreeCAD(), "Front", screenshot_mode="image")
    assert isinstance(resp[0], ImageContent)


def test_add_screenshot_mode_param_overrides_default(tmp_path, monkeypatch):
    monkeypatch.setenv("CADPILOT_HOME", str(tmp_path))
    previous = get_screenshot_mode()
    set_screenshot_mode("image")
    try:
        resp = add_screenshot_if_available(
            text_response("ok"), PNG_1X1, False, screenshot_mode="file"
        )
    finally:
        set_screenshot_mode(previous)
    assert isinstance(resp[1], TextContent)
    assert "Screenshot saved to" in resp[1].text


def test_file_is_the_default_screenshot_mode(tmp_path, monkeypatch):
    """No CLI flag and no per-call mode: screenshots go to disk, not inline."""
    monkeypatch.setenv("CADPILOT_HOME", str(tmp_path))
    assert get_screenshot_mode() == "file"
    resp = add_screenshot_if_available(text_response("ok"), PNG_1X1, False)
    assert isinstance(resp[1], TextContent)
    assert "Screenshot saved to" in resp[1].text
    assert len(list((tmp_path / "screenshots").glob("view-*.png"))) == 1
