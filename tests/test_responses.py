"""Tests for cadpilot.responses helpers."""

import json
from unittest.mock import MagicMock

import pytest
from mcp.types import TextContent

from cadpilot.responses import (
    json_response,
    screenshot_content,
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


PNG_1X1 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


class StubFreeCAD:
    def get_active_screenshot(self, *args, **kwargs):
        return PNG_1X1


@pytest.fixture
def shot_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CADPILOT_HOME", str(tmp_path))
    return tmp_path


def test_screenshot_content_saves_and_returns_path(shot_dir):
    (block,) = [screenshot_content(PNG_1X1)]
    assert isinstance(block, TextContent)
    saved = list((shot_dir / "screenshots").glob("view-*.png"))
    assert len(saved) == 1
    assert str(saved[0]) in block.text
    assert "1x1" in block.text


def test_screenshot_content_reports_save_failure_as_text(shot_dir, monkeypatch):
    monkeypatch.setattr(
        "cadpilot.responses.save_screenshot", MagicMock(side_effect=OSError("disk full"))
    )
    block = screenshot_content(PNG_1X1)
    assert isinstance(block, TextContent)
    assert "could not be saved" in block.text


def test_get_view_returns_path_text(shot_dir):
    from cadpilot.operations.core import get_view_operation

    resp = get_view_operation(StubFreeCAD(), "Isometric")
    assert isinstance(resp[0], TextContent)
    assert "Screenshot saved to" in resp[0].text
    assert len(list((shot_dir / "screenshots").glob("view-*.png"))) == 1
