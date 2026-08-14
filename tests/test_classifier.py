from types import SimpleNamespace

import pytest

from telegram_net.classifier import ClassificationResult, Classifier
from telegram_net.fetchers.base import FetchedItem


class FakeCompletions:
    def __init__(self, parsed):
        self._parsed = parsed
        self.last_kwargs = None

    def parse(self, **kwargs):
        self.last_kwargs = kwargs
        message = SimpleNamespace(parsed=self._parsed)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, parsed):
        self.chat = SimpleNamespace(completions=FakeCompletions(parsed))


def _item(text="Новина про дорогу"):
    return FetchedItem(external_id="1", text=text, url="https://example.com/1", published_at=None)


def test_classify_returns_parsed_result():
    expected = ClassificationResult(
        is_advertisement=False, relevance_to_region=0.9, local_news_interest=0.8, reason="ok"
    )
    client = FakeClient(expected)
    classifier = Classifier(client=client, model="test-model")

    result = classifier.classify(_item(), region_name="Житомир")

    assert result == expected
    assert client.chat.completions.last_kwargs["model"] == "test-model"
    assert client.chat.completions.last_kwargs["response_format"] is ClassificationResult


def test_classify_raises_if_openai_returns_no_parsed_result():
    client = FakeClient(None)
    classifier = Classifier(client=client, model="test-model")
    with pytest.raises(RuntimeError):
        classifier.classify(_item(), region_name="Житомир")


def test_classify_uses_placeholder_text_for_media_only_items():
    expected = ClassificationResult(
        is_advertisement=False, relevance_to_region=0.5, local_news_interest=0.5, reason="ok"
    )
    client = FakeClient(expected)
    classifier = Classifier(client=client, model="test-model")

    classifier.classify(_item(text="   "), region_name="Житомир")

    user_message = client.chat.completions.last_kwargs["messages"][-1]["content"]
    assert "лише медіа" in user_message


def test_classifier_requires_model_env_var_when_not_passed(monkeypatch):
    monkeypatch.delenv("OPENAI_CLASSIFIER_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_CLASSIFIER_MODEL"):
        Classifier(client=FakeClient(None))


def test_classifier_region_name_reaches_system_prompt():
    expected = ClassificationResult(
        is_advertisement=False, relevance_to_region=0.5, local_news_interest=0.5, reason="ok"
    )
    client = FakeClient(expected)
    classifier = Classifier(client=client, model="test-model")

    classifier.classify(_item(), region_name="Коростень")

    system_message = client.chat.completions.last_kwargs["messages"][0]["content"]
    assert "Коростень" in system_message
