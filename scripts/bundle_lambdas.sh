#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Build the Lambda deployment packages under dist/ (pattern from the AI Agents sample).
#
# Python Lambdas (authorizer, read, ingest, writer):
#   dist/<name>/lambda_function.py   from lambdas/<name>/
#   dist/<name>/acxd_memory/         shared modules from src/acxd_memory
#   dist/<name>/boto3, botocore ...  vendored with `pip install -t --require-hashes` from the
#                                    hash-locked lambdas/<name>/requirements.txt
# boto3/botocore are vendored because the managed runtime's copy can predate the
# bedrock-agentcore service model. `pip install -t` needs no Docker daemon.
#
# Node.js fetch step:
#   dist/fetch/index.mjs, package.json, node_modules/ (`npm ci --omit=dev`, lockfile-pinned)
#
# Run from anywhere: ./scripts/bundle_lambdas.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$HERE/dist"
PY_VERSION="3.13"
PY_LAMBDAS=(authorizer read ingest writer)
# pip of the active Python (the venv from `make install`), not whatever pip3 is on PATH.
PIP=(python3 -m pip)
if [[ -n "${PYTHON:-}" ]]; then PIP=("$PYTHON" -m pip); fi

echo "==> cleaning $DIST_DIR"
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

for name in "${PY_LAMBDAS[@]}"; do
  SRC_DIR="$HERE/lambdas/$name"
  OUT_DIR="$DIST_DIR/$name"
  echo "==> bundling $name"
  mkdir -p "$OUT_DIR"
  cp "$SRC_DIR/lambda_function.py" "$OUT_DIR/"
  cp -R "$HERE/src/acxd_memory" "$OUT_DIR/acxd_memory"
  if [[ -f "$SRC_DIR/requirements.txt" ]]; then
    # requirements.txt is the hash-locked output of `make lock` (every transitive
    # dependency pinned with its hashes), so the bundle is the audited set. No --upgrade:
    # $OUT_DIR is fresh.
    "${PIP[@]}" install \
      --require-hashes \
      --no-deps \
      --requirement "$SRC_DIR/requirements.txt" \
      --target "$OUT_DIR" \
      --platform manylinux2014_x86_64 \
      --implementation cp \
      --python-version "$PY_VERSION" \
      --only-binary=:all: \
      --quiet
  fi
  find "$OUT_DIR" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$OUT_DIR" -name "*.dist-info" -type d -prune -exec rm -rf {} + 2>/dev/null || true
  echo "    done: $(du -sh "$OUT_DIR" | cut -f1)"
done

echo "==> bundling fetch (Node.js)"
OUT_DIR="$DIST_DIR/fetch"
mkdir -p "$OUT_DIR"
cp "$HERE/lambdas/fetch/index.mjs" "$HERE/lambdas/fetch/package.json" \
   "$HERE/lambdas/fetch/package-lock.json" "$OUT_DIR/"
(cd "$OUT_DIR" && npm ci --omit=dev --ignore-scripts --no-audit --no-fund --silent)
echo "    done: $(du -sh "$OUT_DIR" | cut -f1)"

echo "==> all Lambda bundles ready under $DIST_DIR"
