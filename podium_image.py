"""Render the headerless Novus trophy podium with live nameplate text."""

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


FILENAME = "novus-live-podium.png"
ASSETS = Path(__file__).parent / "assets" / "podium"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
PLATES = (
    (837, 426, 450, "#f6c77e"),
    (432, 740, 450, "#c5d5ec"),
    (1242, 740, 450, "#eaa477"),
)
COUNT_KEYS = ("firsts", "seconds", "thirds")
COUNT_COLORS = ("#f6c77e", "#c5d5ec", "#eaa477")


@lru_cache(maxsize=16)
def font(size):
    return ImageFont.truetype(FONT, size)


def fit_name(draw, name, width):
    """Fit long Discord names on the plate without overlapping its edge."""
    name = " ".join(name.split())
    for size in (33, 30, 27, 24, 21):
        face = font(size)
        if draw.textlength(name, font=face) <= width:
            return name, face
    while len(name) > 1 and draw.textlength(name + "…", font=face) > width:
        name = name[:-1]
    return name + "…", face


def render_podium(entries, names):
    """Return a PNG from three unambiguously ranked players."""
    if [row["rank"] for row in entries] != [1, 2, 3]:
        raise ValueError("A visual podium requires one player per placement")
    fields = tuple(
        (" ".join(names[row["member_id"]].split()), row["points"],
         tuple(row[key] for key in COUNT_KEYS))
        for row in entries
    )
    return _render_fields(fields)


@lru_cache(maxsize=1)
def _base_artwork():
    """Build the fixed background, including the miniature trophy icons."""
    with Image.open(ASSETS / "stage.webp") as art, Image.open(ASSETS / "trophy-icons.png") as icons:
        image = art.convert("RGBA")
        glyphs = [icons.crop((i * 40, 0, (i + 1) * 40, 48)) for i in range(3)]
    draw = ImageDraw.Draw(image)
    anchors = []
    for index, (x, y, _, _) in enumerate(PLATES):
        # Set icon positions to the original composition, independent of the
        # displayed count. The top pedestal's first count had two digits.
        guides = ("× 00" if index == 0 else "× 0", "× 0", "× 0")
        widths = [40 + int(draw.textlength(label, font=font(22)) + 1) for label in guides]
        gap = 13
        left = x - (sum(widths) + gap * 2) // 2
        plate_anchors = []
        for slot_width, glyph in zip(widths, glyphs):
            image.alpha_composite(glyph, (left, y + 75))
            plate_anchors.append((left + 40, y + 99))
            left += slot_width + gap
        anchors.append(tuple(plate_anchors))
    return image, tuple(anchors)


@lru_cache(maxsize=8)
def _render_fields(fields):
    # Never paint onto the template itself. Only nameplate text is dynamic.
    artwork, anchors = _base_artwork()
    image = artwork.copy()
    draw = ImageDraw.Draw(image)
    for (display_name, points, counts), (x, y, width, accent), labels in zip(fields, PLATES, anchors):
        name, face = fit_name(draw, display_name, width - 38)
        draw.text((x, y + 32), name, font=face, anchor="mm", fill="#f8f4ff")
        draw.text((x, y + 62), f"{points} PTS", font=font(26), anchor="mm", fill=accent)
        for count, position, color in zip(counts, labels, COUNT_COLORS):
            draw.text(position, f"× {count}", font=font(22), anchor="lm", fill=color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
