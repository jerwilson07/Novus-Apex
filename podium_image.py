"""Render the headerless Novus trophy podium with live nameplate text."""

from functools import lru_cache
from io import BytesIO
from pathlib import Path
from math import sin, cos, tau

from PIL import Image, ImageDraw, ImageFont
import cinematic_podium


FILENAME = "novus-live-podium.png"
ANIMATED_FILENAME = "novus-live-podium.webp"
LEGACY_ANIMATED_FILENAME = "novus-live-podium.gif"
ASSETS = Path(__file__).parent / "assets" / "podium"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# The approved Medal Hall composition, rasterized at production size.
SCALE = 1674 / 1200
PLATES = tuple(
    (round(x * SCALE), round(deck * SCALE), round(width * SCALE), tone)
    for x, deck, width, tone in ((600, 364, 340, "#ffd495"),
                                  (270, 435, 320, "#c7e6ff"),
                                  (930, 465, 320, "#ffb481"))
)
COUNT_KEYS = ("firsts", "seconds", "thirds")
COUNT_COLORS = ("#ffd495", "#c7e6ff", "#ffb481")


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
    fields = _label_fields(entries, names)
    if cinematic_podium.available():
        try:
            return cinematic_podium.render_static(fields)
        except (OSError, ValueError):
            import logging
            logging.exception('Cinematic static template unavailable; using previous podium')
    return _render_fields(fields)


def _label_fields(entries, names):
    if [row["rank"] for row in entries] != [1, 2, 3]:
        raise ValueError("A visual podium requires one player per placement")
    return tuple(
        (" ".join(names[row["member_id"]].split()), row["points"],
         tuple(row[key] for key in COUNT_KEYS))
        for row in entries
    )


def render_podium_animation(entries, names):
    """Animate the fixed artwork with live standings on the nameplates."""
    fields = _label_fields(entries, names)
    if cinematic_podium.available():
        return cinematic_podium.render_animated(fields)
    return _animate_fields(fields)


# These are the common aura anchors and colors from the approved preview.
MOTION_ZONES = (
    (600, 364 - 339 * .54, (255, 212, 149)),
    (270, 435 - 335 * .54, (199, 230, 255)),
    (930, 465 - 320 * .54, (255, 180, 129)),
)
FRAME_COUNT = 60
FRAME_DELAY_MS = 100


@lru_cache(maxsize=1)
def _foreground_mask():
    with Image.open(ASSETS / "motion-foreground-mask.png") as image:
        return image.convert("L")


@lru_cache(maxsize=96)
def _glow_sprite(rx, ry, color):
    gradient = Image.radial_gradient("L").resize((rx * 2, ry * 2),Image.Resampling.BILINEAR)
    sprite = Image.new("RGBA", gradient.size, color + (0,))
    sprite.putalpha(gradient.point(lambda v: round(255 * max(0, 1 - v / 172)**1.5)))
    return sprite


def _paint_glow(canvas, x, y, rx, ry, color, intensity):
    sprite = _glow_sprite(rx, ry, color).copy()
    sprite.putalpha(sprite.getchannel("A").point(lambda v: round(v * intensity)))
    canvas.alpha_composite(sprite,(x-rx,y-ry))


def _motion_frame(still, mask, index):
    frame = still.copy()
    phase = tau * index / FRAME_COUNT
    pulse = (1 + sin(phase)) / 2
    for j, (x, y, color) in enumerate(MOTION_ZONES):
        def glow(px, py, rx, ry, shade, intensity):
            _paint_glow(frame, round(px*SCALE), round(py*SCALE),
                        max(2, round(rx*SCALE)), max(2, round(ry*SCALE)),
                        shade, intensity)

        glow(x, y, 161, 157, color, .37 + .10*pulse + (.055 if j == 0 else 0))
        if j == 0:
            glow(x, y, 200, 177, color, .15 + .06*pulse)
            overlay = Image.new("RGBA", frame.size)
            mark = ImageDraw.Draw(overlay)
            ring = tuple(round(v*SCALE) for v in (x-176, 211-176, x+176, 211+176))
            mark.ellipse(ring, outline=(232,184,103,round(255*(.22+.09*pulse))), width=2)
            for offset in (0, tau/2):
                angle = phase + offset - 1.1
                mark.arc(ring, (angle-.63)*360/tau, angle*360/tau,
                         fill=(255,206,130,140), width=2)
                ex, ey = x+176*cos(angle), 211+176*sin(angle)
                glow(ex, ey, 12, 12, color, .60)
                px, py = ex*SCALE, ey*SCALE
                mark.ellipse((px-3, py-3, px+3, py+3), fill=(255,223,158,255))
            frame.alpha_composite(overlay)
        for ox, oy, offset, radius in ((116,-43,.7,5),(91,49,3.4,2.7)):
            angle = phase + offset
            strength = .27+.57*((1+sin(angle))/2)**2
            for side in (-1,1):
                px = x+side*(ox+4*sin(angle))
                py = y+oy+6*cos(angle)
                glow(px, py, radius*3, radius*3, color, strength*.55)
                r = max(2, round(radius*SCALE))
                star = Image.new("RGBA",(2*r+1,2*r+1))
                pen = ImageDraw.Draw(star)
                fill = color+(round(strength*255),)
                if radius >= 5:
                    n = max(1, round(r*.18))
                    pen.polygon(((r,0),(r+n,r-n),(2*r,r),(r+n,r+n),(r,2*r),
                                 (r-n,r+n),(0,r),(r-n,r-n)), fill=fill)
                else:
                    d = max(1, round(r*.55))
                    pen.ellipse((r-d,r-d,r+d,r+d), fill=fill)
                frame.alpha_composite(star,(round(px*SCALE)-r,round(py*SCALE)-r))
    return Image.composite(still,frame,mask)


class _MotionFrames:
    """Seekable frame source for Pillow's multi-frame WebP encoder."""
    n_frames = FRAME_COUNT - 1
    mode = "RGBA"

    def __init__(self, still, mask):
        self.still=still
        self.mask=mask
        self.image=None

    def seek(self,index):
        self.image=_motion_frame(self.still,self.mask,index+1)

    def getim(self):
        return self.image.getim()

    def tobytes(self):
        return self.image.tobytes()


@lru_cache(maxsize=4)
def _animate_fields(fields):
    with Image.open(BytesIO(_render_fields(fields))) as reference:
        still = reference.convert("RGBA")
    mask = _foreground_mask()
    if still.size != mask.size:
        raise ValueError("Podium artwork and motion mask do not match")
    first = _motion_frame(still,mask,0)
    output = BytesIO()
    first.save(output,format="WEBP",save_all=True,
               append_images=[_MotionFrames(still,mask)],duration=FRAME_DELAY_MS,
               loop=0,lossless=True,quality=0,method=1)
    return output.getvalue()


@lru_cache(maxsize=1)
def _base_artwork():
    """Load the approved trophies, stage, and fixed miniature icons."""
    with Image.open(ASSETS / "stage.webp") as art:
        image = art.convert("RGBA")
    anchors = tuple(
        tuple((x+round((-129+89*j+37)*SCALE), y+round(113*SCALE)) for j in range(3))
        for x, y, _, _ in PLATES
    )
    return image, anchors


@lru_cache(maxsize=8)
def _render_fields(fields):
    # Never paint onto the template itself. Only nameplate text is dynamic.
    artwork, anchors = _base_artwork()
    image = artwork.copy()
    draw = ImageDraw.Draw(image)
    for (display_name, points, counts), (x, y, width, accent), labels in zip(fields, PLATES, anchors):
        name, face = fit_name(draw, display_name, width - 40, sizes=(34, 31, 28, 25, 23))
        draw.text((x, y + round(49*SCALE)), name, font=face, anchor="ms", fill="#f8f4ff")
        draw.text((x, y + round(76*SCALE)), f"{points} PTS", font=font(27), anchor="ms", fill=accent)
        for count, position, color in zip(counts, labels, COUNT_COLORS):
            label = f"×{count}"
            fitted, count_font = fit_name(draw, label, round(50*SCALE), sizes=(31, 28, 25, 22, 19))
            draw.text(position, fitted, font=count_font, anchor="ls", fill=color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
