from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from repofit.adapters.base import Adapter
from repofit.model import InventoryItem, MemoryDoc, Session
from repofit.normalize import is_injected, make_call, read_json, shell_detail, text_parts, to_dt, yaml_children
from repofit.paths import HOME, env_path


class Hermes(Adapter):
    id = "hermes"
    name = "Hermes Agent"
    sessions_status = "verified"

    def home(self) -> Path:
        return env_path("HERMES_HOME", HOME / ".hermes")

    def memories(self) -> Iterator[MemoryDoc]:
        home = self.home()
        yield from self._doc(home / "memories" / "USER.md", "memory")
        yield from self._doc(home / "memories" / "MEMORY.md", "memory")
        yield from self._doc(home / "SOUL.md", "persona")

    def inventory(self) -> Iterator[InventoryItem]:
        home = self.home()
        yield from self._skills(home / "skills")
        try:
            config = (home / "config.yaml").read_text(encoding="utf-8")
        except OSError:
            config = ""
        for name in yaml_children(config, "mcp_servers"):
            yield InventoryItem(self.id, "mcp_server", name, "config.yaml")

    def _files(self) -> list[Path]:
        return sorted((self.home() / "sessions").glob("session_*.json"))

    def session_count(self) -> int:
        return len(self._files())

    def sessions(self) -> Iterator[Session]:
        for path in self._files():
            data = read_json(path)
            if not isinstance(data, dict):
                continue
            s = Session(self.id, str(data.get("session_id") or path.stem), None,
                        to_dt(data.get("session_start")), None)
            for msg in data.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") == "user":
                    text = text_parts(msg.get("content")).strip()
                    if text and not is_injected(text):
                        s.user_messages.append(text)
                for call in msg.get("tool_calls") or []:
                    fn = call.get("function") or call if isinstance(call, dict) else {}
                    name = str(fn.get("name") or "")
                    if name:
                        s.tool_calls.append(make_call(name, _detail(name, fn.get("arguments") or "")))
            if s.user_messages or s.tool_calls:
                yield s


def _detail(name: str, args) -> str:
    if name.startswith("skill_"):  # skill_view / skill_manage carry the skill name, not a command
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except ValueError:
            return ""
        return str(args.get("name") or "") if isinstance(args, dict) else ""
    return shell_detail(args)
