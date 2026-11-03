"""Check the files that would be published for personal data before a release.

Run from the repo root:  python tools/leak_audit.py --term <name> --term <host> ...
Built-in checks: email addresses (GitHub noreply excepted), Windows and macOS home paths with a real user name,
phone numbers, IPv4 addresses and random-looking tokens. Extra --term values are matched case-insensitively.
Lines marked `leak-audit: ok` hold deliberate fake data and are skipped. Exit code 1 when anything is found.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT = {".py", ".md", ".toml", ".yml", ".yaml", ".txt", ".json", ".cfg", ".ini", ".html", ".js", ".css", ""}
CHECKS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com)(?!example\.)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "home path": re.compile(r"(?i)(?:C:\\\\?Users\\\\?|/Users/|/home/)(?!<|\{|name|user|you|runner|example)[A-Za-z0-9._-]{2,}"),
    "phone": re.compile(r"(?<![\d.])(?:\+?86[- ]?)?1[3-9]\d{9}(?![\d.])"),
    "ipv4": re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    "token": re.compile(r"(?<![\w/.\\-])(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{16,}(?![\w/.\\-])"),
}
ALLOWED_IPS = {"127.0.0.1", "0.0.0.0"}


def published_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
        return [ROOT / f for f in out]
    except (OSError, subprocess.CalledProcessError):  # not a git checkout yet: honour .gitignore by hand
        ignore = [l.strip() for l in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
                  if l.strip() and not l.startswith("#")] if (ROOT / ".gitignore").is_file() else []
        files = []
        for p in ROOT.rglob("*"):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and ".git/" not in rel and not any(
                    fnmatch.fnmatch(rel, pat.rstrip("/")) or fnmatch.fnmatch(p.name, pat.rstrip("/"))
                    or any(fnmatch.fnmatch(part, pat.rstrip("/")) for part in p.relative_to(ROOT).parts)
                    for pat in ignore):
                files.append(p)
        return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--term", action="append", default=[], help="a private string that must not appear")
    args = parser.parse_args()
    terms = [re.compile(re.escape(t), re.I) for t in args.term]
    found = 0
    for path in sorted(published_files()):
        if path.suffix.lower() not in TEXT or path.resolve() == Path(__file__).resolve():
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if "leak-audit: ok" in line:  # deliberate fake data, such as the samples the redaction tests feed in
                continue
            hits = [(name, m.group(0)) for name, rx in CHECKS.items() for m in rx.finditer(line)]
            hits = [(name, h) for name, h in hits if not (name == "ipv4" and h in ALLOWED_IPS)]
            hits += [("term", m.group(0)) for rx in terms for m in rx.finditer(line)]
            for name, hit in hits:
                found += 1
                print(f"{path.relative_to(ROOT)}:{n}: {name}: {hit}")
    print(f"{found} finding(s) in {len(published_files())} files", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
