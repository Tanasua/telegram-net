"""Перший LLM-крок: чи це реклама, чи релевантно регіону, чи цікаво як місцева
новина. Дешева/швидка модель — обсяг великий (десятки джерел щогодини), тут не
потрібне глибоке міркування."""

from __future__ import annotations

from pydantic import BaseModel, Field

from telegram_net.fetchers.base import FetchedItem
from telegram_net.llm import build_openai_client, require_model


class ClassificationResult(BaseModel):
    is_advertisement: bool
    relevance_to_region: float = Field(ge=0, le=1)
    local_news_interest: float = Field(ge=0, le=1)
    reason: str


_SYSTEM_PROMPT = """Ти — редактор мережі місцевих новин Житомирської області.
Тобі показують текст одного повідомлення з телеграм-каналу або сайту, що стосується
міста/громади "{region}". Оціни:

- is_advertisement: це реклама, оголошення про продаж/послуги, замовний піар — а не новина?
- relevance_to_region: 0..1 — наскільки текст стосується саме цього міста/громади
  (а не області в цілому чи іншого регіону).
- local_news_interest: 0..1 — наскільки це цікаво жителю як КОНКРЕТНА новина
  (щось СТАЛОСЯ чи ВІДБУДЕТЬСЯ в конкретний час: подія, рішення ради, аварія,
  ремонт, захід). КРИТИЧНО: якщо текст просто описує саму організацію/сайт/
  портал і не прив'язаний до жодної конкретної події — це НЕ новина, онижуй
  local_news_interest до ≤0.1, навіть якщо формально "про це місто". Приклади
  такого НЕ-контенту, який треба відсіювати: "сайт X доступний за адресою Y",
  "громада вітає відвідувачів на офіційному сайті", "рада заявила про
  відкритість і прозорість своєї роботи", "портал працює як офіційний ресурс" —
  все це загальний опис організації, а не подія.
- reason: одне речення українською, чому така оцінка.

Відповідай лише структурованим результатом."""


class Classifier:
    def __init__(self, client=None, model: str | None = None):
        self._client = client or build_openai_client()
        self.model = model or require_model("OPENAI_CLASSIFIER_MODEL")

    def classify(self, item: FetchedItem, region_name: str) -> ClassificationResult:
        text = item.text.strip() or "(текст відсутній, лише медіа)"
        completion = self._client.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT.format(region=region_name)},
                {"role": "user", "content": text},
            ],
            response_format=ClassificationResult,
        )
        result = completion.choices[0].message.parsed
        if result is None:
            raise RuntimeError("OpenAI не повернув структурований результат класифікації")
        return result
