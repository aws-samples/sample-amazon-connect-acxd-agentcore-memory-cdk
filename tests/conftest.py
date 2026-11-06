# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared pytest fixtures.

``minimal_lambda_bundles`` creates stand-in ``dist/<name>`` directories (handler + shared
modules, no vendored dependencies) so CDK synth tests run hermetically without
``make bundle``. Pattern from the AI Agents sample. Existing bundles are left untouched.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PY_LAMBDAS = ("authorizer", "read", "ingest", "writer")


@pytest.fixture(scope="session", autouse=True)
def minimal_lambda_bundles():
    dist_dir = REPO_ROOT / "dist"
    created: list[Path] = []
    for name in (*PY_LAMBDAS, "fetch"):
        out_dir = dist_dir / name
        if out_dir.exists():
            continue
        out_dir.mkdir(parents=True)
        created.append(out_dir)
        if name == "fetch":
            shutil.copy(REPO_ROOT / "lambdas" / "fetch" / "index.mjs", out_dir / "index.mjs")
        else:
            shutil.copy(REPO_ROOT / "lambdas" / name / "lambda_function.py", out_dir)
            shutil.copytree(
                REPO_ROOT / "src" / "acxd_memory",
                out_dir / "acxd_memory",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
    yield
    for out_dir in created:
        shutil.rmtree(out_dir, ignore_errors=True)
    if dist_dir.exists() and not any(dist_dir.iterdir()):
        dist_dir.rmdir()


def load_lambda(name: str):
    """Import ``lambdas/<name>/lambda_function.py`` under a unique module name."""
    module_name = f"acxdmem_lambda_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = REPO_ROOT / "lambdas" / name / "lambda_function.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    """Offline AWS settings for boto3 clients used with Stubber (no real credentials)."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
