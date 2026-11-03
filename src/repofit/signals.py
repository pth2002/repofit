"""Evidence pulled from sessions without any model call. The recommender will reason over this."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from repofit.model import Session
from repofit.normalize import command_head

# Commands every agent runs all day. They say nothing about the user, so profiles drop them.
GENERIC_COMMANDS = {
    "cat", "grep", "rg", "sed", "awk", "ls", "dir", "head", "tail", "cp", "mv", "rm", "mkdir", "touch", "chmod",
    "find", "sort", "uniq", "wc", "cut", "xargs", "tee", "diff", "printf", "true", "test", "pwd", "which", "where",
    "sleep", "start-sleep", "get-content", "set-content", "add-content", "get-childitem", "select-string",
    "foreach-object", "where-object", "select-object", "sort-object", "measure-object", "group-object",
    "out-file", "test-path", "new-item", "remove-item", "copy-item", "move-item", "get-item", "join-path",
    "split-path", "resolve-path", "get-location", "write-output", "format-table", "format-list",
    "convertfrom-json", "convertto-json", "get-process", "stop-process", "get-date", "get-command",
    "python", "python3", "py", "node", "bash", "sh", "pwsh", "powershell", "cmd", "git", "type", "more",
    "python -m py_compile", "clear", "exit", "read", "basename", "dirname", "realpath", "stat", "file", "du", "df",
    "date", "env", "export", "kill", "ps", "whoami", "hostname", "nl", "tr", "base64", "md5sum", "sha256sum",
}


# PowerShell cmdlets with these verbs are the Windows equivalent of coreutils.
_CMDLET = re.compile(r"(get|set|new|remove|add|start|stop|push|pop|select|where|foreach|sort|measure|test|import|"
                     r"export|convertto|convertfrom|out|write|format|join|split|resolve|copy|move|rename|clear|wait|"
                     r"register|unregister|enable|disable|invoke|expand|compress|read)-[a-z]+")
# Shell keywords, and code lines that leak in from inline scripts the parser could not fence off.
_KEYWORDS = {"break", "continue", "function", "param", "return", "throw", "catch", "finally", "switch", "elseif",
             "else", "while", "trap", "exit", "local", "declare", "unset", "shift", "wait", "stop", "import", "from",
             "def", "class", "print", "traceback", "program", "const", "let", "var", "with", "try", "except", "raise",
             "assert", "async", "await", "lambda", "begin", "end"}


# A skill read straight from disk, as Codex does: .../skills/<name>/SKILL.md
_SKILL_FILE = re.compile(r"skills[\\/]+(?:[\w.-]+[\\/]+)*?([\w.-]+)[\\/]+SKILL\.md", re.I)


def notable_commands(commands: Counter, limit: int = 40) -> list[tuple[str, int]]:
    return [(c, n) for c, n in commands.most_common()
            if c not in GENERIC_COMMANDS and c not in _KEYWORDS and not _CMDLET.fullmatch(c) and n >= 2][:limit]


@dataclass
class Signals:
    sessions: int = 0
    user_messages: int = 0
    by_agent: Counter = field(default_factory=Counter)
    tool_kinds: Counter = field(default_factory=Counter)
    commands: Counter = field(default_factory=Counter)
    mcp: Counter = field(default_factory=Counter)
    mcp_by_agent: Counter = field(default_factory=Counter)  # (agent, server) -> calls
    skills: Counter = field(default_factory=Counter)  # (agent, skill) -> uses
    web_searches: int = 0
    projects: Counter = field(default_factory=Counter)
    first: datetime | None = None
    last: datetime | None = None

    def add(self, s: Session) -> None:
        self.sessions += 1
        self.user_messages += len(s.user_messages)
        self.by_agent[s.agent] += 1
        if s.project:
            self.projects[s.project] += 1
        if s.started_at:
            self.first = min(self.first or s.started_at, s.started_at)
            self.last = max(self.last or s.started_at, s.started_at)
        for call in s.tool_calls:
            self.tool_kinds[call.kind] += 1
            if call.name.lower() in ("skill", "skill_view") and call.detail:
                self.skills[(s.agent, call.detail.split(":")[-1])] += 1
            if call.kind == "shell" and call.detail:
                head = command_head(call.detail)
                if head:
                    self.commands[head] += 1
                for skill in set(_SKILL_FILE.findall(call.detail)):
                    self.skills[(s.agent, skill)] += 1
            elif call.kind == "mcp" and call.detail:
                self.mcp[call.detail.split("/")[0]] += 1
                self.mcp_by_agent[(s.agent, call.detail.split("/")[0])] += 1
            elif call.kind == "web":
                self.web_searches += 1


def within(s: Session, days: int | None) -> bool:
    if not days or not s.started_at:
        return True
    return s.started_at >= datetime.now(timezone.utc) - timedelta(days=days)
