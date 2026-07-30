"""Pattern memory: a persistent store of reusable modeling workflows.

This is the second tier of the knowledge hierarchy (① the model's own
training knowledge → ② this experience store → ③ runtime introspection via
inspect_freecad). Patterns are saved either manually (save_pattern) or
automatically when a modeling session completes successfully
(session_complete registers the whole workflow).

Storage: ``<data_dir>/patterns.json`` (see session_state.data_dir).
Retrieval is simple keyword-overlap scoring — the store holds dozens to
hundreds of entries, so embeddings would be overkill.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .session_state import data_dir

_lock = threading.Lock()

_MAX_PATTERNS = 500


def _store_path() -> Path:
    return data_dir() / "patterns.json"


def _load() -> list[dict[str, Any]]:
    path = _store_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save(patterns: list[dict[str, Any]]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(patterns, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def add_pattern(
    name: str,
    description: str,
    code: str = "",
    steps: list[str] | None = None,
    tags: list[str] | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    """Add a pattern; returns the stored entry."""
    with _lock:
        patterns = _load()
        entry = {
            "pattern_id": uuid.uuid4().hex[:10],
            "name": name,
            "description": description,
            "code": code,
            "steps": steps or [],
            "tags": tags or [],
            "source": source,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        patterns.append(entry)
        if len(patterns) > _MAX_PATTERNS:
            patterns = patterns[-_MAX_PATTERNS:]
        _save(patterns)
        return entry


_TOKEN_RE = re.compile(r"[A-Za-z0-9_一-鿿]+")


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text) if len(t) > 1}


def search_patterns(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Keyword-overlap search over name/description/tags/code/steps."""
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []
    scored: list[tuple[int, dict[str, Any]]] = []
    with _lock:
        for entry in _load():
            haystack = " ".join(
                [
                    entry.get("name", ""),
                    entry.get("description", ""),
                    " ".join(entry.get("tags", [])),
                    entry.get("code", ""),
                    " ".join(entry.get("steps", [])),
                ]
            )
            # CJK text has no word boundaries, so exact token equality misses
            # e.g. query "法兰" vs entry "法兰盘" — count substring hits too.
            hay_tokens = _tokenize(haystack)
            hay_lower = haystack.lower()
            score = sum(1 for t in query_tokens if t in hay_tokens or t in hay_lower)
            if score > 0:
                scored.append((score, entry))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in scored[:limit]]


def list_patterns(limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        return _load()[-limit:]


def get_pattern(pattern_id: str) -> dict[str, Any] | None:
    with _lock:
        for entry in _load():
            if entry.get("pattern_id") == pattern_id:
                return entry
    return None
