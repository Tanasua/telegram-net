import datetime as dt

from telegram_net.fetchers.article import extract_article, looks_like_logo

_HTML = """<html><head>
<meta property="og:image" content="/uploads/photo.jpg"/>
<meta property="article:published_time" content="2026-11-14T10:30:00+02:00"/>
</head><body>
<nav><p>Головна Новини Контакти — меню сайту, яке не є текстом статті</p></nav>
<article>
  <h1>Заголовок</h1>
  <p>У Малині відкрили оновлений міст через річку Ірша після пів року ремонту.</p>
  <p>Роботи коштували 12 мільйонів гривень, повідомили у міській раді.</p>
  <ul><li><a href="/2">Маршрутка Київ — Житомир: звідки відправляється і де розклад</a></li></ul>
  <div class="related-posts"><p>Читайте також: зовсім інша новина про щось інше в місті.</p></div>
</article>
<footer><p>Усі права захищено, передрук лише з посиланням на сайт.</p></footer>
</body></html>"""


def test_extracts_body_image_and_date_without_page_chrome():
    article = extract_article(_HTML, "https://example.com/news/1")
    assert "оновлений міст" in article.text and "12 мільйонів" in article.text
    assert "меню сайту" not in article.text
    assert "Читайте також" not in article.text and "Маршрутка" not in article.text
    assert "права захищено" not in article.text
    assert article.image_url == "https://example.com/uploads/photo.jpg"
    assert article.published_at == dt.datetime(2026, 11, 14, 8, 30, tzinfo=dt.timezone.utc)


def test_page_without_article_markup_yields_empty_text_not_garbage():
    article = extract_article("<html><body><div>Коротко</div></body></html>", "https://example.com/x")
    assert article.text == "" and article.image_url is None and article.published_at is None


def test_logo_detection_by_file_name():
    assert looks_like_logo("http://www.zhitomirnews.com/images/logo_small.png")
    assert looks_like_logo("https://site.ua/img/no-image.jpg")
    assert looks_like_logo("https://site.ua/static/site.svg")
    assert not looks_like_logo("https://site.ua/uploads/2026/11/photo_2026-11-14.jpeg")
