#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pre-publication scan: no real account data and no internal references in the repo.

Scans every file git would publish (tracked plus untracked, not ignored), except lockfiles
and binary files, for:

- AWS account ids and ARNs with an account that is not a documentation placeholder;
- UUIDs, 32-hex profile ids, API Gateway ids and AgentCore Memory ids that are not
  placeholders (live contact, conversation, flow and resource ids);
- phone numbers outside the fictional +1-555-01xx range;
- hosts and terms that only exist inside the company that wrote the sample.

``--deny-file`` adds literal strings (one per line) that must not appear, for example the
ids of a live test deployment kept outside the repo. Exit code 1 on any finding.

Usage:
    python3 scripts/check_public.py [--deny-file ids.txt] [--root .]
"""

from __future__ import annotations

import argparse
import re
# Runs git with fixed arguments only.
import subprocess  # nosec B404
import sys
from pathlib import Path

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip"}
SKIP_NAMES = {"package-lock.json"}
# This file names the patterns it looks for.
SKIP_PATHS = {"scripts/check_public.py", "tests/test_check_public.py"}

# Documentation placeholders that may appear anywhere.
PLACEHOLDER_ACCOUNTS = {"111111111111", "111122223333", "123456789012", "000000000000"}
# Real random ids practically never contain these runs; the repo's placeholders always do.
PLACEHOLDER_UUID = re.compile(r"0000-4000-8000-|^([0-9a-f])\1{7}-", re.I)

CHECKS: list[tuple[str, re.Pattern[str]]] = [
    ("account id", re.compile(r"(?<![0-9A-Za-z-])\d{12}(?![0-9A-Za-z])")),
    ("arn account", re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:(\d{12}):")),
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),
    ("profile id", re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")),
    ("api gateway id", re.compile(r"\b([a-z0-9]{10})\.execute-api\.")),
    ("memory id", re.compile(r"\b[a-z0-9_]+_memory-([A-Za-z0-9]{10})\b")),
    ("phone number", re.compile(r"(?<![\w+])\+[1-9]\d{9,14}\b")),
    (
        "internal host",
        re.compile(
            r"\b(?:[\w-]+\.)*(?:a2z\.com|amazon\.dev|aws\.dev|corp\.amazon\.com|amazon\.corp)\b"
            r"|\b(?:w|issues|t|sim|code|phonetool|quip-amazon|taskei|sage|builderhub|kiro)"
            r"\.amazon\.com\b",
            re.I,
        ),
    ),
    (
        "internal term",
        re.compile(
            r"\b(?:LilyNLX\w*|Epoxy\w*|Fable|KiRoom|Obsidian|Midway|mwinit|PCSR|Taskei|"
            r"heisventui|margerbe|ProtoShield|Aristotle|ASBX|CodeDefender|Isengard|"
            r"Golden Path)\b|/Users/|\bGate [A-Z]\b|\btask [0-9a-f]{8}\b",
            re.I,
        ),
    ),
]

FICTIONAL_PHONE = re.compile(r"^\+1555(?:555)?01\d\d$")
PLACEHOLDER_PROFILE = re.compile(r"0{12}|^([0-9a-f])\1{31}$")
PLACEHOLDER_API = {"abcdef1234", "abc123defg", "a1b2c3d4e5", "placeholdr"}


def _allowed(kind: str, match: re.Match[str]) -> bool:
    text = match.group(0)
    if kind == "account id":
        return text in PLACEHOLDER_ACCOUNTS or set(text) <= {"0"}
    if kind == "arn account":
        return match.group(1) in PLACEHOLDER_ACCOUNTS
    if kind == "uuid":
        return bool(PLACEHOLDER_UUID.search(text))
    if kind == "profile id":
        return bool(PLACEHOLDER_PROFILE.search(text))
    if kind == "api gateway id":
        return match.group(1) in PLACEHOLDER_API
    if kind == "memory id":
        return match.group(1) in {"AbCdEf1234", "EXAMPLE123", "XXXXXXXXXX"}
    if kind == "phone number":
        return bool(FICTIONAL_PHONE.match(text))
    return False


def publishable_files(root: Path) -> list[Path]:
    # Fixed argument list, no shell, no external input. git is resolved from PATH.
    proc = subprocess.run(  # nosec B603 B607 # nosemgrep: dangerous-subprocess-use-audit
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=root, check=True, capture_output=True, text=True,
    )
    out = proc.stdout.splitlines()
    files = []
    for rel in out:
        p = root / rel
        if rel in SKIP_PATHS or p.name in SKIP_NAMES or p.suffix.lower() in SKIP_SUFFIXES:
            continue
        if p.is_file():
            files.append(p)
    return files


def scan_text(text: str, deny: list[str]) -> list[tuple[int, str, str]]:
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for kind, pattern in CHECKS:
            for m in pattern.finditer(line):
                if not _allowed(kind, m):
                    findings.append((lineno, kind, m.group(0)))
        for literal in deny:
            if literal in line:
                findings.append((lineno, "denied literal", literal))
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--deny-file")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    deny = []
    if args.deny_file:
        deny = [s.strip() for s in Path(args.deny_file).read_text().splitlines() if s.strip()]

    total = 0
    files = publishable_files(root)
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, kind, value in scan_text(text, deny):
            total += 1
            shown = value if kind != "denied literal" else value[:4] + "…"
            print(f"{path.relative_to(root)}:{lineno}: {kind}: {shown}")
    print(f"check_public: {len(files)} files, {len(deny)} denied literals, {total} findings")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
