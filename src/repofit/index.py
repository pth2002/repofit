"""The candidate pool: GitHub repositories the recommender picks from.

A weekly GitHub Action builds it and publishes one gzipped JSON file, so users only download it.
`repofit index build` rebuilds it locally. That needs a GitHub token because it uses the GraphQL API.
"""

from __future__ import annotations

import gzip
import html
import http.client
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from repofit.paths import HOME, env_path

INDEX_VERSION = 1
DEFAULT_URL = "https://github.com/pth2002/repofit/releases/download/index/index.json.gz"
_API = "https://api.github.com/graphql"
_HEADERS = {"User-Agent": "repofit", "Accept": "application/json"}
# OSError covers unreachable hosts, timeouts and reset connections. HTTPException covers a response cut off
# halfway (IncompleteRead), which is not an OSError and used to end a whole weekly build.
_NETWORK = (OSError, http.client.HTTPException)

# Stars alone favour old giants, so new repos and the agent ecosystem get lanes of their own.
TOPICS = ("mcp", "mcp-server", "model-context-protocol", "claude-code", "codex", "cursor", "agent-skills",
          "ai-agents", "llm", "cli", "self-hosted", "developer-tools")
TRENDING_LANGUAGES = ("", "python", "typescript", "javascript", "rust", "go", "java", "c++", "c%23", "swift",
                      "kotlin", "shell", "jupyter-notebook")
AWESOME_LISTS = ("punkpeye/awesome-mcp-servers", "hesreallyhim/awesome-claude-code", "agarrharr/awesome-cli-apps")

_FIELDS = """nameWithOwner description stargazerCount pushedAt createdAt isArchived isFork isTemplate homepageUrl
primaryLanguage { name } licenseInfo { spdxId } repositoryTopics(first: 12) { nodes { topic { name } } }"""
_READMES = ("README.md", "readme.md", "Readme.md", "README.rst", "README")
# Lists, books and interview prep are worth reading, but nobody installs them.
_NOT_A_TOOL = re.compile(r"\b(curated list|awesome list|list of (awesome|free|useful)|interview questions|"
                         r"cheat ?sheets?|free programming books|learning resources)\b", re.I)


def default_path() -> Path:
    return env_path("REPOFIT_HOME", HOME / ".repofit") / "index.json.gz"


def load(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def save(index: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def pull(url: str, path: Path, attempts: int = 3) -> dict:
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=120) as resp:
                data = resp.read()
            index = json.loads(gzip.decompress(data).decode("utf-8"))
            break
        except urllib.error.HTTPError:
            raise
        except (*_NETWORK, EOFError, ValueError) as exc:  # a cut-off download fails to decompress or parse
            if attempt == attempts - 1:
                raise RuntimeError(f"download of {url} kept failing: {exc}") from exc
            time.sleep(3 * (attempt + 1))
    if index.get("version") != INDEX_VERSION:
        raise RuntimeError(f"index at {url} has version {index.get('version')}, this repofit reads {INDEX_VERSION}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return index


def ensure(path: Path, progress=None) -> dict:
    """Load the index, downloading the prebuilt one first if this machine has none."""
    if not path.is_file():
        url = os.environ.get("REPOFIT_INDEX_URL") or DEFAULT_URL
        if progress:
            progress(f"no index yet, downloading {url}")
        try:
            return pull(url, path)
        except (OSError, ValueError, RuntimeError) as exc:
            raise RuntimeError(f"no index at {path} and the download failed ({exc}). "
                               "Run `repofit index pull`, or `repofit index build` with a GitHub token.") from exc
    return load(path)


def fetch_repo(full_name: str, token: str | None = None) -> dict:
    """One repository from the REST API, for repos outside the index. Works without a token (60 calls an hour)."""
    def get(url: str, accept: str) -> bytes:
        headers = {**_HEADERS, "Accept": accept}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
            return resp.read()

    base = f"https://api.github.com/repos/{full_name}"
    try:
        meta = json.loads(get(base, "application/vnd.github+json"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(f"no public repository named {full_name}") from exc
        hint = " (rate limit without a token; set GITHUB_TOKEN or log in with gh)" if exc.code in (403, 429) else ""
        raise RuntimeError(f"GitHub API HTTP {exc.code}{hint}") from exc
    except (*_NETWORK, ValueError) as exc:
        raise RuntimeError(f"cannot reach the GitHub API: {exc}") from exc
    try:
        readme = get(base + "/readme", "application/vnd.github.raw").decode("utf-8", "replace")
    except _NETWORK:  # the README only sharpens matching, so the repo is still worth returning without it
        readme = ""
    return {"name": meta["full_name"], "description": (meta.get("description") or "").strip(),
            "topics": meta.get("topics") or [], "language": meta.get("language"),
            "stars": meta.get("stargazers_count") or 0, "pushed": (meta.get("pushed_at") or "")[:10],
            "created": (meta.get("created_at") or "")[:10], "license": (meta.get("license") or {}).get("spdx_id"),
            "homepage": meta.get("homepage") or None, "readme": excerpt(readme), "sources": ["github"],
            "archived": bool(meta.get("archived")), "fork": bool(meta.get("fork"))}


def github_token() -> str | None:
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(key):
            return os.environ[key].strip()
    gh = shutil.which("gh")
    if gh:
        try:
            out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() or None
    return None


class _Throttle:
    """Paces the requests of every worker that shares one GitHub client.

    GitHub answers a burst of heavy queries with a secondary rate limit and a Retry-After. All workers pause
    until then, because each one would hit the same limit on its own. Each limit also lowers by one how many
    requests may run at once, so the build settles on the pace GitHub accepts instead of bursting again.
    """

    def __init__(self, limit: int):
        self.limit = limit
        self.running = 0
        self.resume = 0.0
        self.cond = threading.Condition()

    def __enter__(self):
        while True:
            with self.cond:
                pause = self.resume - time.monotonic()
                if pause <= 0 and self.running < self.limit:
                    self.running += 1
                    return self
                if pause <= 0:
                    self.cond.wait()
                    continue
            time.sleep(pause)

    def __exit__(self, *exc):
        with self.cond:
            self.running -= 1
            self.cond.notify_all()

    def limited(self, wait: float) -> bool:
        """Pause everyone for `wait` seconds. True when this limit is a new one, not part of the last burst."""
        with self.cond:
            now = time.monotonic()
            new = now >= self.resume  # requests already in flight come back limited too, a few seconds later
            if new:
                self.limit = max(1, self.limit - 1)
            self.resume = max(self.resume, now + wait)
            self.cond.notify_all()
            return new


def _limit_wait(code: int, headers, detail: str) -> int | None:
    """Seconds to wait when GitHub refused a request for going over a rate limit, None for any other refusal."""
    try:
        if headers.get("Retry-After"):  # secondary limit
            return max(1, int(headers["Retry-After"]))
        if headers.get("X-RateLimit-Remaining") == "0":  # hourly budget spent, refilled at a fixed time
            return max(1, int(headers.get("X-RateLimit-Reset") or 0) - int(time.time()) + 1)
    except ValueError:
        pass
    return 60 if code == 429 or "rate limit" in detail.lower() or "abuse" in detail.lower() else None


def _backoff(failures: int) -> int:
    """Seconds before retry number `failures`: 5, 10, 20, 40. A GitHub outage often lasts longer than a few seconds."""
    return 5 * 2 ** (failures - 1)


def _too_slow(exc: Exception) -> bool:
    """True when a request failed because GitHub ran out of time or the answer never arrived, so a smaller one may pass."""
    msg = str(exc)
    return "HTTP 50" in msg or "timeout" in msg.lower() or "cannot reach" in msg


class GitHub:
    def __init__(self, token: str, progress=None, workers: int = 4):
        self.token = token
        self.note = progress or (lambda msg: None)
        self.calls = 0
        self.workers = workers
        self.throttle = _Throttle(workers)

    def query(self, query: str, variables: dict | None = None) -> dict:
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        failures = waits = 0
        while True:
            req = urllib.request.Request(_API, data=body, method="POST",
                                         headers={**_HEADERS, "Authorization": f"bearer {self.token}"})
            try:
                with self.throttle, urllib.request.urlopen(req, timeout=90) as resp:
                    self.calls += 1
                    headers, data = resp.headers, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", "replace")
                except (*_NETWORK, ValueError):
                    detail = ""
                wait = _limit_wait(exc.code, exc.headers, detail) if exc.code in (403, 429) else None
                if wait is None:
                    if exc.code in (502, 503, 504) and failures < 4:
                        failures += 1
                        time.sleep(_backoff(failures))
                        continue
                    raise RuntimeError(f"GitHub API HTTP {exc.code}: {detail[:200]}") from exc
            except (*_NETWORK, ValueError) as exc:  # ValueError: a body cut off before the JSON ends
                if failures < 4:
                    failures += 1
                    time.sleep(_backoff(failures))
                    continue
                raise RuntimeError(f"cannot reach the GitHub API: {exc}") from exc
            else:
                errors = data.get("errors") or []
                if not any(e.get("type") == "RATE_LIMITED" for e in errors):
                    # Missing repos come back as null data plus an error; everything else is a real failure.
                    errors = [e for e in errors if e.get("type") != "NOT_FOUND"]
                    if errors and not data.get("data"):
                        raise RuntimeError(f"GitHub API error: {errors[0].get('message')}")
                    return data.get("data") or {}
                wait = _limit_wait(200, headers, "rate limit")
            if waits == 20:
                raise RuntimeError("GitHub kept rate limiting the requests")
            waits += 1
            if self.throttle.limited(wait):
                n = self.throttle.limit
                self.note(f"rate limited, waiting {wait}s, then {n} request{'s' if n > 1 else ''} at a time")

    def count(self, q: str) -> int:
        data = self.query("query($q: String!) { search(query: $q, type: REPOSITORY, first: 1) { repositoryCount } }",
                          {"q": q})
        return data["search"]["repositoryCount"]

    def search(self, q: str) -> Iterator[dict]:
        """All results of one search. GitHub stops at 1000, so callers keep queries under that."""
        gql = ("query($q: String!, $after: String, $n: Int!) { search(query: $q, type: REPOSITORY, first: $n, "
               "after: $after) { pageInfo { hasNextPage endCursor } nodes { ... on Repository { " + _FIELDS + " } } } }")
        after, size = None, 100
        while True:
            try:
                page = self.query(gql, {"q": q, "after": after, "n": size})["search"]
            except RuntimeError as exc:
                # A busy GitHub cannot always finish a page of 100 repositories in time. Smaller pages get through,
                # and the cursor stays valid across page sizes.
                if size > 12 and _too_slow(exc):
                    size //= 2
                    self.note(f"search page for {q!r} timed out, retrying with {size} per page")
                    continue
                raise
            yield from (n for n in page["nodes"] if n)
            if not page["pageInfo"]["hasNextPage"]:
                return
            after = page["pageInfo"]["endCursor"]

    def search_all(self, base: str, lo: int, hi: int | None = None) -> Iterator[dict]:
        """Split the star range until each slice fits under the 1000-result cap."""
        q = f"{base} stars:{lo}..{hi}" if hi is not None else f"{base} stars:>={lo}"
        n = self.count(q)
        if n <= 1000 or (hi is not None and hi - lo < 2):
            yield from self.search(q)
            return
        mid = (lo + hi) // 2 if hi is not None else lo * 2
        yield from self.search_all(base, mid + 1, hi)
        yield from self.search_all(base, lo, mid)

    def repos(self, names: list[str], fields: str, batch: int) -> Iterator[tuple[str, dict]]:
        """Look repositories up by name, many per request and a few requests at a time."""
        chunks = [names[i:i + batch] for i in range(0, len(names), batch)]
        with ThreadPoolExecutor(self.workers) as pool:
            for found in pool.map(lambda chunk: self._lookup(chunk, fields), chunks):
                yield from found

    def _lookup(self, chunk: list[str], fields: str) -> list[tuple[str, dict]]:
        parts = []
        for j, full in enumerate(chunk):
            owner, _, name = full.partition("/")
            parts.append(f"r{j}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {fields} }}")
        try:
            data = self.query("query { " + " ".join(parts) + " }")
        except RuntimeError as exc:
            # Large READMEs can push a request past GitHub's time limit; smaller requests get through.
            if len(chunk) > 5 and _too_slow(exc):
                half = len(chunk) // 2
                return self._lookup(chunk[:half], fields) + self._lookup(chunk[half:], fields)
            raise
        return [(full, data[f"r{j}"]) for j, full in enumerate(chunk) if data.get(f"r{j}")]


def trending(progress=None) -> dict[str, dict]:
    """Repos on github.com/trending, with the stars they gained in that period."""
    note = progress or (lambda msg: None)
    found: dict[str, dict] = {}
    for since in ("daily", "weekly", "monthly"):
        for lang in TRENDING_LANGUAGES:
            url = f"https://github.com/trending/{lang}?since={since}" if lang else f"https://github.com/trending?since={since}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 repofit"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    page = resp.read().decode("utf-8", "replace")
            except _NETWORK as exc:
                note(f"trending {since} {lang or 'all'} failed: {exc}")
                continue
            for article in page.split("<article")[1:]:
                m = re.search(r'<h2[^>]*>\s*<a[^>]*href="/([^/"]+/[^/"]+)"', article)
                if not m:
                    continue
                gained = re.search(r"([\d,]+) stars (today|this week|this month)", article)
                stars = int(gained.group(1).replace(",", "")) if gained else 0
                entry = found.setdefault(m.group(1), {})
                entry[since] = max(entry.get(since, 0), stars)
    return found


def awesome_links(gh: GitHub, lists=AWESOME_LISTS) -> dict[str, str]:
    """GitHub repos linked from curated lists, mapped to the list that links them."""
    found: dict[str, str] = {}
    fields = " ".join(f'f{k}: object(expression: "HEAD:{f}") {{ ... on Blob {{ text }} }}' for k, f in enumerate(_READMES[:3]))
    for full, node in gh.repos(list(lists), fields, batch=len(lists)):
        text = next((node[k]["text"] for k in sorted(node) if node.get(k) and node[k].get("text")), "")
        for owner, name in re.findall(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", text):
            name = re.sub(r"\.git$", "", name.rstrip("."))
            if owner.lower() in ("topics", "sponsors", "orgs", "features", "apps", "marketplace", "collections"):
                continue
            found.setdefault(f"{owner}/{name}", full)
    return found


def excerpt(text: str, limit: int = 1000) -> str:
    """The prose at the top of a README: badges, images, code, tables and URLs removed."""
    text = re.sub(r"<!--.*?-->|```.*?```|~~~.*?~~~", " ", text, flags=re.S)
    text = re.sub(r"<(script|style|table|picture|svg)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)|!\[[^\]]*\]\[[^\]]*\]", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if (not line or line.startswith(("|", "[", "..", ":", "=", "-->")) or re.fullmatch(r"[-=*_#~`\s]+", line)):
            continue
        line = re.sub(r"https?://\S+", "", line)
        line = re.sub(r"^#+\s*|^[*-]\s+|\*\*|__|`", "", line).strip()
        if sum(ch.isalpha() for ch in line) < 0.5 * len(line) or len(line.split()) < 3:
            continue
        lines.append(line)
        if sum(len(l) for l in lines) > limit:
            break
    out = " ".join(lines)
    return out if len(out) <= limit else out[:limit].rsplit(" ", 1)[0] + " …"


def _record(node: dict) -> dict | None:
    if node.get("isArchived") or node.get("isFork") or node.get("isTemplate"):
        return None
    name = node["nameWithOwner"]
    topics = [t["topic"]["name"] for t in (node.get("repositoryTopics") or {}).get("nodes") or []]
    desc = (node.get("description") or "").strip()
    lang = (node.get("primaryLanguage") or {}).get("name")
    short = name.split("/")[1].lower()
    if not lang or short.startswith("awesome") or "awesome" in topics or "awesome-list" in topics or _NOT_A_TOOL.search(desc):
        return None
    return {"name": name, "description": desc, "topics": topics, "language": lang,
            "stars": node.get("stargazerCount") or 0, "pushed": (node.get("pushedAt") or "")[:10],
            "created": (node.get("createdAt") or "")[:10], "license": (node.get("licenseInfo") or {}).get("spdxId"),
            "homepage": node.get("homepageUrl") or None, "readme": "", "sources": []}


def build(token: str, min_stars: int = 3000, days: int = 180, new_stars: int = 1000, topic_stars: int = 300,
          list_stars: int = 100, readmes: bool = True, progress=None) -> dict:
    note = progress or (lambda msg: None)
    gh = GitHub(token, note)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    repos: dict[str, dict] = {}

    def add(node: dict, source: str, floor: int = 0) -> None:
        key = node["nameWithOwner"].lower()
        if key not in repos:
            rec = _record(node)
            if not rec or rec["stars"] < floor or rec["pushed"] < since:
                return
            repos[key] = rec
        if source not in repos[key]["sources"]:
            repos[key]["sources"].append(source)

    lanes = [("popular", f"pushed:>={since} archived:false fork:false", min_stars),
             ("new", f"created:>={since} fork:false", new_stars)]
    lanes += [(f"topic:{t}", f"topic:{t} pushed:>={since} archived:false fork:false", topic_stars) for t in TOPICS]
    for label, base, lo in lanes:
        before = len(repos)
        for node in gh.search_all(base, lo):
            add(node, label)
        note(f"{label}: +{len(repos) - before} (pool {len(repos)}, {gh.calls} API calls)")

    note("reading trending pages")
    hot = trending(note)
    by_name = {}
    for name in hot:
        by_name.setdefault(name.lower(), name)
    note(f"reading curated lists: {', '.join(AWESOME_LISTS)}")
    linked = awesome_links(gh)
    wanted = [n for n in {**{k.lower(): k for k in linked}, **by_name}.values() if n.lower() not in repos]
    for full, node in gh.repos(wanted, _FIELDS, batch=50):
        if full.lower() in by_name:
            add(node, "trending")
        if full in linked:
            add(node, f"list:{linked[full]}", list_stars)
    for full, gained in hot.items():
        if full.lower() in repos:
            rec = repos[full.lower()]
            rec["trending"] = gained
            if "trending" not in rec["sources"]:
                rec["sources"].append("trending")
    for full, source in linked.items():
        rec = repos.get(full.lower())
        if rec and f"list:{source}" not in rec["sources"]:
            rec["sources"].append(f"list:{source}")
    note(f"pool {len(repos)} after trending and lists ({gh.calls} API calls)")

    if readmes:
        fields = " ".join(f'f{k}: object(expression: "HEAD:{f}") {{ ... on Blob {{ text }} }}' for k, f in enumerate(_READMES))
        names = [r["name"] for r in repos.values()]
        for done, (full, node) in enumerate(gh.repos(names, fields, batch=40), 1):
            text = next((node[k]["text"] for k in sorted(node) if node.get(k) and node[k].get("text")), "")
            repos[full.lower()]["readme"] = excerpt(text)
            if done % 1000 == 0:
                note(f"READMEs {done}/{len(names)} ({gh.calls} API calls)")

    ordered = sorted(repos.values(), key=lambda r: -r["stars"])
    return {"version": INDEX_VERSION, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "criteria": {"pushed_since": since, "min_stars": min_stars, "new_stars": new_stars,
                         "topic_stars": topic_stars, "list_stars": list_stars, "topics": list(TOPICS),
                         "lists": list(AWESOME_LISTS)},
            "repos": ordered}
