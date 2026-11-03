from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from repofit.adapters.base import Adapter
from repofit.model import InventoryItem, MemoryDoc, Session
from repofit.normalize import is_injected, make_call, read_json, shell_detail, text_parts, yaml_children
from repofit.paths import HOME, env_path


class ContinueDev(Adapter):
    id = "continue"
    name = "Continue"
    sessions_status = "unverified"

    def home(self) -> Path:
        return env_path("CONTINUE_GLOBAL_DIR", HOME / ".continue")

    def memories(self) -> Iterator[MemoryDoc]:
        for path in sorted((self.home() / "rules").glob("*.md")):
            yield from self._doc(path, "instructions")

    def inventory(self) -> Iterator[InventoryItem]:
        home = self.home()
        try:
            config = (home / "config.yaml").read_text(encoding="utf-8")
        except OSError:
            config = ""
        for name in yaml_children(config, "mcpServers"):
            yield InventoryItem(self.id, "mcp_server", name, "config.yaml")
        for path in sorted((home / "mcpServers").glob("*.y*ml")):
            yield InventoryItem(self.id, "mcp_server", path.stem, "mcpServers/")

    def _files(self) -> list[Path]:
        return sorted(p for p in (self.home() / "sessions").glob("*.json") if p.name != "sessions.json")

    def session_count(self) -> int:
        return len(self._files())

    def sessions(self) -> Iterator[Session]:
        for path in self._files():
            data = read_json(path)
            if not isinstance(data, dict):
                continue
            s = Session(self.id, str(data.get("sessionId") or path.stem),
                        data.get("workspaceDirectory") or None, None, data.get("title") or None)
            for entry in data.get("history") or []:
                msg = (entry or {}).get("message") if isinstance(entry, dict) else None
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") == "user":
                    text = text_parts(msg.get("content")).strip()
                    if text and not is_injected(text):
                        s.user_messages.append(text)
                for call in msg.get("toolCalls") or []:
                    fn = (call or {}).get("function") or {}
                    if fn.get("name"):
                        s.tool_calls.append(make_call(fn["name"], shell_detail(fn.get("arguments") or "")))
            if s.user_messages or s.tool_calls:
                yield s
