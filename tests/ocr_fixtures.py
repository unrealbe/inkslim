"""Deterministic synthetic scan pages for OCR tests (Pillow's bundled font, no system fonts)."""

from __future__ import annotations

import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

DUTCH_LINES = [
    "Het was een koude ochtend toen Marie de trap afdaalde naar de keuken.",
    "Op tafel stond nog de kop van gisteren, half leeg en lauw geworden.",
    "Ze keek door het raam naar de tuin waar de eerste sneeuw al lag.",
    "Haar vader las de krant en zei niets, zoals hij zelden iets zei.",
    "Buiten blafte een hond en ergens in de verte sloeg een deur dicht.",
    "Marie dacht aan de brief die ze de avond ervoor had gevonden.",
    "Misschien was het beter om er niet over te praten, dacht ze.",
    "Toch bleef de gedachte haar de hele ochtend achtervolgen.",
]
ENGLISH_LINES = [
    "The following morning John went to the library early in the day.",
    "He had not expected anybody to be there at such an hour at all.",
    "A single lamp burned near the window and the room smelled of dust.",
    "Nothing in the quiet building suggested that anything had happened.",
]


# Pillow's bundled default font has no accented letters: it draws empty boxes for "ï" and "é".
_ACCENT_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
)


def accent_font_path() -> str | None:
    """A system font that really contains accented Latin letters, or None."""
    from pathlib import Path

    return next((path for path in _ACCENT_FONT_CANDIDATES if Path(path).is_file()), None)


def font(size: int, *, accents: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if accents:
        path = accent_font_path()
        if path is None:
            raise RuntimeError("no font with accented letters available")
        return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def page_image(
    lines: list[str],
    *,
    size: tuple[int, int] = (1654, 2339),  # A4 at 200 dpi
    font_size: int = 30,
    margin: int = 150,
    line_height: int | None = None,
    repeat: int = 1,
    paper: int = 240,
    ink: int = 25,
    accents: bool = False,
) -> Image.Image:
    image = Image.new("L", size, paper)
    draw = ImageDraw.Draw(image)
    face = font(font_size, accents=accents)
    step = line_height or int(font_size * 1.9)
    y = margin
    for _ in range(repeat):
        for line in lines:
            if y > size[1] - margin:
                break
            draw.text((margin, y), line, fill=ink, font=face)
            y += step
    return image


def degrade(
    image: Image.Image,
    *,
    blur: float = 0.0,
    noise: float = 0.0,
    scale: float = 1.0,
    rotate: float = 0.0,
    seed: int = 7,
) -> Image.Image:
    result = image
    if scale != 1.0:
        small = result.resize((max(1, int(result.width * scale)), max(1, int(result.height * scale))), Image.LANCZOS)
        result = small.resize(result.size, Image.BILINEAR)
    if blur:
        result = result.filter(ImageFilter.GaussianBlur(blur))
    if rotate:
        result = result.rotate(rotate, resample=Image.BICUBIC, fillcolor=240)
    if noise:
        generator = random.Random(seed)
        pixels = result.load()
        for _ in range(int(noise * result.width * result.height)):
            x, y = generator.randrange(result.width), generator.randrange(result.height)
            pixels[x, y] = generator.choice((0, 255))
    return result
