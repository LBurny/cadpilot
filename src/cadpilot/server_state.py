from dataclasses import dataclass

from .freecad_client import FreeCADConnection


@dataclass
class ServerState:
    only_text_feedback: bool = False
    with_screenshots: bool = False
    rpc_host: str = "localhost"
    auto_audit: bool = True
    freecad_connection: FreeCADConnection | None = None

    def resolve_screenshot(self, with_screenshot: bool | None) -> bool:
        """Resolve the per-call ``with_screenshot`` override against the
        server-level flags.

        ``--only-text-feedback`` is a hard guarantee for text-only models and
        always wins. Otherwise the per-call value decides; when the caller
        passes None, the ``--with-screenshots`` startup flag is the default.
        """
        if self.only_text_feedback:
            return False
        if with_screenshot is not None:
            return with_screenshot
        return self.with_screenshots
