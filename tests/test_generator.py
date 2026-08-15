from types import SimpleNamespace

import pytest

from telegram_net.fetchers.base import FetchedItem
from telegram_net.generator import Generator, build_prompt
from telegram_net.style import PostStyle


def _item(text="Джерело новини"):
    return FetchedItem(external_id="1", text=text, url="https://example.com/1", published_at=None)


def test_build_prompt_with_headline_instruction():
    messages = build_prompt(_item(), PostStyle(length="short", with_headline=True), "Житомир")
    assert "заголовок" in messages[0]["content"]
    assert "Житомир" in messages[0]["content"]
    assert "Джерело новини" in messages[1]["content"]


def test_build_prompt_without_headline_instruction():
    messages = build_prompt(_item(), PostStyle(length="long", with_headline=False), "Житомир")
    assert "без заголовка" in messages[0]["content"]


def test_build_prompt_handles_empty_source_text():
    messages = build_prompt(_item(text="  "), PostStyle(length="medium", with_headline=False), "Малин")
    assert "лише фото/відео" in messages[1]["content"]


def test_build_prompt_requires_exactly_one_mood_emoji():
    messages = build_prompt(_item(), PostStyle(length="short", with_headline=True), "Житомир")
    assert "РІВНО З ОДНОГО емодзі" in messages[0]["content"]
    assert "НІ ОДНОГО емодзі" in messages[0]["content"]


def test_build_prompt_forbids_inventing_quotes():
    messages = build_prompt(_item(), PostStyle(length="long", with_headline=True), "Житомир")
    assert "НІКОЛИ не вигадуй" in messages[0]["content"]


class FakeCompletions:
    def __init__(self, content):
        self._content = content
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        message = SimpleNamespace(content=self._content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, content):
        self.chat = SimpleNamespace(completions=FakeCompletions(content))


def test_generate_returns_stripped_text():
    client = FakeClient("  Готовий текст посту  ")
    generator = Generator(client=client, model="test-model")

    text = generator.generate(_item(), PostStyle(length="short", with_headline=False), "Житомир")

    assert text == "Готовий текст посту"
    assert client.chat.completions.last_kwargs["model"] == "test-model"


def test_generate_raises_on_empty_response():
    client = FakeClient("")
    generator = Generator(client=client, model="test-model")
    with pytest.raises(RuntimeError):
        generator.generate(_item(), PostStyle(length="short", with_headline=False), "Житомир")


def test_generator_requires_model_env_var_when_not_passed(monkeypatch):
    monkeypatch.delenv("OPENAI_GENERATOR_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_GENERATOR_MODEL"):
        Generator(client=FakeClient(""))
