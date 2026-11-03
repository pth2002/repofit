from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote, urlparse

from repofit.adapters.base import Adapter
from repofit.model import InventoryItem, MemoryDoc, Session
from repofit.normalize import (
    is_injected, make_call, read_json, shell_detail, sqlite_ro, text_parts, to_dt,
)
from repofit.paths import HOME, app_support

_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)


class Cursor(Adapter):
    """Cursor desktop. Older chats live as composer 'bubbles'; newer agent chats as content-addressed
    message blobs that carry no conversation id, so those are returned as one ungrouped session."""

    id = "cursor"
    name = "Cursor"
    sessions_status = "verified"

    def home(self) -> Path:
        return HOME / ".cursor"

    def _user_dir(self) -> Path:
        return app_support() / "Cursor" / "User"

    def _db(self):
        return sqlite_ro(self._user_dir() / "globalStorage" / "state.vscdb")

    def detect(self) -> bool:
        return self.home().exists() or self._user_dir().exists()

    def memories(self) -> Iterator[MemoryDoc]:
        db = self._db()
        if not db:
            return
        path = self._user_dir() / "globalStorage" / "state.vscdb"
        with db:
            rules = db.execute("select value from ItemTable where key='aicontext.personalContext'").fetchone()
            if rules and str(rules[0]).strip():
                yield MemoryDoc(self.id, path, "instructions", inline=str(rules[0]))
            pending = db.execute("select value from ItemTable where key='cursorPendingMemories'").fetchone()
            try:
                items = json.loads(pending[0]) if pending else []
            except ValueError:
                items = []
            texts = [text_parts(i) if not isinstance(i, dict) else str(i.get("memory") or i.get("text") or i.get("content") or "")
                     for i in items]
            if any(texts):
                yield MemoryDoc(self.id, path, "memory", inline="\n".join(t for t in texts if t))

    def inventory(self) -> Iterator[InventoryItem]:
        yield from self._mcp_servers(read_json(self.home() / "mcp.json"), "~/.cursor/mcp.json")
        yield from self._skills(self.home() / "skills")
        for ext in read_json(self.home() / "extensions" / "extensions.json") or []:
            ident = ((ext or {}).get("identifier") or {}).get("id")
            if ident:
                yield InventoryItem(self.id, "extension", ident, "cursor-extensions")

    def projects(self) -> set[str]:
        roots = set()
        for ws in (self._user_dir() / "workspaceStorage").glob("*/workspace.json"):
            uri = (read_json(ws) or {}).get("folder")
            if uri and uri.startswith("file:"):
                roots.add(_uri_path(uri))
        return roots

    def session_count(self) -> int | None:
        db = self._db()
        if not db:
            return None
        with db:
            composers = db.execute(
                "select count(distinct substr(key, 10, 36)) from cursorDiskKV where key like 'bubbleId:%'").fetchone()[0]
            blobs = db.execute("select count(*) from cursorDiskKV where key like 'agentKv:blob:%'").fetchone()[0]
        return composers + (1 if blobs else 0)

    def sessions(self) -> Iterator[Session]:
        db = self._db()
        if not db:
            return
        with db:
            yield from self._composer_sessions(db)
            blob = self._blob_session(db)
            if blob.user_messages or blob.tool_calls:
                yield blob

    def _composer_sessions(self, db) -> Iterator[Session]:
        meta = {}
        for key, value in db.execute("select key, value from cursorDiskKV where key like 'composerData:%'"):
            try:
                data = json.loads(value)
            except (TypeError, ValueError):
                continue
            meta[key.split(":", 1)[1]] = (data.get("name"), data.get("createdAt"))
        sessions: dict[str, Session] = {}
        for key, value in db.execute("select key, value from cursorDiskKV where key like 'bubbleId:%'"):
            parts = key.split(":")
            if len(parts) < 3:
                continue
            try:
                bubble = json.loads(value)
            except (TypeError, ValueError):
                continue
            cid = parts[1]
            if cid not in sessions:
                title, created = meta.get(cid, (None, None))
                sessions[cid] = Session(self.id, cid, None, to_dt(created), title)
            s = sessions[cid]
            if bubble.get("type") == 1:
                text = str(bubble.get("text") or "").strip()
                if text and not is_injected(text):
                    s.user_messages.append(text)
            tool = bubble.get("toolFormerData")
            if isinstance(tool, dict) and tool.get("name"):
                s.tool_calls.append(_call(tool["name"], tool.get("params") or tool.get("rawArgs") or ""))
        yield from (s for s in sessions.values() if s.user_messages or s.tool_calls)

    def _blob_session(self, db) -> Session:
        s = Session(self.id, "agent-blobs", title="Cursor agent chats (ungrouped)")
        for (value,) in db.execute("select value from cursorDiskKV where key like 'agentKv:blob:%'"):
            if isinstance(value, bytes):
                if not value.startswith(b"{"):
                    continue
                value = value.decode("utf-8", "replace")
            try:
                msg = json.loads(value)
            except (TypeError, ValueError):
                continue
            if not isinstance(msg, dict):
                continue
            role, content = msg.get("role"), msg.get("content")
            if role == "user":
                full = text_parts(content)
                queries = _QUERY.findall(full) or ([full.strip()] if full.strip() and not full.lstrip().startswith("<") else [])
                s.user_messages.extend(q for q in queries if q)
            elif role == "assistant" and isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool-call":
                        s.tool_calls.append(_call(part.get("toolName") or "", part.get("args") or part.get("input") or {}))
        return s


def _call(name: str, args):
    if name.lower().startswith("mcp"):
        return make_call(name, name)
    detail = shell_detail(args)
    if not detail and isinstance(args, dict):
        detail = str(args.get("query") or args.get("search_term") or args.get("pattern") or "")
    return make_call(name, detail)


def _uri_path(uri: str) -> str:
    path = unquote(urlparse(uri).path)
    return path[1:] if re.match(r"^/[A-Za-z]:", path) else path
