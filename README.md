# repofit

Find GitHub projects that fit you, using what your coding agents already know about you.

GitHub has thousands of high-star repositories, and most lists of "tools you should try" are written for
everyone at once. Your coding agents know you better. Claude Code, Codex, Cursor and the rest keep your
memory files, your instructions and the history of what you asked them to do. repofit reads that on your
machine, writes a one-page profile, and recommends repositories you probably have not heard of and would
want. Every pick names the task in your history that it serves.

It also tells you which installed skills and MCP servers none of your sessions ever called. Each one costs
context on every request.

```text
 1. owner/project  ★2.1k  Python  [install]
    for       T3 turn experiment logs into paper figures
    why       You redraw matplotlib figures by hand after every run; this applies a
              publication style and fixes the layout in one call.
    seen in   S41 2026-10-20 codex: "这张图的配色和留白还是不对，再改一版…"
    https://github.com/owner/project

Installed but never called in your sessions:
  - claude-code: 5 skills unused in 81 sessions: canvas-design, pdf, ...
```

*(Illustrative output. Explanations come in the language you choose.)*

## Install

Python 3.11 or newer. There are no dependencies.

```bash
pipx install repofit
# or the latest code from GitHub
pipx install git+https://github.com/pth2002/repofit
```

The first run downloads the candidate index (about 18,000 active repositories, rebuilt every week).

## Use it from your agent

repofit is an MCP server. Your agent's own model does the reasoning, so you need no API key.

**Claude Code**

```bash
claude mcp add repofit -- repofit mcp
```

**Codex** (`~/.codex/config.toml`)

```toml
[mcp_servers.repofit]
command = "repofit"
args = ["mcp"]
```

**Cursor** (`~/.cursor/mcp.json`), **Gemini CLI** and **Qwen Code** (`settings.json`)

```json
{ "mcpServers": { "repofit": { "command": "repofit", "args": ["mcp"] } } }
```

Then ask in a new session:

- "Use repofit to recommend some GitHub projects for me."
- "Does github.com/owner/name fit me?"
- "Is there a good tool for turning PDFs into Markdown?"
- "Which of my installed skills do I never use?"

The first recommendation takes a few minutes: the agent reads your history, writes your profile and search
terms, then ranks about 30 candidates. Later requests reuse the profile.

## Use it from the command line

```bash
repofit scan                                  # which agents are installed and what each one stores
repofit profile build --level L2 --provider deepseek
repofit recommend --provider deepseek --lang zh
```

Providers: `deepseek`, `openai`, `ollama`, `anthropic`, or any OpenAI-compatible endpoint with
`--provider compatible --base-url ... --model ...`. Keys come from the usual environment variables
(`DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). repofit shows how many characters it will send
and asks before sending anything.

Without any provider:

```bash
repofit profile prompt --level L2 > prompt.md   # paste into any agent, save its JSON answer
repofit profile apply answer.json
repofit profile expand --prompt                 # same for the search terms, then --apply
repofit recommend --prompt --lang en            # same for the ranking, then --apply
repofit recommend --no-llm                      # keyword retrieval only, nothing leaves your machine
```

## Privacy

- Everything runs locally. repofit reads the files your agents already keep under your home directory and
  writes its own files to `~/.repofit`.
- You choose how much it reads. `L0`: installed tools and configs. `L1` (default): plus memory and
  instruction files. `L2`: plus your own messages and the tool calls in your sessions.
- Text is redacted before any model sees it: API keys and tokens, private keys, passwords, email addresses,
  phone numbers and random-looking strings such as a password pasted after an `ssh` command. Your home
  directory becomes `~`.
- In MCP mode the text goes to the model your agent already uses. In CLI mode it goes only to the provider
  you name, after you confirm.
- Your memory files and other people's READMEs can contain instructions. repofit marks everything it returns
  as data, and the agent is told not to follow instructions found inside it.

## Supported agents

| Agent | Sessions | Memory and instructions | Installed tools | Status |
|---|---|---|---|---|
| Claude Code | yes | `CLAUDE.md`, project memory | skills, plugins, MCP servers | tested on real data |
| Codex | yes (CLI and desktop) | `AGENTS.md`, `memories/` | skills, plugins, MCP servers | tested on real data |
| Cursor | yes | user rules, pending memories | MCP servers, extensions | tested on real data |
| Hermes Agent | yes | `USER.md`, `MEMORY.md` | skills, MCP servers | tested on real data |
| Gemini CLI, Qwen Code | yes | `GEMINI.md`, `QWEN.md` | MCP servers, extensions | written from docs |
| Cline, Roo Code, Kilo Code | yes | `.clinerules` | MCP servers | written from docs |
| Continue | yes | `rules/` | MCP servers | written from docs |
| VS Code Copilot | yes | `copilot-instructions.md` | MCP servers, extensions | written from docs |
| Kimi CLI, Aider | yes | | | written from docs |
| OpenCode | counted only | `AGENTS.md` | MCP servers | partial |
| Windsurf, Claude Desktop, WorkBuddy, Trae | no local sessions | memories where present | MCP servers | partial |

Instruction files inside your projects (`AGENTS.md`, `CLAUDE.md`, `.cursorrules` and others) are read too.
If your agent stores data somewhere repofit misses, `repofit scan` shows it as not found; issues with the
paths are welcome.

## How it works

1. **Read.** One adapter per agent turns its sessions, memory files and installed tools into the same
   records.
2. **Profile.** A model writes a one-page profile from a redacted, numbered evidence pack: your recurring
   tasks, interests, preferences and constraints. Every claim cites the evidence it came from, and repofit
   drops claims whose citations do not exist.
3. **Expand.** The model lists project names and search phrases for each task, so retrieval can find the
   libraries a task depends on even when your own words never mention them.
4. **Retrieve.** BM25 over each repository's name, description, topics and README opening, one query per task
   and interest. Repositories you already use are set aside first. New and trending repositories get a small
   boost.
5. **Rank.** The model picks the candidates you most likely do not know and would want, and says what each
   one changes. Picks that point at unknown candidates or tasks are dropped.

The candidate index comes from the GitHub API: active repositories above 3,000 stars, new repositories above
1,000, agent-ecosystem topics (MCP, Claude Code, Codex, agent skills) above 300, GitHub trending and three
curated lists. A GitHub Action rebuilds it every Monday.

## How well it works

So far it has been measured on one person, the author, with blind ratings: recommendations from different
versions were mixed, shown without their source, and rated one by one. In the current version, 10 of 10
picks were rated useful, and 8 of those were projects the author did not know before. The first version
scored 5 of 10 useful and 0 new. That is a single user, and the second version was tuned after seeing the
first ratings, so ratings from other people are the next step.

`repofit eval prepare --cutoff YYYY-MM-DD` runs a time-split check on your own history: it rebuilds your
profile as of that date and compares the picks with what you started using afterwards.

## Development

```bash
git clone https://github.com/pth2002/repofit && cd repofit
python -m pip install -e . pytest
python -m pytest
repofit index build      # rebuild the index yourself; needs a GitHub token (GITHUB_TOKEN or `gh auth login`)
```

## License

MIT
