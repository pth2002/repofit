from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

from repofit.adapters.base import Adapter
from repofit.model import InventoryItem, MemoryDoc, Session
from repofit.normalize import (
    is_injected, iter_jsonl, make_call, shell_detail, sqlite_ro, text_parts, to_dt,
)
from repofit.paths import HOME, env_path

_ITEM_TYPES = ("userMessage", "commandExecution", "mcpToolCall", "webSearch", "fileChange")


class Codex(Adapter):
    """Codex CLI and desktop. Desktop indexes threads in SQLite; the CLI writes rollout JSONL."""

    id = "codex"
    name = "Codex"
    sessions_status = "verified"

    def home(self) -> Path:
        return env_path("CODEX_HOME", HOME / ".codex")

    def _latest(self, stem: str) -> Path | None:
        """Newest schema version of e.g. state_5.sqlite."""
        found = []
        for path in self.home().glob(f"{stem}_*.sqlite"):
            m = re.fullmatch(rf"{stem}_(\d+)\.sqlite", path.name)
            if m:
                found.append((int(m.group(1)), path))
        return max(found)[1] if found else None

    def memories(self) -> Iterator[MemoryDoc]:
        home = self.home()
        yield from self._doc(home / "AGENTS.md", "instructions")
        yield from self._doc(home / "memories" / "MEMORY.md", "memory")
        yield from self._doc(home / "memories" / "memory_summary.md", "memory")

    def inventory(self) -> Iterator[InventoryItem]:
        home = self.home()
        try:
            config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            config = {}
        for name in config.get("mcp_servers") or {}:
            yield InventoryItem(self.id, "mcp_server", name, "config.toml")
        for key, cfg in (config.get("plugins") or {}).items():
            if not isinstance(cfg, dict) or cfg.get("enabled", True):
                yield InventoryItem(self.id, "plugin", key.split("@")[0], key)
        yield from self._skills(home / "skills")

    def projects(self) -> set[str]:
        db = sqlite_ro(self._latest("state") or Path())
        if not db:
            return set()
        with db:
            roots = {r[0] for r in db.execute("select distinct cwd from threads where cwd is not null")}
            try:
                roots |= {r[0] for r in db.execute("select path from project_roots")}
            except Exception:
                pass
        return {r.removeprefix("\\\\?\\") for r in roots if r}

    def _rollouts(self) -> list[Path]:
        home = self.home()
        return sorted(p for d in ("sessions", "archived_sessions") for p in (home / d).rglob("rollout-*.jsonl"))

    def session_count(self) -> int:
        db = sqlite_ro(self._latest("state") or Path())
        if db:
            with db:
                return db.execute("select count(*) from threads").fetchone()[0]
        return len(self._rollouts())

    def sessions(self) -> Iterator[Session]:
        seen: set[str] = set()
        for s in self._from_sqlite():
            seen.add(s.id)
            yield s
        for path in self._rollouts():
            s = self._from_rollout(path)
            if s.id not in seen and (s.user_messages or s.tool_calls):
                yield s

    def _from_sqlite(self) -> Iterator[Session]:
        state, history = sqlite_ro(self._latest("state") or Path()), sqlite_ro(self._latest("thread_history") or Path())
        if not state or not history:
            return
        with state, history:
            threads = {}
            for tid, cwd, title, created_ms, created in state.execute(
                    "select id, cwd, title, created_at_ms, created_at from threads"):
                threads[tid] = Session(self.id, tid, (cwd or "").removeprefix("\\\\?\\") or None,
                                       to_dt(created_ms or created), title or None)
            marks = ",".join("?" * len(_ITEM_TYPES))
            rows = history.execute(
                f"""select thread_id, item_type, json_extract(item_json, '$.content'),
                           json_extract(item_json, '$.command'), json_extract(item_json, '$.server'),
                           json_extract(item_json, '$.tool'), json_extract(item_json, '$.query')
                    from thread_items where item_type in ({marks})
                    order by thread_id, rollout_ordinal""", _ITEM_TYPES)
            for tid, kind, content, command, server, tool, query in rows:
                s = threads.setdefault(tid, Session(self.id, tid))
                _apply_item(s, {"type": kind, "content": json.loads(content) if content else None,
                                "command": command, "server": server, "tool": tool, "query": query})
            for s in threads.values():
                if s.user_messages or s.tool_calls:
                    yield s

    def _from_rollout(self, path: Path) -> Session:
        s = Session(self.id, path.stem.removeprefix("rollout-"))
        item_based, legacy = Session(self.id, ""), Session(self.id, "")
        for r in iter_jsonl(path):
            kind, p = r.get("type"), r.get("payload")
            if not isinstance(p, dict):
                continue
            if kind == "session_meta":
                s.id = p.get("id") or s.id
                s.project = p.get("cwd") or s.project
                s.started_at = to_dt(p.get("timestamp")) or to_dt(r.get("timestamp"))
            elif kind == "turn_context":
                s.project = s.project or p.get("cwd")
            elif kind == "event_msg" and p.get("type") == "item_completed":
                _apply_item(item_based, p.get("item") or {})
            elif kind == "event_msg" and p.get("type") == "user_message":
                text = str(p.get("message") or "").strip()
                if text and not is_injected(text):
                    legacy.user_messages.append(text)
            elif kind == "response_item" and p.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
                name = p.get("name") or "local_shell"
                detail = shell_detail(p.get("arguments") or p.get("action") or "") if name in (
                    "shell", "exec_command", "local_shell", "shell_command") else ""
                legacy.tool_calls.append(make_call(name, detail))
            elif kind == "response_item" and p.get("type") == "web_search_call":
                legacy.tool_calls.append(make_call("web_search", str((p.get("action") or {}).get("query") or "")))
        src = item_based if (item_based.user_messages or item_based.tool_calls) else legacy
        s.user_messages, s.tool_calls = src.user_messages, src.tool_calls
        return s


def _apply_item(s: Session, item: dict) -> None:
    kind = str(item.get("type") or "")
    kind = kind[:1].lower() + kind[1:]  # the app database writes "commandExecution", newer rollouts "CommandExecution"
    if kind == "userMessage":
        text = text_parts(item.get("content")).strip()
        if text and not is_injected(text):
            s.user_messages.append(text)
    elif kind == "commandExecution":
        command = item.get("command")
        if isinstance(command, list):  # newer rollouts store argv, e.g. ["pwsh.exe", "-Command", "..."]
            command = " ".join(str(part) for part in command)
        s.tool_calls.append(make_call("commandExecution", str(command or "")))
    elif kind == "mcpToolCall":
        s.tool_calls.append(make_call("mcpToolCall", f"{item.get('server')}/{item.get('tool')}"))
    elif kind == "webSearch" or (kind == "extension" and item.get("kind") == "web.search"):
        s.tool_calls.append(make_call("web_search", str(item.get("query") or "")))
    elif kind == "fileChange":
        s.tool_calls.append(make_call("fileChange"))
