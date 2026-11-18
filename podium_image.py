"""Render the headerless Novus trophy podium with live nameplate text."""

from functools import lru_cache
from io import BytesIO
from pathlib import Path
from math import sin, cos, tau

from PIL import Image, ImageDraw, ImageFont


FILENAME = "novus-live-podium.png"
ANIMATED_FILENAME = "novus-live-podium.webp"
LEGACY_ANIMATED_FILENAME = "novus-live-podium.gif"
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


# Coordinates are in the approved 1116 by 628 stage drawing, whose layers
# were translated up 72 units. The production artwork is exactly 1.5x scale.
MOTION_ZONES = (
    ((558,220,175,136), (255,230,159),
     ((439,161,.1,5.6), (666,170,1.2,6.5), (700,249,2.4,4), (413,232,4,3), (460,265,5,3))),
    ((288,468,152,120), (197,231,255),
     ((182,418,1.7,5.6), (377,432,.8,5.2), (157,474,3.2,4), (399,493,4.8,3))),
    ((828,468,152,120), (255,181,129),
     ((725,421,.2,5.1), (928,431,1.2,5.5), (707,494,3.2,3), (945,481,4.4,4))),
)
FRAME_COUNT = 60
FRAME_DELAY_MS = 100
SCALE = 1.5


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
    for j, ((x,y,rx,ry), color, stars) in enumerate(MOTION_ZONES):
        pulse = (1 + sin(phase-j*.9))/2
        _paint_glow(frame,round(x*SCALE),round((y-72)*SCALE),
                    round(rx*SCALE),round(ry*SCALE),color,
                    (.09+.10*pulse) if j==0 else (.07+.08*pulse))
        for sx,sy,offset,radius in stars:
            px=round((sx+8*sin(phase+offset))*SCALE)
            py=round((sy-72+11*cos(phase+offset))*SCALE)
            twinkle=((1+sin(phase+offset))/2)**1.7
            strength=.13+.57*twinkle
            scale=radius*(.64+.36*twinkle)*SCALE
            glow=max(2,round(scale*2.3))
            _paint_glow(frame,px,py,glow,glow,color,strength*.46)
            r=max(1,round(scale))
            star=Image.new("RGBA",(2*r+1,2*r+1))
            draw=ImageDraw.Draw(star)
            fill=color+(round(strength*255),)
            if radius>=4.4:
                n=max(1,round(r*.19))
                draw.polygon(((r,0),(r+n,r-n),(2*r,r),(r+n,r+n),(r,2*r),
                              (r-n,r+n),(0,r),(r-n,r-n)),fill=fill)
            else:
                d=max(1,round(r*.34))
                draw.ellipse((r-d,r-d,r+d,r+d),fill=fill)
            frame.alpha_composite(star,(px-r,py-r))
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
