"""
Generate 180×180 apple-touch-icon PNGs for Accologise key pages.

Usage (dev only — commit the PNGs; Pillow is not a runtime app dependency):
  pip install pillow
  python scripts/generate_home_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 180
MARGIN = 18  # iOS safe zone

# Brand / WC section colours
SLATE = (15, 23, 42)  # #0f172a
BLUE = (29, 78, 216)  # #1d4ed8
AMBER = (217, 119, 6)  # #d97706
GREEN = (21, 128, 61)  # #15803d
RED = (185, 28, 28)  # #b91c1c
WHITE = (255, 255, 255)
SOFT = (226, 232, 240)  # #e2e8f0

OUT = Path(__file__).resolve().parents[1] / "app" / "static" / "icons" / "home"


def _font(size: int) -> ImageFont.ImageFont:
    for name in (
        "segoeui.ttf",
        "SegoeUI.ttf",
        "arial.ttf",
        "Arial.ttf",
        "DejaVuSans-Bold.ttf",
        "DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _new(bg: tuple[int, int, int]) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (SIZE, SIZE), bg)
    return img, ImageDraw.Draw(img)


def _circle(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, fill) -> None:
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)


def icon_dashboard() -> Image.Image:
    """Slate field + bold A monogram (main Accologise mark)."""
    img, d = _new(SLATE)
    # Soft blue orb
    _circle(d, 90, 88, 52, BLUE)
    font = _font(78)
    text = "A"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((SIZE - tw) / 2 - bbox[0], (SIZE - th) / 2 - bbox[1] - 4), text, font=font, fill=WHITE)
    return img


def icon_clients() -> Image.Image:
    """Blue field + two simple person silhouettes."""
    img, d = _new(BLUE)
    # person 1
    _circle(d, 68, 62, 16, WHITE)
    d.rounded_rectangle((48, 86, 88, 138), radius=18, fill=WHITE)
    # person 2 (offset)
    _circle(d, 118, 70, 14, SOFT)
    d.rounded_rectangle((100, 92, 136, 138), radius=16, fill=SOFT)
    return img


def icon_jobs() -> Image.Image:
    """Slate + clipboard / checklist glyph."""
    img, d = _new(SLATE)
    # board
    d.rounded_rectangle((48, 42, 132, 148), radius=12, fill=BLUE)
    d.rounded_rectangle((54, 52, 126, 142), radius=8, fill=WHITE)
    # clip
    d.rounded_rectangle((72, 34, 108, 54), radius=6, fill=SOFT)
    # lines / tick
    d.line((68, 78, 112, 78), fill=SLATE, width=4)
    d.line((68, 98, 112, 98), fill=SLATE, width=4)
    d.line((68, 118, 98, 118), fill=SLATE, width=4)
    # green tick
    d.line((100, 112, 110, 124), fill=GREEN, width=5)
    d.line((110, 124, 128, 100), fill=GREEN, width=5)
    return img


def icon_debtors() -> Image.Image:
    """Amber + document with £."""
    img, d = _new(AMBER)
    d.rounded_rectangle((50, 36, 130, 150), radius=10, fill=WHITE)
    # fold corner
    d.polygon([(100, 36), (130, 36), (130, 66)], fill=SOFT)
    font = _font(54)
    text = "£"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text((90 - tw / 2 - bbox[0], 88 - th / 2 - bbox[1]), text, font=font, fill=AMBER)
    d.line((64, 128, 116, 128), fill=SOFT, width=4)
    return img


def icon_cash() -> Image.Image:
    """Green + stacked bank notes / card."""
    img, d = _new(GREEN)
    # back notes
    d.rounded_rectangle((42, 58, 128, 108), radius=10, fill=(22, 101, 52))
    d.rounded_rectangle((48, 70, 138, 122), radius=10, fill=(34, 197, 94))
    # front card
    d.rounded_rectangle((40, 82, 140, 138), radius=12, fill=WHITE)
    d.rounded_rectangle((40, 82, 140, 100), radius=0, fill=SLATE)
    # chip
    d.rounded_rectangle((54, 108, 76, 122), radius=3, fill=AMBER)
    font = _font(28)
    d.text((90, 108), "£", font=font, fill=GREEN)
    return img


def icon_creditors() -> Image.Image:
    """Red + purchase bag / outgoing invoice."""
    img, d = _new(RED)
    # bag body
    d.rounded_rectangle((52, 72, 128, 148), radius=14, fill=WHITE)
    # handles
    d.arc((62, 42, 96, 86), start=200, end=340, fill=WHITE, width=7)
    d.arc((84, 42, 118, 86), start=200, end=340, fill=WHITE, width=7)
    # tag / £
    font = _font(40)
    text = "£"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text((90 - tw / 2 - bbox[0], 100 - th / 2 - bbox[1]), text, font=font, fill=RED)
    return img


ICONS = {
    "dashboard": icon_dashboard,
    "clients": icon_clients,
    "jobs": icon_jobs,
    "debtors": icon_debtors,
    "cash": icon_cash,
    "creditors": icon_creditors,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in ICONS.items():
        img = fn()
        if img.size != (SIZE, SIZE):
            img = img.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
        path = OUT / f"{name}.png"
        img.save(path, format="PNG", optimize=True)
        print(f"wrote {path} {img.size}")


if __name__ == "__main__":
    main()
