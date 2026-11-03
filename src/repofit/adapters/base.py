from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from repofit.model import InventoryItem, MemoryDoc, Session


class Adapter:
    """One local agent. Every method must be cheap to call when the agent is absent."""

    id = ""
    name = ""
    # "verified": parser checked against real data; "unverified": written from public docs;
    # "none": this agent keeps no local transcripts we can read.
    sessions_status = "none"

    def __init__(self) -> None:
        # Project roots discovered by other adapters, for agents that keep history per repo.
        self.known_projects: set[str] = set()

    def home(self) -> Path | None:
        return None

    def detect(self) -> bool:
        home = self.home()
        return bool(home and home.exists())

    def memories(self) -> Iterator[MemoryDoc]:
        return iter(())

    def inventory(self) -> Iterator[InventoryItem]:
        return iter(())

    def projects(self) -> set[str]:
        return set()

    def session_count(self) -> int | None:
        return None

    def sessions(self) -> Iterator[Session]:
        return iter(())

    # Helpers shared by subclasses.

    def _doc(self, path: Path, kind: str, scope: str = "global") -> Iterator[MemoryDoc]:
        if path.is_file() and path.stat().st_size > 0:
            yield MemoryDoc(self.id, path, kind, scope)

    def _mcp_servers(self, config, source: str, key: str = "mcpServers") -> Iterator[InventoryItem]:
        servers = config.get(key) if isinstance(config, dict) else None
        if isinstance(servers, dict):
            for name in servers:
                yield InventoryItem(self.id, "mcp_server", name, source)
        elif isinstance(servers, list):
            for entry in servers:
                if isinstance(entry, dict) and entry.get("name"):
                    yield InventoryItem(self.id, "mcp_server", entry["name"], source)

    def _skills(self, root: Path, source: str = "user") -> Iterator[InventoryItem]:
        if not root.is_dir():
            return
        for manifest in sorted(root.rglob("SKILL.md")):
            parts = manifest.relative_to(root).parts
            if any(p.startswith(".") for p in parts):
                continue
            yield InventoryItem(self.id, "skill", manifest.parent.name, source)
