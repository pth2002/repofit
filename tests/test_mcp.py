import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def rpc(messages, home):
    env = dict(os.environ, REPOFIT_HOME=str(home), PYTHONPATH=str(SRC), PYTHONIOENCODING="utf-8")
    payload = "".join(json.dumps(m) + "\n" for m in messages)
    out = subprocess.run([sys.executable, "-m", "repofit", "mcp"], input=payload, capture_output=True,
                         text=True, encoding="utf-8", env=env, timeout=60)
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def test_handshake_tools_and_errors(tmp_path):
    replies = rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "show_profile", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "check_repo", "arguments": {"repo": "not a repo"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "ping"},
    ], tmp_path)
    by_id = {r["id"]: r for r in replies}
    assert set(by_id) == {1, 2, 3, 4, 5, 6}  # the notification gets no reply
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    names = {t["name"] for t in by_id[2]["result"]["tools"]}
    assert {"get_evidence", "find_candidates", "save_recommendations", "check_repo"} <= names
    assert "No profile yet" in by_id[3]["result"]["content"][0]["text"]
    assert by_id[4]["error"]["code"] == -32602
    assert by_id[5]["result"]["isError"] is True
    assert by_id[6]["result"] == {}


def test_unknown_protocol_version_gets_the_latest(tmp_path):
    reply = rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}],
                tmp_path)[0]
    assert reply["result"]["protocolVersion"] == "2025-06-18"
