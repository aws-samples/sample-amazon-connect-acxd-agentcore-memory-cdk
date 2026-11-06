.PHONY: install lock bundle synth synth-placeholders test test-python test-node test-assets test-e2e scan ci preflight deploy destroy clean

PYTHON ?= python3
# Extra arguments for cdk deploy / destroy, e.g. CDK_ARGS="--profile my-profile -c key=value".
CDK_ARGS ?=
# AWS CDK CLI. Tested with 2.1138.0; e.g. CDK="npx --yes aws-cdk@2.1138.0" to pin it.
CDK ?= cdk
PIP_TOOLS_VERSION = 7.6.2
LOCKED = requirements.txt requirements-dev.txt \
	lambdas/authorizer/requirements.txt lambdas/ingest/requirements.txt \
	lambdas/read/requirements.txt lambdas/writer/requirements.txt

# Python (CDK + tests) and Node (fetch step, ACXD assets) dependencies. Python installs from
# the hash-locked requirements-dev.txt (every transitive dependency pinned with hashes),
# Node from the package-lock.json files.
install:
	$(PYTHON) -m pip install --require-hashes -r requirements-dev.txt
	cd lambdas/fetch && npm ci --no-audit --no-fund
	cd assets/acxd && npm ci --no-audit --no-fund

# Regenerate the hash-locked requirements*.txt from the requirements*.in files (direct
# dependencies). Run after changing a .in file; commit both.
lock:
	$(PYTHON) -m pip install pip-tools==$(PIP_TOOLS_VERSION)
	for f in $(LOCKED); do \
	  CUSTOM_COMPILE_COMMAND="make lock" $(PYTHON) -m piptools compile --quiet --generate-hashes \
	    --strip-extras --no-emit-index-url --annotation-style=line \
	    --output-file "$$f" "$${f%.txt}.in" || exit 1; \
	done

# Lambda packages under dist/ (vendored boto3/botocore; fetch step with npm ci).
# Required before `synth`, `deploy` and `destroy` with real context.
bundle:
	PYTHON=$(PYTHON) ./scripts/bundle_lambdas.sh

# Fails fast, credential-free, on a missing, placeholder or malformed context key (app.py).
synth:
	$(CDK) synth

# Synth with well-formed dummy values for the required keys. For CI and review only; the
# output is not deployable. Runs `bundle` first because CDK hashes the real dist/ assets.
synth-placeholders: bundle
	$(CDK) synth -c allowPlaceholders=true --quiet

# Everything that runs without AWS credentials, in one command:
#   unit tests, CDK assertions (security rules, CDK Nag), the Node fetch-step tests, the ACXD
#   asset checks (typecheck, tests, exported JSON in sync), a syntax check of the e2e tools,
#   and the pre-publication scan.
# tests/conftest.py creates stand-in dist/ bundles, so `make test` does not need `bundle`.
test: test-python test-node test-assets test-e2e scan

test-python:
	$(PYTHON) -m pytest

test-node:
	cd lambdas/fetch && node --test

test-assets:
	cd assets/acxd && npm run verify

# The e2e tools drive a live deployment; offline we only check that they parse.
test-e2e:
	$(PYTHON) -m py_compile e2e/connect_flow.py e2e/voice_call.py e2e/check_contact_memory.py scripts/verify_live_security.py
	for f in e2e/*.mjs; do node --check "$$f" || exit 1; done

# No real account ids, ARNs, resource ids, phone numbers or internal references.
scan:
	$(PYTHON) scripts/check_public.py

# Clean-checkout CI: install pinned dependencies, run every offline check, synth.
ci: install test synth-placeholders

# Live, read-only checks against the target account (AWS credentials required).
#   make preflight PREFLIGHT_ARGS="--profile my-profile"
preflight:
	$(PYTHON) scripts/preflight.py $(PREFLIGHT_ARGS)

deploy: bundle
	$(CDK) deploy --all $(CDK_ARGS)

# cdk destroy synthesizes the app first, and synth needs the dist/ bundles (a fresh clone or
# `make clean` has none), so destroy bundles too.
destroy: bundle
	$(CDK) destroy --all $(CDK_ARGS)

clean:
	rm -rf dist cdk.out .pytest_cache
