"""Cline and its forks (Roo Code, Kilo Code): one folder per task inside the host editor's globalStorage."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from repofit.adapters.base import Adapter
from repofit.model import InventoryItem, Session
from repofit.normalize import make_call, read_json, text_parts, to_dt
from repofit.paths import HOME, VSCODE_HOSTS, app_support

_TOOL_TAGS = ("execute_command", "read_file", "write_to_file", "replace_in_file", "apply_diff", "search_files",
              "list_files", "list_code_definition_names", "browser_action", "use_mcp_tool", "access_mcp_resource",
              "web_fetch", "new_task", "insert_content", "search_and_replace", "codebase_search")
_TAG_RE = re.compile(r"<(" + "|".join(_TOOL_TAGS) + r")>(.*?)</\1>", re.S)
_USER_RE = re.compile(r"<(task|feedback|user_message|answer)>\s*(.*?)\s*</\1>", re.S)
_CWD_RE = re.compile(r"Current (?:Working Directory|Workspace Directory) \((.+?)\)")


class Cline(Adapter):
    id = "cline"
    name = "Cline"
    sessions_status = "unverified"
    extension_id = "saoudrizwan.claude-dev"
    mcp_file = "cline_mcp_settings.json"
    standalone_dir: Path | None = HOME / ".cline" / "data"

    def _roots(self) -> list[Path]:
        roots = [app_support() / host / "User" / "globalStorage" / self.extension_id for host in VSCODE_HOSTS]
        if self.standalone_dir:
            roots.append(self.standalone_dir)
        return [r for r in roots if r.is_dir()]

    def detect(self) -> bool:
        return bool(self._roots())

    def inventory(self) -> Iterator[InventoryItem]:
        for root in self._roots():
            yield from self._mcp_servers(read_json(root / "settings" / self.mcp_file), str(root.parent.parent.parent.name))

    def _task_dirs(self) -> list[Path]:
        return sorted(d for root in self._roots() for d in (root / "tasks").glob("*") if d.is_dir())

    def session_count(self) -> int:
        return len(self._task_dirs())

    def sessions(self) -> Iterator[Session]:
        for task in self._task_dirs():
            history = read_json(task / "api_conversation_history.json")
            if not isinstance(history, list):
                continue
            s = Session(self.id, task.name, None, to_dt(task.name) if task.name.isdigit() else None, None)
            for msg in history:
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use" and block.get("name"):
                        args = block.get("input") or {}
                        s.tool_calls.append(_call(block["name"], args if isinstance(args, dict) else {}))
                        continue
                    text = text_parts([block])
                    if msg.get("role") == "user":
                        s.project = s.project or next(iter(_CWD_RE.findall(text)), None)
                        s.user_messages.extend(m[1] for m in _USER_RE.findall(text) if m[1])
                    elif msg.get("role") == "assistant":
                        for tag, inner in _TAG_RE.findall(text):
                            s.tool_calls.append(_call(tag, _inner_args(inner)))
            if s.user_messages or s.tool_calls:
                yield s


class RooCode(Cline):
    id = "roo-code"
    name = "Roo Code"
    extension_id = "rooveterinaryinc.roo-cline"
    mcp_file = "mcp_settings.json"
    standalone_dir = None


class KiloCode(Cline):
    id = "kilo-code"
    name = "Kilo Code"
    extension_id = "kilocode.kilo-code"
    mcp_file = "mcp_settings.json"
    standalone_dir = None


def _inner_args(inner: str) -> dict:
    return {k: v.strip() for k, v in re.findall(r"<(\w+)>(.*?)</\1>", inner, re.S)}


def _call(name: str, args: dict):
    if name in ("use_mcp_tool", "access_mcp_resource"):
        return make_call(name, f"{args.get('server_name')}/{args.get('tool_name') or args.get('uri')}")
    return make_call(name, str(args.get("command") or args.get("regex") or args.get("url") or ""))
