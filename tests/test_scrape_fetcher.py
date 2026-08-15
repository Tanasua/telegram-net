import httpx
import respx

from telegram_net.config import SourceConfig
from telegram_net.fetchers.base import FetchedMedia
from telegram_net.fetchers.scrape_fetcher import HtmlListFetcher

URL = "https://example.com/news"


def _source(item_selector="li.card a", title_selector=None):
    return SourceConfig(
        channel_id="x",
        type="web_scrape",
        name="Test",
        handle_or_url=URL,
        scrape_item_selector=item_selector,
        scrape_title_selector=title_selector,
    )


_HTML = """
<html><body>
  <ul>
    <li class="card"><a href="/news/1">Перша новина</a></li>
    <li class="card"><a href="/news/2">Друга новина</a></li>
  </ul>
</body></html>
"""


def _article_html(image_url=None):
    meta = f'<meta property="og:image" content="{image_url}" />' if image_url else ""
    return f"<html><head>{meta}</head><body>стаття</body></html>"


@respx.mock
def test_first_run_establishes_baseline_without_backfill():
    """Як і з Telethon: без надійної дати публікації перше підключення не
    повинно одразу публікувати все, що вже висіло на сторінці до підключення.
    Тут не має бути жодного запиту по фото — базова лінія не заводить items."""
    respx.get(URL).mock(return_value=httpx.Response(200, text=_HTML))
    fetcher = HtmlListFetcher(http_client=httpx.Client())

    items, cursor = fetcher.fetch_new(_source(), cursor=None)

    assert items == []
    assert set(cursor.split(",")) == {"https://example.com/news/1", "https://example.com/news/2"}


@respx.mock
def test_subsequent_run_returns_only_new_entries_with_image():
    respx.get(URL).mock(return_value=httpx.Response(200, text=_HTML))
    respx.get("https://example.com/news/2").mock(
        return_value=httpx.Response(200, text=_article_html("https://example.com/2.jpg"))
    )
    fetcher = HtmlListFetcher(http_client=httpx.Client())

    items, cursor = fetcher.fetch_new(_source(), cursor="https://example.com/news/1")

    assert len(items) == 1
    assert items[0].text == "Друга новина"
    assert items[0].url == "https://example.com/news/2"
    assert items[0].published_at is None
    assert items[0].media == (FetchedMedia(type="photo", url="https://example.com/2.jpg"),)
    assert set(cursor.split(",")) == {"https://example.com/news/1", "https://example.com/news/2"}


@respx.mock
def test_item_without_article_og_image_has_no_media():
    respx.get(URL).mock(return_value=httpx.Response(200, text=_HTML))
    respx.get("https://example.com/news/1").mock(return_value=httpx.Response(200, text=_article_html(None)))
    respx.get("https://example.com/news/2").mock(return_value=httpx.Response(200, text=_article_html(None)))
    fetcher = HtmlListFetcher(http_client=httpx.Client())

    items, _ = fetcher.fetch_new(_source(), cursor="")

    assert all(i.media == () for i in items)


@respx.mock
def test_repeated_og_image_across_articles_is_treated_as_site_logo_not_a_photo():
    """Регресія-запобіжник: zhitomirnews.com у продакшні завжди повертає ту
    саму og:image (логотип сайту) для будь-якої статті — якщо не відкидати
    такий збіг, правило "немає фото — немає посту" пропустило б логотип,
    виданий за фото конкретної новини."""
    respx.get(URL).mock(return_value=httpx.Response(200, text=_HTML))
    respx.get("https://example.com/news/1").mock(
        return_value=httpx.Response(200, text=_article_html("https://example.com/logo.png"))
    )
    respx.get("https://example.com/news/2").mock(
        return_value=httpx.Response(200, text=_article_html("https://example.com/logo.png"))
    )
    fetcher = HtmlListFetcher(http_client=httpx.Client())

    items, _ = fetcher.fetch_new(_source(), cursor="")

    assert all(i.media == () for i in items)


@respx.mock
def test_distinct_og_images_across_articles_are_kept():
    respx.get(URL).mock(return_value=httpx.Response(200, text=_HTML))
    respx.get("https://example.com/news/1").mock(
        return_value=httpx.Response(200, text=_article_html("https://example.com/1.jpg"))
    )
    respx.get("https://example.com/news/2").mock(
        return_value=httpx.Response(200, text=_article_html("https://example.com/2.jpg"))
    )
    fetcher = HtmlListFetcher(http_client=httpx.Client())

    items, _ = fetcher.fetch_new(_source(), cursor="")

    by_url = {i.url: i for i in items}
    assert by_url["https://example.com/news/1"].media[0].url == "https://example.com/1.jpg"
    assert by_url["https://example.com/news/2"].media[0].url == "https://example.com/2.jpg"


@respx.mock
def test_no_new_entries_keeps_cursor():
    respx.get(URL).mock(return_value=httpx.Response(200, text=_HTML))
    fetcher = HtmlListFetcher(http_client=httpx.Client())
    cursor = "https://example.com/news/1,https://example.com/news/2"

    items, new_cursor = fetcher.fetch_new(_source(), cursor=cursor)

    assert items == []
    assert new_cursor == cursor


@respx.mock
def test_missing_selector_in_config_yields_nothing():
    respx.get(URL).mock(return_value=httpx.Response(200, text=_HTML))
    fetcher = HtmlListFetcher(http_client=httpx.Client())

    items, cursor = fetcher.fetch_new(_source(item_selector=None), cursor="anything")

    assert items == []
    assert cursor == "anything"


@respx.mock
def test_title_selector_picks_specific_element_when_item_has_extra_text():
    """Регресія: без прицільного title_selector береться весь текст картки —
    підходить, коли картка сама по собі й є посиланням (item==<a>), але коли
    в картці ще є дата/лічильник переглядів перед заголовком (як на malyn-rada),
    вони потрапляють у текст без явного селектора заголовка."""
    html = """
    <html><body>
      <div class="card">
        <a class="link" href="/news/1">
          <span class="meta">14.08.2026 15:59</span>
          <span class="title">Реальний заголовок</span>
        </a>
      </div>
    </body></html>
    """
    respx.get(URL).mock(return_value=httpx.Response(200, text=html))
    respx.get("https://example.com/news/1").mock(return_value=httpx.Response(200, text=_article_html(None)))
    source = _source(item_selector="div.card a.link", title_selector="span.title")
    fetcher = HtmlListFetcher(http_client=httpx.Client())

    items, _ = fetcher.fetch_new(source, cursor="")

    assert items[0].text == "Реальний заголовок"


@respx.mock
def test_title_falls_back_to_img_alt_when_no_text():
    html = """
    <html><body>
      <div class="card">
        <a class="link" href="/news/1"><img src="/x.jpg" alt="Заголовок з alt картинки" /></a>
      </div>
    </body></html>
    """
    respx.get(URL).mock(return_value=httpx.Response(200, text=html))
    respx.get("https://example.com/news/1").mock(return_value=httpx.Response(200, text=_article_html(None)))
    source = _source(item_selector="div.card a.link")
    fetcher = HtmlListFetcher(http_client=httpx.Client())

    items, _ = fetcher.fetch_new(source, cursor="")

    assert items[0].text == "Заголовок з alt картинки"


@respx.mock
def test_relative_links_are_resolved_against_source_url():
    respx.get(URL).mock(return_value=httpx.Response(200, text=_HTML))
    respx.get("https://example.com/news/1").mock(return_value=httpx.Response(200, text=_article_html(None)))
    respx.get("https://example.com/news/2").mock(return_value=httpx.Response(200, text=_article_html(None)))
    fetcher = HtmlListFetcher(http_client=httpx.Client())

    items, _ = fetcher.fetch_new(_source(), cursor="")

    assert all(i.url.startswith("https://example.com/") for i in items)


@respx.mock
def test_item_selector_matching_the_anchor_itself_also_works():
    """Деякі сайти мають картку-посилання без окремого дочірнього <a> —
    сам обраний елемент і є посиланням (item.has_attr('href'))."""
    html = """
    <html><body>
      <section class="post"><a class="post-link" href="/n/1">Заголовок картки-посилання</a></section>
    </body></html>
    """
    respx.get(URL).mock(return_value=httpx.Response(200, text=html))
    respx.get("https://example.com/n/1").mock(return_value=httpx.Response(200, text=_article_html(None)))
    source = _source(item_selector="section.post a.post-link")
    fetcher = HtmlListFetcher(http_client=httpx.Client())

    items, _ = fetcher.fetch_new(source, cursor="")

    assert len(items) == 1
    assert items[0].text == "Заголовок картки-посилання"
