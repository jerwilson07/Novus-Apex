import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import proof_archive as archive
from test_pb_ui import record


URL = "https://cdn.discordapp.com/attachments/123/987/proof.png?ex=1&is=2&hm=old"
FRESH = URL.replace("hm=old", "hm=fresh")
DATA = b"\x89PNG\r\n\x1a\n" + b"saved-proof-image"


class MockStream(asyncio.StreamReader):
    async def iter_chunked(self, size):
        while chunk := await self.read(size):
            yield chunk


class MockResponse:
    def __init__(self, data, status=200):
        self.status = status
        self.content_length = len(data)
        self.content = MockStream()
        self.content.feed_data(data)
        self.content.feed_eof()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class ProofArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_archives_file_atomically_on_database_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "pb.sqlite3"
            session = SimpleNamespace(get=lambda *_a, **_k: MockResponse(DATA))
            ref = await archive.archive_discord_proof(db_path, session, URL)
            path = archive.archive_path(db_path, ref)
            self.assertEqual(path.parent, db_path.parent / "pb-proof-archive")
            self.assertEqual(path.read_bytes(), DATA)
            self.assertEqual(list(path.parent.iterdir()), [path])
            self.assertIsNone(archive.archive_path(db_path, "archive:../../pb.sqlite3"))

    async def test_rejects_wrong_content_oversize_and_non_discord_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pb.sqlite3"
            for data, url, limit in [
                (b"<html>not a screenshot</html>", URL, 100),
                (DATA, URL, 12),
                (DATA, URL.replace("cdn.discordapp.com", "cdn.discordapp.com.example.org"), 100),
            ]:
                session = SimpleNamespace(get=lambda *_a, **_k: MockResponse(data))
                with patch.object(archive, "MAX_PROOF_BYTES", limit):
                    with self.assertRaises(archive.ProofUnavailable):
                        await archive.archive_discord_proof(db_path, session, url)
            self.assertFalse(list((Path(tmp) / "pb-proof-archive").glob("*")))

    async def test_recovery_uses_refreshed_matching_media_not_other_attachments(self):
        wrong = FRESH.replace("/987/", "/123/")
        gallery = SimpleNamespace(items=[SimpleNamespace(
            media=SimpleNamespace(url=FRESH, proxy_url=None)
        )], children=[])
        message = SimpleNamespace(attachments=[SimpleNamespace(url=wrong)],
                                  embeds=[], components=[gallery])
        get_message = AsyncMock(return_value=message)
        channel = SimpleNamespace(fetch_message=get_message)
        rec = record(proof=URL, review_channel=12, review_message=34)
        saved = "archive:" + "a" * 32 + ".png"
        with (patch.object(bot.bot, "get_channel", return_value=channel),
              patch.object(bot, "archive_discord_proof", new=AsyncMock(return_value=saved)) as write):
            self.assertEqual(await bot.recover_discord_proof(rec, object()), saved)
            self.assertEqual(write.await_args.args[-1], FRESH)
            get_message.assert_awaited_once_with(34)

    async def test_archive_backfill_does_not_change_approved_record_fields(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(bot, "DBPATH", str(Path(tmp) / "pb.sqlite3")):
            db = bot.Store()
            await db.open()
            try:
                sid = await db.add(123, 456, "Raids", "The Fractured Archive", "Solo",
                                   "Player", "456", 42000, "2026-11-12", URL)
                await db.change(sid, "pending", "approved", 333)
                before = dict(await db.one("SELECT * FROM submissions WHERE id=?", (sid,)))
                saved = "archive:" + "a" * 32 + ".png"
                self.assertTrue(await db.replace_proof(sid, 123, URL, saved, status="approved"))
                self.assertFalse(await db.replace_proof(sid, 123, URL, "old copy"))
                after = dict(await db.one("SELECT * FROM submissions WHERE id=?", (sid,)))
                self.assertEqual(after, dict(before, proof=saved))
            finally:
                await db.conn.close()

    async def test_delivery_is_scoped_to_guild_and_private_before_approval(self):
        i = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=99), guild=None,
                            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                            followup=SimpleNamespace(send=AsyncMock()), filesize_limit=1024)
        ref = "archive:" + "a" * 32 + ".png"
        lookup = AsyncMock(return_value=record(proof=ref))
        with (tempfile.TemporaryDirectory() as tmp,
              patch.object(bot, "DBPATH", str(Path(tmp) / "pb.sqlite3")),
              patch.object(bot.store, "one", lookup)):
            await bot.show_record_proof(i, 1)
            self.assertEqual(lookup.await_args.args[-1], (123, 1))
            i.response.defer.assert_not_awaited()
            lookup.return_value = record(proof=ref, status="approved")
            dst = archive.archive_path(bot.DBPATH, ref)
            dst.parent.mkdir()
            dst.write_bytes(DATA)
            await bot.show_record_proof(i, 1)
            i.followup.send.assert_awaited_once()
            kwargs = i.followup.send.await_args.kwargs
            self.assertTrue(kwargs["ephemeral"])
            self.assertEqual(kwargs["file"].filename, "PB-1.png")

    async def test_leaderboard_omits_expiring_url_but_keeps_external_links(self):
        ref = "archive:" + "a" * 32 + ".png"
        for proof in (URL, ref):
            text = bot.short_record(record(proof=proof), 0)
            self.assertNotIn("/pbviewproof", text)
            self.assertNotIn(proof, text)
        self.assertIn("https://example.com/proof.png", bot.short_record(record(), 0))

    async def test_archived_proof_buttons_fit_populated_boards(self):
        ref = "archive:" + "a" * 32 + ".png"
        activity = "Chambers of Xeric"
        board = bot.RaidLeaderboard(activity, [
            (variant, [record(id=n, activity=activity, variant=variant, proof=ref)])
            for n, variant in enumerate(bot.RAIDS[activity], 1)
        ])
        self.assertLessEqual(board.total_children_count, 40)
        menus = [item for item in board.walk_children() if isinstance(item, bot.BoardProofSelect)]
        self.assertEqual(len(menus), 1)
        self.assertEqual(menus[0].item.options[0].label, "Solo · #1 · 14:32.60")
        self.assertTrue(board.is_persistent())
        badge = bot.ProofButton(8)
        self.assertEqual(badge.custom_id, "pb:proof:view:8")
        with patch.object(bot, "show_record_proof", new=AsyncMock()) as show:
            await badge.callback("interaction")
            show.assert_awaited_once_with("interaction", 8)

    async def test_araxxor_proof_buttons_target_each_record_without_typing(self):
        ref = "archive:" + "b" * 32 + ".png"
        rows = [record(id=sid, proof=ref, ms=ms) for sid, ms in ((14, 43800), (11, 46200), (67, 51800))]
        board = bot.BossLeaderboard("Araxxor", [("Standard", rows)])
        buttons = list(board.children[-1].children)
        self.assertEqual([button.item.label for button in buttons],
                         ["Proof #1 · 0:43.80", "Proof #2 · 0:46.20", "Proof #3 · 0:51.80"])
        self.assertEqual([button.sid for button in buttons], [14, 11, 67])
        self.assertNotIn("/pbviewproof", str(board.to_components()))

    async def test_araxxor_shows_three_full_image_thumbnails_without_buttons(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(bot, "DBPATH", str(Path(tmp) / "pb.sqlite3")):
            rows = []
            for sid, ms in ((14, 43800), (11, 46200), (67, 51800)):
                ref = f"archive:{sid:032x}.png"
                path = archive.archive_path(bot.DBPATH, ref)
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(DATA)
                rows.append(record(id=sid, proof=ref, ms=ms, activity="Araxxor"))
            blocks = [("Standard", rows)]
            channel = SimpleNamespace(guild=SimpleNamespace(filesize_limit=1024))
            message = SimpleNamespace(attachments=[], edit=AsyncMock())
            with patch.object(bot.BossLeaderboard, "blocks", new=AsyncMock(return_value=blocks)):
                await bot.write_public_board(channel, 123, "Araxxor", message=message)
                call = message.edit.await_args.kwargs
                view = call["view"]
                files = call["attachments"]
                self.assertEqual(len(files), 3)
                native = [item for item in view.walk_children() if isinstance(item, bot.ui.Thumbnail)]
                self.assertEqual([item.media.url for item in native],
                                 [f"attachment://{file.filename}" for file in files])
                self.assertEqual([item.description for item in native],
                                 ["View proof for rank 1, 0:43.80", "View proof for rank 2, 0:46.20",
                                  "View proof for rank 3, 0:51.80"])
                self.assertFalse(any(isinstance(item, (bot.ProofButton, bot.BoardProofSelect))
                                     for item in view.walk_children()))
                existing = [SimpleNamespace(filename=file.filename) for file in files]
                message.attachments = existing
                await bot.write_public_board(channel, 123, "Araxxor", message=message)
                self.assertEqual(message.edit.await_args.kwargs["attachments"], existing)

    async def test_large_board_splits_menus_and_dispatches_real_selected_value(self):
        ref = "archive:" + "c" * 32 + ".png"
        activity = "Chambers of Xeric"
        variants = bot.RAIDS[activity]
        board = bot.RaidLeaderboard(activity, [
            (variant, [record(id=5*n+rank+1, activity=activity, variant=variant, proof=ref)
                       for rank in range(5)])
            for n, variant in enumerate(variants)
        ])
        self.assertLessEqual(board.total_children_count, 40)
        menus = [item for item in board.walk_children() if isinstance(item, bot.BoardProofSelect)]
        self.assertEqual([len(menu.item.options) for menu in menus], [25, 15])
        original = menus[1]
        match = original.template.fullmatch(original.custom_id)
        restored = await bot.BoardProofSelect.from_custom_id(None, original.item, match)
        restored._refresh_state(None, {"values": ["26"]})
        with patch.object(bot, "show_record_proof", new=AsyncMock()) as show:
            await restored.callback("interaction")
            show.assert_awaited_once_with("interaction", 26)
