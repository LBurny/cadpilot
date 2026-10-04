"""Tests for cadpilot.screenshot_store."""

import base64
from pathlib import Path

import pytest

from cadpilot.screenshot_store import _png_size, save_screenshot

PNG_1X1 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def test_save_screenshot_writes_png_and_reports_size(tmp_path, monkeypatch):
    monkeypatch.setenv("CADPILOT_HOME", str(tmp_path))
    info = save_screenshot(PNG_1X1)
    assert info["width"] == 1 and info["height"] == 1
    assert info["bytes"] == len(base64.b64decode(PNG_1X1))
    p = Path(info["path"])
    assert p.parent == tmp_path / "screenshots"
    assert p.read_bytes() == base64.b64decode(PNG_1X1)


def test_save_screenshot_unique_names(tmp_path, monkeypatch):
    monkeypatch.setenv("CADPILOT_HOME", str(tmp_path))
    a = save_screenshot(PNG_1X1)
    b = save_screenshot(PNG_1X1)
    assert a["path"] != b["path"]
    assert Path(a["path"]).exists() and Path(b["path"]).exists()


def test_prune_keeps_only_newest(tmp_path, monkeypatch):
    monkeypatch.setenv("CADPILOT_HOME", str(tmp_path))
    monkeypatch.setattr("cadpilot.screenshot_store._KEPT_SCREENSHOTS", 3)
    for _ in range(5):
        save_screenshot(PNG_1X1)
    assert len(list((tmp_path / "screenshots").glob("view-*.png"))) == 3


def test_save_screenshot_rejects_bad_base64(tmp_path, monkeypatch):
    monkeypatch.setenv("CADPILOT_HOME", str(tmp_path))
    with pytest.raises(ValueError):
        save_screenshot("!!!not-base64!!!")


def test_png_size_non_png_returns_none():
    assert _png_size(b"not a png") == (None, None)
