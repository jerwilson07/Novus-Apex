"""Render the headerless Novus trophy podium with live nameplate text."""

from functools import lru_cache
from io import BytesIO
from lzma import LZMAError, decompress
from pathlib import Path
from struct import unpack

from PIL import Image, ImageDraw, ImageFont


FILENAME = "novus-live-podium.png"
ANIMATED_FILENAME = "novus-live-podium.gif"
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


def fit_name(draw, name, width, sizes=(33, 30, 27, 24, 21)):
    """Fit long Discord names on the plate without overlapping its edge."""
    name = " ".join(name.split())
    for size in sizes:
        face = font(size)
        if draw.textlength(name, font=face) <= width:
            return name, face
    while len(name) > 1 and draw.textlength(name + "…", font=face) > width:
        name = name[:-1]
    return name + "…", face


def render_podium(entries, names):
    """Return a PNG from three unambiguously ranked players."""
    return _render_fields(_label_fields(entries, names))


def _label_fields(entries, names):
    if [row["rank"] for row in entries] != [1, 2, 3]:
        raise ValueError("A visual podium requires one player per placement")
    return tuple(
        (" ".join(names[row["member_id"]].split()), row["points"],
         tuple(row[key] for key in COUNT_KEYS))
        for row in entries
    )


def render_podium_animation(entries, names):
    """Superimpose live standings on the pre-rendered six-second loop."""
    return _animate_fields(_label_fields(entries, names))


@lru_cache(maxsize=1)
def _motion_stage():
    # Packed palette-index frames: size, timing, RGB palette, LZMA XOR deltas.
    data = b"".join((ASSETS / f"stage-motion.part{i}").read_bytes() for i in range(5))
    if len(data) <= 776:
        raise ValueError("Incomplete podium motion asset")
    width, height, count, delay = unpack(">HHHH", data[:8])
    if (width, height, count, delay) != (1008, 567, 60, 100):
        raise ValueError("Unexpected podium motion format")
    try:
        raw = decompress(data[776:])
    except LZMAError as exc:
        raise ValueError("Unreadable podium motion asset") from exc
    size = width * height
    if len(raw) != count * size:
        raise ValueError("Incomplete podium motion frames")
    frames = []
    current = 0
    for offset in range(0, len(raw), size):
        current ^= int.from_bytes(raw[offset:offset + size], "big")
        frames.append(current.to_bytes(size, "big"))
    return (width, height), delay, data[8:776], tuple(frames)


@lru_cache(maxsize=4)
def _animate_fields(fields):
    size, delay, palette, source = _motion_stage()
    base = Image.frombytes("P", size, source[0])
    base.putpalette(palette)
    color_frame = base.convert("RGB")
    draw = ImageDraw.Draw(color_frame)
    scale = size[0] / 1116
    patches = []
    for (name, points, counts), (x, y, color) in zip(
        fields, ((558, 356, "#f6c77e"), (288, 565, "#c5d5ec"), (828, 565, "#eaa477"))
    ):
        y -= 72  # Matches the approved stage's nameplate transform.
        center = round(x * scale)
        label, face = fit_name(draw, name, 282 * scale, (20, 18, 16, 14, 13))
        draw.text((center, round((y + 29) * scale)), label, font=face, anchor="ms", fill="#f8f4ff")
        draw.text((center, round((y + 48) * scale)), f"{points} PTS",
                  font=font(15), anchor="ms", fill=color)
        for j, (count, accent) in enumerate(zip(counts, COUNT_COLORS)):
            left = round((x + (j - 1) * 60 - 2) * scale)
            draw.text((left, round((y + 70) * scale)), f"× {count}", font=font(13),
                      anchor="ls", fill=accent)
        box = (round((x - 144) * scale), round((y + 10) * scale),
               round((x + 144) * scale), round((y + 77) * scale))
        patch = color_frame.crop(box).quantize(palette=base,dither=Image.Dither.NONE)
        patches.append((box[:2], patch))
    frames = []
    for raw in source:
        frame = Image.frombytes("P", size, raw)
        frame.putpalette(palette)
        for position, patch in patches:
            frame.paste(patch, position)
        frames.append(frame)
    output = BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:],
                   duration=delay, loop=0, disposal=1, optimize=True)
    return output.getvalue()


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
