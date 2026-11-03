from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from repofit import __version__, evaluate, llm, match, mcp_server, prompts
from repofit import index as index_mod
from repofit import profile as profile_mod
from repofit.adapters import all_adapters
from repofit.evidence import LEVELS, Evidence
from repofit.evidence import build as build_evidence
from repofit.prompts import SYNTHESIS_SYSTEM, synthesis_user
from repofit.scan import all_sessions, detected, known_projects, project_docs, report
from repofit.signals import Signals, within


def _kb(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n >= 1024 else f"{n} B"


def cmd_scan(args) -> int:
    adapters = detected()
    if not adapters:
        print("No supported agents found.")
        return 0
    rows = [("agent", "sessions", "parser", "memory files", "installed")]
    errors = []
    for a in adapters:
        r = report(a)
        kinds = Counter(i.kind for i in r.inventory)
        installed = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())) or "-"
        mem = f"{len(r.memories)} ({_kb(sum(m.size for m in r.memories))})" if r.memories else "-"
        rows.append((a.name, "-" if r.sessions is None else str(r.sessions), a.sessions_status, mem, installed))
        if r.error:
            errors.append(f"{a.name}: {r.error}")
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for i, row in enumerate(rows):
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
        if i == 0:
            print("  ".join("-" * w for w in widths))
    projects = known_projects(adapters)
    docs = project_docs(projects)
    print(f"\nProjects seen by agents: {len(projects)}; instruction files in them: {len(docs)}")
    if errors:
        print("\nErrors (other agents unaffected):")
        for e in errors:
            print(f"  {e}")
    skipped = [a.name for a in all_adapters() if a.id not in {x.id for x in adapters}]
    print(f"\nNot found: {', '.join(skipped)}")
    return 0


def cmd_sessions(args) -> int:
    count = 0
    for adapter, s in all_sessions(detected(args.agent)):
        if isinstance(s, Exception):
            print(f"[{adapter.id}] error: {type(s).__name__}: {s}", file=sys.stderr)
            continue
        if not within(s, args.days):
            continue
        if args.json:
            print(json.dumps(asdict(s), default=str, ensure_ascii=False))
        else:
            when = s.started_at.astimezone().strftime("%Y-%m-%d") if s.started_at else "?"
            title = s.title or (s.user_messages[0].splitlines()[0][:60] if args.show_text and s.user_messages else "")
            print(f"{when:<10}  {adapter.id:<14} {len(s.user_messages):>4} msgs {len(s.tool_calls):>5} tools  "
                  f"{(s.project or '-')[-40:]:<40}  {title}")
        count += 1
        if args.limit and count >= args.limit:
            break
    return 0


def cmd_signals(args) -> int:
    sig = Signals()
    errors = []
    for adapter, s in all_sessions(detected(args.agent)):
        if isinstance(s, Exception):
            errors.append(f"{adapter.name}: {type(s).__name__}: {s}")
        elif within(s, args.days):
            sig.add(s)
    span = f"{sig.first:%Y-%m-%d} .. {sig.last:%Y-%m-%d}" if sig.first and sig.last else "unknown span"
    print(f"{sig.sessions} sessions, {sig.user_messages} user messages, {span}")
    print("by agent:   " + ", ".join(f"{k} {v}" for k, v in sig.by_agent.most_common()))
    print("tool use:   " + ", ".join(f"{k} {v}" for k, v in sig.tool_kinds.most_common()))
    print(f"web lookups: {sig.web_searches}")
    print(f"\nTop commands you (or your agents) run:")
    for head, n in sig.commands.most_common(args.top):
        print(f"  {n:>6}  {head}")
    if sig.mcp:
        print("\nMCP servers actually called:")
        for name, n in sig.mcp.most_common(args.top):
            print(f"  {n:>6}  {name}")
    print("\nMost active projects:")
    for path, n in sig.projects.most_common(min(args.top, 10)):
        print(f"  {n:>6}  {path}")
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    return 0


def cmd_memories(args) -> int:
    adapters = detected(args.agent)
    docs = [m for a in adapters for m in report(a).memories]
    if not args.agent:
        docs += project_docs(known_projects(adapters))
    for m in docs:
        where = f"{m.path} [inline]" if m.inline is not None else str(m.path)
        print(f"{m.agent:<14} {m.kind:<12} {m.scope:<8} {_kb(m.size):>7}  {where}")
    return 0


def cmd_inventory(args) -> int:
    for a in detected(args.agent):
        for item in report(a).inventory:
            print(f"{item.agent:<14} {item.kind:<11} {item.name:<40} {item.source or ''}")
    return 0


def _evidence(args) -> Evidence:
    return build_evidence(args.level, args.days, args.session_budget, args.memory_budget,
                          progress=lambda msg: print(f"… {msg}", file=sys.stderr))


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"{question} Re-run with --yes to confirm.", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def cmd_profile_build(args) -> int:
    path = Path(args.path) if args.path else profile_mod.default_path()
    try:
        cfg = None if args.no_llm else llm.resolve(args.provider, args.model, args.base_url)
    except llm.LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    ev = _evidence(args)
    profile = profile_mod.from_evidence(ev)
    profile_mod.save_evidence(ev, path)
    if cfg and ev.items:
        pack = ev.render()
        counts = ev.counts()
        question = (f"Send {len(pack):,} characters ({counts['memory']} memory excerpts, {counts['session']} session "
                    f"excerpts, redacted) to {cfg.label()}, then the resulting profile summary for search terms?")
        if _confirm(question, args.yes):
            print(f"… asking {cfg.label()}", file=sys.stderr)
            try:
                answer = llm.chat_json(cfg, SYNTHESIS_SYSTEM, synthesis_user(pack))
            except llm.LLMError as exc:
                print(f"error: {exc}\nSaved the profile without synthesis.", file=sys.stderr)
            else:
                profile_mod.apply_synthesis(profile, ev, answer, cfg.label())
                print(f"… asking {cfg.label()} for search terms", file=sys.stderr)
                _expand(profile, cfg)
    elif not cfg and not args.no_llm:
        print("No model configured, so only the rule-based part was built. Either set a provider key "
              "(e.g. DEEPSEEK_API_KEY) and re-run, or run `repofit profile prompt`, paste the output into "
              "any agent, and import its answer with `repofit profile apply`.", file=sys.stderr)
    profile_mod.save(profile, path)
    print(profile_mod.render(profile))
    print(f"\nSaved to {path}", file=sys.stderr)
    return 0


def cmd_profile_show(args) -> int:
    path = Path(args.path) if args.path else profile_mod.default_path()
    if not path.is_file():
        print(f"No profile at {path}. Run `repofit profile build` first.", file=sys.stderr)
        return 1
    profile = profile_mod.load(path)
    print(json.dumps(profile, ensure_ascii=False, indent=2) if args.json else profile_mod.render(profile))
    return 0


def cmd_profile_evidence(args) -> int:
    ev = _evidence(args)
    print(json.dumps(ev.to_dict(), ensure_ascii=False, indent=2) if args.json else ev.render())
    return 0


def cmd_profile_prompt(args) -> int:
    ev = _evidence(args)
    path = Path(args.path) if args.path else profile_mod.default_path()
    saved = profile_mod.save_evidence(ev, path)
    print(SYNTHESIS_SYSTEM + "\n---\n\n" + synthesis_user(ev.render()))
    print(f"\nEvidence saved to {saved}. Paste the text above into any agent, save its JSON answer to a file, "
          f"then run: repofit profile apply <file>", file=sys.stderr)
    return 0


def cmd_profile_apply(args) -> int:
    path = Path(args.path) if args.path else profile_mod.default_path()
    ev = profile_mod.load_evidence(path)
    if ev is None:
        print(f"No evidence pack next to {path}. Run `repofit profile prompt` first.", file=sys.stderr)
        return 1
    try:
        answer = llm.parse_json(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, llm.LLMError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # The rule-based part is rebuilt from the same pack the answer was written from, so the two always match.
    profile = profile_mod.from_evidence(ev)
    profile_mod.apply_synthesis(profile, ev, answer, args.by)
    profile_mod.save(profile, path)
    print(profile_mod.render(profile))
    print("\nNext: `repofit profile expand` adds search terms and project names, which retrieval needs "
          "to find libraries a task depends on.", file=sys.stderr)
    return 0


def _expand(profile: dict, cfg: llm.LLMConfig) -> bool:
    try:
        answer = llm.chat_json(cfg, prompts.EXPANSION_SYSTEM, prompts.expansion_user(match.expansion_input(profile)))
    except llm.LLMError as exc:
        print(f"error: {exc}\nThe profile was saved without search expansion.", file=sys.stderr)
        return False
    match.apply_expansion(profile, answer, cfg.label())
    return True


def cmd_profile_expand(args) -> int:
    path = Path(args.path) if args.path else profile_mod.default_path()
    if not path.is_file():
        print(f"No profile at {path}. Run `repofit profile build` first.", file=sys.stderr)
        return 1
    profile = profile_mod.load(path)
    if not profile.get("synthesis"):
        print("Expansion works on synthesized tasks and interests, and this profile has none yet.", file=sys.stderr)
        return 1
    if args.prompt:
        print(prompts.EXPANSION_SYSTEM + "\n---\n\n" + prompts.expansion_user(match.expansion_input(profile)))
        print("\nPaste the text above into any agent, save its JSON answer to a file, then run: "
              "repofit profile expand --apply <file>", file=sys.stderr)
        return 0
    if args.apply:
        try:
            answer = llm.parse_json(Path(args.apply).read_text(encoding="utf-8"))
        except (OSError, llm.LLMError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        match.apply_expansion(profile, answer, args.by)
    else:
        try:
            cfg = llm.resolve(args.provider, args.model, args.base_url)
        except llm.LLMError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not cfg:
            print("No model configured. Set a provider key, or use --prompt and paste it into any agent.", file=sys.stderr)
            return 2
        if not _confirm(f"Send your profile summary (tasks and interests, no session text) to {cfg.label()}?", args.yes):
            return 1
        if not _expand(profile, cfg):
            return 1
    profile_mod.save(profile, path)
    refs = match.expansion_refs(profile)
    for ref, entry in profile["expansions"].items():
        print(f"[{ref}] {refs[ref][:70]}\n      projects: {', '.join(entry['projects']) or '-'}")
    print(f"\nSaved to {path}", file=sys.stderr)
    return 0


def cmd_index_build(args) -> int:
    token = index_mod.github_token()
    if not token:
        print("Building the index needs a GitHub token (GITHUB_TOKEN, GH_TOKEN or a logged-in `gh`). "
              "Most users can run `repofit index pull` instead.", file=sys.stderr)
        return 2
    path = Path(args.out) if args.out else index_mod.default_path()
    try:
        idx = index_mod.build(token, args.min_stars, args.days, args.new_stars, args.topic_stars,
                              readmes=not args.no_readme, progress=lambda msg: print(f"… {msg}", file=sys.stderr))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    index_mod.save(idx, path)
    print(f"{len(idx['repos']):,} repositories saved to {path}")
    return 0


def cmd_index_pull(args) -> int:
    path = Path(args.out) if args.out else index_mod.default_path()
    url = args.url or os.environ.get("REPOFIT_INDEX_URL") or index_mod.DEFAULT_URL
    try:
        idx = index_mod.pull(url, path)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: could not download {url}: {exc}", file=sys.stderr)
        return 1
    print(f"{len(idx['repos']):,} repositories (built {idx['built_at'][:10]}) saved to {path}")
    return 0


def cmd_index_info(args) -> int:
    path = Path(args.path) if args.path else index_mod.default_path()
    if not path.is_file():
        print(f"No index at {path}. Run `repofit index pull` first.", file=sys.stderr)
        return 1
    idx = index_mod.load(path)
    repos = idx["repos"]
    print(f"{path}\nbuilt {idx['built_at']}, {len(repos):,} repositories")
    sources = Counter(s.split(":")[0] if s.startswith("list:") else s for r in repos for s in r["sources"])
    print("by source: " + ", ".join(f"{k} {v}" for k, v in sources.most_common()))
    langs = Counter(r["language"] for r in repos)
    print("languages: " + ", ".join(f"{k} {v}" for k, v in langs.most_common(10)))
    print(f"with README text: {sum(bool(r['readme']) for r in repos):,}")
    return 0


_LANGS = {"en": "English", "zh": "Simplified Chinese", "zh-cn": "Simplified Chinese", "zh-tw": "Traditional Chinese",
          "ja": "Japanese", "ko": "Korean", "es": "Spanish", "fr": "French", "de": "German"}


def _home(profile_path: Path) -> Path:
    return profile_path.parent


def _finish(result: dict, home: Path, as_json: bool) -> int:
    path = match.save_result(result, home)
    print(json.dumps(result, ensure_ascii=False, indent=2) if as_json else match.render(result))
    print(f"\nSaved to {path}", file=sys.stderr)
    return 0


def cmd_recommend(args) -> int:
    profile_path = Path(args.path) if args.path else profile_mod.default_path()
    home = _home(profile_path)
    if args.show:
        path = home / "recommendations.json"
        if not path.is_file():
            print(f"No recommendations at {path}. Run `repofit recommend` first.", file=sys.stderr)
            return 1
        result = json.loads(path.read_text(encoding="utf-8"))
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else match.render(result))
        return 0
    if not profile_path.is_file():
        print(f"No profile at {profile_path}. Run `repofit profile build` first.", file=sys.stderr)
        return 1
    profile = profile_mod.load(profile_path)
    result = match.new_result(profile)

    if args.apply:  # the candidates were saved by --prompt, so the index is not needed again
        loaded = match.load_candidates(home)
        if loaded is None:
            print(f"No candidate list in {home}. Run `repofit recommend --prompt` first.", file=sys.stderr)
            return 1
        cands, meta = loaded
        result.update(meta)
        try:
            answer = llm.parse_json(Path(args.apply).read_text(encoding="utf-8"))
        except (OSError, llm.LLMError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        result["picks"], result["skips"] = match.apply_ranking(profile, cands, answer, args.top)
        result["ranked_by"] = args.by
        return _finish(result, home, args.json)

    index_path = Path(args.index) if args.index else index_mod.default_path()
    try:
        idx = index_mod.ensure(index_path, progress=lambda msg: print(f"… {msg}", file=sys.stderr))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not profile.get("synthesis"):
        print("The profile has no synthesized tasks or interests yet, so matching falls back to the programs you run. "
              "See `repofit profile build --help`.", file=sys.stderr)
    cands, in_use = match.candidates(profile, idx, args.pool)
    result.update(in_use=in_use, index_built=idx.get("built_at"), index_size=len(idx["repos"]))
    language = _LANGS.get(args.lang.lower(), args.lang)
    system, user = prompts.ranking_system(args.top, language), prompts.ranking_user(match.ranker_input(profile, cands))
    if args.prompt:
        saved = match.save_candidates(home, cands, in_use, idx)
        print(system + "\n---\n\n" + user)
        print(f"\nCandidates saved to {saved}. Paste the text above into any agent, save its JSON answer to a file, "
              f"then run: repofit recommend --apply <file>", file=sys.stderr)
        return 0
    try:
        cfg = None if args.no_llm else llm.resolve(args.provider, args.model, args.base_url)
    except llm.LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if cfg and _confirm(f"Send your profile summary and {len(cands)} candidate descriptions "
                        f"({len(system) + len(user):,} characters) to {cfg.label()}?", args.yes):
        print(f"… asking {cfg.label()}", file=sys.stderr)
        try:
            answer = llm.chat_json(cfg, system, user)
        except llm.LLMError as exc:
            print(f"error: {exc}\nShowing retrieval results instead.", file=sys.stderr)
        else:
            result["picks"], result["skips"] = match.apply_ranking(profile, cands, answer, args.top)
            result["ranked_by"] = cfg.label()
    elif not cfg and not args.no_llm:
        print("No model configured, so these are retrieval results without ranking. Set a provider key "
              "(e.g. DEEPSEEK_API_KEY), or run `repofit recommend --prompt` and paste it into any agent.", file=sys.stderr)
    if not result["ranked_by"]:
        result["picks"] = match.retrieval_only(profile, cands, args.top)
    return _finish(result, home, args.json)


def cmd_eval_prepare(args) -> int:
    own = set(args.exclude_owner or [])
    if not own:  # the user's own repos say nothing about recommendations
        gh = shutil.which("gh")
        if gh:
            try:
                login = subprocess.run([gh, "api", "user", "--jq", ".login"], capture_output=True, text=True, timeout=20)
                own = {login.stdout.strip()} if login.stdout.strip() else set()
            except (OSError, subprocess.SubprocessError):
                pass
    if not index_mod.default_path().is_file():
        print("No index. Run `repofit index pull` or `repofit index build` first.", file=sys.stderr)
        return 1
    meta = evaluate.prepare(args.cutoff, args.window, own, progress=lambda msg: print(f"… {msg}", file=sys.stderr))
    out = evaluate.eval_dir(args.cutoff)
    truth = meta["truth"]
    print(f"{meta['sessions_before']} sessions before {args.cutoff}, {meta['sessions_after']} in the {args.window} days after; "
          f"{sum(t['in_index'] for t in truth)} ground-truth repos in the index, {sum(not t['in_index'] for t in truth)} outside.")
    print(f"\nNext, from {out}:\n"
          f"  1. give profile_prompt.md to a model that has not seen sessions after the cutoff, save its JSON answer\n"
          f"  2. repofit profile apply <answer> --path {out / 'profile.json'}\n"
          f"  3. optional: repofit recommend --path {out / 'profile.json'} --index {out / 'index.json.gz'} --prompt\n"
          f"     then rank with the same kind of model and --apply its answer\n"
          f"  4. repofit eval report --cutoff {args.cutoff}")
    return 0


def cmd_eval_report(args) -> int:
    try:
        rep = evaluate.report(args.cutoff, args.k, args.pool)
    except FileNotFoundError:
        print(f"Nothing prepared for {args.cutoff}. Run `repofit eval prepare --cutoff {args.cutoff}` first.", file=sys.stderr)
        return 1
    (evaluate.eval_dir(args.cutoff) / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else evaluate.render(rep))
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="repofit", description="See what your local agents know about you.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("scan", help="list detected agents and what each one stores")
    agent_ids = [a.id for a in all_adapters()]
    for name, fn, help_ in (("sessions", cmd_sessions, "list normalized sessions"),
                            ("signals", cmd_signals, "aggregate evidence across sessions"),
                            ("memories", cmd_memories, "list memory and instruction files"),
                            ("inventory", cmd_inventory, "list installed skills, MCP servers, plugins")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--agent", choices=agent_ids)
        p.set_defaults(fn=fn)
        if name in ("sessions", "signals"):
            p.add_argument("--days", type=int, help="only sessions started in the last N days")
        if name == "sessions":
            p.add_argument("--limit", type=int)
            p.add_argument("--json", action="store_true")
            p.add_argument("--show-text", action="store_true", help="show the first user message as a title")
        if name == "signals":
            p.add_argument("--top", type=int, default=15)

    prof = sub.add_parser("profile", help="build and inspect your profile").add_subparsers(dest="profile_cmd", required=True)
    for name, fn, help_ in (("build", cmd_profile_build, "build the profile and save it"),
                            ("evidence", cmd_profile_evidence, "print the evidence pack; nothing is saved or sent"),
                            ("prompt", cmd_profile_prompt, "print a prompt to paste into any agent"),
                            ("show", cmd_profile_show, "show the saved profile"),
                            ("apply", cmd_profile_apply, "import an agent's JSON answer into the profile"),
                            ("expand", cmd_profile_expand, "add search terms and project names for retrieval")):
        p = prof.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        if name in ("build", "evidence", "prompt"):
            p.add_argument("--level", choices=LEVELS, default="L1",
                           help="L0 installed tools, L1 + memory files (default), L2 + session messages")
            p.add_argument("--days", type=int, help="only sessions from the last N days")
            p.add_argument("--session-budget", type=int, default=40_000, help="max characters of session excerpts")
            p.add_argument("--memory-budget", type=int, default=40_000, help="max characters of memory excerpts")
        if name != "evidence":
            p.add_argument("--path", help="profile file (default ~/.repofit/profile.json)")
        if name == "build":
            p.add_argument("--provider", choices=llm.PROVIDERS)
            p.add_argument("--model")
            p.add_argument("--base-url")
            p.add_argument("--no-llm", action="store_true", help="only the rule-based part")
            p.add_argument("--yes", action="store_true", help="send to the model without asking")
        if name in ("evidence", "show"):
            p.add_argument("--json", action="store_true")
        if name == "apply":
            p.add_argument("file", help="file holding the agent's JSON answer")
            p.add_argument("--by", default="external agent", help="who produced the answer, for the record")
        if name == "expand":
            p.add_argument("--prompt", action="store_true", help="print a prompt to paste into any agent")
            p.add_argument("--apply", metavar="FILE", help="import an agent's JSON answer to --prompt")
            p.add_argument("--by", default="external agent", help="with --apply: who produced the answer")
            p.add_argument("--provider", choices=llm.PROVIDERS)
            p.add_argument("--model")
            p.add_argument("--base-url")
            p.add_argument("--yes", action="store_true", help="send to the model without asking")
    p = sub.add_parser("recommend", help="recommend GitHub projects from your profile")
    p.set_defaults(fn=cmd_recommend)
    p.add_argument("--top", type=int, default=10, help="how many picks to show")
    p.add_argument("--pool", type=int, default=40, help="how many retrieved candidates the model ranks")
    p.add_argument("--lang", default="en", help="language for the explanations, e.g. en, zh, ja (default en)")
    p.add_argument("--provider", choices=llm.PROVIDERS)
    p.add_argument("--model")
    p.add_argument("--base-url")
    p.add_argument("--no-llm", action="store_true", help="retrieval only, nothing is sent anywhere")
    p.add_argument("--yes", action="store_true", help="send to the model without asking")
    p.add_argument("--prompt", action="store_true", help="print a prompt to paste into any agent")
    p.add_argument("--apply", metavar="FILE", help="import an agent's JSON answer to --prompt")
    p.add_argument("--by", default="external agent", help="with --apply: who produced the answer")
    p.add_argument("--show", action="store_true", help="show the last saved recommendations")
    p.add_argument("--json", action="store_true")
    p.add_argument("--index", help="index file (default ~/.repofit/index.json.gz)")
    p.add_argument("--path", help="profile file (default ~/.repofit/profile.json)")

    idx = sub.add_parser("index", help="the pool of GitHub repositories to recommend from").add_subparsers(
        dest="index_cmd", required=True)
    p = idx.add_parser("pull", help="download the prebuilt index (updated weekly)")
    p.set_defaults(fn=cmd_index_pull)
    p.add_argument("--url", help=f"default {index_mod.DEFAULT_URL}")
    p.add_argument("--out", help="where to save it (default ~/.repofit/index.json.gz)")
    p = idx.add_parser("build", help="build the index yourself from the GitHub API (needs a token)")
    p.set_defaults(fn=cmd_index_build)
    p.add_argument("--out", help="where to save it (default ~/.repofit/index.json.gz)")
    p.add_argument("--min-stars", type=int, default=3000, help="stars needed in the popular lane")
    p.add_argument("--new-stars", type=int, default=1000, help="stars needed for repos created in the window")
    p.add_argument("--topic-stars", type=int, default=300, help="stars needed in the agent-ecosystem topic lanes")
    p.add_argument("--days", type=int, default=180, help="only repos pushed to in the last N days")
    p.add_argument("--no-readme", action="store_true", help="skip README text (faster, worse matching)")
    p = idx.add_parser("info", help="summarize the local index")
    p.set_defaults(fn=cmd_index_info)
    p.add_argument("--path")
    p = sub.add_parser("mcp", help="run the MCP server over stdio, for agents to call")
    p.set_defaults(fn=lambda args: mcp_server.serve())

    ev_cmd = sub.add_parser("eval", help="offline evaluation on your own history").add_subparsers(dest="eval_cmd", required=True)
    p = ev_cmd.add_parser("prepare", help="rebuild the profile as of a date and collect what came after")
    p.set_defaults(fn=cmd_eval_prepare)
    p.add_argument("--cutoff", required=True, help="YYYY-MM-DD; the profile only sees sessions before it")
    p.add_argument("--window", type=int, default=60, help="days after the cutoff that count as ground truth")
    p.add_argument("--exclude-owner", action="append", help="GitHub owner whose repos are not ground truth "
                   "(default: your gh login)")
    p = ev_cmd.add_parser("report", help="score each method against the ground truth")
    p.set_defaults(fn=cmd_eval_report)
    p.add_argument("--cutoff", required=True)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--pool", type=int, default=40)
    p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    return getattr(args, "fn", cmd_scan)(args)


if __name__ == "__main__":
    raise SystemExit(main())
