"""Дедуп контенту: одна й та ж новина часто зринає і в телеграм-каналі, і на сайті,
і в кількох джерелах одного каналу. Порівнюємо не сирий текст, а нормалізовану форму.

Це навмисно точний хеш, а не fuzzy-подібність (simhash/embeddings) — для MVP достатньо,
ускладнювати нема сенсу, поки не побачимо реальні пропущені дублі на практиці.
"""

from __future__ import annotations

import hashlib
import re

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_text(text: str) -> str:
    lowered = text.strip().lower()
    no_punct = _PUNCT_RE.sub("", lowered)
    return _WHITESPACE_RE.sub(" ", no_punct).strip()


def content_hash(text: str) -> str:
    normalized = normalize_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_duplicate(text: str, existing_hashes: set[str]) -> bool:
    return content_hash(text) in existing_hashes
