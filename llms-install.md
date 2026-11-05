# Installing repofit (for AI agents)

repofit is a stdio MCP server published on PyPI as `repofit`. It needs Python 3.11 or newer. It has no
dependencies, needs no API key and reads no environment variables.

## 1. Install

If `uv` is available, skip this step and use `uvx` as the command in step 2. Otherwise:

```bash
pipx install repofit
```

`pip install repofit` into any Python 3.11+ environment works too. The command it installs is `repofit`.

## 2. Add the server to the MCP settings

For Cline, open `cline_mcp_settings.json` and add:

```json
{
  "mcpServers": {
    "repofit": {
      "command": "repofit",
      "args": ["mcp"]
    }
  }
}
```

With uv, use `"command": "uvx"` and `"args": ["repofit", "mcp"]` instead.

## 3. Check it works

The server lists ten tools, including `show_profile`, `get_evidence` and `find_candidates`. Ask:
"Use repofit to recommend some GitHub projects for me."

The first recommendation downloads the candidate index (about 10 MB) from the project's GitHub release and
stores it in `~/.repofit`. Nothing else needs to be configured.
