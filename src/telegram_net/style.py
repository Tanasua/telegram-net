"""Розподіл стилів постів контролюється кодом, а не 'проханням' до LLM — так
гарантується реальна різноманітність (короткі/довгі, з заголовком/без), а не
те, що модель вирішить сама і зробить усі пости однаковими."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

LengthStyle = Literal["short", "medium", "long"]


@dataclass(frozen=True)
class PostStyle:
    length: LengthStyle
    with_headline: bool


def pick_style(
    length_weights: dict[str, float],
    headline_probability: float,
    rng: random.Random | None = None,
) -> PostStyle:
    rng = rng or random.Random()
    if not length_weights:
        raise ValueError("length_weights не може бути порожнім")

    lengths = list(length_weights.keys())
    weights = list(length_weights.values())
    length = rng.choices(lengths, weights=weights, k=1)[0]
    with_headline = rng.random() < headline_probability
    return PostStyle(length=length, with_headline=with_headline)  # type: ignore[arg-type]
