import json
import subprocess
import sys
from pathlib import Path

from test_adapters import build_home, put, run


def claude_session(root, *tool_uses):
    lines = [{"type": "user", "message": {"role": "user", "content": "find the open issues"},
              "timestamp": "2026-10-03T09:00:00Z", "cwd": str(root)},
             {"type": "assistant", "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": name, "input": args} for name, args in tool_uses]}}]
    put(root / ".claude" / "projects" / "p1" / "s1.jsonl",
        "\n".join(json.dumps(line, separators=(",", ":")) for line in lines) + "\n")


def test_unused_splits_used_unused_and_uncountable(tmp_path):
    env = build_home(tmp_path)
    put(tmp_path / ".claude.json", {"mcpServers": {"github": {}, "sqlite-db": {}}})
    for skill in ("pdf", "docx"):
        put(tmp_path / ".claude" / "skills" / skill / "SKILL.md", f"---\nname: {skill}\ndescription: x\n---\n")
    claude_session(tmp_path, ("mcp__github__search_issues", {}), ("Skill", {"skill": "pdf"}))

    data = json.loads(run(env, "unused", "--json"))
    unused = {(u["agent"], u["kind"], u["name"]) for u in data["unused"]}
    used = {(u["agent"], u["kind"], u["name"]) for u in data["used"]}
    assert unused == {("Claude Code", "mcp_server", "sqlite-db"), ("Claude Code", "skill", "docx")}
    assert used == {("Claude Code", "mcp_server", "github"), ("Claude Code", "skill", "pdf")}
    # Gemini CLI and Continue logs do not name the server behind a call, so their servers are not called unused.
    assert {r["name"] for r in data["not_checked"]} >= {"playwright", "sqlite"}

    text = run(env, "unused")
    assert "2 of 4 installed MCP servers and skills were never called" in text
    assert "sqlite-db" in text and "docx" in text and "Not checked: 2 tools in" in text


def test_unused_with_nothing_to_flag(tmp_path):
    env = build_home(tmp_path)
    put(tmp_path / ".claude.json", {"mcpServers": {"github": {}}})
    claude_session(tmp_path, ("mcp__github__search_issues", {}))
    assert "that can be checked (1) was called at least once." in run(env, "unused")


def codex_rollout(root, *items):
    """A rollout in the newer Codex format: capitalised item types and argv lists."""
    lines = [{"type": "session_meta", "payload": {"id": "cx1", "cwd": str(root), "timestamp": "2026-10-04T10:00:00Z"}}]
    lines += [{"type": "event_msg", "payload": {"type": "item_completed", "item": item}} for item in items]
    put(root / ".codex" / "sessions" / "2026" / "10" / "04" / "rollout-cx1.jsonl",
        "\n".join(json.dumps(line) for line in lines) + "\n")


def test_newer_codex_rollouts_count_skill_reads(tmp_path):
    env = build_home(tmp_path)
    for skill in ("paper-lookup", "pufferlib"):
        put(tmp_path / ".codex" / "skills" / skill / "SKILL.md", f"---\nname: {skill}\ndescription: x\n---\n")
    skill_path = str(tmp_path / ".codex" / "skills" / "paper-lookup" / "SKILL.md")
    codex_rollout(tmp_path,
                  {"type": "UserMessage", "content": [{"type": "text", "text": "find the paper"}]},
                  {"type": "CommandExecution", "command": ["pwsh.exe", "-Command", f"Get-Content -Raw '{skill_path}'"]},
                  {"type": "Extension", "kind": "web.search", "query": "attention is all you need"})
    data = json.loads(run(env, "unused", "--json", "--agent", "codex"))
    assert {u["name"] for u in data["used"]} == {"paper-lookup"}
    assert {u["name"] for u in data["unused"]} == {"pufferlib"}
    sessions = [json.loads(line) for line in run(env, "sessions", "--json", "--agent", "codex").splitlines()]
    assert sessions[0]["user_messages"] == ["find the paper"]


def test_claude_slash_command_counts_as_skill_use(tmp_path):
    env = build_home(tmp_path)
    put(tmp_path / ".claude.json", {})
    for skill in ("docx", "pdf"):
        put(tmp_path / ".claude" / "skills" / skill / "SKILL.md", f"---\nname: {skill}\ndescription: x\n---\n")
    lines = [{"type": "user", "message": {"role": "user", "content": "<command-name>/docx</command-name>\n"
                                                                        "<command-message>docx</command-message>"},
              "timestamp": "2026-10-03T09:00:00Z", "cwd": str(tmp_path)},
             {"type": "user", "message": {"role": "user", "content": "<command-name>/model</command-name>"},
              "timestamp": "2026-10-03T09:01:00Z"},
             {"type": "user", "message": {"role": "user", "content": "write the report"},
              "timestamp": "2026-10-03T09:02:00Z"}]
    put(tmp_path / ".claude" / "projects" / "p1" / "s1.jsonl",
        "\n".join(json.dumps(line, separators=(",", ":")) for line in lines) + "\n")
    data = json.loads(run(env, "unused", "--json", "--agent", "claude-code"))
    assert {u["name"] for u in data["used"]} == {"docx"}  # /model is built in, not an installed skill
    assert {u["name"] for u in data["unused"]} == {"pdf"}


def test_cline_servers_are_not_checked(tmp_path):
    env = build_home(tmp_path)
    cline = Path(env["APPDATA"]) / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"
    put(cline / "settings" / "cline_mcp_settings.json", {"mcpServers": {"fetch": {"command": "uvx"}}})
    data = json.loads(run(env, "unused", "--json", "--agent", "cline"))
    assert [r["name"] for r in data["not_checked"]] == ["fetch"] and not data["unused"]


def test_days_window_and_bad_arguments(tmp_path):
    env = build_home(tmp_path)
    put(tmp_path / ".claude.json", {"mcpServers": {"github": {}}})
    claude_session(tmp_path, ("mcp__github__search_issues", {}))
    data = json.loads(run(env, "unused", "--json", "--agent", "claude-code", "--days", "1"))
    assert data["sessions"] == {"Claude Code": 0}
    assert [r["name"] for r in data["not_checked"]] == ["github"]  # no session in the window to check against
    assert "Found no sessions from the last 1 days" in run(env, "unused", "--agent", "claude-code", "--days", "1")
    out = subprocess.run([sys.executable, "-m", "repofit", "unused", "--days", "0"], env=env, capture_output=True,
                         text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 2 and "--days" in out.stderr
    assert run(env, "unused", "--agent", "trae").strip() == "No data from trae found on this machine."


def test_mcp_tool_needs_no_profile_and_drops_the_footer(tmp_path):
    env = build_home(tmp_path)
    put(tmp_path / ".claude.json", {"mcpServers": {"github": {}, "sqlite": {}}})
    claude_session(tmp_path, ("mcp__github__search_issues", {}))
    payload = "".join(json.dumps(m) + "\n" for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "unused_tools", "arguments": {}}}])
    out = subprocess.run([sys.executable, "-m", "repofit", "mcp"], input=payload, env=dict(env, REPOFIT_HOME=str(tmp_path / "rf")),
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    reply = [json.loads(line) for line in out.stdout.splitlines() if line.strip()][-1]
    text = reply["result"]["content"][0]["text"]
    assert "sqlite" in text and "never called" in text and "Connect `repofit mcp`" not in text


def test_profile_path_skips_untracked_mcp_servers():
    from repofit.match import unused_tools
    profile = {"level": "L2", "environment": {"agent_ids": {"Gemini CLI": "gemini-cli", "Codex": "codex"}},
               "sources": {"Gemini CLI": {"sessions": 3}, "Codex": {"sessions": 2}},
               "installed": [{"agent": "gemini-cli", "kind": "mcp_server", "name": "playwright"},
                             {"agent": "codex", "kind": "mcp_server", "name": "puppeteer"}],
               "mcp_calls": [], "skills_used": []}
    assert [u["name"] for u in unused_tools(profile)] == ["puppeteer"]
