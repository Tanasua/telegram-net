from telegram_net.fetchers.base import FetchedMedia
from telegram_net.media import build_media_batches


def _photos(n: int) -> tuple[FetchedMedia, ...]:
    return tuple(FetchedMedia(type="photo", url=f"https://example.com/{i}.jpg") for i in range(n))


def test_no_media_returns_empty_list():
    assert build_media_batches(()) == []


def test_single_photo_is_one_batch_of_one():
    batches = build_media_batches(_photos(1))
    assert len(batches) == 1
    assert len(batches[0]) == 1


def test_ten_photos_fit_in_one_batch():
    batches = build_media_batches(_photos(10))
    assert len(batches) == 1
    assert len(batches[0]) == 10


def test_eleven_photos_split_into_two_batches():
    batches = build_media_batches(_photos(11))
    assert [len(b) for b in batches] == [10, 1]


def test_twenty_five_photos_split_into_three_batches():
    batches = build_media_batches(_photos(25))
    assert [len(b) for b in batches] == [10, 10, 5]
