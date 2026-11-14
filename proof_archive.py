"""Private, persistent storage for Discord PB proof files."""
import os
import re
import tempfile
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

import aiohttp


MAX_PROOF_BYTES = 20 * 1024 * 1024
CDN_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}
REF = re.compile(r"archive:([a-f0-9]{32}\.(?:png|jpe?g|gif|webp|mp4|mov|webm))\Z")


class ProofUnavailable(Exception):
    pass


def discord_attachment_key(url):
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc.lower() not in CDN_HOSTS:
            return None
        match = re.fullmatch(r"/(ephemeral-attachments|attachments)/(\d+)/(\d+)/([^/]+)", parts.path)
    except ValueError:
        return None
    if not match:
        return None
    scope, channel, attachment, filename = match.groups()
    return scope, channel, attachment, unquote(filename)


def is_private_proof(value):
    return bool(isinstance(value, str) and (REF.fullmatch(value) or discord_attachment_key(value)))


def same_attachment(left, right):
    key = discord_attachment_key(left)
    return bool(key and key == discord_attachment_key(right))


def archive_path(db_path, ref):
    match = REF.fullmatch(ref or "")
    return Path(db_path).resolve().parent / "pb-proof-archive" / match[1] if match else None


def media_matches(suffix, header):
    if suffix == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return header.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return header.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    if suffix == ".webm":
        return header.startswith(b"\x1aE\xdf\xa3")
    if suffix in {".mp4", ".mov"}:
        return header[4:8] == b"ftyp"
    return False


async def archive_discord_proof(db_path, session, url):
    key = discord_attachment_key(url)
    if not key:
        raise ProofUnavailable("Not a Discord attachment.")
    suffix = Path(key[-1]).suffix.lower()
    ref = f"archive:{uuid.uuid4().hex}{suffix}"
    target = archive_path(db_path, ref)
    if target is None:
        raise ProofUnavailable("Unsupported screenshot or video format.")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = None
    try:
        timeout = aiohttp.ClientTimeout(total=45, sock_read=15)
        async with session.get(url, allow_redirects=False, timeout=timeout) as response:
            if response.status != 200:
                raise ProofUnavailable(f"Discord returned HTTP {response.status}.")
            if response.content_length and response.content_length > MAX_PROOF_BYTES:
                raise ProofUnavailable("Proof exceeds 20 MiB.")
            header = await response.content.readexactly(12)
            if not media_matches(suffix, header):
                raise ProofUnavailable("Invalid screenshot or video data.")
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                temp = Path(output.name)
                output.write(header)
                size = len(header)
                async for chunk in response.content.iter_chunked(256 * 1024):
                    size += len(chunk)
                    if size > MAX_PROOF_BYTES:
                        raise ProofUnavailable("Proof exceeds 20 MiB.")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            temp.replace(target)
            return ref
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def attachment_candidates(message, original):
    """Only recover media that matches the saved attachment, not unrelated proof."""
    candidates = []

    def collect(media):
        candidates.extend(getattr(media, field, None) for field in ("url", "proxy_url"))

    def walk(component):
        for item in getattr(component, "items", ()) or ():
            collect(getattr(item, "media", None))
        if getattr(component, "media", None):
            collect(component.media)
        for child in getattr(component, "children", ()) or ():
            walk(child)

    for attachment in getattr(message, "attachments", ()):
        collect(attachment)
    for embed in getattr(message, "embeds", ()):
        collect(embed.image)
        collect(embed.video)
        collect(embed.thumbnail)
    for component in getattr(message, "components", ()):
        walk(component)
    return list(dict.fromkeys(url for url in candidates if same_attachment(url, original)))
