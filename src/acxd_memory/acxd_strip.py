# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Writer-side stripping of restated memory from assistant turns (design §4.2, D2).

Optional backstop behind the ``enableStripping`` context flag (default off). The primary
feedback-loop controls are the agent prompt rule and the extraction-prompt override on the
memory strategies (``scripts/provision_memory.sh``).

Rules:

- Only **assistant** turns are stripped. A customer restating a fact is new evidence.
- Matching runs on normalised tokens (lowercase, punctuation stripped), but the matching
  spans are deleted from the **original** text, so what is written keeps its casing.
- Any run of ``MIN_RUN_WORDS`` (8) or more consecutive words that also occurs in the match
  corpus is removed. The corpus is every retrieved record plus the
  ``format_memory_context`` string the agent was shown.
- The raw token form ``<variable>:NLX.Context|`` seen in spike S9 is removed.
- A turn with fewer than ``MIN_REMAINING_WORDS`` (4) words left is dropped.

Limits [L]: verbatim and near-verbatim restatement only, not paraphrase; the re-retrieve
can rank records differently from what the agent saw. The 8-word threshold is [U] until
the build-phase live test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

from acxd_memory.acxd_transcript import ASSISTANT, Turn

MIN_RUN_WORDS = 8
MIN_REMAINING_WORDS = 4

_WORD = re.compile(r"\S+")
_PUNCT = re.compile(r"[^\w]+", re.UNICODE)
# "{memoryContext:NLX.Context}" leaked as "memoryContext:NLX.Context|<value>" (spike S9).
_RAW_CONTEXT_TOKEN = re.compile(r"\{?[A-Za-z0-9_]+:NLX\.Context\}?\|?")


@dataclass(frozen=True)
class StripResult:
    turns: tuple[Turn, ...]
    turns_changed: int
    """Assistant turns whose text changed or that were dropped. Emitted as
    ``MemoryRestatementStripped``; no text is logged."""


def _normalise(token: str) -> str:
    return _PUNCT.sub("", token.lower())


def _tokens(text: str) -> list[tuple[str, int, int]]:
    """(normalised token, start, end) for every whitespace-separated word with content."""
    out: list[tuple[str, int, int]] = []
    for match in _WORD.finditer(text):
        norm = _normalise(match.group(0))
        if norm:
            out.append((norm, match.start(), match.end()))
    return out


def build_ngram_index(corpus: Iterable[str], n: int = MIN_RUN_WORDS) -> set[tuple[str, ...]]:
    """All n-grams of normalised tokens in the corpus texts (each text separately)."""
    index: set[tuple[str, ...]] = set()
    for text in corpus:
        if not text:
            continue
        words = [t[0] for t in _tokens(_RAW_CONTEXT_TOKEN.sub(" ", text))]
        for i in range(len(words) - n + 1):
            index.add(tuple(words[i : i + n]))
    return index


def strip_text(text: str, index: set[tuple[str, ...]], n: int = MIN_RUN_WORDS) -> str:
    """Remove every run of >= n words found in ``index`` and every raw context token."""
    text = _RAW_CONTEXT_TOKEN.sub(" ", text)
    tokens = _tokens(text)
    covered = [False] * len(tokens)
    if index:
        words = [t[0] for t in tokens]
        for i in range(len(words) - n + 1):
            if tuple(words[i : i + n]) in index:
                for j in range(i, i + n):
                    covered[j] = True
    if not any(covered):
        return re.sub(r"\s+", " ", text).strip()

    pieces: list[str] = []
    cursor = 0
    for (_, start, end), drop in zip(tokens, covered):
        if drop:
            pieces.append(text[cursor:start])
            cursor = end
    pieces.append(text[cursor:])
    return re.sub(r"\s+", " ", "".join(pieces)).strip()


def strip_restated_memory(turns: Sequence[Turn], corpus: Iterable[str]) -> StripResult:
    """Apply the §4.2 rules to ``turns``. User turns pass through unchanged."""
    index = build_ngram_index(corpus)
    kept: list[Turn] = []
    changed = 0
    for turn in turns:
        if turn.role != ASSISTANT:
            kept.append(turn)
            continue
        stripped = strip_text(turn.text, index)
        original_normalised = re.sub(r"\s+", " ", turn.text).strip()
        if stripped == original_normalised:
            kept.append(turn)
            continue
        changed += 1
        if len(_tokens(stripped)) < MIN_REMAINING_WORDS:
            continue
        kept.append(replace(turn, text=stripped))
    return StripResult(turns=tuple(kept), turns_changed=changed)
