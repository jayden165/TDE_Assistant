"""Per-member welcome card: the dark "Member #N" card with the member's avatar and name.

The card is drawn on top of assets/welcome_card_template.png (the card, pill and Daily Emerald
logo with the avatar / name / member number blanked out), so the look comes from that PNG.
To change the design, swap the template - keep it 500x350 and keep the blanked areas flat.

Fonts: drop Raleway-Bold.ttf and Raleway-SemiBold.ttf into assets/fonts/ to match the original
card exactly. Without them it falls back to a system bold font, and finally to Pillow's built-in
font, so it always renders something. Characters the main font can't draw (emoji, CJK, fancy
unicode names) are drawn with a fallback font where one is installed.
"""
import asyncio
import io
import logging
import os
from functools import lru_cache

import discord
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(_BASE, "assets", "welcome_card_template.png")
FONT_DIR = os.path.join(_BASE, "assets", "fonts")
CARD_FILENAME = "welcome_card.png"

# ---- layout (pixels, measured from the example cards) ----
CARD_COLOR = (34, 36, 37, 255)
CENTER_X = 249.5
AVATAR_BOX = (201, 96, 298, 193)       # 97px circle
PILL_BASELINE_Y = 76
PILL_SIZE = 14
NAME_BASELINE_Y = 268
NAME_SIZE = 25
NAME_MIN_SIZE = 13
NAME_MAX_WIDTH = 320                   # card is 366px wide; leave a margin
TEXT_COLOR = (255, 255, 255, 255)
FALLBACK_AVATAR_COLOR = (88, 101, 242, 255)

_SYSTEM_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
]
# only used for characters the main font has no glyph for
_GLYPH_FALLBACKS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansSymbols-Bold.ttf",
    "/System/Library/Fonts/Apple Symbols.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "C:\\Windows\\Fonts\\seguisym.ttf",
    "C:\\Windows\\Fonts\\msyh.ttc",
]


def _load(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return None


@lru_cache(maxsize=64)
def _main_font(weight: str, size: int):
    """weight: 'bold' or 'semibold'."""
    names = ["Raleway-SemiBold.ttf", "Raleway-Bold.ttf"] if weight == "semibold" else ["Raleway-Bold.ttf"]
    for n in names:
        f = _load(os.path.join(FONT_DIR, n), size)
        if f:
            return f
    for p in _SYSTEM_BOLD:
        f = _load(p, size)
        if f:
            return f
    return ImageFont.load_default(size)


@lru_cache(maxsize=64)
def _fallback_fonts(size: int):
    return tuple(f for f in (_load(p, size) for p in _GLYPH_FALLBACKS) if f)


def _size_scale() -> float:
    """Raleway is narrower than the fallback fonts, so shrink them a little to keep the look."""
    return 1.0 if os.path.exists(os.path.join(FONT_DIR, "Raleway-Bold.ttf")) else 0.9


@lru_cache(maxsize=4096)
def _glyph_pixels(font, ch: str) -> bytes:
    im = Image.new("L", (96, 96), 0)
    ImageDraw.Draw(im).text((8, 8), ch, font=font, fill=255)
    return im.tobytes()


def _has_glyph(font, ch: str) -> bool:
    """True if `font` really draws `ch` (a missing glyph renders as the same box as U+FFFF)."""
    if ch.isspace():
        return True
    try:
        return _glyph_pixels(font, ch) != _glyph_pixels(font, "\uffff")
    except Exception:
        return True


def _runs(text: str, weight: str, size: int):
    """Split text into [(font, run_text)] so each run is drawn with a font that has its glyphs."""
    main = _main_font(weight, size)
    fallbacks = _fallback_fonts(size)
    runs = []
    for ch in text:
        font = main
        if not _has_glyph(main, ch):
            font = next((f for f in fallbacks if _has_glyph(f, ch)), main)
        if runs and runs[-1][0] is font:
            runs[-1][1] += ch
        else:
            runs.append([font, ch])
    return runs


def _width(runs) -> float:
    return sum(f.getlength(t) for f, t in runs)


def _draw_centered(draw: ImageDraw.ImageDraw, text: str, baseline_y: int, weight: str, size: int,
                   max_width: int | None = None, min_size: int | None = None):
    """Draw text centred on CENTER_X. If max_width is given, shrink the font (down to min_size) and
    finally truncate with an ellipsis until it fits."""
    size = round(size * _size_scale())
    min_size = round(min_size * _size_scale()) if min_size else None
    runs = _runs(text, weight, size)
    if max_width:
        while _width(runs) > max_width and size > (min_size or size):
            size -= 1
            runs = _runs(text, weight, size)
        while _width(runs) > max_width and len(text) > 1:
            text = text[:-1].rstrip()
            runs = _runs(text + "…", weight, size)
    x = CENTER_X - _width(runs) / 2
    for font, t in runs:
        draw.text((x, baseline_y), t, font=font, fill=TEXT_COLOR, anchor="ls")
        x += font.getlength(t)


def _circle_avatar(avatar_bytes: bytes | None) -> Image.Image:
    """The avatar cropped to a square, resized to the circle and masked (anti-aliased edge)."""
    w = AVATAR_BOX[2] - AVATAR_BOX[0]
    h = AVATAR_BOX[3] - AVATAR_BOX[1]
    img = None
    if avatar_bytes:
        try:
            img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")  # animated: first frame
        except Exception:
            img = None
    if img is None:
        img = Image.new("RGBA", (w, h), FALLBACK_AVATAR_COLOR)
    else:
        side = min(img.size)
        left, top = (img.width - side) // 2, (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side)).resize((w, h), Image.LANCZOS)
        flat = Image.new("RGBA", (w, h), CARD_COLOR)   # transparent avatars sit on the card colour
        flat.alpha_composite(img)
        img = flat
    ss = 4  # supersample the mask so the circle edge is smooth
    mask = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, w * ss - 1, h * ss - 1), fill=255)
    img.putalpha(mask.resize((w, h), Image.LANCZOS))
    return img


def render_card(avatar_bytes: bytes | None, name: str, number: int) -> bytes:
    """PNG bytes of the welcome card. Blocking (Pillow) - call it through asyncio.to_thread."""
    card = Image.open(TEMPLATE_PATH).convert("RGBA")
    card.alpha_composite(_circle_avatar(avatar_bytes), AVATAR_BOX[:2])
    draw = ImageDraw.Draw(card)
    _draw_centered(draw, f"Member #{number}", PILL_BASELINE_Y, "semibold", PILL_SIZE)
    _draw_centered(draw, name, NAME_BASELINE_Y, "bold", NAME_SIZE, NAME_MAX_WIDTH, NAME_MIN_SIZE)
    out = io.BytesIO()
    card.save(out, "PNG")
    return out.getvalue()


async def _avatar_bytes(member: discord.Member) -> bytes | None:
    for asset in (member.display_avatar, member.default_avatar):
        try:
            return await asset.replace(size=256, static_format="png").read()
        except (discord.HTTPException, discord.NotFound):
            continue
    return None


async def build_card_file(member: discord.Member):
    """A fresh discord.File (single-use) with this member's card, or None if it can't be made -
    callers then fall back to the server's normal welcome image."""
    try:
        avatar = await _avatar_bytes(member)
        number = member.guild.member_count or 0
        png = await asyncio.to_thread(render_card, avatar, member.display_name, number)
        return discord.File(io.BytesIO(png), filename=CARD_FILENAME)
    except Exception:
        log.exception("Could not build the welcome card for %s", member.id)
        return None
