from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from repofit.adapters.base import Adapter
from repofit.model import InventoryItem, MemoryDoc, Session
from repofit.normalize import (
    is_injected, iter_jsonl, make_call, mcp_label, read_json, text_parts, to_dt,
)
from repofit.paths import HOME, env_path

_MARKERS = ('"type":"user"', '"type":"assistant"', '"customTitle"')


class ClaudeCode(Adapter):
    id = "claude-code"
    name = "Claude Code"
    sessions_status = "verified"

    def home(self) -> Path:
        return env_path("CLAUDE_CONFIG_DIR", HOME / ".claude")

    def _state_path(self) -> Path:
        return self.home() / ".claude.json" if "CLAUDE_CONFIG_DIR" in os.environ else HOME / ".claude.json"

    def _state(self) -> dict:
        return read_json(self._state_path()) or {}

    def detect(self) -> bool:
        return self.home().exists() or self._state_path().exists()

    def memories(self) -> Iterator[MemoryDoc]:
        home = self.home()
        yield from self._doc(home / "CLAUDE.md", "instructions")
        for path in sorted(home.glob("projects/*/memory/*.md")):
            yield from self._doc(path, "memory", "project")

    def inventory(self) -> Iterator[InventoryItem]:
        home = self.home()
        yield from self._skills(home / "skills")
        installed = (read_json(home / "plugins" / "installed_plugins.json") or {}).get("plugins") or {}
        enabled = (read_json(home / "settings.json") or {}).get("enabledPlugins") or {}
        for key in sorted(set(installed) | {k for k, on in enabled.items() if on}):
            yield InventoryItem(self.id, "plugin", key.split("@")[0], key)
        state = self._state()
        yield from self._mcp_servers(state, "global")
        for project, cfg in (state.get("projects") or {}).items():
            yield from self._mcp_servers(cfg, project)

    def projects(self) -> set[str]:
        return set((self._state().get("projects") or {}).keys())

    def _files(self) -> list[Path]:
        return sorted(self.home().glob("projects/*/*.jsonl"))

    def session_count(self) -> int:
        return len(self._files())

    def sessions(self) -> Iterator[Session]:
        for path in self._files():
            session = self._parse(path)
            if session.user_messages or session.tool_calls:
                yield session

    def _parse(self, path: Path) -> Session:
        s = Session(self.id, path.stem)
        for r in iter_jsonl(path, _MARKERS):
            if r.get("isSidechain"):
                continue
            s.project = s.project or r.get("cwd")
            s.started_at = s.started_at or to_dt(r.get("timestamp"))
            kind = r.get("type")
            if kind == "custom-title":
                s.title = r.get("customTitle") or s.title
            elif kind == "user":
                content = (r.get("message") or {}).get("content")
                if isinstance(content, list) and any(p.get("type") == "tool_result" for p in content if isinstance(p, dict)):
                    continue
                text = text_parts(content).strip()
                if text and not is_injected(text):
                    s.user_messages.append(text)
            elif kind == "assistant":
                for block in (r.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        s.tool_calls.append(_call(block.get("name", ""), block.get("input") or {}))
        return s


def _call(name: str, args: dict):
    if name.startswith("mcp__"):
        return make_call(name, mcp_label(name))
    detail = (args.get("command") or args.get("query") or args.get("url") or args.get("skill")
              or args.get("subagent_type") or args.get("pattern") or "")
    return make_call(name, str(detail))
