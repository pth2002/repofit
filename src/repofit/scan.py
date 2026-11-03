"""Find the agents on this machine and what each one can tell us."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from repofit.adapters import Adapter, all_adapters
from repofit.model import InventoryItem, MemoryDoc, Session

# Instruction files agents read from a repo root, regardless of which agent wrote them.
PROJECT_DOCS = [
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", "QWEN.md", ".cursorrules", ".windsurfrules", ".clinerules",
    ".github/copilot-instructions.md",
]
PROJECT_DOC_DIRS = [".cursor/rules", ".clinerules", ".windsurf/rules"]


@dataclass
class AgentReport:
    adapter: Adapter
    sessions: int | None = None
    memories: list[MemoryDoc] = field(default_factory=list)
    inventory: list[InventoryItem] = field(default_factory=list)
    error: str | None = None


_RAISE = object()


def _safe(fn, default):
    try:
        return fn()
    except Exception as exc:  # one broken agent store must not hide the others
        return exc if default is _RAISE else default


def detected(only: str | None = None) -> list[Adapter]:
    adapters = all_adapters()
    found = [a for a in adapters if _safe(a.detect, False)]
    projects = known_projects(found)
    for a in adapters:
        a.known_projects = projects
    # Agents that keep history inside repos only show up once project roots are known.
    found += [a for a in adapters if a not in found and _safe(a.detect, False)]
    found.sort(key=adapters.index)
    return [a for a in found if only is None or a.id == only]


def known_projects(adapters: list[Adapter]) -> set[str]:
    roots: set[str] = set()
    for a in adapters:
        roots |= _safe(a.projects, set())
    return {r for r in roots if r and Path(r).is_dir()}


def report(adapter: Adapter) -> AgentReport:
    r = AgentReport(adapter)
    for attr, fn in (("sessions", adapter.session_count),
                     ("memories", lambda: list(adapter.memories())),
                     ("inventory", lambda: list(adapter.inventory()))):
        value = _safe(fn, _RAISE)
        if isinstance(value, Exception):
            r.error = f"{attr}: {type(value).__name__}: {value}"
        else:
            setattr(r, attr, value)
    return r


def project_docs(projects: set[str]) -> list[MemoryDoc]:
    docs = []
    for root in sorted(projects):
        base = Path(root)
        for name in PROJECT_DOCS:
            path = base / name
            if path.is_file() and path.stat().st_size > 0:
                docs.append(MemoryDoc("project", path, "instructions", "project"))
        for name in PROJECT_DOC_DIRS:
            for path in sorted((base / name).glob("*.md*")) if (base / name).is_dir() else ():
                docs.append(MemoryDoc("project", path, "instructions", "project"))
    return docs


def all_sessions(adapters: list[Adapter]) -> Iterator[tuple[Adapter, Session | Exception]]:
    for a in adapters:
        try:
            for s in a.sessions():
                yield a, s
        except Exception as exc:
            yield a, exc
