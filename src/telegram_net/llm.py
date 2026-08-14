"""Спільне для класифікатора й генератора: клієнт OpenAI і назви моделей.

Назви моделей НЕ хардкодяться тут як 'дефолт' — на момент написання цього коду
(серпень 2026) я не мав надійного першоджерельного підтвердження точних поточних
ID моделей (сторонні агрегатори давали суперечливі дані). Тому модель — обов'язкова
змінна середовища: звір актуальний список на platform.openai.com/docs/models і
встанови сам, а не отримай від мене вгадану назву.
"""

from __future__ import annotations

import os

from openai import OpenAI


def build_openai_client() -> OpenAI:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не задано в середовищі")
    return OpenAI(api_key=api_key)


def require_model(env_var: str) -> str:
    value = os.environ.get(env_var)
    if not value:
        raise RuntimeError(
            f"{env_var} не задано. Обери актуальну модель на platform.openai.com/docs/models "
            "і встанови змінну середовища явно — навмисно без хардкодженого дефолту в коді."
        )
    return value
