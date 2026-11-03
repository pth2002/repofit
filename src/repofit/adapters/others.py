"""Agents we can detect and inventory, plus a few simple transcript formats."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from repofit.adapters.base import Adapter
from repofit.model import InventoryItem, MemoryDoc, Session
from repofit.normalize import make_call, mtime_dt, read_json, sqlite_ro, text_parts, to_dt
from repofit.paths import HOME, app_support, xdg_config, xdg_data


class WorkBuddy(Adapter):
    id = "workbuddy"
    name = "WorkBuddy"

    def home(self) -> Path:
        return HOME / ".workbuddy"

    def memories(self) -> Iterator[MemoryDoc]:
        home = self.home()
        yield from self._doc(home / "USER.md", "memory")
        for path in sorted((home / "memery").glob("*.md")):
            yield from self._doc(path, "memory")
        yield from self._doc(home / "SOUL.md", "persona")
        yield from self._doc(home / "IDENTITY.md", "persona")

    def inventory(self) -> Iterator[InventoryItem]:
        yield from self._mcp_servers(read_json(self.home() / "mcp.json"), "mcp.json")


class Windsurf(Adapter):
    id = "windsurf"
    name = "Windsurf"

    def home(self) -> Path:
        return HOME / ".codeium" / "windsurf"

    def memories(self) -> Iterator[MemoryDoc]:
        for path in sorted((self.home() / "memories").glob("*.md")):
            yield from self._doc(path, "instructions" if path.stem == "global_rules" else "memory")

    def inventory(self) -> Iterator[InventoryItem]:
        yield from self._mcp_servers(read_json(self.home() / "mcp_config.json"), "mcp_config.json")


class OpenCode(Adapter):
    id = "opencode"
    name = "OpenCode"
    sessions_status = "detect-only"

    def home(self) -> Path:
        return xdg_data() / "opencode"

    def memories(self) -> Iterator[MemoryDoc]:
        yield from self._doc(xdg_config() / "opencode" / "AGENTS.md", "instructions")

    def inventory(self) -> Iterator[InventoryItem]:
        for name in ("opencode.json", "opencode.jsonc"):
            yield from self._mcp_servers(read_json(xdg_config() / "opencode" / name), name, key="mcp")

    def session_count(self) -> int | None:
        db = sqlite_ro(self.home() / "opencode.db")
        if db:
            with db:
                try:
                    return db.execute("select count(*) from session").fetchone()[0]
                except Exception:
                    return None
        files = list((self.home() / "storage" / "session").rglob("*.json"))
        return len(files) if files else None


class Trae(Adapter):
    id = "trae"
    name = "Trae"

    def _dirs(self) -> list[Path]:
        return [d for d in (app_support() / "Trae", app_support() / "Trae CN", HOME / ".trae", HOME / ".trae-cn") if d.is_dir()]

    def detect(self) -> bool:
        return bool(self._dirs())

    def inventory(self) -> Iterator[InventoryItem]:
        for d in self._dirs():
            yield from self._mcp_servers(read_json(d / "User" / "mcp.json"), d.name)


class ClaudeDesktop(Adapter):
    """Chats live on Anthropic's servers; locally there is only the MCP config."""

    id = "claude-desktop"
    name = "Claude Desktop"

    def home(self) -> Path:
        return app_support() / "Claude"

    def inventory(self) -> Iterator[InventoryItem]:
        yield from self._mcp_servers(read_json(self.home() / "claude_desktop_config.json"), "claude_desktop_config.json")


class VSCodeCopilot(Adapter):
    id = "vscode-copilot"
    name = "VS Code (Copilot Chat)"
    sessions_status = "unverified"

    def home(self) -> Path:
        return app_support() / "Code" / "User"

    def inventory(self) -> Iterator[InventoryItem]:
        yield from self._mcp_servers(read_json(self.home() / "mcp.json"), "User/mcp.json", key="servers")
        for ext in read_json(HOME / ".vscode" / "extensions" / "extensions.json") or []:
            ident = ((ext or {}).get("identifier") or {}).get("id")
            if ident:
                yield InventoryItem(self.id, "extension", ident, "vscode-extensions")

    def _files(self) -> list[Path]:
        home = self.home()
        return sorted(set(home.glob("workspaceStorage/*/chatSessions/*.json"))
                      | set(home.glob("globalStorage/emptyWindowChatSessions/*.json")))

    def session_count(self) -> int:
        return len(self._files())

    def sessions(self) -> Iterator[Session]:
        for path in self._files():
            data = read_json(path)
            if not isinstance(data, dict):
                continue
            s = Session(self.id, str(data.get("sessionId") or path.stem), None,
                        to_dt(data.get("creationDate")) or mtime_dt(path), data.get("customTitle") or None)
            for req in data.get("requests") or []:
                if not isinstance(req, dict):
                    continue
                text = text_parts(req.get("message")).strip()
                if text:
                    s.user_messages.append(text)
                for part in req.get("response") or []:
                    if isinstance(part, dict) and part.get("kind") == "toolInvocationSerialized" and part.get("toolId"):
                        s.tool_calls.append(make_call(str(part["toolId"])))
            if s.user_messages or s.tool_calls:
                yield s


class Aider(Adapter):
    """Aider writes .aider.chat.history.md into each repo, so we look in projects other agents know about."""

    id = "aider"
    name = "Aider"
    sessions_status = "unverified"

    def _files(self) -> list[Path]:
        out = []
        for root in self.known_projects:
            path = Path(root) / ".aider.chat.history.md"
            if path.is_file():
                out.append(path)
        return sorted(out)

    def detect(self) -> bool:
        return any((HOME / n).exists() for n in (".aider.conf.yml", ".aider")) or bool(self._files())

    def session_count(self) -> int:
        return len(self._files())

    def sessions(self) -> Iterator[Session]:
        for path in self._files():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, chunk in enumerate(re.split(r"^# aider chat started at ", text, flags=re.M)[1:]):
                stamp, _, body = chunk.partition("\n")
                s = Session(self.id, f"{path.parent.name}-{i}", str(path.parent), to_dt(stamp.strip().replace(" ", "T")))
                s.user_messages = [line[5:].strip() for line in body.splitlines()
                                   if line.startswith("#### ") and not line[5:].lstrip().startswith("/")]
                s.tool_calls = [make_call("aider_command", line[5:].strip()) for line in body.splitlines()
                                if line.startswith("#### /")]
                if s.user_messages or s.tool_calls:
                    yield s
