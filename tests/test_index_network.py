"""Dropped connections and cut-off responses, at every place repofit talks to GitHub.

A response cut off halfway raises http.client.IncompleteRead, which is not an OSError. On 2026-11-16 one of
those ended the whole weekly index build, so each call site is checked against it here.
"""
import gzip
import http.client
import io
import json

import pytest

from repofit import index


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(monkeypatch, *outcomes):
    """Each call takes the next outcome: an exception to raise, or bytes to return."""
    calls = []

    def urlopen(req, timeout=None):
        outcome = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append(getattr(req, "full_url", req))
        if isinstance(outcome, BaseException):
            raise outcome
        return Reply(outcome)

    monkeypatch.setattr(index.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(index.time, "sleep", lambda s: None)
    return calls


CUT = http.client.IncompleteRead(b"{\"data\": {", 449679)


def test_query_retries_a_cut_off_response(monkeypatch):
    calls = fake_urlopen(monkeypatch, CUT, b'{"data": {"x": 1}}')
    assert index.GitHub("t").query("query { x }") == {"x": 1}
    assert len(calls) == 2


def test_query_retries_a_truncated_json_body(monkeypatch):
    calls = fake_urlopen(monkeypatch, b'{"data": {"x"', b'{"data": {"x": 2}}')
    assert index.GitHub("t").query("query { x }") == {"x": 2}
    assert len(calls) == 2


def test_query_gives_up_with_a_clear_error(monkeypatch):
    calls = fake_urlopen(monkeypatch, CUT)
    with pytest.raises(RuntimeError, match="cannot reach the GitHub API"):
        index.GitHub("t").query("query { x }")
    assert len(calls) == 5


def test_lookup_splits_a_batch_that_keeps_getting_cut_off(monkeypatch):
    def query(self, q, variables=None):
        if q.count("repository(") > 10:
            raise RuntimeError("cannot reach the GitHub API: IncompleteRead(219264 bytes read, 449679 more expected)")
        return {f"r{j}": {"nameWithOwner": f"o/n{j}"} for j in range(q.count("repository("))}

    monkeypatch.setattr(index.GitHub, "query", query)
    names = [f"o/n{j}" for j in range(40)]
    found = index.GitHub("t")._lookup(names, "nameWithOwner")
    assert [full for full, _ in found] == names


def test_pull_retries_a_cut_off_download(monkeypatch, tmp_path):
    good = gzip.compress(json.dumps({"version": index.INDEX_VERSION, "repos": []}).encode())
    calls = fake_urlopen(monkeypatch, CUT, good[: len(good) // 2], good)
    idx = index.pull("https://example.test/index.json.gz", tmp_path / "index.json.gz")
    assert idx["version"] == index.INDEX_VERSION and (tmp_path / "index.json.gz").read_bytes() == good
    assert len(calls) == 3


def test_first_run_download_failure_gives_the_friendly_message(monkeypatch, tmp_path):
    fake_urlopen(monkeypatch, CUT)
    with pytest.raises(RuntimeError, match="Run `repofit index pull`"):
        index.ensure(tmp_path / "index.json.gz")
    assert not (tmp_path / "index.json.gz").exists()


def test_trending_skips_pages_that_get_cut_off(monkeypatch):
    fake_urlopen(monkeypatch, CUT)
    assert index.trending() == {}


def test_check_repo_survives_a_cut_off_readme(monkeypatch):
    meta = {"full_name": "o/n", "description": "d", "topics": [], "language": "Python", "stargazers_count": 5,
            "pushed_at": "2026-11-01T00:00:00Z", "created_at": "2026-01-01T00:00:00Z", "license": None,
            "homepage": None, "archived": False, "fork": False}
    fake_urlopen(monkeypatch, json.dumps(meta).encode(), CUT)
    repo = index.fetch_repo("o/n")
    assert repo["name"] == "o/n" and repo["readme"] == ""
