"""Approved Novus cinematic podium; update only the first WebP frame.

The prepared artwork has transparent, non-disposing delta frames over every
live label area. Dynamic text is encoded losslessly once; all later animation
chunks are copied unchanged, retaining the eight-second 40 FPS loop.
"""
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import struct
from PIL import Image, ImageDraw, ImageFont

ASSET = Path(__file__).parent / 'assets' / 'podium' / 'cinematic-stage.webp'
ASSET_SHA256 = '749a18743f4a19c2f9a5ec5713a0295105dd5f89d9a9b6e722806768297ef0de'
SIZE = (1672, 941)
FRAME_COUNT = 320
FRAME_MS = 25
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
PLATES = (
    (830, 474, 513, 340, '#ffd495', ((744, 566, 72), (858, 566, 54), (970, 566, 54))),
    (388, 559, 598, 278, '#c7e6ff', ((320, 640, 64), (416, 640, 46), (506, 640, 46))),
    (1275, 559, 598, 280, '#ffd495', ((1200, 640, 48), (1303, 640, 48), (1398, 640, 48))),
)

def available():
    return ASSET.is_file()

@lru_cache(maxsize=64)
def _font(size):
    return ImageFont.truetype(FONT, size)

def _xy(x, y):
    return round(x * SIZE[0] / 1648), round(y * SIZE[1] / 928)

def _text(draw, pos, label, width, size, color):
    width = round(width * SIZE[0] / 1648)
    label = ' '.join(str(label).split())
    while size > 13 and draw.textlength(label, font=_font(size)) > width:
        size -= 1
    face = _font(size)
    if draw.textlength(label, font=face) > width:
        while label and draw.textlength(label + '…', font=face) > width:
            label = label[:-1]
        label += '…'
    draw.text(_xy(*pos), label, font=face, fill=color, anchor='mm')

def _chunks(data):
    if data[:4] != b'RIFF' or data[8:12] != b'WEBP' or int.from_bytes(data[4:8], 'little') + 8 != len(data):
        raise ValueError('Invalid cinematic WebP container')
    pos = 12
    while pos < len(data):
        if pos + 8 > len(data):
            raise ValueError('Truncated WebP chunk')
        tag, size = struct.unpack_from('<4sI', data, pos)
        end = pos + 8 + size + size % 2
        if end > len(data):
            raise ValueError('Truncated WebP chunk payload')
        yield tag, data[pos + 8:pos + 8 + size], data[pos:end]
        pos = end

def _chunk(tag, data):
    return tag + struct.pack('<I', len(data)) + data + (b'\0' if len(data) % 2 else b'')

@lru_cache(maxsize=1)
def _stage():
    data = ASSET.read_bytes()
    if sha256(data).hexdigest() != ASSET_SHA256:
        raise ValueError('Cinematic stage checksum does not match the approved template')
    chunks = tuple(_chunks(data))
    frames = [p for t, p, _ in chunks if t == b'ANMF']
    if len(frames) != FRAME_COUNT or any(int.from_bytes(p[12:15], 'little') != FRAME_MS for p in frames):
        raise ValueError('Unexpected cinematic animation timing')
    if any(p[15] != 0 for p in frames[1:]):
        raise ValueError('Cinematic template has a destructive delta frame')
    with Image.open(BytesIO(data)) as im:
        if im.size != SIZE or im.n_frames != FRAME_COUNT or im.info.get('loop') != 0:
            raise ValueError('Unexpected cinematic template dimensions or loop')
        still = im.convert('RGB')
    return still, chunks

def _labeled_still(fields):
    image = _stage()[0].copy()
    draw = ImageDraw.Draw(image)
    if len(fields) != 3:
        raise ValueError('Three distinct placements are required')
    for (name, points, counts), (x, name_y, points_y, width, accent, slots) in zip(fields, PLATES):
        _text(draw, (x, name_y), name, width, 34, '#f8f4ff')
        _text(draw, (x, points_y), f'{points} PTS', width, 29, accent)
        for count, (cx, cy, cw) in zip(counts, slots):
            _text(draw, (cx, cy), f'×{count}', cw, 29, '#ffd495')
    return image

@lru_cache(maxsize=8)
def render_static(fields):
    out = BytesIO()
    _labeled_still(fields).save(out, 'PNG')
    return out.getvalue()

@lru_cache(maxsize=4)
def render_animated(fields):
    image = _labeled_still(fields)
    still = BytesIO()
    image.save(still, 'WEBP', lossless=True, quality=80, method=2)
    payload = b''.join(raw for tag, _, raw in _chunks(still.getvalue()) if tag in (b'ALPH', b'VP8 ', b'VP8L'))
    if not payload:
        raise ValueError('Cinematic first frame could not be encoded')
    parts = []
    replaced = False
    for tag, data, raw in _stage()[1]:
        if tag == b'ANMF' and not replaced:
            parts.append(_chunk(tag, data[:16] + payload))
            replaced = True
        else:
            parts.append(raw)
    body = b'WEBP' + b''.join(parts)
    return b'RIFF' + struct.pack('<I', len(body)) + body
