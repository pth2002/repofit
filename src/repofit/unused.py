"""Installed MCP servers and skills that no session ever called, read straight from the agents' own files.

It needs no profile and no model, so it is the first thing repofit can show on a new machine. A tool counts as
unused only for agents whose session logs show which server or skill each call went to; the rest are reported
as not checked, so a missing log never turns into a false "unused".
"""
from __future__ import annotations

import textwrap
from collections import defaultdict

from repofit.match import MCP_TRACKED, SKILL_TRACKED, _norm
from repofit.model import Session
from repofit.scan import all_sessions, detected, report
from repofit.signals import Signals, within

TRACKED = {"mcp_server": MCP_TRACKED, "skill": SKILL_TRACKED}
LABELS = {"mcp_server": ("MCP server", "MCP servers"), "skill": ("skill", "skills")}
REPO_URL = "https://github.com/pth2002/repofit"
WIDTH = 92


def collect(only: str | None = None, days: int | None = None, progress=None) -> dict:
    note = progress or (lambda msg: None)
    adapters = detected(only)
    names = {a.id: a.name for a in adapters}
    installed = [i for a in adapters for i in report(a).inventory if i.kind in TRACKED]
    note("reading sessions")
    sig = Signals()
    for _, s in all_sessions(adapters):
        if isinstance(s, Session) and within(s, days):
            sig.add(s)
    called = ({("mcp_server", a, _norm(srv)) for a, srv in sig.mcp_by_agent}
              | {("skill", a, _norm(skill)) for a, skill in sig.skills})
    result = {"agent": only, "days": days, "sessions": {names[a]: sig.by_agent.get(a, 0) for a in names},
              "unused": [], "used": [], "not_checked": []}
    seen = set()
    for item in installed:
        key = (item.kind, item.agent, _norm(item.name))
        if key in seen:
            continue
        seen.add(key)
        row = {"agent": names.get(item.agent, item.agent), "kind": item.kind, "name": item.name}
        if item.agent not in TRACKED[item.kind] or not sig.by_agent.get(item.agent):
            result["not_checked"].append(row)
        elif key in called:
            result["used"].append(row)
        else:
            result["unused"].append(row)
    return result


def _names(names: list[str], limit: int | None) -> str:
    names = sorted(names, key=str.lower)
    if limit is None or len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" and {len(names) - limit} more"


def _label(kind: str, n: int) -> str:
    return LABELS[kind][n != 1]


def render(result: dict, limit: int | None = 10, footer: bool = True) -> str:
    sessions = {a: n for a, n in result["sessions"].items() if n}
    if not result["sessions"]:
        if result.get("agent"):
            return f"No data from {result['agent']} found on this machine."
        return "No supported agents found on this machine."
    total = sum(sessions.values())
    span = f" from the last {result['days']} days" if result.get("days") else ""
    if total:
        out = [f"Read {total:,} sessions{span} from {', '.join(sessions)}. Nothing left this machine.", ""]
    else:
        out = [f"Found no sessions{span} to check against.", ""]
    counted = len(result["unused"]) + len(result["used"])
    if not counted:
        out.append("None of the installed MCP servers or skills can be checked against these sessions.")
    elif not result["unused"]:
        out.append(f"Every installed MCP server and skill that can be checked ({counted}) was called at least once.")
    else:
        out += [f"{len(result['unused'])} of {counted} installed MCP servers and skills were never called:", ""]
        groups: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for u in result["unused"]:
            groups[u["agent"]][u["kind"]].append(u["name"])
        for agent, kinds in groups.items():
            out.append(f"  {agent} · {sessions.get(agent, 0):,} sessions")
            for kind in ("mcp_server", "skill"):
                if kinds.get(kind):
                    out.append(textwrap.fill(_names(kinds[kind], limit), WIDTH, break_on_hyphens=False,
                                             initial_indent=f"    {_label(kind, len(kinds[kind])):<12} ",
                                             subsequent_indent=" " * 17))
        out += ["", "Each one still costs context on every request. Remove the ones you do not need."]
    if result["not_checked"]:
        by_agent = defaultdict(int)
        for row in result["not_checked"]:
            by_agent[row["agent"]] += 1
        n = len(result["not_checked"])
        tools = "tool" if n == 1 else "tools"
        if len(by_agent) == 1:
            out += ["", f"Not checked: {n} {tools} in {next(iter(by_agent))}. Its logs do not say which server or skill "
                    "each call went to, or hold no sessions yet."]
        else:
            listed = ", ".join(f"{agent} ({k})" for agent, k in by_agent.items())
            out += ["", f"Not checked: {n} {tools} in {listed}. Their logs do not say which server or skill each call "
                    "went to, or hold no sessions yet."]
    if footer:
        out += ["", f"Want GitHub projects picked from the same history? Connect `repofit mcp` to your agent: {REPO_URL}"]
    # Prose lines wrap to fit a terminal; the indented tool lists are already wrapped above.
    return "\n".join(line if len(line) <= WIDTH or line.startswith("    ")
                     else textwrap.fill(line, WIDTH, break_on_hyphens=False) for line in out)
