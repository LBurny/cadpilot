"""Screenshot storage: decode base64 PNGs to disk, prune old ones.

File delivery is the only mode: the tool result stays a tiny path string
instead of a permanent base64 blob in the conversation, and a multimodal
client opens the PNG with its own file-reading tool.
"""

import base64
import logging
from datetime import datetime
from pathlib import Path

from .session_state import data_dir

logger = logging.getLogger("CADPilot")

# Old screenshots are pruned down to this many newest files on each save.
_KEPT_SCREENSHOTS = 100


def screenshot_dir() -> Path:
    return data_dir() / "screenshots"


def _png_size(png: bytes) -> tuple[int | None, int | None]:
    # Width/height are big-endian uint32 at bytes 16..24 of a PNG (IHDR).
    if len(png) >= 24 and png[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
    return None, None


def _unique_path(out_dir: Path) -> Path:
    stamp = f"{datetime.now():%Y%m%d-%H%M%S-%f}"
    for i in range(1000):
        candidate = out_dir / f"view-{stamp}-{i}.png"
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not allocate a screenshot filename")


def save_screenshot(screenshot_b64: str) -> dict:
    """Write base64-PNG *screenshot_b64* to a unique file; return its info."""
    png = base64.b64decode(screenshot_b64, validate=True)
    out_dir = screenshot_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _unique_path(out_dir)
    path.write_bytes(png)
    _prune(out_dir)
    width, height = _png_size(png)
    return {"path": str(path), "width": width, "height": height, "bytes": len(png)}


def _prune(out_dir: Path) -> None:
    shots = sorted(out_dir.glob("view-*.png"))
    excess = len(shots) - _KEPT_SCREENSHOTS
    for old in shots[: max(0, excess)]:
        try:
            old.unlink()
        except OSError:
            logger.warning(f"Could not prune old screenshot {old}")
