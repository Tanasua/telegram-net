from telegram_net.dedup import content_hash, is_duplicate, normalize_text


def test_normalize_text_strips_case_punctuation_and_whitespace():
    assert normalize_text("Привіт,   світе!!!") == "привіт світе"
    assert normalize_text("  Hello   World  ") == "hello world"


def test_content_hash_is_stable_for_normalized_equivalents():
    a = content_hash("У Житомирі відкрили новий парк!")
    b = content_hash("у житомирі   відкрили новий парк")
    assert a == b


def test_content_hash_differs_for_different_text():
    a = content_hash("Новина перша")
    b = content_hash("Новина друга")
    assert a != b


def test_is_duplicate():
    existing = {content_hash("Новина перша")}
    assert is_duplicate("новина перша", existing) is True
    assert is_duplicate("зовсім інша новина", existing) is False
