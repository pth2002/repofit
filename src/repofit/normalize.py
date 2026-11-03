"""Shared helpers for turning agent-specific records into model objects."""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path, PurePath

from repofit.model import ToolCall

_KINDS = {
    "shell": {
        "bash", "powershell", "shell", "exec_command", "local_shell", "shell_command",
        "run_terminal_cmd", "run_terminal_command", "run_shell_command", "execute_command",
        "commandexecution",
    },
    "read": {
        "read", "read_file", "read_file_v2", "readfile", "view", "list_dir", "list_files", "ls",
        "read_many_files", "list_directory", "notebookread", "readlints",
    },
    "edit": {
        "edit", "write", "multiedit", "notebookedit", "apply_patch", "applypatch", "edit_file",
        "edit_file_v2", "write_file", "write_to_file", "replace", "replace_in_file", "str_replace",
        "strreplace", "search_replace", "apply_diff", "filechange", "insert_content",
    },
    "search": {
        "grep", "glob", "glob_file_search", "ripgrep_raw_search", "codebase_search",
        "search_files", "file_search", "grep_search", "search_file_content", "toolsearch",
    },
    "web": {
        "websearch", "webfetch", "web_search", "web_fetch", "google_web_search", "fetch",
        "browser_action", "websearch_call",
    },
    "agent": {"agent", "task", "spawn_agent", "subagent", "new_task", "subagentactivity"},
}
_LOOKUP = {name: kind for kind, names in _KINDS.items() for name in names}

# Launchers whose next word says more than the launcher itself.
_LAUNCHERS = {"python", "python3", "py", "uv", "uvx", "npx", "pnpm", "npm", "yarn", "bunx", "pipx", "conda"}
# Shells that wrap the real command, as in `pwsh -Command "..."` or `bash -lc "..."`.
_WRAPPERS = {"pwsh", "powershell", "bash", "sh", "zsh", "cmd", "wsl"}
_WRAPPER_FLAGS = {"-command", "-c", "-lc", "/c", "-e", "--exec", "--"}
# Words that start a segment without being the program it runs.
_PREFIXES = {"do", "then", "else", "time", "nohup", "exec", "sudo", "env", "command", "&"}
_SKIP_HEADS = {"cd", "set-location", "pushd", "export", "set", "source", "echo", "write-host", "for", "while",
               "if", "foreach", "until", "case", "fi", "done", "end", "{", "}", "(", ")", "try", "rem"}
_SEPARATORS = {"&&", "||", ";", "|", "&"}
_INJECTED = ("<command-", "<local-command", "<system-reminder>", "<environment_context>",
             "<user_instructions>", "Caveat:", "[Request interrupted", "# AGENTS.md instructions",
             "<heartbeat>", "<task-notification>", "<in-app-browser-context",
             "This session is being continued from a previous conversation", "Base directory for this skill")


def tool_kind(name: str) -> str:
    n = name.lower()
    if n.startswith(("mcp__", "mcp:")) or n in ("use_mcp_tool", "mcptoolcall", "access_mcp_resource"):
        return "mcp"
    return _LOOKUP.get(n, "other")


def make_call(name: str, detail: str = "") -> ToolCall:
    return ToolCall(tool_kind(name), name, (detail or "")[:500])


def mcp_label(name: str) -> str:
    """'mcp__server__tool' -> 'server/tool'."""
    parts = name.split("__")
    return f"{parts[1]}/{'__'.join(parts[2:])}" if len(parts) >= 3 else name


def is_injected(text: str) -> bool:
    return text.lstrip().startswith(_INJECTED)


def _unquote(token: str) -> str:
    return token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'" else token.strip("\"'")


def _tokens(line: str) -> list[str]:
    line = re.sub(r"\d*>&\d+|&>", " ", line)  # 2>&1 is a redirection, not a background job
    # POSIX mode handles quoted assignments like K="-i key"; Windows paths need non-POSIX to keep backslashes.
    lex = shlex.shlex(line, posix="\\" not in line, punctuation_chars=";&|")
    lex.whitespace_split = True
    try:
        return [_unquote(t) for t in lex]
    except ValueError:  # unbalanced quotes, usually an inline script continuing on later lines
        return [_unquote(t) for t in re.findall(r"&&|\|\||[;|&]|[^\s;|&]+", line)]


def _program(token: str) -> str:
    name = PurePath(token.replace("\\", "/")).name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        name = name.removesuffix(suffix)
    return name


_WRAPPED = re.compile(r"\s(?:-command|-c|-lc|/c|-e|--exec|--)\s+(.*)", re.I | re.S)
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)")
_HERESTRING_END = re.compile(r"['\"]+@")


def command_head(cmd: str, _depth: int = 0) -> str | None:
    """Program a shell command actually runs, e.g. 'cd x && python -m pytest -q' -> 'python -m pytest'.
    Only the first meaningful program counts, so pipes into grep or head do not inflate the tally."""
    first = _tokens(cmd.lstrip().split("\n", 1)[0])
    if first and _program(first[0]) in _WRAPPERS and _depth < 3:
        m = _WRAPPED.search(cmd)
        if m:  # `pwsh -Command "<script>"`: the script is the command, the shell is noise
            return command_head(_unquote(m.group(1).strip()), _depth + 1)
    end = None  # regex for the line that closes a heredoc or PowerShell here-string
    cmd = re.sub(r"[\\`]\r?\n", " ", cmd)  # join bash `\` and PowerShell backtick line continuations
    for line in cmd.splitlines():
        stripped = line.strip()
        if end is not None:
            m = end.match(stripped)
            if not m:
                continue
            line, end = stripped[m.end():], None
        head = _line_head(line)
        if head:
            return head
        m = _HEREDOC.search(line)
        if m:
            end = re.compile(re.escape(m.group(1)) + r"\b")
        elif re.search(r"@['\"]+$", stripped):
            end = _HERESTRING_END
    return None


def _line_head(line: str) -> str | None:
    segments, current = [], []
    for t in _tokens(line):
        if t in _SEPARATORS:
            segments.append(current)
            current = []
        else:
            current.append(t)
    segments.append(current)
    for seg in segments:
        while seg and (seg[0].lower() in _PREFIXES or re.fullmatch(r"\w+=.*", seg[0], re.S)):
            seg = seg[1:]
        if len(seg) > 2 and seg[0].startswith("$") and seg[1] == "=":  # PowerShell: $out = <command>
            seg = seg[2:]
        if seg and seg[0].lower() == "timeout":
            seg = seg[2:] if len(seg) > 2 and seg[1].replace(".", "").isdigit() else seg[1:]
        if not seg or seg[0][:1] in "$<>-@#['\"(":
            continue
        head = _program(seg[0])
        if head in _SKIP_HEADS or not re.fullmatch(r"[a-z_][\w.+-]*", head):
            continue
        if head in _WRAPPERS:
            flag = next((i for i, t in enumerate(seg) if t.lower() in _WRAPPER_FLAGS), None)
            if flag is not None:
                inner = command_head(" ".join(seg[flag + 1:]), 1)
                if inner:
                    return inner
                continue
        if head in _LAUNCHERS:
            rest = [t for t in seg[1:] if t == "-m" or not t.startswith(("-", "<", ">"))]
            if rest[:1] == ["-m"] and len(rest) > 1:
                return f"{head} -m {rest[1]}"
            if rest[:1] in (["run"], ["exec"], ["x"]) and len(rest) > 1:
                return f"{head} {rest[0]} {_program(rest[1])}"
            if rest and head in ("npx", "uvx", "bunx", "pipx"):
                return f"{head} {_program(rest[0])}"
        return head
    return None


def to_dt(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            x = float(value)
            if x > 1e12:
                x /= 1000
            return datetime.fromtimestamp(x, tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.astimezone()
    except (ValueError, OverflowError, OSError):
        return None


def mtime_dt(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def iter_jsonl(path: Path, must_contain: tuple[str, ...] = ()) -> Iterator[dict]:
    """Yield JSON objects per line; lines without any `must_contain` marker are skipped unparsed."""
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for line in f:
            if must_contain and not any(m in line for m in must_contain):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def sqlite_ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def yaml_children(text: str, key: str) -> list[str]:
    """Names directly under a top-level YAML key, as a mapping (`name:`) or a list (`- name: x`).
    Deliberately tiny so the core stays dependency-free; good enough for agent config files."""
    lines = text.splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        if not re.fullmatch(rf"{re.escape(key)}:\s*(#.*)?", line.rstrip()):
            continue
        indent = None
        for child in lines[i + 1:]:
            if not child.strip() or child.lstrip().startswith("#"):
                continue
            width = len(child) - len(child.lstrip())
            if width == 0:
                break
            indent = width if indent is None else indent
            if width != indent:
                continue
            m = re.match(r"\s*-\s*name:\s*['\"]?([^'\"#]+?)['\"]?\s*$", child) or re.match(r"\s*['\"]?([\w.@/-]+)['\"]?:", child)
            if m:
                out.append(m.group(1))
        break
    return out


def text_parts(content) -> str:
    """Flatten str | [{'type': 'text', 'text': ...}] | {'text': ...} into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or "")
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict) and part.get("type") in (None, "text", "input_text") and part.get("text"):
                out.append(part["text"])
        return "\n".join(out)
    return ""


def shell_detail(args) -> str:
    """Command string from the many shapes shell tool arguments take."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args
    if not isinstance(args, dict):
        return ""
    cmd = args.get("command") or args.get("cmd") or args.get("CommandLine") or ""
    if isinstance(cmd, list):
        if len(cmd) >= 3 and cmd[1] in ("-lc", "-c", "-Command", "/c"):
            return str(cmd[2])
        return " ".join(map(str, cmd))
    return str(cmd)
