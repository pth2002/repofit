"""Dropped connections and cut-off responses, at every place repofit talks to GitHub.

A response cut off halfway raises http.client.IncompleteRead, which is not an OSError. On 2026-11-16 one of
those ended the whole weekly index build, so each call site is checked against it here.
"""
import email.message
import gzip
import http.client
import io
import json
import threading
import urllib.error

import pytest

from repofit import index


def _headers(fields=None):
    msg = email.message.Message()
    for key, value in (fields or {}).items():
        msg[key] = value
    return msg


class Reply(io.BytesIO):
    def __init__(self, body=b"", headers=None):
        super().__init__(body)
        self.headers = _headers(headers)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(monkeypatch, *outcomes):
    """Each call takes the next outcome: an exception to raise, a Reply, or bytes to return."""
    calls = []

    def urlopen(req, timeout=None):
        outcome = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append(getattr(req, "full_url", req))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome if isinstance(outcome, Reply) else Reply(outcome)

    monkeypatch.setattr(index.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(index.time, "sleep", lambda s: None)
    return calls


def fake_clock(monkeypatch):
    """A clock that only moves when the code sleeps, so waits of minutes take no time."""
    now = [1000.0]
    monkeypatch.setattr(index.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(index.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


def refused(code, headers=None, body=b""):
    return urllib.error.HTTPError(index._API, code, "refused", _headers(headers), io.BytesIO(body))


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


# On 2026-11-16 GitHub answered the README batches with secondary limits and Retry-After: 300. Four workers
# resumed together and hit the limit again, so the build ran into the two-hour job timeout.

def test_secondary_limit_waits_retry_after_and_lowers_concurrency(monkeypatch):
    limit = refused(403, {"Retry-After": "300"}, b'{"message": "You have exceeded a secondary rate limit."}')
    calls = fake_urlopen(monkeypatch, limit, b'{"data": {"x": 1}}')
    now, notes = fake_clock(monkeypatch), []
    gh = index.GitHub("t", notes.append)
    assert gh.query("query { x }") == {"x": 1}
    assert len(calls) == 2 and now[0] == 1300
    assert gh.throttle.limit == 3 and notes == ["rate limited, waiting 300s, then 3 requests at a time"]


def test_one_burst_of_limited_requests_lowers_concurrency_once(monkeypatch):
    now = fake_clock(monkeypatch)
    throttle = index._Throttle(4)
    # The requests already in flight come back limited a moment after the first one.
    assert throttle.limited(300) and not throttle.limited(300) and not throttle.limited(300)
    assert throttle.limit == 3
    now[0] += 301
    assert throttle.limited(300) and throttle.limit == 2
    for _ in range(3):
        now[0] += 301
        throttle.limited(300)
    assert throttle.limit == 1


def test_spent_hourly_budget_waits_for_the_reset(monkeypatch):
    reset = int(index.time.time()) + 120
    spent = Reply(json.dumps({"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}).encode(),
                  {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)})
    fake_urlopen(monkeypatch, spent, b'{"data": {"x": 3}}')
    now = fake_clock(monkeypatch)
    assert index.GitHub("t").query("query { x }") == {"x": 3}
    assert 119 <= now[0] - 1000 <= 123


def test_a_refusal_that_is_not_a_rate_limit_fails_at_once(monkeypatch):
    calls = fake_urlopen(monkeypatch, refused(403, body=b'{"message": "Resource not accessible by integration"}'))
    with pytest.raises(RuntimeError, match="HTTP 403: .*not accessible"):
        index.GitHub("t").query("query { x }")
    assert len(calls) == 1


def test_endless_rate_limits_end_with_a_clear_error(monkeypatch):
    calls = fake_urlopen(monkeypatch, refused(429))
    fake_clock(monkeypatch)
    with pytest.raises(RuntimeError, match="kept rate limiting"):
        index.GitHub("t").query("query { x }")
    assert len(calls) == 21


def test_lookups_never_run_more_requests_than_the_throttle_allows(monkeypatch):
    lock, state = threading.Lock(), {"running": 0, "most": 0}

    def urlopen(req, timeout=None):
        with lock:
            state["running"] += 1
            state["most"] = max(state["most"], state["running"])
        threading.Event().wait(0.02)
        n = json.loads(req.data)["query"].count("repository(")
        with lock:
            state["running"] -= 1
        return Reply(json.dumps({"data": {f"r{j}": {"nameWithOwner": f"o/n{j}"} for j in range(n)}}).encode())

    monkeypatch.setattr(index.urllib.request, "urlopen", urlopen)
    gh = index.GitHub("t")
    gh.throttle.limit = 2
    found = list(gh.repos([f"o/n{j}" for j in range(40)], "nameWithOwner", batch=5))
    assert len(found) == 40 and state["most"] <= 2


# On 2026-11-23 GitHub answered one page of 100 search results with HTTP 502 five times in 30 seconds, and the
# weekly build stopped there.

def test_gateway_errors_are_retried_with_growing_waits(monkeypatch):
    calls = fake_urlopen(monkeypatch, refused(502), refused(502), refused(504), refused(502), b'{"data": {"x": 5}}')
    now = fake_clock(monkeypatch)
    assert index.GitHub("t").query("query { x }") == {"x": 5}
    assert len(calls) == 5 and now[0] - 1000 == 5 + 10 + 20 + 40


def fake_search(monkeypatch, total, fails):
    """GitHub search over `total` repos, answering 502 for any page that `fails(size)` says is too big."""
    sizes = []

    def query(self, q, variables=None):
        sizes.append(variables["n"])
        if fails(variables["n"]):
            raise RuntimeError("GitHub API HTTP 502: <html><title>502 Bad Gateway</title></html>")
        start = int(variables["after"] or 0)
        end = min(start + variables["n"], total)
        nodes = [{"nameWithOwner": f"o/n{j}"} for j in range(start, end)]
        return {"search": {"nodes": nodes, "pageInfo": {"hasNextPage": end < total, "endCursor": str(end)}}}

    monkeypatch.setattr(index.GitHub, "query", query)
    return sizes


def test_search_halves_the_page_when_github_runs_out_of_time(monkeypatch):
    sizes, notes = fake_search(monkeypatch, 60, lambda n: n > 25), []
    found = [r["nameWithOwner"] for r in index.GitHub("t", notes.append).search("topic:self-hosted stars:>=300")]
    assert found == [f"o/n{j}" for j in range(60)]  # nothing skipped, nothing twice
    assert sizes == [100, 50, 25, 25, 25]
    assert notes[-1] == "search page for 'topic:self-hosted stars:>=300' timed out, retrying with 25 per page"


def test_search_gives_up_when_small_pages_fail_too(monkeypatch):
    sizes = fake_search(monkeypatch, 60, lambda n: True)
    with pytest.raises(RuntimeError, match="HTTP 502"):
        list(index.GitHub("t").search("q"))
    assert sizes == [100, 50, 25, 12]


def test_search_keeps_the_page_size_for_other_errors(monkeypatch):
    calls = []

    def query(self, q, variables=None):
        calls.append(variables["n"])
        raise RuntimeError("GitHub API HTTP 401: Bad credentials")

    monkeypatch.setattr(index.GitHub, "query", query)
    with pytest.raises(RuntimeError, match="HTTP 401"):
        list(index.GitHub("t").search("q"))
    assert calls == [100]
