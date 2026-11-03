"""Smoke tests for the adapters written from public docs, using fixtures shaped like those docs.
They prove the code paths run; real-format checks still need real installs."""

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def put(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def build_home(root: Path) -> dict:
    if sys.platform == "win32":
        app = root / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        app = root / "Library" / "Application Support"
    else:
        app = root / ".config"
    proj = root / "work" / "demo"
    proj.mkdir(parents=True)
    put(proj / ".aider.chat.history.md", "# aider chat started at 2026-10-01 10:00:00\n\n#### add a cli flag\n\nok\n")
    put(root / ".claude.json", {"projects": {str(proj): {"mcpServers": {"github": {}}}}})
    put(root / ".gemini" / "GEMINI.md", "prefer typescript")
    put(root / ".gemini" / "settings.json", {"mcpServers": {"playwright": {}}})
    put(root / ".gemini" / "tmp" / "abc" / "chats" / "session-1.json", {
        "sessionId": "g1", "startTime": "2026-10-02T08:00:00Z",
        "messages": [{"type": "user", "content": "convert these pdfs to markdown"},
                     {"type": "gemini", "content": "",
                      "toolCalls": [{"name": "run_shell_command", "args": {"command": "pandoc a.pdf"}}]}]})
    put(root / ".continue" / "sessions" / "s1.json", {
        "sessionId": "c1", "title": "Refactor auth", "workspaceDirectory": str(proj),
        "history": [{"message": {"role": "user", "content": [{"type": "text", "text": "refactor auth"}]}}]})
    put(root / ".continue" / "config.yaml", "name: x\nmcpServers:\n  - name: sqlite\n    command: uvx\n")
    cline = app / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"
    put(cline / "tasks" / "1760000000000" / "api_conversation_history.json", [
        {"role": "user", "content": [{"type": "text", "text": "<task>\nbuild a scraper\n</task>"}]},
        {"role": "assistant", "content": [{"type": "text",
                                           "text": "<execute_command>\n<command>pip install scrapy</command>\n</execute_command>"}]}])
    put(root / ".kimi" / "sessions" / "md5" / "sess1" / "context.jsonl",
        json.dumps({"role": "user", "content": "write a crawler"}) + "\n")
    put(app / "Code" / "User" / "workspaceStorage" / "h1" / "chatSessions" / "x.json", {
        "sessionId": "v1", "creationDate": 1760000000000, "requests": [{"message": {"text": "explain this regex"}}]})
    # Agent home overrides from the real environment would point back at real data, so they are removed.
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME")}
    return dict(env, USERPROFILE=str(root), HOME=str(root), APPDATA=str(app),
                XDG_CONFIG_HOME=str(root / ".config"), XDG_DATA_HOME=str(root / ".local" / "share"),
                PYTHONPATH=str(SRC), PYTHONIOENCODING="utf-8")


def run(env, *args):
    out = subprocess.run([sys.executable, "-m", "repofit", *args], env=env, capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_doc_shaped_sessions_are_read(tmp_path):
    env = build_home(tmp_path)
    sessions = [json.loads(line) for line in run(env, "sessions", "--json").splitlines()]
    first = {s["agent"]: (s["user_messages"] or [""])[0] for s in sessions}
    assert first["gemini-cli"] == "convert these pdfs to markdown"
    assert first["continue"] == "refactor auth"
    assert "build a scraper" in first["cline"]
    assert first["kimi"] == "write a crawler"
    assert first["vscode-copilot"] == "explain this regex"
    assert first["aider"] == "add a cli flag"


def test_doc_shaped_inventory_is_read(tmp_path):
    env = build_home(tmp_path)
    inventory = run(env, "inventory")
    for name in ("playwright", "sqlite", "github"):
        assert name in inventory
