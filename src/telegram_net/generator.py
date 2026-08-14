"""Другий LLM-крок: сам пост. Варіативність стилю (довжина/заголовок) вирішується
до виклику (style.pick_style), тут вона тільки виконується — так розподіл
короткі/довгі/з заголовком/без гарантований кодом, а не 'сподіванням' на модель."""

from __future__ import annotations

from telegram_net.fetchers.base import FetchedItem
from telegram_net.llm import build_openai_client, require_model
from telegram_net.style import PostStyle

_LENGTH_GUIDANCE = {
    "short": "1-2 короткі речення, максимально стисло, тільки суть.",
    "medium": "3-5 речень, трохи контексту, але без зайвого.",
    "long": "6-10 речень, розгорнутий виклад з деталями й контекстом.",
}

_SYSTEM_PROMPT = """Ти пишеш пост для телеграм-каналу місцевих новин міста "{region}".
Стиль — живий, розмовний, як пише людина-редактор, а не шаблонний AI-текст:
уникай завжди однакових зачинів ("Увага!", "Шановні..."), не став емодзі в кожному
реченні, не повторюй структуру "факт-емодзі-факт-емодзі" з посту в пост.

Вимоги до цього конкретного посту:
- Довжина: {length_guidance}
- Заголовок: {headline_instruction}

Пиши українською, за фактами з наданого тексту-джерела. Не додавай фактів, яких
там немає. Не пиши "за повідомленням джерела" чи подібні канцеляризми — просто подай
як новину каналу."""


def build_prompt(item: FetchedItem, style: PostStyle, region_name: str) -> list[dict]:
    headline_instruction = (
        "додай короткий яскравий заголовок першим рядком, потім порожній рядок, потім текст"
        if style.with_headline
        else "без заголовка, почни прямо з суті"
    )
    system = _SYSTEM_PROMPT.format(
        region=region_name,
        length_guidance=_LENGTH_GUIDANCE[style.length],
        headline_instruction=headline_instruction,
    )
    source_text = item.text.strip() or (
        "(джерело без тексту — лише фото/відео, опиши коротко на основі контексту URL)"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Джерело: {item.url}\n\nТекст джерела:\n{source_text}"},
    ]


class Generator:
    def __init__(self, client=None, model: str | None = None):
        self._client = client or build_openai_client()
        self.model = model or require_model("OPENAI_GENERATOR_MODEL")

    def generate(self, item: FetchedItem, style: PostStyle, region_name: str) -> str:
        messages = build_prompt(item, style, region_name)
        completion = self._client.chat.completions.create(model=self.model, messages=messages)
        text = completion.choices[0].message.content
        if not text:
            raise RuntimeError("OpenAI не повернув текст посту")
        return text.strip()
