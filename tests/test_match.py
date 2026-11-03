from repofit import match


def repo(name, desc, stars=5000, topics=(), created="2025-01-01", readme=""):
    return {"name": name, "description": desc, "topics": list(topics), "language": "Python", "stars": stars,
            "pushed": "2026-10-01", "created": created, "license": "MIT", "homepage": None, "readme": readme,
            "sources": ["popular"]}


INDEX = {"built_at": "2026-11-01T00:00:00+00:00", "repos": [
    repo("acme/gpuwatch", "Watch GPU jobs on remote servers and get notified when training finishes",
         topics=["gpu", "monitoring"]),
    repo("acme/figkit", "Publication quality scientific figures for matplotlib", topics=["plotting"]),
    repo("vllm-project/vllm", "High-throughput inference and serving engine for LLMs", stars=70000),
    repo("openai/codex", "Lightweight coding agent that runs in your terminal", stars=60000),
    repo("someone/run", "run anything", stars=9000),
]}

PROFILE = {
    "level": "L2", "sources": {"Codex": {"sessions": 10}}, "environment": {"agent_ids": {"Codex": "codex"}},
    "programs": [{"name": "ssh", "runs": 300}], "stack": {"dependencies": []}, "installed": [],
    "synthesis": {
        "summary": "researcher", "roles": [],
        "recurring_tasks": [{"task": "monitor training jobs on rented GPU servers", "how_now": "ssh by hand",
                             "pain": "jobs fail overnight", "evidence": ["S1", "S2"]}],
        "interests": [{"topic": "scientific figures for papers", "evidence": ["S3"]}],
        "preferences": [], "constraints": [], "stack": [],
    },
    "evidence": {"S1": {"agent": "codex", "when": "2026-10-01", "project": None, "excerpt": "is the run done?"}},
}


def test_already_used_matches_agents_and_ignores_generic_names():
    known = match.known_names(PROFILE)
    by_name = {r["name"]: r for r in INDEX["repos"]}
    assert match.already_used(by_name["openai/codex"], known)
    assert not match.already_used(by_name["acme/figkit"], known)
    assert "run" not in match.aliases(INDEX["repos"], stripped=True)


def test_candidates_serve_each_profile_entry():
    cands, in_use = match.candidates(PROFILE, INDEX, pool=3)
    names = [c.repo["name"] for c in cands]
    assert "acme/gpuwatch" in names and "acme/figkit" in names
    assert [r["name"] for r in in_use] == ["openai/codex"]


def test_expansion_project_names_reach_the_pool():
    expanded = {**PROFILE, "expansions": {"I1": {"projects": [], "keywords": []},
                                          "T1": {"projects": ["vllm"], "keywords": []}}}
    names = [c.repo["name"] for c in match.candidates(expanded, INDEX, pool=4)[0]]
    assert "vllm-project/vllm" in names


def test_ranking_answers_are_checked():
    cands, _ = match.candidates(PROFILE, INDEX, pool=3)
    answer = {"picks": [{"candidate": "C1", "verdict": "install", "for": ["T1", "T9"], "why": "ok"},
                        {"candidate": "C99", "verdict": "install", "for": ["T1"]},
                        {"candidate": "C2", "verdict": "maybe", "for": ["X1"]},
                        {"candidate": "C1", "verdict": "try", "for": ["T1"]}],
              "skips": [{"candidate": "C2", "reason": "known"}, {"candidate": "C1", "reason": "dup"}]}
    picks, skips = match.apply_ranking(PROFILE, cands, answer, top=10)
    assert len(picks) == 1 and [f["ref"] for f in picks[0]["for"]] == ["T1"]
    assert picks[0]["evidence"][0]["ref"] == "S1"
    assert [s["name"] for s in skips] == [cands[1].repo["name"]]  # a skip of something already picked is dropped


def test_new_and_trending_repos_count_as_new():
    assert match.is_new(repo("a/b", "x", created="2026-06-01"), INDEX)
    assert not match.is_new(repo("a/c", "x", created="2023-06-01"), INDEX)
    assert match.is_new({**repo("a/d", "x", created="2020-01-01"), "trending": {"weekly": 900}}, INDEX)
