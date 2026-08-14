import random

import pytest

from telegram_net.style import pick_style


def test_pick_style_respects_headline_probability_extremes():
    rng = random.Random(0)
    always = pick_style({"short": 1.0}, headline_probability=1.0, rng=rng)
    assert always.with_headline is True

    never = pick_style({"short": 1.0}, headline_probability=0.0, rng=rng)
    assert never.with_headline is False


def test_pick_style_only_picks_from_given_lengths():
    rng = random.Random(42)
    seen = {pick_style({"short": 1.0, "long": 0.0}, 0.5, rng=rng).length for _ in range(50)}
    assert seen == {"short"}


def test_pick_style_distribution_roughly_matches_weights():
    rng = random.Random(1)
    counts = {"short": 0, "long": 0}
    n = 2000
    for _ in range(n):
        style = pick_style({"short": 0.8, "long": 0.2}, 0.5, rng=rng)
        counts[style.length] += 1
    # не точна перевірка ймовірності, лише що перекіс у правильний бік і в розумних межах
    assert 0.7 < counts["short"] / n < 0.9


def test_pick_style_raises_on_empty_weights():
    with pytest.raises(ValueError):
        pick_style({}, 0.5)
