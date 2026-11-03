"""Offline evaluation by time split.

The profile is rebuilt from sessions before a cutoff date, candidates are limited to repos that existed then,
and the picks are checked against what the user took up afterwards. Ground truth comes from sessions in the
window after the cutoff, in two kinds:
  mentioned  a GitHub repo the user named in one of their own messages
  adopted    a package installed or a program run for the first time
Anything already seen before the cutoff is left out, and so are the user's own repos.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from repofit import index as index_mod
from repofit import match
from repofit import profile as profile_mod
from repofit.evidence import build as build_evidence
from repofit.normalize import command_head
from repofit.prompts import SYNTHESIS_SYSTEM, synthesis_user
from repofit.scan import all_sessions, detected
from repofit.signals import GENERIC_COMMANDS, notable_commands

_GH = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
_INSTALL = re.compile(r"\b(?:pip3?|uv pip|python -m pip|pipx|uv tool|npm|pnpm|yarn|bun|cargo|go|winget|brew|conda)\s+"
                      r"(?:install|i|add|get)\s+(?:-{1,2}[\w-]+(?:[= ]\S+)?\s+)*([@A-Za-z][\w./@\[\]=<>~-]*)")
_NOT_OWNERS = {"repos", "users", "orgs", "topics", "sponsors", "features", "marketplace", "apps", "settings", "login",
               "search", "notifications", "collections", "trending"}


def eval_dir(cutoff: str) -> Path:
    return profile_mod.default_path().parent / "eval" / cutoff


def _package(raw: str) -> list[str]:
    """Names a package spec could go by: `torch==2.1` -> torch, `@scope/cli` -> scope and cli."""
    if "://" in raw or raw.endswith((".txt", ".toml", ".whl")):
        return []
    if raw.startswith("@") and "/" in raw:
        scope, _, rest = raw[1:].partition("/")
        return [scope, re.split(r"@", rest)[0]]
    name = re.split(r"[=<>~\[@]", raw)[0]
    return [name] if "/" not in name or name.startswith("github.com/") else []


def signals(sessions) -> dict[tuple, dict]:
    """First sighting of every repo, package and program, keyed by (kind, normalized name)."""
    found: dict[tuple, dict] = {}

    def see(kind: str, name: str, raw: str, when, source: str) -> None:
        key = (kind, name)
        entry = found.setdefault(key, {"first": when, "raw": raw, "sources": set()})
        entry["first"] = min(entry["first"], when)
        entry["sources"].add(source)

    for s in sessions:
        when = s.started_at.date()
        texts = [("message", t) for t in s.user_messages] + [("tool", c.detail or "") for c in s.tool_calls]
        for source, text in texts:
            for owner, name in _GH.findall(text):
                name = re.sub(r"\.git$", "", name.rstrip(".)"))
                if owner.lower() not in _NOT_OWNERS:
                    see("repo", f"{owner}/{name}".lower(), f"{owner}/{name}", when, source)
            if source == "tool":
                for raw in _INSTALL.findall(text):
                    for name in _package(raw):
                        see("package", match._norm(name.split("/")[-1]), raw, when, source)
        for c in s.tool_calls:
            if c.kind == "shell" and c.detail:
                head = command_head(c.detail)
                words = (head or "").split()
                # `npm run build` runs a project script, and a head like `python.` is a parsing leftover.
                if (not head or head in GENERIC_COMMANDS or (len(words) > 1 and words[1] == "run")
                        or not re.fullmatch(r"[\w+-]+", words[-1])):
                    continue
                see("program", match._norm(words[-1]), head, when, "tool")
    return found


def ground_truth(sessions, cutoff: datetime, window_days: int, repos: list[dict], profile: dict,
                 own: set[str]) -> list[dict]:
    end = cutoff + timedelta(days=window_days)
    before = signals([s for s in sessions if s.started_at < cutoff])
    after = signals([s for s in sessions if cutoff <= s.started_at < end])
    by_name = {r["name"].lower(): r for r in repos}
    aliases = {"package": match.aliases(repos, stripped=True), "program": match.aliases(repos, stripped=False)}
    known = match.known_names(profile)
    seen_names = {k[1] for k in before} | {k[1].split("/")[-1] for k in before if k[0] == "repo"}
    # Programs worth counting are the distinctive ones; `python` or `git` would match half the index.
    distinctive = {match._norm(c.split()[-1]) for c, _ in notable_commands(
        Counter({v["raw"]: 2 for k, v in after.items() if k[0] == "program"}), limit=10_000)}
    truth: dict[str, dict] = {}
    for (kind, name), info in sorted(after.items(), key=lambda kv: kv[1]["first"]):
        if (kind, name) in before:
            continue
        if kind == "repo":
            if "message" not in info["sources"] or name.split("/")[0] in own:
                continue
            repo, label = by_name.get(name), "mentioned"
            if repo is None:
                truth.setdefault(name, {"repo": info["raw"], "kind": label, "first": str(info["first"]),
                                        "signal": info["raw"], "in_index": False})
                continue
        else:
            if kind == "program" and name not in distinctive:
                continue
            repo, label = aliases[kind].get(name), "adopted"
            if repo is None:
                continue
        key = repo["name"].lower()
        short = match._norm(key.split("/")[1])
        if key in truth or short in seen_names or match.already_used(repo, known):
            continue
        truth[key] = {"repo": repo["name"], "kind": label, "first": str(info["first"]), "signal": info["raw"],
                      "in_index": True}
    return list(truth.values())


def prepare(cutoff: str, window_days: int = 60, own: set[str] | None = None, progress=None) -> dict:
    note = progress or (lambda msg: None)
    when = datetime.fromisoformat(cutoff).replace(tzinfo=timezone.utc)
    out = eval_dir(cutoff)
    out.mkdir(parents=True, exist_ok=True)
    ev = build_evidence("L2", before=when, progress=note)
    prof = profile_mod.from_evidence(ev)
    path = out / "profile.json"
    profile_mod.save(prof, path)
    profile_mod.save_evidence(ev, path)
    (out / "profile_prompt.md").write_text(SYNTHESIS_SYSTEM + "\n---\n\n" + synthesis_user(ev.render()), encoding="utf-8")
    full = index_mod.load(index_mod.default_path())
    idx = {**full, "repos": [r for r in full["repos"] if r.get("created") and r["created"] < cutoff]}
    index_mod.save(idx, out / "index.json.gz")
    note("collecting what came after the cutoff")
    sessions = [s for _, s in all_sessions(detected()) if not isinstance(s, Exception) and s.started_at]
    truth = ground_truth(sessions, when, window_days, idx["repos"], prof, {o.lower() for o in own or ()})
    meta = {"cutoff": cutoff, "window_days": window_days, "own_owners": sorted(own or ()),
            "sessions_before": sum(s.started_at < when for s in sessions),
            "sessions_after": sum(when <= s.started_at < when + timedelta(days=window_days) for s in sessions),
            "index_repos": len(idx["repos"]), "truth": truth}
    (out / "ground_truth.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def report(cutoff: str, k: int = 10, pool: int = 40) -> dict:
    out = eval_dir(cutoff)
    meta = json.loads((out / "ground_truth.json").read_text(encoding="utf-8"))
    prof = profile_mod.load(out / "profile.json")
    idx = index_mod.load(out / "index.json.gz")
    truth = [t for t in meta["truth"] if t["in_index"]]
    wanted = {t["repo"].lower(): t for t in truth}
    known = match.known_names(prof)
    usable = [r for r in idx["repos"] if not match.already_used(r, known)]

    arms: dict[str, list[str]] = {}
    arms["popular (most stars)"] = [r["name"] for r in sorted(usable, key=lambda r: -r["stars"])[:pool]]
    raw = {**prof, "synthesis": None}
    arms["retrieval, programs only"] = [c.repo["name"] for c in match.candidates(raw, idx, pool)[0]]
    plain = {**prof, "expansions": None}
    if prof.get("synthesis"):
        arms["retrieval, profile"] = [c.repo["name"] for c in match.candidates(plain, idx, pool)[0]]
    if prof.get("expansions"):
        arms["retrieval, profile + expansion"] = [c.repo["name"] for c in match.candidates(prof, idx, pool)[0]]
    recs = out / "recommendations.json"
    if recs.is_file():
        result = json.loads(recs.read_text(encoding="utf-8"))
        if result.get("ranked_by"):
            arms[f"ranked by {result['ranked_by']}"] = [p["name"] for p in result["picks"]]
    # Where each true repo sits in a deep retrieval list tells whether retrieval or ranking lost it.
    deep = {label: {c.repo["name"].lower(): c.seat + 1 for c in match.candidates(p, idx, 3000, per_query=400)[0]}
            for label, p in (("plain", plain), ("expanded", prof))
            if p.get("synthesis") and (label == "plain" or p.get("expansions"))}
    rows = []
    for name, ranked in arms.items():
        top = [r.lower() for r in ranked[:k]]
        hits = [wanted[r] for r in top if r in wanted]
        pool_hits = sum(r.lower() in wanted for r in ranked[:pool])
        rows.append({"arm": name, f"hits@{k}": len(hits), f"hits@{pool}": pool_hits if len(ranked) >= pool else None,
                     "mentioned": sum(h["kind"] == "mentioned" for h in hits),
                     "adopted": sum(h["kind"] == "adopted" for h in hits),
                     "which": [r for r in ranked[:pool] if r.lower() in wanted]})
    for t in truth:
        for label, ranked in deep.items():
            t[f"rank_{label}"] = ranked.get(t["repo"].lower())
    return {"cutoff": cutoff, "k": k, "pool": pool, "truth_in_index": len(truth),
            "truth_outside_index": sum(not t["in_index"] for t in meta["truth"]), "index_repos": len(idx["repos"]),
            "sessions_before": meta["sessions_before"], "sessions_after": meta["sessions_after"],
            "rows": rows, "truth": truth}


def render(rep: dict) -> str:
    k, pool = rep["k"], rep["pool"]
    out = [f"Time-split evaluation · cutoff {rep['cutoff']} · {rep['sessions_before']} sessions before, "
           f"{rep['sessions_after']} after · {rep['index_repos']:,} repos existed at the cutoff",
           f"Ground truth: {rep['truth_in_index']} repos in the index "
           f"({rep['truth_outside_index']} more the user mentioned are outside it and cannot be recommended)", ""]
    width = max(len(r["arm"]) for r in rep["rows"])
    out.append(f"{'arm'.ljust(width)}  hits@{k}  (mentioned/adopted)  hits@{pool}")
    for r in rep["rows"]:
        deep = "-" if r[f"hits@{pool}"] is None else str(r[f"hits@{pool}"])
        out.append(f"{r['arm'].ljust(width)}  {r[f'hits@{k}']:>6}  ({r['mentioned']}/{r['adopted']}){'':13}{deep:>6}"
                   + (f"   {', '.join(r['which'])}" if r["which"] else ""))
    labels = [l for l in ("plain", "expanded") if any(f"rank_{l}" in t for t in rep["truth"])]
    out += ["", "Ground truth, and the smallest retrieval pool that would hold it (" + " / ".join(
        {"plain": "without expansion", "expanded": "with expansion"}[l] for l in labels) + "):"]
    for t in rep["truth"]:
        ranks = " / ".join(f"#{t[f'rank_{l}']}" if t.get(f"rank_{l}") else "-" for l in labels)
        out.append(f"  {t['first']}  {t['kind']:<9} {t['repo']:<45} {ranks:>13}   (signal: {t['signal']})")
    return "\n".join(out)
