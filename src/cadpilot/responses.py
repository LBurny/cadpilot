import json
import logging

from mcp.types import TextContent

from .screenshot_store import save_screenshot

logger = logging.getLogger("CADPilot")

type ToolResponse = list[TextContent]

# Screenshots are always delivered as files: the PNG is written under
# $CADPILOT_HOME/screenshots/ and the tool result carries only its path, so
# the base64 blob never becomes a permanent, cache-busting part of the
# conversation. A multimodal client opens the PNG with its own file tool.


def text_response(message: str) -> ToolResponse:
    return [TextContent(type="text", text=message)]


def json_response(data: object) -> ToolResponse:
    # Compact separators keep tool output small — every token here is paid by
    # the LLM client on each call.
    return text_response(json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str))


def screenshot_content(screenshot: str) -> TextContent:
    """Save the base64 PNG to disk; return a text block carrying its path."""
    try:
        info = save_screenshot(screenshot)
    except (ValueError, OSError) as e:
        logger.warning(f"Saving screenshot to file failed: {e}")
        return TextContent(
            type="text", text=f"A screenshot was captured but could not be saved: {e}"
        )
    dims = f"{info['width']}x{info['height']}" if info["width"] else "unknown size"
    return TextContent(
        type="text",
        text=(f"Screenshot saved to {info['path']} ({dims}). View it with your file-reading tool."),
    )
