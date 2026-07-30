"""Tests for cadpilot.responses helpers."""

import json

from mcp.types import ImageContent, TextContent

from cadpilot.responses import add_screenshot_if_available, json_response, text_response


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
    resp = add_screenshot_if_available(text_response("ok"), "aGVsbG8=", False)
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
