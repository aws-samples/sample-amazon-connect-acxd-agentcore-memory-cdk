# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every tracked source file carries the aws-samples MIT-0 header (matches LICENSE)."""

from __future__ import annotations

# Runs git with fixed arguments only.
import subprocess  # nosec B404
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SOURCE_SUFFIXES = {".py", ".sh", ".mjs", ".js", ".cjs", ".ts"}
COPYRIGHT = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
SPDX = "SPDX-License-Identifier: MIT-0"


def _tracked_sources() -> list[Path]:
    # Fixed argv, no shell.
    out = subprocess.run(  # nosec B603 B607
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    return [REPO / p for p in out.stdout.split() if Path(p).suffix in SOURCE_SUFFIXES]


def test_license_file_is_mit0():
    # MIT-0 is the MIT text without the attribution clause.
    text = (REPO / "LICENSE").read_text()
    assert "Permission is hereby granted, free of charge" in text
    assert "shall be included in all" not in text


def test_every_source_file_has_the_spdx_header():
    sources = _tracked_sources()
    assert sources
    missing = []
    for path in sources:
        head = "".join(path.read_text().splitlines(keepends=True)[:4])
        if COPYRIGHT not in head or SPDX not in head:
            missing.append(str(path.relative_to(REPO)))
    assert not missing, f"missing MIT-0 header: {missing}"
