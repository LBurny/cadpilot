import json
import logging

from mcp.types import ImageContent, TextContent

from .screenshot_store import save_screenshot

logger = logging.getLogger("CADPilot")

type ToolResponse = list[TextContent | ImageContent]

# How screenshots are returned: "image" inlines base64 ImageContent (default,
# works with every MCP client); "file" writes the PNG to disk and returns only
# the path, so agentic clients with a file-reading tool keep base64 out of the
# conversation (permanent, cache-busting text). Set once at startup from the
# --screenshot-mode CLI flag via set_screenshot_mode().
_SCREENSHOT_MODE = "image"


def set_screenshot_mode(mode: str) -> None:
    global _SCREENSHOT_MODE
    if mode not in ("image", "file"):
        raise ValueError(f"Unknown screenshot mode: {mode!r}")
    _SCREENSHOT_MODE = mode


def get_screenshot_mode() -> str:
    return _SCREENSHOT_MODE


def text_response(message: str) -> ToolResponse:
    return [TextContent(type="text", text=message)]


def json_response(data: object) -> ToolResponse:
    # Compact separators keep tool output small — every token here is paid by
    # the LLM client on each call.
    return text_response(json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str))


def screenshot_content(screenshot: str) -> TextContent | ImageContent:
    """One screenshot content block, inline image or file pointer per mode."""
    if _SCREENSHOT_MODE == "file":
        try:
            info = save_screenshot(screenshot)
        except (ValueError, OSError) as e:
            logger.warning(f"Saving screenshot to file failed, returning inline image: {e}")
        else:
            dims = f"{info['width']}x{info['height']}" if info["width"] else "unknown size"
            return TextContent(
                type="text",
                text=(
                    f"Screenshot saved to {info['path']} ({dims}). "
                    "View it with your file-reading tool."
                ),
            )
    return ImageContent(type="image", data=screenshot, mimeType="image/png")


def add_screenshot_if_available(
    response: ToolResponse,
    screenshot: str | None,
    only_text_feedback: bool,
) -> ToolResponse:
    if only_text_feedback or screenshot is None:
        return response
    return [*response, screenshot_content(screenshot)]
