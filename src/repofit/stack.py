"""What a project is built with, read from its manifests and file types."""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

EXT_LANG = {
    ".py": "Python", ".ipynb": "Jupyter", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".mjs": "JavaScript", ".vue": "Vue", ".svelte": "Svelte", ".rs": "Rust", ".go": "Go",
    ".java": "Java", ".kt": "Kotlin", ".scala": "Scala", ".rb": "Ruby", ".php": "PHP", ".r": "R", ".swift": "Swift",
    ".c": "C", ".h": "C/C++", ".cpp": "C++", ".cc": "C++", ".hpp": "C++", ".cu": "CUDA", ".cs": "C#",
    ".dart": "Dart", ".lua": "Lua", ".jl": "Julia", ".sh": "Shell", ".ps1": "PowerShell", ".tex": "LaTeX",
    ".sql": "SQL", ".sol": "Solidity", ".zig": "Zig", ".ex": "Elixir", ".hs": "Haskell",
}
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", ".next", "target",
    ".mypy_cache", ".pytest_cache", "site-packages", ".idea", ".vscode", ".tox", "vendor", ".cache",
    ".gradle", "out", "coverage", ".turbo", "wandb", "checkpoints", "outputs",
}
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


@dataclass
class ProjectStack:
    path: str
    files: Counter = field(default_factory=Counter)  # language -> file count
    manifests: list[str] = field(default_factory=list)
    dependencies: set[str] = field(default_factory=set)


def scan_project(root: Path, max_files: int = 3000, max_depth: int = 4) -> ProjectStack:
    stack = ProjectStack(str(root))
    seen = 0
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - base_depth
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")] if depth < max_depth else []
        for name in filenames:
            lang = EXT_LANG.get(os.path.splitext(name)[1].lower())
            if lang:
                stack.files[lang] += 1
            if depth <= 1 and name in _PARSERS:
                stack.manifests.append(name)
                try:
                    stack.dependencies |= _PARSERS[name](Path(dirpath) / name)
                except (OSError, ValueError, tomllib.TOMLDecodeError):
                    pass
            seen += 1
        if seen >= max_files:
            break
    return stack


def _pep508(specs) -> set[str]:
    out = set()
    for spec in specs or []:
        m = _REQ_NAME.match(str(spec))
        if m:
            out.add(m.group(1).lower())
    return out


def _pyproject(path: Path) -> set[str]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project") or {}
    deps = _pep508(project.get("dependencies"))
    for extra in (project.get("optional-dependencies") or {}).values():
        deps |= _pep508(extra)
    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
    deps |= {k.lower() for k in poetry if k.lower() != "python"}
    return deps


def _requirements(path: Path) -> set[str]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return _pep508(line for line in lines if line.strip() and not line.lstrip().startswith(("#", "-")))


def _package_json(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k for section in ("dependencies", "devDependencies") for k in (data.get(section) or {})}


def _cargo(path: Path) -> set[str]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return set(data.get("dependencies") or {}) | set(data.get("dev-dependencies") or {})


def _gomod(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"^\s*(?:require\s+)?([a-z0-9.-]+\.[a-z]{2,}/[^\s]+)\s+v", text, re.M))


def _gemfile(path: Path) -> set[str]:
    return set(re.findall(r"^\s*gem\s+['\"]([^'\"]+)", path.read_text(encoding="utf-8", errors="replace"), re.M))


def _composer(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k for k in (data.get("require") or {}) if "/" in k}


_PARSERS = {
    "pyproject.toml": _pyproject, "requirements.txt": _requirements, "requirements-dev.txt": _requirements,
    "package.json": _package_json, "Cargo.toml": _cargo, "go.mod": _gomod, "Gemfile": _gemfile,
    "composer.json": _composer,
}
