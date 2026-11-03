"""Matching a profile against the index.

Retrieval is plain BM25 over each repo's name, description, topics and README opening. Every recurring
task and interest in the profile is one query, so a candidate always knows which part of the profile
pulled it in. Repos the user already runs are set aside by name before any model sees the list.
"""

from __future__ import annotations

import heapq
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_STOP = set("""
a an and are as at be been but by can could do does for from had has have how i if in into is it its just
let me more most my no not now of on one only or our out over so some such than that the their them then
there these they this those to too up us very was we were what when where which while who why will with
would you your yours also any all each every other another via per etc eg ie use used uses using user users
based make makes made get gets got new like way ways well want need needs help helps easy easily simple
quick fast powerful open source free lets let's things thing stuff lot lots
""".split())
_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#]*")
# Name parts that say how a project ships rather than what it is.
_PACKAGING = {"mcp", "server", "cli", "py", "js", "python", "node", "rs", "go", "app", "the"}
SKILL_TRACKED = {"claude-code", "codex", "hermes"}  # agents whose sessions show which skills ran


def tokens(text: str) -> list[str]:
    out = []
    for word in _TOKEN.findall(text.lower()):
        if word in _STOP or len(word) < 2:
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.append(word)
    return out


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(d) for d in docs]
        self.length = [len(d) for d in docs]
        self.avg = sum(self.length) / max(len(docs), 1)
        df = Counter(t for tf in self.tf for t in tf)
        n = len(docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self.postings: dict[str, list[int]] = defaultdict(list)
        for i, tf in enumerate(self.tf):
            for t in tf:
                self.postings[t].append(i)

    def search(self, query: Counter, k: int) -> list[tuple[int, float]]:
        scores: dict[int, float] = defaultdict(float)
        for term, weight in query.items():
            idf = self.idf.get(term)
            if not idf:
                continue
            for i in self.postings[term]:
                f = self.tf[i][term]
                norm = 1 - self.b + self.b * self.length[i] / self.avg
                scores[i] += weight * idf * f * (self.k1 + 1) / (f + self.k1 * norm)
        return heapq.nlargest(k, scores.items(), key=lambda kv: kv[1])

    def score(self, query: Counter, doc: list[str]) -> float:
        """Score a document that may not be in the collection, using the collection's statistics."""
        tf, norm = Counter(doc), 1 - self.b + self.b * len(doc) / self.avg
        return sum(w * self.idf[t] * tf[t] * (self.k1 + 1) / (tf[t] + self.k1 * norm)
                   for t, w in query.items() if t in self.idf and tf[t])


def doc_tokens(repo: dict) -> list[str]:
    name = repo["name"].split("/")[1]
    topics = " ".join(t.replace("-", " ") for t in repo.get("topics") or [])
    head = f"{name} {name} {repo.get('description') or ''} {repo.get('description') or ''} {topics} {topics}"
    return tokens(head + " " + (repo.get("readme") or ""))


@dataclass
class Query:
    ref: str  # T1, I2, or P1 for the fallback queries
    text: str
    weight: float
    terms: Counter = field(default_factory=Counter)


def queries(profile: dict) -> list[Query]:
    """One query per recurring task and interest. Task words count double, how-now and pain words once."""
    syn = profile.get("synthesis") or {}
    out = []
    for i, task in enumerate(syn.get("recurring_tasks") or [], 1):
        q = Query(f"T{i}", task.get("task") or "", min(1.5, 1.0 + 0.1 * max(len(task.get("evidence") or []) - 2, 0)))
        q.terms.update({t: 2 for t in tokens(q.text)})
        q.terms.update(t for t in tokens(f"{task.get('how_now') or ''} {task.get('pain') or ''}") if t not in q.terms)
        out.append(q)
    for i, interest in enumerate(syn.get("interests") or [], 1):
        q = Query(f"I{i}", interest.get("topic") or "", 0.7)
        q.terms.update(tokens(q.text))
        out.append(q)
    if not out:  # no synthesis yet: the distinctive programs are the best clue left
        for i, prog in enumerate((profile.get("programs") or [])[:10], 1):
            name = prog["name"].split()[-1]
            out.append(Query(f"P{i}", name, 0.5, Counter(tokens(name))))
    return [q for q in out if q.terms]


def known_names(profile: dict) -> dict[str, str]:
    """Normalized names of things the user already has, mapped to how we know."""
    known: dict[str, str] = {}

    def put(name: str, why: str) -> None:
        key = _norm(name)
        if len(key) >= 2:
            known.setdefault(key, why)

    for agent in profile.get("sources") or {}:
        put(agent, f"you use {agent}")
    for prog in profile.get("programs") or []:
        words = prog["name"].split()
        if len(words) > 1 and words[1] == "run":  # npm run dev: a script name, not a program
            continue
        put(words[-1], f"you ran `{prog['name']}` {prog['runs']} times")
    for dep in (profile.get("stack") or {}).get("dependencies") or []:
        put(dep["name"], f"a dependency in {dep['projects']} of your projects")
    for entry in ((profile.get("synthesis") or {}).get("stack") or []):
        # A language name would match repos such as TheAlgorithms/Python, which nobody "uses".
        if entry.get("name") and entry.get("kind") != "language":
            put(entry["name"], "part of your stack")
    for item in profile.get("installed") or []:
        put(item["name"], f"installed in {item['agent']} as a {item['kind'].replace('_', ' ')}")
    return known


def already_used(repo: dict, known: dict[str, str]) -> str | None:
    owner, name = repo["name"].lower().split("/")
    parts = [p for p in re.split(r"[-_.]", name) if p]
    variants = {_norm(name), _norm("".join(p for p in parts if p not in _PACKAGING))}
    if owner == name or _norm(owner) in variants:
        variants.add(_norm(owner))
    for v in variants:
        if v in known:
            return known[v]
    return None


# Names too generic to tie to one repo: `run`, `eval` or `src` would each match some repo in the index.
GENERIC_NAMES = {"python", "node", "run", "src", "script", "scripts", "eval", "assert", "demo", "test", "tests", "code",
                 "app", "main", "core", "api", "docs", "data", "web", "server", "client", "tools", "utils", "lib", "bin",
                 "build", "dev", "start", "check", "lint", "format", "clean", "install", "setup", "init", "config",
                 "debug", "serve", "watch", "deploy", "cmp", "github", "git", "go", "java", "rust"}


def aliases(repos: list[dict], stripped: bool) -> dict[str, dict]:
    """Normalized short names that point at a repo; the most starred repo wins a shared name.
    `stripped` also accepts the name without packaging words (three.js -> three), which suits package and
    project names but not program names: the program `code` is VS Code, not coder/code-server."""
    out: dict[str, dict] = {}
    for r in sorted(repos, key=lambda r: r["stars"]):
        name = r["name"].lower().split("/")[1]
        parts = [p for p in re.split(r"[-_.]", name) if p]
        names = {_norm(name)}
        if stripped:
            names.add(_norm("".join(p for p in parts if p not in _PACKAGING)))
        for alias in names:
            if len(alias) >= 2 and alias not in GENERIC_NAMES:
                out[alias] = r
    return out


def expansion_refs(profile: dict) -> dict[str, str]:
    return {ref: entry.get("task") or entry.get("topic") or "" for ref, entry in _refs(profile).items()}


def apply_expansion(profile: dict, answer: dict, by: str | None) -> dict:
    """Keep expansions only for real profile entries, with short lists of plain strings."""
    refs = expansion_refs(profile)
    clean = {}
    for ref, entry in (answer or {}).items():
        if ref not in refs or not isinstance(entry, dict):
            continue
        clean[ref] = {key: [str(x).strip() for x in entry.get(key) or [] if str(x).strip()][:limit]
                      for key, limit in (("projects", 10), ("keywords", 8))}
    profile["expansions"] = clean
    profile["expanded_by"] = by
    return profile


def expansion_input(profile: dict) -> str:
    return "\n".join(profile_brief(profile)) + "\n"


@dataclass
class Candidate:
    repo: dict
    score: float
    matched: list[str]  # query refs that pulled this repo in
    seat: int = 0  # order of entry into the pool: the smallest pool that still holds this repo, minus one


def candidates(profile: dict, index: dict, pool: int = 40, per_query: int = 30) -> tuple[list[Candidate], list[dict]]:
    """Shortlist for the ranker, plus the repos set aside because the user already has them."""
    repos = index["repos"]
    known = known_names(profile)
    in_use, usable = [], []
    for r in repos:
        why = already_used(r, known)
        if why:
            in_use.append({"name": r["name"], "stars": r["stars"], "why": why})
        else:
            usable.append(r)
    engine = BM25([doc_tokens(r) for r in usable])
    novel = [is_new(r, index) for r in usable]
    expansions = profile.get("expansions") or {}
    position = {r["name"]: i for i, r in enumerate(usable)}
    by_alias = aliases(usable, stripped=True) if expansions else {}
    totals: dict[int, float] = defaultdict(float)
    strength: dict[int, dict[str, float]] = defaultdict(dict)
    ranked: dict[str, list[int]] = {}
    qs = queries(profile)
    for q in qs:
        # A profile entry is searched by its own words, by the model's search phrases for it, and by the project
        # names the model listed. A repo keeps its best match across these.
        merged: dict[int, float] = {}
        extra = expansions.get(q.ref) or {}
        searches = [(q.terms, 1.0)] + [(Counter(tokens(k)), 0.7) for k in extra.get("keywords") or []]
        for terms, factor in searches:
            hits = engine.search(terms, per_query) if terms else []
            for i, s in hits:
                merged[i] = max(merged.get(i, 0.0), factor * s / hits[0][1])
        for name in extra.get("projects") or []:
            repo = by_alias.get(_norm(name))
            if repo:
                merged[position[repo["name"]]] = 1.0
        if not merged:
            continue
        ranked[q.ref] = sorted(merged, key=lambda i: (-merged[i], -novel[i], -usable[i]["stars"]))
        for i, s in merged.items():
            strength[i][q.ref] = s
    weight = {q.ref: q.weight for q in qs}
    for i, refs in strength.items():
        # A repo is scored by the profile entry it serves best. Summing every entry instead would let a generic
        # repo that matches all of them weakly outrank one that matches a single need exactly.
        parts = sorted((weight[r] * s for r, s in refs.items()), reverse=True)
        totals[i] = parts[0] + 0.15 * sum(parts[1:])
        # A mild nudge towards adopted projects, so a 30-star clone does not outrank the original, and a
        # larger one towards new or trending projects, which the user is least likely to know already.
        totals[i] *= 0.8 + 0.2 * min(math.log10(max(usable[i]["stars"], 10)) / 5, 1.0)
        totals[i] *= 1.15 if novel[i] else 1.0
    chosen: list[int] = []
    order = sorted(qs, key=lambda q: -q.weight)
    for depth in range(3):  # entries take turns, so every task and interest gets a seat before any gets a third
        for q in order:
            fresh = [i for i in ranked.get(q.ref, []) if i not in chosen]
            if len(chosen) < pool and len(fresh) > 0 and depth < len(ranked.get(q.ref, [])):
                chosen.append(fresh[0])
    for i, _ in sorted(totals.items(), key=lambda kv: -kv[1]):
        if len(chosen) >= pool:
            break
        if i not in chosen:
            chosen.append(i)
    seat = {i: n for n, i in enumerate(chosen[:pool])}
    chosen = sorted(chosen[:pool], key=lambda i: -totals[i])
    in_use.sort(key=lambda r: -r["stars"])
    # The one or two parts of the profile that pulled a repo in most strongly; weaker overlaps are noise.
    matched = {i: [r for r, s in sorted(strength[i].items(), key=lambda kv: -kv[1]) if s >= 0.5][:2] for i in chosen}
    return [Candidate(usable[i], round(totals[i], 3), matched[i], seat[i]) for i in chosen], in_use


def unused_tools(profile: dict) -> list[dict]:
    """Installed MCP servers and skills that no session called. Needs a level L2 profile."""
    if profile.get("level") != "L2":
        return []
    ids = (profile.get("environment") or {}).get("agent_ids") or {}
    sessions = {ids.get(name, name): info.get("sessions") or 0 for name, info in (profile.get("sources") or {}).items()}
    mcp = {(c["agent"], _norm(c["name"])) for c in profile.get("mcp_calls") or []}
    skills = {(s["agent"], _norm(s["name"])) for s in profile.get("skills_used") or []}
    out, seen = [], set()
    for item in profile.get("installed") or []:
        agent, kind, name = item["agent"], item["kind"], item["name"]
        key = (agent, kind, _norm(name))
        if key in seen or not sessions.get(agent):
            continue
        seen.add(key)
        if kind == "mcp_server" and (agent, _norm(name)) not in mcp:
            out.append({"agent": agent, "kind": kind, "name": name, "sessions": sessions[agent]})
        elif kind == "skill" and agent in SKILL_TRACKED and (agent, _norm(name)) not in skills:
            out.append({"agent": agent, "kind": kind, "name": name, "sessions": sessions[agent]})
    return out


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def ranker_input(profile: dict, cands: list[Candidate], readme_chars: int = 500) -> str:
    out = profile_brief(profile) + ["", "# Candidates"]
    for n, c in enumerate(cands, 1):
        out += [""] + repo_brief(c.repo, f"C{n}", readme_chars)
        if c.matched:
            out.append("retrieved for: " + ", ".join(c.matched))
    return "\n".join(out) + "\n"


def is_new(repo: dict, index: dict, days: int = 365) -> bool:
    """Created within a year of the index build, or seen on GitHub trending when the index was built."""
    if repo.get("trending"):
        return True
    built = (index.get("built_at") or "")[:10]
    created = repo.get("created") or ""
    if not built or not created:
        return False
    return (datetime.fromisoformat(built) - datetime.fromisoformat(created)).days <= days


def _trend(repo: dict) -> str:
    gained = repo.get("trending") or {}
    for period, label in (("monthly", "this month"), ("weekly", "this week"), ("daily", "today")):
        if gained.get(period):
            return f"trending +{_k(gained[period])} stars {label}"
    return ""


def repo_brief(r: dict, ref: str | None = None, readme_chars: int = 500) -> list[str]:
    meta = [f"{_k(r['stars'])} stars", r.get("language") or "", r.get("license") or "",
            f"created {r['created'][:7]}" if r.get("created") else "", f"last push {r.get('pushed')}", _trend(r),
            "ARCHIVED" if r.get("archived") else ""]
    out = [(f"[{ref}] " if ref else "") + f"{r['name']} · " + " · ".join(m for m in meta if m)]
    if r.get("topics"):
        out.append("topics: " + ", ".join(r["topics"]))
    if r.get("description"):
        out.append(r["description"])
    if r.get("readme"):
        text = r["readme"]
        out.append("readme: " + (text if len(text) <= readme_chars else text[:readme_chars].rsplit(" ", 1)[0] + " …"))
    return out


def profile_brief(profile: dict) -> list[str]:
    """The profile as the ranker reads it: tasks and interests numbered so answers can cite them."""
    syn = profile.get("synthesis") or {}
    env = profile.get("environment") or {}
    stack = profile.get("stack") or {}
    out = ["# Developer"]
    if syn.get("summary"):
        out.append(f"Summary: {syn['summary']}")
    if syn.get("roles"):
        out.append("Roles: " + ", ".join(syn["roles"]))
    out.append(f"OS: {env.get('os')}" + (f"; shells: {', '.join(env['shells'])}" if env.get("shells") else ""))
    if stack.get("languages"):
        out.append("Languages: " + ", ".join(f"{l['name']} ({l['projects']} projects)" for l in stack["languages"][:8]))
    if stack.get("dependencies"):
        out.append("Main dependencies: " + ", ".join(d["name"] for d in stack["dependencies"][:20]))
    for title, key, field_, prefix in (("Recurring tasks", "recurring_tasks", "task", "T"), ("Interests", "interests", "topic", "I")):
        entries = syn.get(key) or []
        if entries:
            out += ["", f"## {title}"]
            for i, e in enumerate(entries, 1):
                out.append(f"[{prefix}{i}] {e.get(field_)}")
                if key == "recurring_tasks":
                    if e.get("how_now"):
                        out.append(f"  now: {e['how_now']}")
                    if e.get("pain"):
                        out.append(f"  pain: {e['pain']}")
    for title, key in (("Preferences", "preferences"), ("Constraints", "constraints")):
        entries = syn.get(key) or []
        if entries:
            out += ["", f"## {title}"] + [f"- {e.get('text')}" for e in entries]
    out += ["", "## Already in use (do not recommend these again)",
            "Agents: " + ", ".join(profile.get("sources") or {})]
    if profile.get("programs"):
        out.append("Programs run often: " + ", ".join(p["name"] for p in profile["programs"][:30]))
    installed = sorted({i["name"] for i in profile.get("installed") or [] if i["kind"] in ("mcp_server", "plugin")})
    if installed:
        out.append("Installed MCP servers and plugins: " + ", ".join(installed[:60]))
    return out


def _refs(profile: dict) -> dict[str, dict]:
    syn = profile.get("synthesis") or {}
    refs = {f"T{i}": t for i, t in enumerate(syn.get("recurring_tasks") or [], 1)}
    refs.update({f"I{i}": t for i, t in enumerate(syn.get("interests") or [], 1)})
    return refs


def _entry(profile: dict, c: Candidate, for_refs: list[str]) -> dict:
    refs = _refs(profile)
    evidence = profile.get("evidence") or {}
    seen_in = []
    for ref in for_refs:
        for ev_ref in (refs.get(ref) or {}).get("evidence") or []:
            if ev_ref in evidence and ev_ref not in [s["ref"] for s in seen_in]:
                seen_in.append({"ref": ev_ref, **{k: evidence[ev_ref].get(k) for k in ("agent", "when", "project", "excerpt")}})
    r = c.repo
    return {"name": r["name"], "url": f"https://github.com/{r['name']}", "stars": r["stars"],
            "language": r.get("language"), "license": r.get("license"), "pushed": r.get("pushed"),
            "description": r.get("description"),
            "for": [{"ref": ref, "text": refs[ref].get("task") or refs[ref].get("topic")} for ref in for_refs if ref in refs],
            "evidence": seen_in[:3]}


def apply_ranking(profile: dict, cands: list[Candidate], answer: dict, top: int) -> tuple[list[dict], list[dict]]:
    """Keep only picks that point at real candidates and real profile entries."""
    refs = _refs(profile)
    by_id = {f"C{n}": c for n, c in enumerate(cands, 1)}
    picks, used = [], set()
    for p in answer.get("picks") or []:
        if not isinstance(p, dict):
            continue
        cid = str(p.get("candidate") or "").strip()
        for_refs = [r for r in p.get("for") or [] if r in refs]
        if cid not in by_id or cid in used or not for_refs:
            continue
        used.add(cid)
        entry = _entry(profile, by_id[cid], for_refs)
        entry.update(verdict=p.get("verdict") if p.get("verdict") in ("install", "try") else "try",
                     why=p.get("why"), replaces=p.get("replaces") or None, caveat=p.get("caveat") or None)
        picks.append(entry)
        if len(picks) >= top:
            break
    skips = []
    for s in answer.get("skips") or []:
        cid = str((s or {}).get("candidate") or "").strip() if isinstance(s, dict) else ""
        if cid in by_id and cid not in used:
            skips.append({"name": by_id[cid].repo["name"], "reason": s.get("reason")})
    return picks, skips


def retrieval_only(profile: dict, cands: list[Candidate], top: int) -> list[dict]:
    picks = []
    for c in cands[:top]:
        entry = _entry(profile, c, c.matched)
        entry.update(verdict="match", why=None, replaces=None, caveat=None)
        picks.append(entry)
    return picks


def render(result: dict) -> str:
    out = []
    by = result.get("ranked_by") or "retrieval only (no model)"
    out.append(f"Recommendations · ranked by {by} · index built {result.get('index_built', '')[:10]} "
               f"({result.get('index_size', 0):,} repos)")
    for n, p in enumerate(result.get("picks") or [], 1):
        lang = f"  {p['language']}" if p.get("language") else ""
        out += ["", f"{n:>2}. {p['name']}  ★{_k(p['stars'])}{lang}  [{p['verdict']}]"]
        for f in p.get("for") or []:
            out.append(f"    for       {f['ref']} {f['text']}")
        if p.get("why"):
            out.append(f"    why       {p['why']}")
        elif p.get("description"):
            out.append(f"    about     {p['description']}")
        if p.get("replaces"):
            out.append(f"    replaces  {p['replaces']}")
        if p.get("caveat"):
            out.append(f"    caveat    {p['caveat']}")
        for e in p.get("evidence") or []:
            out.append(f"    seen in   {e['ref']} {e.get('when') or ''} {e.get('agent') or ''}: \"{(e.get('excerpt') or '')[:90]}\"")
        out.append(f"    {p['url']}")
    if result.get("skips"):
        out += ["", "Looked relevant, skipped:"] + [f"  - {s['name']}: {s['reason']}" for s in result["skips"]]
    if result.get("unused"):
        out += ["", render_unused(result["unused"])]
    if result.get("in_use"):
        names = ", ".join(f"{r['name']} ({r['why']})" for r in result["in_use"][:8])
        out += ["", f"Already in use, so not recommended ({len(result['in_use'])}): {names}"]
    return "\n".join(out)


def render_unused(unused: list[dict], limit: int = 8) -> str:
    out = ["Installed but never called in your sessions (each one costs context on every request):"]
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for u in unused:
        groups[(u["agent"], u["kind"])].append(u)
    for (agent, kind), items in groups.items():
        names = ", ".join(u["name"] for u in items[:limit]) + (f" and {len(items) - limit} more" if len(items) > limit else "")
        label = kind.replace("_", " ") + ("s" if len(items) > 1 else "")
        out.append(f"  - {agent}: {len(items)} {label} unused in {items[0]['sessions']} sessions: {names}")
    return "\n".join(out)


# Files under ~/.repofit shared by the CLI and the MCP server.

def new_result(profile: dict) -> dict:
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ranked_by": None,
            "index_built": None, "index_size": 0, "profile_synthesized_by": profile.get("synthesized_by"),
            "picks": [], "skips": [], "unused": unused_tools(profile), "in_use": []}


def save_candidates(home: Path, cands: list[Candidate], in_use: list[dict], index: dict) -> Path:
    path = home / "candidates.json"
    path.write_text(json.dumps({"candidates": [{"repo": c.repo, "score": c.score, "matched": c.matched} for c in cands],
                                "in_use": in_use, "index_built": index.get("built_at"),
                                "index_size": len(index["repos"])}, ensure_ascii=False), encoding="utf-8")
    return path


def load_candidates(home: Path) -> tuple[list[Candidate], dict] | None:
    """The shortlist a ranking was asked for, so its answer is checked against the same list."""
    path = home / "candidates.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    cands = [Candidate(c["repo"], c["score"], c["matched"]) for c in data["candidates"]]
    return cands, {"in_use": data.get("in_use") or [], "index_built": data.get("index_built"),
                   "index_size": data.get("index_size", 0)}


def save_result(result: dict, home: Path) -> Path:
    path = home / "recommendations.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
