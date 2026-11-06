# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Conversation-start memory read: profileId -> RetrieveMemoryRecords -> prompt context.

Log lines never carry the
``profileId`` or a namespace built from it (hashed id plus the namespace suffix only,
threat T5). In record text, ``<`` and ``>`` are neutralised so a stored record
cannot close the prompt's ``<prior_context>`` block (threat T4). The ACXD read Lambda
(``lambdas/read/lambda_function.py``) and the writer's optional restatement stripping
(``acxd_memory.acxd_strip``, design §4.2) both call it.

- One ``RetrieveMemoryRecords`` call per namespace from
  ``acxd_memory.identity.read_namespaces``: retrieval is exact-match only.
- A shared client-side time budget and a graceful "no memory" fallback: a lookup failure
  (timeout, throttling, empty result, malformed profile id) never blocks the conversation.

Identity is not resolved here. The ACXD read data request sends ``{System.userId}``, which
the Connect Customer flow set to the Customer Profiles ``profileId`` (design §5).
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from acxd_memory.identity import is_valid_profile_id, log_id, read_namespaces

logger = logging.getLogger(__name__)

# Small maxResults, short client-side budget (design §4). The read Lambda passes its own value from the environment.
DEFAULT_MAX_RESULTS_PER_NAMESPACE = 3
DEFAULT_TIMEOUT_SECONDS = 1.5

# Contact-start read path has no natural free-text query - RetrieveMemoryRecords requires a
# non-empty searchQuery for semantic search (confirmed in scripts/smoke_test.py's usage), so
# a fixed, deliberately generic query is used rather than inventing one from the (as-yet
# nonexistent) current-turn transcript. This retrieves the actor's most salient records
# rather than trying to match a specific query - appropriate for a "what do we know about
# this customer" lookup, not a targeted search.
CONTACT_START_SEARCH_QUERY = (
    "customer preferences, facts, and prior contact history relevant to this call"
)


class SkipReason:
    """Why the read path produced no memory context. Emit as a metric dimension, same
    convention as ``acxd_memory.identity.SkipReason`` and ``acxd_memory.writer.SkipReason``."""

    NO_PROFILE_ID = "no_profile_id"
    """The flow passed no customerProfileId (or an empty string) - e.g. an anonymous
    contact that never identified the customer. Not an error; the expected case."""

    MALFORMED_PROFILE_ID = "malformed_profile_id"
    """A value was present but does not look like a Customer Profiles profileId. Treated as
    absent rather than risking a request built from a bad id."""

    NO_RECORDS = "no_records"
    """Every namespace call succeeded but returned zero records - a real customer with no
    memory yet (e.g. their first-ever contact, or extraction lag has not caught up)."""

    RETRIEVAL_ERROR = "retrieval_error"
    """At least one namespace call failed (timeout, throttling, service error) and no
    namespace produced records. Logged with the underlying errors; never propagated to the
    caller as an exception - see module docstring."""


@dataclass(frozen=True)
class NamespaceResult:
    """Outcome of retrieving one namespace."""

    namespace: str
    records: tuple[str, ...] = ()
    error: str | None = None
    """Set when the call itself failed: the exception class name, or ``timeout``. Never the
    exception message, which can carry request details such as the namespace. ``records``
    is empty in that case - never a mix of "some records, but also an error" for the same
    namespace."""


@dataclass(frozen=True)
class MemoryContext:
    """Result of a contact-start memory lookup, ready to hand to the Lambda handler for
    formatting into the read data request response."""

    actor_id: str | None
    available: bool
    """True when at least one record was retrieved. False covers every "nothing to show"
    case - no profile id, malformed profile id, empty result, and retrieval failure alike -
    so the caller has one boolean to branch on for the fallback path."""

    records: tuple[str, ...] = ()
    skip_reason: str | None = None
    namespace_results: tuple[NamespaceResult, ...] = field(default=(), repr=False)
    """Per-namespace detail, kept for logging/metrics - not needed by the flow response."""


class MemoryClient(Protocol):
    """The subset of the ``bedrock-agentcore`` data-plane client this module calls."""

    def retrieve_memory_records(self, **kwargs: Any) -> Mapping[str, Any]: ...


def _extract_record_text(record: Mapping[str, Any]) -> str | None:
    """Pull the human-readable text out of one ``memoryRecordSummaries`` entry.

    ``RetrieveMemoryRecords`` wraps content as ``{"content": {"text": "..."}}`` for both the
    SEMANTIC and CUSTOM (semanticOverride) strategy types used here - not a union of shapes that needs per-type branching.
    """
    text = record.get("content", {}).get("text")
    return text if isinstance(text, str) and text.strip() else None


def namespace_label(namespace: str) -> str:
    """Loggable form of a namespace: its last segment (``semantic`` / ``recap``).

    The full namespace embeds the ``profileId`` (``connect/profile/<profileId>/semantic``),
    so it never goes into a log line (threat T5); log ``log_id(profile_id)`` next to it.
    """
    return namespace.rstrip("/").rsplit("/", 1)[-1]


def _retrieve_one_namespace(
    memory: MemoryClient,
    *,
    memory_id: str,
    namespace: str,
    max_results: int,
    profile_ref: str = "-",
) -> NamespaceResult:
    """Blocking single-namespace call. Runs inside a worker thread - see
    ``retrieve_memory_context``'s use of ``ThreadPoolExecutor``. ``profile_ref`` is the
    hashed profile id, for log lines only."""
    try:
        response = memory.retrieve_memory_records(
            memoryId=memory_id,
            namespace=namespace,
            searchCriteria={"searchQuery": CONTACT_START_SEARCH_QUERY, "topK": max_results},
            maxResults=max_results,
        )
    except Exception as exc:  # noqa: BLE001 - any failure means "no records from here"
        # Class name only: botocore error messages can echo request parameters.
        logger.warning(
            "RetrieveMemoryRecords failed profile=%s namespace=%s: %s",
            profile_ref, namespace_label(namespace), type(exc).__name__,
        )
        return NamespaceResult(namespace=namespace, error=type(exc).__name__)

    texts = tuple(
        text
        for summary in response.get("memoryRecordSummaries", [])
        if (text := _extract_record_text(summary)) is not None
    )
    return NamespaceResult(namespace=namespace, records=texts)


def retrieve_memory_context(
    profile_id: str | None,
    *,
    memory: MemoryClient,
    memory_id: str,
    max_results_per_namespace: int = DEFAULT_MAX_RESULTS_PER_NAMESPACE,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> MemoryContext:
    """Retrieve contact-start memory context for one profile id.

    Issues one ``RetrieveMemoryRecords`` call per namespace from
    ``acxd_memory.identity.read_namespaces`` (never a prefix - see module docstring), in
    parallel via a thread pool, bounded by ``timeout_seconds`` for the whole retrieval (not
    per-call): a slow namespace must not silently extend the customer's wait past the
    budget just because a sibling namespace already returned.

    Never raises. Every failure mode - no profile id, malformed profile id, a namespace
    error, a timeout, or a genuinely empty result - resolves to
    ``MemoryContext(available=False, ...)`` with a ``skip_reason`` for observability. The
    caller (the Lambda handler) is expected to treat ``available=False`` as "render no
    memory context" and proceed, per architecture decision #6.
    """
    if not profile_id:
        return MemoryContext(actor_id=None, available=False, skip_reason=SkipReason.NO_PROFILE_ID)

    if not is_valid_profile_id(profile_id):
        logger.warning("malformed profile id on read path - skipping retrieval")
        return MemoryContext(
            actor_id=None, available=False, skip_reason=SkipReason.MALFORMED_PROFILE_ID
        )

    namespaces = read_namespaces(profile_id)
    profile_ref = log_id(profile_id)

    # One worker per namespace so all calls are in flight concurrently - this is what turns
    # N sequential calls into one wall-clock wait. A
    # single wait() against ALL_COMPLETED with the full budget as its timeout is sufficient
    # here: it returns as soon as every future finishes OR the timeout elapses, whichever is
    # first - there is no need to wake up early on the FIRST namespace finishing, since the
    # remaining ones are still worth waiting for up to the same shared budget.
    #
    # Deliberately NOT a `with ThreadPoolExecutor(...) as pool:` block: the context
    # manager's __exit__ calls shutdown(wait=True), which blocks until every submitted
    # worker actually finishes - including a genuinely slow/hung RetrieveMemoryRecords call
    # that this function's own timeout was supposed to stop waiting on. That would silently
    # turn "bounded by timeout_seconds" into "bounded by timeout_seconds, unless a worker is
    # still running, in which case wait for it anyway" - defeating the entire point of the
    # budget. shutdown(wait=False) here lets the function return on schedule; the abandoned
    # worker thread finishes in the background and its result is simply never collected (a
    # boto3 call has no cancellation hook either way, so "cancel" was never more than a
    # local bookkeeping no-op regardless of how the pool is shut down).
    pool = ThreadPoolExecutor(max_workers=len(namespaces))
    try:
        futures: dict[Future[NamespaceResult], str] = {
            pool.submit(
                _retrieve_one_namespace,
                memory,
                memory_id=memory_id,
                namespace=ns,
                max_results=max_results_per_namespace,
                profile_ref=profile_ref,
            ): ns
            for ns in namespaces
        }
        done, _not_done = wait(futures, timeout=timeout_seconds, return_when="ALL_COMPLETED")

        results: list[NamespaceResult] = []
        for future, namespace in futures.items():
            if future in done:
                results.append(future.result())
            else:
                logger.warning(
                    "RetrieveMemoryRecords timed out after %.1fs profile=%s namespace=%s",
                    timeout_seconds, profile_ref, namespace_label(namespace),
                )
                results.append(NamespaceResult(namespace=namespace, error="timeout"))
    finally:
        pool.shutdown(wait=False)

    all_records: list[str] = []
    for result in results:
        all_records.extend(result.records)

    if all_records:
        return MemoryContext(
            actor_id=profile_id,
            available=True,
            records=tuple(all_records),
            namespace_results=tuple(results),
        )

    any_error = any(r.error for r in results)
    skip_reason = SkipReason.RETRIEVAL_ERROR if any_error else SkipReason.NO_RECORDS
    logger.info(
        "profile=%s: no memory context available (%s): %s",
        profile_ref, skip_reason,
        "; ".join(f"{namespace_label(r.namespace)}={r.error or 'empty'}" for r in results),
    )
    return MemoryContext(
        actor_id=profile_id,
        available=False,
        skip_reason=skip_reason,
        namespace_results=tuple(results),
    )


# 2000 characters is plenty for a prompt-context block
# and keeps the agent's own prompt budget from being crowded out by memory context.
MAX_CONTEXT_CHARS = 2000

# The sample prompt wraps memory in <prior_context>...</prior_context>. A record is
# customer-stated and LLM-extracted text, so it must not be able to close that block or
# open another tag: angle brackets become single guillemets (threat T4, defence in depth).
_TAG_NEUTRALISER = str.maketrans({"<": "\u2039", ">": "\u203a"})


def neutralise_record(text: str) -> str:
    """One record as a single prompt line: no angle brackets, no line breaks."""
    return " ".join(text.translate(_TAG_NEUTRALISER).split())


def format_memory_context(context: MemoryContext) -> str:
    """Render a ``MemoryContext`` into the flat text block the ACXD flow stores in the
    ``memoryContext`` context variable and the agent prompt references.

    Returns an empty string when ``available`` is False; the agent prompt treats an empty
    value as "no memory available". Each record is passed through ``neutralise_record``,
    so the block contains no ``<`` or ``>`` and one line per record.
    """
    if not context.available:
        return ""

    lines = [f"- {neutralise_record(record)}" for record in context.records]
    text = "\n".join(lines)
    if len(text) <= MAX_CONTEXT_CHARS:
        return text

    # Truncate whole lines rather than mid-sentence - a cut-off fact read back to the
    # customer verbatim is worse than a slightly shorter list.
    kept: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > MAX_CONTEXT_CHARS:
            break
        kept.append(line)
        total += len(line) + 1
    return "\n".join(kept)
