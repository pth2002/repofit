"""The evidence pack: everything a profile may be built from, sampled, redacted and numbered.

Levels decide what is read at all:
  L0  installed tools and configs only
  L1  plus memory and instruction files
  L2  plus user messages and tool calls from sessions
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from repofit.adapters import Adapter
from repofit.model import MemoryDoc, Session
from repofit.paths import HOME
from repofit.scan import all_sessions, detected, known_projects, project_docs, report
from repofit.signals import Signals, notable_commands, within
from repofit.stack import scan_project

LEVELS = ("L0", "L1", "L2")

_SECRETS = [
    re.compile(r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"hf_[A-Za-z0-9]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret|token)\b\s*[:=]\s*\S+"),
]
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\d.])(?:\+?86[- ]?)?1[3-9]\d{9}(?![\d.])|\+\d{8,15}\b")
# Random-looking strings such as an SSH password pasted after the host: mixed case and digits, no separators.
_OPAQUE = re.compile(r"(?<![\w/.\\-])(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*\d)"
                     r"[A-Za-z0-9]{10,}(?![\w/.\\-])")
# Messages that are mostly pasted logs, tracebacks or code tell us little and cost a lot.
_PASTE = re.compile(r"^(Traceback|\s*at |\s*File \"|\[\d{2}:\d{2}|\d{4}-\d{2}-\d{2}[ T]\d{2}:|\$ |PS [A-Z]:\\|>>> )")
# Wrappers some agents put around what the user typed, and pasted blocks that are not the user's words.
_PREAMBLE = re.compile(r"^#\s*Files (?:mentioned|pasted) by the user:.*?##\s*My request(?: for Codex)?:\s*", re.S)
_PASTED = re.compile(r"<pasted_content[^>]*>.*?(?:</pasted_content>|$)|\[Image[^\]]*\]", re.S)


def redact(text: str) -> str:
    for pattern in _SECRETS:
        text = pattern.sub("[REDACTED]", text)
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    text = _OPAQUE.sub("[REDACTED]", text)
    for home in {str(HOME), str(HOME).replace("\\", "/")}:
        text = text.replace(home, "~")
    return text


@dataclass
class Item:
    ref: str  # "M3", "S12": what the model cites
    kind: str  # memory | session
    agent: str
    source: str  # file path or session id
    project: str | None
    when: str | None
    text: str


@dataclass
class Evidence:
    level: str
    generated_at: str
    agents: dict = field(default_factory=dict)
    environment: dict = field(default_factory=dict)
    installed: list = field(default_factory=list)
    stack: dict = field(default_factory=dict)
    commands: list = field(default_factory=list)
    mcp_used: dict = field(default_factory=dict)
    skills_used: list = field(default_factory=list)  # [agent, skill, uses]
    mcp_calls: list = field(default_factory=list)  # [agent, server, calls]
    items: list[Item] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Evidence":
        fields = {k: v for k, v in data.items() if k != "items"}
        return cls(**fields, items=[Item(**i) for i in data.get("items") or []])

    def counts(self) -> Counter:
        return Counter(i.kind for i in self.items)

    def render(self) -> str:
        out = [f"# Evidence pack (level {self.level}, generated {self.generated_at[:10]})", "", "## Agents"]
        for name, info in self.agents.items():
            out.append(f"- {name}: " + ", ".join(f"{k} {v}" for k, v in info.items() if v not in (None, 0, "")))
        env = self.environment
        out += ["", "## Environment", f"- OS: {env.get('os')}",
                f"- Shells seen: {', '.join(env.get('shells') or []) or 'unknown'}",
                f"- Agents: {', '.join(env.get('agents') or [])}"]
        if self.installed:
            out += ["", "## Installed by the user (across agents)"]
            by_kind = defaultdict(set)
            for item in self.installed:
                by_kind[item["kind"]].add(item["name"])
            for kind, names in sorted(by_kind.items()):
                out.append(f"- {kind}: {', '.join(sorted(names))}")
        if self.stack.get("languages"):
            out += ["", "## Stack (from project manifests and file types)",
                    "- Languages: " + ", ".join(f"{l['name']} ({l['projects']} projects)" for l in self.stack["languages"]),
                    "- Dependencies: " + ", ".join(f"{d['name']} ({d['projects']})" for d in self.stack["dependencies"])]
        if self.commands:
            out += ["", "## Programs run in sessions, mostly by agents on the user's behalf (generic shell utilities removed)",
                    ", ".join(f"{c} {n}" for c, n in self.commands)]
        if self.mcp_used:
            out += ["", "## MCP servers called in sessions (by agents)"]
            for group, servers in self.mcp_used.items():
                if servers:
                    out.append(f"- {group}: " + ", ".join(f"{s} {n}" for s, n in servers))
        if self.skills_used:
            out += ["", "## Skills agents loaded in sessions", ", ".join(f"{s} {n}" for _, s, n in self.skills_used)]
        memories = [i for i in self.items if i.kind == "memory"]
        if memories:
            out += ["", "## Memory and instruction files"]
            for i in memories:
                out += ["", f"[{i.ref}] {i.agent} · {i.source}", i.text]
        sessions = [i for i in self.items if i.kind == "session"]
        if sessions:
            out += ["", "## Session excerpts (user messages only, truncated)"]
            for i in sessions:
                head = f"[{i.ref}] {i.when or '?'} · {i.agent}" + (f" · project {i.project}" if i.project else "")
                out += ["", head, i.text]
        return "\n".join(out) + "\n"


def build(level: str = "L1", days: int | None = None, session_budget: int = 40_000,
          memory_budget: int = 40_000, only: str | None = None, progress=None,
          before: datetime | None = None) -> Evidence:
    """`before` rebuilds the pack as it would have looked on that date, for evaluation. Installed tools have no
    install date, so they are left out, and memory files count only if they were last changed before it."""
    rank = LEVELS.index(level)
    note = progress or (lambda msg: None)
    adapters = detected(only)
    ev = Evidence(level, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    reports = {a.id: report(a) for a in adapters}
    for a in adapters:
        r = reports[a.id]
        ev.agents[a.name] = {"sessions": r.sessions, "memory files": len(r.memories) if rank >= 1 else None,
                             "installed": len(r.inventory), "parser": a.sessions_status}
        if before is None:
            ev.installed += [{"agent": i.agent, "kind": i.kind, "name": i.name} for i in r.inventory]
    ev.environment = {"os": sys.platform, "agents": [a.name for a in adapters], "shells": [],
                      "agent_ids": {a.name: a.id for a in adapters}}
    if before is not None:
        ev.environment["as_of"] = before.date().isoformat()

    sessions: list[Session] = []
    if rank >= 2:
        note("reading sessions")
        for _, s in all_sessions(adapters):
            if isinstance(s, Session) and within(s, days) and (before is None or (s.started_at and s.started_at < before)):
                sessions.append(s)
    sig = Signals()
    for s in sessions:
        sig.add(s)

    projects = Counter({p: 0 for p in known_projects(adapters)} if before is None else {})
    projects.update({p: n for p, n in sig.projects.items() if p and Path(p).is_dir()})
    note(f"scanning {len(projects)} projects")
    ev.stack = _stack(projects)

    if rank >= 2:
        ev.commands = notable_commands(sig.commands)
        shells = {"pwsh": "PowerShell", "powershell": "PowerShell", "bash": "bash", "zsh": "zsh", "wsl": "WSL", "cmd": "cmd"}
        ev.environment["shells"] = sorted({shells[c] for c in sig.commands if c in shells}
                                          | ({"PowerShell"} if any(c.startswith(("get-", "set-")) for c in sig.commands) else set()))
        installed_mcp = {i["name"].lower() for i in ev.installed if i["kind"] == "mcp_server"}
        mine = [(s, n) for s, n in sig.mcp.most_common() if s.lower() in installed_mcp]
        host = [(s, n) for s, n in sig.mcp.most_common() if s.lower() not in installed_mcp]
        ev.mcp_used = {"installed by the user": mine, "provided by the agent app": host}
        ev.skills_used = [[agent, skill, n] for (agent, skill), n in sig.skills.most_common()]
        ev.mcp_calls = [[agent, server, n] for (agent, server), n in sig.mcp_by_agent.most_common()]
        if sig.first and sig.last:
            ev.environment["sessions_span"] = [sig.first.date().isoformat(), sig.last.date().isoformat()]

    if before is not None:
        for a in adapters:
            ev.agents[a.name]["sessions"] = sig.by_agent.get(a.id, 0)
            ev.agents[a.name]["installed"] = None
    if rank >= 1:
        docs = [m for a in adapters for m in reports[a.id].memories] + project_docs(set(projects))
        if before is not None:
            docs = [m for m in docs if m.inline is None and _mtime(m) < before.timestamp()]
        ev.items += _memory_items(docs, memory_budget)
    if rank >= 2:
        ev.items += _session_items(sessions, session_budget)
    return ev


def _stack(projects: Counter) -> dict:
    langs: dict[str, list] = defaultdict(lambda: [0, 0])  # name -> [projects, weighted activity]
    deps: dict[str, list] = defaultdict(lambda: [0, 0])
    for path, activity in projects.items():
        stack = scan_project(Path(path))
        weight = 1 + activity
        total = sum(stack.files.values()) or 1
        for lang, n in stack.files.items():
            if n >= 3 or n / total > 0.2:
                langs[lang][0] += 1
                langs[lang][1] += weight * n / total
        for dep in stack.dependencies:
            deps[dep][0] += 1
            deps[dep][1] += weight
    top = lambda d, k: [{"name": n, "projects": v[0]} for n, v in sorted(d.items(), key=lambda kv: -kv[1][1])[:k]]
    return {"projects": len(projects), "languages": top(langs, 12), "dependencies": top(deps, 40)}


def _mtime(doc: MemoryDoc) -> float:
    try:
        return doc.path.stat().st_mtime
    except OSError:
        return float("inf")


_FRONTMATTER = re.compile(r"\A﻿?---\s*\n(.*?)\n---\s*\n", re.S)
# Unfilled agent templates (WorkBuddy, OpenClaw-style SOUL.md / IDENTITY.md) say nothing about the user.
_TEMPLATE = ("Fill this in during your first conversation", "Learn about the person you're helping",
             "This file is yours to evolve", "Workspace template for", "Bootstrapping a workspace manually")
_INDEX_FILES = ("memory.md", "memory_summary.md", "user.md")
# Memory tiers, best first. Notes that describe the person outrank notes on how to work with them and notes
# on single projects. A repo's own instruction file (another project's AGENTS.md) describes code rather than
# the user, so it comes last.
_TIERS = ("instructions", "user", "index", "memory", "feedback", "project", "reference", "persona", "repo")
_CAPS = {"instructions": 4000, "index": 4000, "user": 1500, "feedback": 1200, "memory": 2000, "project": 1200,
         "reference": 800, "persona": 800, "repo": 1500}


def _frontmatter(text: str) -> tuple[dict, str]:
    """Simple `key: value` lines from a leading --- block, nested keys flattened (metadata.type -> type)."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        key, _, value = line.strip().partition(":")
        if value.strip():
            meta.setdefault(key.strip(), value.strip().strip("'\""))
    return meta, text[m.end():]


def _tier(doc: MemoryDoc, meta: dict) -> str:
    if doc.agent == "project":
        return "repo"
    if doc.kind == "instructions" and doc.scope == "global":
        return "instructions"
    if doc.kind == "persona":
        return "persona"
    if meta.get("type") in ("user", "feedback", "project", "reference"):
        return meta["type"]
    return "index" if doc.path.name.lower() in _INDEX_FILES else "memory"


def _memory_items(docs: list[MemoryDoc], budget: int) -> list[Item]:
    picked = []
    for doc in docs:
        try:
            raw = doc.read(limit=20_000).strip()
        except OSError:
            continue
        if not raw or sum(marker in raw for marker in _TEMPLATE) >= 1:
            continue
        meta, body = _frontmatter(raw)
        tier = _tier(doc, meta)
        # A typed memory's one-line description is its best summary, so it leads the excerpt.
        text = (meta["description"] + "\n" + body.strip()) if meta.get("description") else body.strip()
        picked.append((_TIERS.index(tier), -_mtime(doc) if doc.inline is None else 0, tier, doc, text))
    items, used, seen = [], 0, set()
    for _, _, tier, doc, text in sorted(picked, key=lambda p: (p[0], p[1])):
        text = redact(text)
        key = text[:200]
        if key in seen:
            continue
        seen.add(key)
        cap = _CAPS[tier]
        text = text if len(text) <= cap else text[:cap] + " …"
        if used + len(text) > budget:
            continue
        used += len(text)
        source = redact(str(doc.path)) + (" (database entry)" if doc.inline is not None else "")
        items.append(Item(f"M{len(items) + 1}", "memory", doc.agent, source, None, None, text))
    return items


def _clean(msg: str, cap: int) -> str | None:
    text = _PASTED.sub(" ", _PREAMBLE.sub("", msg.strip())).strip()
    if not text or _PASTE.match(text) or text.startswith("<pasted_content"):
        return None
    letters = sum(ch.isalpha() for ch in text[:400])
    if letters < 0.4 * min(len(text), 400):
        return None
    text = re.sub(r"\s+", " ", redact(text))
    return text if len(text) <= cap else text[:cap] + " …"


def _picks(n: int) -> list[int]:
    """Indices of the messages to keep from a session with n user messages: first, then spread out."""
    want = min(n, 1 + n // 20, 5)
    return sorted({round(i * (n - 1) / max(want - 1, 1)) for i in range(want)}) if n else []


def _session_items(sessions: list[Session], budget: int) -> list[Item]:
    by_project: dict[str, list[Session]] = defaultdict(list)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    for s in sorted(sessions, key=lambda s: s.started_at or epoch, reverse=True):
        by_project[s.project or "?"].append(s)
    queues = sorted(by_project.values(), key=len, reverse=True)
    items, used, seen = [], 0, set()
    while queues and used < budget:
        nxt = []
        for queue in queues:  # round-robin over projects, newest session first in each
            s = queue.pop(0)
            if queue:
                nxt.append(queue)
            lines = []
            for idx in _picks(len(s.user_messages)):
                text = _clean(s.user_messages[idx], 300 if not lines else 200)
                key = (text or "")[:80].lower()
                if text and key not in seen:
                    seen.add(key)
                    lines.append(f"- {text}")
            if not lines:
                continue
            # Some agents use the first message as the title; only keep titles that add something.
            title = re.sub(r"\s+", " ", redact(s.title or "").strip().split("\n")[0])[:80]
            if title and (lines[0][2:].lower().startswith(title[:30].lower()) or title.startswith(("#", "<"))):
                title = ""
            body = (f"title: {title}\n" if title else "") + "\n".join(lines)
            if used + len(body) > budget:
                continue
            used += len(body)
            items.append(Item(f"S{len(items) + 1}", "session", s.agent, s.id, redact(s.project) if s.project else None,
                              s.started_at.date().isoformat() if s.started_at else None, body))
        queues = nxt
    return items
