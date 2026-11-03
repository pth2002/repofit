from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from repofit.adapters.base import Adapter
from repofit.model import Session
from repofit.normalize import is_injected, iter_jsonl, make_call, mtime_dt, shell_detail, text_parts
from repofit.paths import HOME, env_path


class Kimi(Adapter):
    """Kimi CLI (~/.kimi) and its successor Kimi Code CLI (~/.kimi-code); both keep context.jsonl per session."""

    id = "kimi"
    name = "Kimi CLI"
    sessions_status = "unverified"

    def _homes(self) -> list[Path]:
        return [h for h in (HOME / ".kimi", env_path("KIMI_CODE_HOME", HOME / ".kimi-code")) if h.is_dir()]

    def detect(self) -> bool:
        return bool(self._homes())

    def _files(self) -> list[Path]:
        return sorted(p for h in self._homes() for p in (h / "sessions").glob("*/*/context.jsonl"))

    def session_count(self) -> int:
        return len(self._files())

    def sessions(self) -> Iterator[Session]:
        for path in self._files():
            s = Session(self.id, path.parent.name, None, mtime_dt(path), None)
            for msg in iter_jsonl(path):
                if msg.get("role") == "user":
                    text = text_parts(msg.get("content")).strip()
                    if text and not is_injected(text):
                        s.user_messages.append(text)
                for call in msg.get("tool_calls") or []:
                    fn = (call or {}).get("function") or {}
                    if fn.get("name"):
                        s.tool_calls.append(make_call(fn["name"], shell_detail(fn.get("arguments") or "")))
            if s.user_messages or s.tool_calls:
                yield s
