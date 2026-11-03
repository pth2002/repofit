"""Gemini CLI and its forks (Qwen Code) share one on-disk layout."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from repofit.adapters.base import Adapter
from repofit.model import InventoryItem, MemoryDoc, Session
from repofit.normalize import is_injected, make_call, mtime_dt, read_json, shell_detail, text_parts, to_dt
from repofit.paths import HOME


class GeminiCli(Adapter):
    id = "gemini-cli"
    name = "Gemini CLI"
    sessions_status = "unverified"
    dirname = ".gemini"
    memory_file = "GEMINI.md"
    extension_manifest = "gemini-extension.json"

    def home(self) -> Path:
        return HOME / self.dirname

    def memories(self) -> Iterator[MemoryDoc]:
        yield from self._doc(self.home() / self.memory_file, "instructions")

    def inventory(self) -> Iterator[InventoryItem]:
        home = self.home()
        yield from self._mcp_servers(read_json(home / "settings.json"), "settings.json")
        for manifest in sorted((home / "extensions").glob(f"*/{self.extension_manifest}")):
            yield InventoryItem(self.id, "extension", manifest.parent.name, "extensions")

    def _files(self) -> list[Path]:
        tmp = self.home() / "tmp"
        return sorted(set(tmp.glob("*/chats/*.json")) | set(tmp.glob("*/checkpoint-*.json")))

    def session_count(self) -> int:
        return len(self._files())

    def sessions(self) -> Iterator[Session]:
        for path in self._files():
            data = read_json(path)
            s = Session(self.id, path.stem, None, mtime_dt(path), None)
            if isinstance(data, dict):  # recorded session: {sessionId, startTime, messages: [...]}
                s.id = str(data.get("sessionId") or s.id)
                s.started_at = to_dt(data.get("startTime")) or s.started_at
                for msg in data.get("messages") or []:
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("type") == "user":
                        self._user(s, msg.get("content"))
                    for call in msg.get("toolCalls") or []:
                        if isinstance(call, dict) and call.get("name"):
                            s.tool_calls.append(make_call(call["name"], shell_detail(call.get("args") or {})))
            elif isinstance(data, list):  # /chat save checkpoint: [{role, parts: [...]}]
                for content in data:
                    if not isinstance(content, dict):
                        continue
                    parts = content.get("parts") or []
                    if content.get("role") == "user":
                        self._user(s, [p for p in parts if isinstance(p, dict) and "text" in p])
                    for p in parts:
                        call = p.get("functionCall") if isinstance(p, dict) else None
                        if isinstance(call, dict) and call.get("name"):
                            s.tool_calls.append(make_call(call["name"], shell_detail(call.get("args") or {})))
            if s.user_messages or s.tool_calls:
                yield s

    @staticmethod
    def _user(s: Session, content) -> None:
        text = text_parts(content).strip()
        if text and not is_injected(text):
            s.user_messages.append(text)


class QwenCode(GeminiCli):
    id = "qwen-code"
    name = "Qwen Code"
    dirname = ".qwen"
    memory_file = "QWEN.md"
    extension_manifest = "qwen-extension.json"
