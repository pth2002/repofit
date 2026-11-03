import pytest

from repofit.normalize import command_head, is_injected, mcp_label, tool_kind

HEADS = [
    (r'"C:\Program Files\PowerShell\7\pwsh.exe" -NoProfile -Command "Get-ChildItem -Force"', "get-childitem"),
    (r'powershell.exe -Command "cd D:\x; python -m pytest -q"', "python -m pytest"),
    ("cd /d/x && python - <<'EOF'\nimport json\nprint(1)\nEOF", "python"),
    (r'python "D:\a b\run.py" --x 1', "python"),
    ("for f in *.py; do python $f; done", "python"),
    ('timeout 60 ssh -p 22 root@host "ls"', "ssh"),
    ("npx prettier --write .", "npx prettier"),
    ("uv run pytest", "uv run pytest"),
    ('bash -lc "git status && git diff"', "git"),
    ("FOO=1 npm run build", "npm run build"),
    ('wsl -e bash -lc "nvidia-smi"', "nvidia-smi"),
    ("echo don't && rg foo", "rg"),
    ('"C:\\Users\\x\\pwsh.exe" -Command "@\'\nimport ast\nprint(1)\n\'@ | python -"', "python"),
    ("cat > f.py <<'EOF'\nimport x\nEOF\npython f.py", "cat"),
    ('cd D:/x/generated; python -c "\nimport csv\nprint(1)"', "python"),
    ('H="root@h" K="-i $HOME/.ssh/key -p 22"; ssh $K $H \'ls\'', "ssh"),
    ("npm.cmd run dist", "npm run dist"),
    ("ssh -p 22 \\\n  root@h 'ls'", "ssh"),
    ("python run.py 2>&1 | tail -5", "python"),
]


@pytest.mark.parametrize("cmd,want", HEADS)
def test_command_head(cmd, want):
    assert command_head(cmd) == want


def test_tool_kinds():
    assert tool_kind("Bash") == "shell"
    assert tool_kind("mcp__github__search") == "mcp"
    assert tool_kind("apply_patch") == "edit"
    assert mcp_label("mcp__github__search_issues") == "github/search_issues"


def test_injected_messages():
    assert is_injected("<task-notification> done")
    assert is_injected("This session is being continued from a previous conversation that ran out")
    assert not is_injected("please refactor the parser")
