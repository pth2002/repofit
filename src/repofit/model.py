"""Agent-agnostic records every adapter normalizes into."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class ToolCall:
    kind: str  # shell | read | edit | search | web | mcp | agent | other
    name: str  # tool name exactly as the agent recorded it
    detail: str = ""  # command line, search query, or "server/tool" for MCP


@dataclass
class Session:
    agent: str
    id: str
    project: str | None = None
    started_at: datetime | None = None
    title: str | None = None
    user_messages: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class MemoryDoc:
    agent: str
    path: Path  # the file, or the database an inline entry came from
    kind: str  # instructions | memory | persona
    scope: str = "global"  # global | project
    inline: str | None = None  # text stored inside a database rather than in its own file

    @property
    def size(self) -> int:
        if self.inline is not None:
            return len(self.inline.encode("utf-8"))
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def read(self, limit: int = 200_000) -> str:
        if self.inline is not None:
            return self.inline[:limit]
        with open(self.path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)


@dataclass
class InventoryItem:
    agent: str
    kind: str  # skill | mcp_server | plugin | extension
    name: str
    source: str | None = None
