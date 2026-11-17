"""Render the headerless Novus trophy podium with live nameplate text."""

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


FILENAME = "novus-live-podium.png"
ASSETS = Path(__file__).parent / "assets" / "podium"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
PLATES = (
    (585, 426, 360, "#f6c77e"),
    (339, 740, 343, "#c5d5ec"),
    (831, 740, 343, "#eaa477"),
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
    with Image.open(ASSETS / "stage.webp") as art, Image.open(ASSETS / "trophy-icons.png") as icons:
        image = art.convert("RGBA")
        glyphs = [icons.crop((i * 40, 0, (i + 1) * 40, 48)) for i in range(3)]
    draw = ImageDraw.Draw(image)
    for entry, (x, y, width, accent) in zip(entries, PLATES):
        name, face = fit_name(draw, names[entry["member_id"]], width - 38)
        draw.text((x, y + 32), name, font=face, anchor="mm", fill="#f8f4ff")
        draw.text((x, y + 62), f"{entry['points']} PTS", font=font(26), anchor="mm", fill=accent)

        # These miniatures use the same art as pb1/pb2/pb3. Discord emoji codes
        # can't be drawn inside an uploaded PNG, so draw the actual icons.
        counts = [entry[key] for key in COUNT_KEYS]
        count_font = font(22)
        labels = [f"× {count}" for count in counts]
        widths = [40 + int(draw.textlength(label, font=count_font) + 1) for label in labels]
        gap = 13
        left = x - (sum(widths) + gap * 2) // 2
        for label, slot_width, color, glyph in zip(labels, widths, COUNT_COLORS, glyphs):
            image.alpha_composite(glyph, (left, y + 75))
            draw.text((left + 40, y + 99), label, font=count_font, anchor="lm", fill=color)
            left += slot_width + gap
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
