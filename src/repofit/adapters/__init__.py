from repofit.adapters.base import Adapter
from repofit.adapters.claude_code import ClaudeCode
from repofit.adapters.cline import Cline, KiloCode, RooCode
from repofit.adapters.codex import Codex
from repofit.adapters.continue_dev import ContinueDev
from repofit.adapters.cursor import Cursor
from repofit.adapters.gemini import GeminiCli, QwenCode
from repofit.adapters.hermes import Hermes
from repofit.adapters.kimi import Kimi
from repofit.adapters.others import Aider, ClaudeDesktop, OpenCode, Trae, VSCodeCopilot, Windsurf, WorkBuddy

ALL = [
    ClaudeCode, Codex, Cursor, GeminiCli, QwenCode, VSCodeCopilot, Cline, RooCode, KiloCode,
    ContinueDev, Kimi, OpenCode, Windsurf, Trae, Aider, Hermes, WorkBuddy, ClaudeDesktop,
]


def all_adapters() -> list[Adapter]:
    return [cls() for cls in ALL]
