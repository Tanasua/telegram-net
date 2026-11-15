"""Маленьке червоне сердечко біля назви каналу на картинках (як ❤️ у підписі).

Малюється векторно з 4-кратним згладжуванням, а не емодзі зі шрифту — так воно
чітке на будь-якому розмірі."""

from __future__ import annotations

import math

HEART_RED = (239, 68, 68)


def heart_image(size: int, color=HEART_RED):
    from PIL import Image, ImageDraw

    k = 4
    s = size * k
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    pts = []
    for i in range(200):
        t = 2 * math.pi * i / 200
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((s / 2 + x * s / 34, s / 2 - y * s / 34 + s * 0.03))
    ImageDraw.Draw(im).polygon(pts, fill=color + (255,))
    # блік, як у емодзі
    ImageDraw.Draw(im).ellipse((s * 0.24, s * 0.22, s * 0.38, s * 0.34), fill=(255, 255, 255, 90))
    return im.resize((size, size), Image.LANCZOS)


def draw_title_with_heart(img, draw, center: tuple[float, float], text: str, font, fill, gap: int = 10) -> None:
    """Пише `text` по центру точки `center`, а ліворуч від нього — сердечко висотою з літери."""
    size = max(12, round(font.size * 0.9))
    width = draw.textlength(text, font=font)
    total = size + gap + width
    left = center[0] - total / 2
    heart = heart_image(size)
    img.alpha_composite(heart, (round(left), round(center[1] - size / 2)))
    draw.text((left + size + gap, center[1]), text, font=font, fill=fill, anchor="lm")
