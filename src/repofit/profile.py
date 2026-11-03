"""The profile: a local JSON file the user can read and edit, and the recommender's only input."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from repofit.evidence import Evidence
from repofit.paths import HOME, env_path
from repofit.prompts import SYNTHESIS_KEYS

PROFILE_VERSION = 1


def default_path() -> Path:
    return env_path("REPOFIT_HOME", HOME / ".repofit") / "profile.json"


def from_evidence(ev: Evidence) -> dict:
    """Everything that needs no model: sources, environment, installed tools, stack, programs run."""
    return {
        "version": PROFILE_VERSION,
        "generated_at": ev.generated_at,
        "level": ev.level,
        "sources": ev.agents,
        "environment": ev.environment,
        "installed": ev.installed,
        "stack": ev.stack,
        "programs": [{"name": c, "runs": n} for c, n in ev.commands],
        "mcp_used": {group: [{"name": s, "calls": n} for s, n in servers] for group, servers in ev.mcp_used.items()},
        "skills_used": [{"agent": a, "name": s, "uses": n} for a, s, n in ev.skills_used],
        "mcp_calls": [{"agent": a, "name": s, "calls": n} for a, s, n in ev.mcp_calls],
        "synthesis": None,
        "evidence": {},
    }


def apply_synthesis(profile: dict, ev: Evidence | None, synthesis: dict, model: str | None) -> dict:
    """Merge a model's answer into the profile, keeping only refs that exist in the pack."""
    clean = {k: synthesis.get(k) for k in SYNTHESIS_KEYS}
    for key in SYNTHESIS_KEYS:
        if key in ("summary",):
            clean[key] = str(clean[key] or "").strip()
        elif key == "roles":
            clean[key] = [str(r) for r in clean[key] or [] if r]
        else:
            clean[key] = [e for e in clean[key] or [] if isinstance(e, dict)]
    items = {i.ref: i for i in ev.items} if ev else {}
    for key in SYNTHESIS_KEYS[2:]:
        for entry in clean[key]:
            entry["evidence"] = [r for r in entry.get("evidence") or [] if not items or r in items]
    # Enforced here rather than trusted to the model: a recurring task needs two sessions or a memory file.
    clean["recurring_tasks"] = [
        t for t in clean["recurring_tasks"]
        if sum(r.startswith("S") for r in t["evidence"]) >= 2 or any(r.startswith("M") for r in t["evidence"])
    ]
    cited = Counter(r for key in SYNTHESIS_KEYS[2:] for e in clean[key] for r in e["evidence"])
    profile["synthesis"] = clean
    profile["synthesized_by"] = model
    profile["synthesized_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if items:
        profile["evidence"] = {
            ref: {"kind": items[ref].kind, "agent": items[ref].agent, "source": items[ref].source,
                  "project": items[ref].project, "when": items[ref].when,
                  "excerpt": _excerpt(items[ref])}
            for ref in sorted(cited, key=lambda r: (r[0], int(r[1:]) if r[1:].isdigit() else 0))
        }
    return profile


def _excerpt(item) -> str:
    """The quote shown next to a pick. Session items are bulleted messages in the pack; the quote drops the
    bullets and the title line so it reads as what the user said."""
    if item.kind != "session":
        return item.text.replace("\n", " ")[:160]
    lines = [line.removeprefix("- ").strip() for line in item.text.splitlines() if not line.startswith("title: ")]
    return " / ".join(line for line in lines if line)[:160]


def save(profile: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_evidence(ev: Evidence, profile_path: Path) -> Path:
    """The pack a synthesis was written from lives next to the profile, so refs can be checked later."""
    path = profile_path.with_name("evidence.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ev.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_evidence(profile_path: Path) -> Evidence | None:
    path = profile_path.with_name("evidence.json")
    return Evidence.from_dict(json.loads(path.read_text(encoding="utf-8"))) if path.is_file() else None


def render(profile: dict) -> str:
    out = []
    when = profile.get("generated_at", "")[:10]
    by = profile.get("synthesized_by")
    out.append(f"Profile · level {profile.get('level')} · {when}" + (f" · synthesized by {by}" if by else ""))
    sources = ", ".join(f"{name} ({info.get('sessions') or 0} sessions)" for name, info in (profile.get("sources") or {}).items())
    out.append(f"Sources: {sources}")
    env = profile.get("environment") or {}
    out.append(f"Environment: {env.get('os')}" + (f"; shells {', '.join(env['shells'])}" if env.get("shells") else ""))
    syn = profile.get("synthesis")
    if syn:
        if syn.get("summary"):
            out += ["", syn["summary"]]
        if syn.get("roles"):
            out.append("Roles: " + ", ".join(syn["roles"]))
        sections = (("Interests", "interests", "topic"), ("Recurring tasks", "recurring_tasks", "task"),
                    ("Preferences", "preferences", "text"), ("Constraints", "constraints", "text"))
        for title, key, field in sections:
            entries = syn.get(key) or []
            if not entries:
                continue
            out += ["", f"{title}:"]
            for e in entries:
                refs = f"  [{', '.join(e.get('evidence') or [])}]" if e.get("evidence") else ""
                out.append(f"  - {e.get(field)}{refs}")
                if key == "recurring_tasks":
                    if e.get("how_now"):
                        out.append(f"      now: {e['how_now']}")
                    if e.get("pain"):
                        out.append(f"      pain: {e['pain']}")
        if syn.get("stack"):
            out += ["", "Stack: " + ", ".join(s.get("name", "") for s in syn["stack"])]
    else:
        out += ["", "(No synthesis yet: interests, tasks and preferences need a model. See `repofit profile build --help`.)"]
    stack = profile.get("stack") or {}
    if stack.get("languages"):
        out += ["", "Languages in projects: " + ", ".join(f"{l['name']} ({l['projects']})" for l in stack["languages"])]
    if stack.get("dependencies"):
        out.append("Top dependencies: " + ", ".join(d["name"] for d in stack["dependencies"][:20]))
    if profile.get("programs"):
        out.append("Programs run: " + ", ".join(f"{p['name']} {p['runs']}" for p in profile["programs"][:20]))
    kinds = Counter(i["kind"] for i in profile.get("installed") or [])
    if kinds:
        out.append("Installed: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())))
    return "\n".join(out)
