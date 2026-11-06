# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""API Gateway Lambda REQUEST authorizer for one route (design §6).

One function per route (read, write), each bound to its own Secrets Manager secret, so a
leaked read secret cannot authorize a write. The caller (ACXD) sends the secret in header
``x-acxdmem-secret``, resolved from an ACXD Secret (``{<name>:NLX.Secret}``).

- Accepts the secret's ``AWSCURRENT`` and ``AWSPREVIOUS`` values (rotation window).
- Constant-time comparison (``hmac.compare_digest``).
- Allow is scoped to the exact ``methodArn`` of the request, and only when that ARN is the
  route this authorizer is bound to (``EXPECTED_ROUTE``, e.g. ``POST/memory/read``). Never
  ``Resource: *``.
- Logs the decision and route only, never the header value.

Environment:
    SECRET_ARN       Secrets Manager secret for this route
    EXPECTED_ROUTE   ``<METHOD>/<path without leading slash>``
    SECRET_CACHE_SECONDS  in-container cache of the secret values (default 300)
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from typing import Any, Mapping

import boto3

from acxd_memory import metrics

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HEADER = "x-acxdmem-secret"

_secrets_client = None
_cache: dict[str, Any] = {"values": (), "expires": 0.0}


def _client():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def _load_secret_values(client, secret_arn: str) -> tuple[str, ...]:
    values: list[str] = []
    for stage in ("AWSCURRENT", "AWSPREVIOUS"):
        try:
            response = client.get_secret_value(SecretId=secret_arn, VersionStage=stage)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if stage == "AWSPREVIOUS" and code == "ResourceNotFoundException":
                continue  # no previous version yet
            raise
        value = response.get("SecretString")
        if value:
            values.append(value)
    return tuple(values)


def _secret_values(now: float | None = None) -> tuple[str, ...]:
    now = time.time() if now is None else now
    if _cache["values"] and now < _cache["expires"]:
        return _cache["values"]
    values = _load_secret_values(_client(), os.environ["SECRET_ARN"])
    _cache["values"] = values
    _cache["expires"] = now + int(os.environ.get("SECRET_CACHE_SECONDS", "300"))
    return values


def _header(headers: Mapping[str, Any] | None, name: str) -> str | None:
    for key, value in (headers or {}).items():
        if key.lower() == name:
            return value if isinstance(value, str) else None
    return None


def _route_matches(method_arn: str, expected_route: str) -> bool:
    # arn:aws:execute-api:<region>:<account>:<apiId>/<stage>/<METHOD>/<path>
    parts = method_arn.split(":", 5)
    if len(parts) != 6 or parts[2] != "execute-api":
        return False
    segments = parts[5].split("/", 2)
    return len(segments) == 3 and segments[2] == expected_route


def _policy(effect: str, method_arn: str) -> dict[str, Any]:
    return {
        "principalId": "acxd",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {"Action": "execute-api:Invoke", "Effect": effect, "Resource": method_arn}
            ],
        },
    }


def is_authorized(presented: str | None, accepted: tuple[str, ...]) -> bool:
    if not presented:
        return False
    presented_b = presented.encode("utf-8")
    matched = False
    for value in accepted:
        # Compare against every accepted value so timing does not reveal which one matched.
        matched |= hmac.compare_digest(presented_b, value.encode("utf-8"))
    return matched


def _warm() -> None:
    """Create the client and load the secret during Lambda init, not on the first request.

    The first request after a cold start is the customer's greeting (read route) or the exit
    (write route). A failure here is ignored:
    the handler loads the secret again and denies if it still cannot.
    """
    try:
        _secret_values()
    except Exception as exc:  # noqa: BLE001
        logger.warning("init secret prefetch failed: %s", type(exc).__name__)


if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    _warm()


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    method_arn = event.get("methodArn", "")
    expected_route = os.environ["EXPECTED_ROUTE"]
    if not _route_matches(method_arn, expected_route):
        logger.warning("deny: route mismatch (expected %s)", expected_route)
        metrics.emit("AuthorizerDeny", dimensions={"Route": expected_route, "Reason": "route"})
        return _policy("Deny", method_arn or "*")

    presented = _header(event.get("headers"), HEADER)
    if is_authorized(presented, _secret_values()):
        logger.info("allow route=%s", expected_route)
        return _policy("Allow", method_arn)

    logger.info("deny route=%s", expected_route)
    metrics.emit("AuthorizerDeny", dimensions={"Route": expected_route, "Reason": "secret"})
    return _policy("Deny", method_arn)
