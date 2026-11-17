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
            i.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            i.followup.send.assert_awaited_once_with("PB not found or not available to you.", ephemeral=True)
            i.followup.send.reset_mock()
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

    async def test_full_raid_board_uses_inline_links_with_no_proof_controls(self):
        activity = "Chambers of Xeric"
        ref = "archive:" + "a" * 32 + ".png"
        with patch.dict(bot.PUBLIC_PROOF_URLS, {ref: FRESH}, clear=True):
            board = bot.RaidLeaderboard(activity, [
                (variant, [record(id=5*n+rank+1, activity=activity, variant=variant, proof=ref)
                           for rank in range(5)])
                for n, variant in enumerate(bot.RAIDS[activity])
            ])
        self.assertLessEqual(board.total_children_count, 40)
        text = "\n".join(item.content for item in board.walk_children() if isinstance(item, bot.ui.TextDisplay))
        self.assertEqual(text.count(f"[Proof]({FRESH})"), 40)
        self.assertFalse(any(isinstance(item, (bot.ui.Thumbnail, bot.ui.MediaGallery,
                                              bot.ProofButton, bot.BoardProofSelect))
                             for item in board.walk_children()))
        self.assertTrue(board.is_persistent())

    async def test_araxxor_links_each_review_image_without_buttons_or_uploads(self):
        with (tempfile.TemporaryDirectory() as tmp,
              patch.object(bot, "DBPATH", str(Path(tmp) / "pb.sqlite3")),
              patch.dict(bot.PUBLIC_PROOF_URLS, clear=True)):
            rows = []
            for sid, ms in ((14, 43800), (11, 46200), (67, 51800), (999, 20000)):
                ref = f"archive:{sid:032x}.png"
                path = archive.archive_path(bot.DBPATH, ref)
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(DATA)
                rows.append(record(id=sid, proof=ref, ms=ms, activity="Araxxor",
                                   status="pending" if sid == 999 else "approved",
                                   category="Bosses", variant="Standard", member_ids=str(sid), members="Test"))
            blocks = [("Standard", rows)]
            message = SimpleNamespace(id=42, edit=AsyncMock())
            channel = SimpleNamespace(send=AsyncMock(), fetch_message=AsyncMock(return_value=message))

            def proof_url(sid, nonce):
                return f"https://cdn.discordapp.com/attachments/123/{sid}/proof.png?ex=ffffffff&hm={nonce}"

            nonce = "initial"

            async def cache(r, path):
                bot.PUBLIC_PROOF_URLS[r["proof"]] = proof_url(r["id"], nonce)

            with (patch.object(bot.BossLeaderboard, "blocks", new=AsyncMock(return_value=blocks)),
                  patch.object(bot, "cache_proof_from_review", new=AsyncMock(side_effect=cache)) as backing):
                await bot.write_linked_board(channel, 123, "Araxxor", message=message)
                self.assertEqual([call.args[0]["id"] for call in backing.await_args_list], [14, 11, 67])
                view = message.edit.await_args.kwargs["view"]
                text = "\n".join(item.content for item in view.walk_children() if isinstance(item, bot.ui.TextDisplay))
                for sid in (14, 11, 67):
                    self.assertIn(f"<@{sid}> · [Proof]({proof_url(sid, 'initial')})", text)
                self.assertNotIn("/pbviewproof", text)
                self.assertFalse(any(isinstance(item, (bot.ui.Thumbnail, bot.ui.MediaGallery,
                                                      bot.ProofButton, bot.BoardProofSelect))
                                     for item in view.walk_children()))
                self.assertEqual(message.edit.await_args.kwargs["attachments"], [])
                for r in rows[:3]:
                    bot.PUBLIC_PROOF_URLS[r["proof"]] = proof_url(r["id"], nonce).replace("ffffffff", "1")
                nonce = "fresh"
                await bot.write_linked_board(channel, 123, "Araxxor", 42, archived_only=True)
                self.assertEqual(message.edit.await_count, 2)
                channel.fetch_message.assert_awaited_once_with(42)
                channel.send.assert_not_awaited()
                updated = str(message.edit.await_args.kwargs["view"].to_components())
                self.assertIn("&hm=fresh", updated)
                self.assertNotIn("&hm=initial", updated)

    async def test_approved_review_retains_an_attachment_via_native_file_component(self):
        r = record(status="approved", proof=f"archive:{1:032x}.png", review_channel=17, review_message=42)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(bot.PUBLIC_PROOF_URLS, clear=True):
            path = Path(tmp) / f"{1:032x}.png"
            path.write_bytes(DATA)
            filename = bot.proof_attachment_name(r, path)
            url = f"https://cdn.discordapp.com/attachments/17/987/{filename}?ex=ffffffff"
            media = SimpleNamespace(url=url)
            component = SimpleNamespace(type=bot.discord.ComponentType.file, media=media)
            published = SimpleNamespace(attachments=[], components=[SimpleNamespace(children=[component])])
            message = SimpleNamespace(attachments=[], components=[], author=SimpleNamespace(id=5),
                                      edit=AsyncMock(return_value=published))
            channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
            with (patch.object(bot, "bot", SimpleNamespace(get_channel=lambda _: channel, user=SimpleNamespace(id=5))),
                  patch.object(bot.store, "unmark_proof_button_card", new=AsyncMock())):
                await bot.cache_legacy_review_proof(r, path)
                edit = message.edit.await_args.kwargs
                self.assertEqual(edit["attachments"][0].filename, filename)
                components = [item for item in edit["view"].walk_children() if isinstance(item, bot.ui.File)]
                self.assertEqual([item.media.url for item in components], [f"attachment://{filename}"])
                self.assertEqual(bot.PUBLIC_PROOF_URLS[r["proof"]], url)
                channel.fetch_message.return_value = SimpleNamespace(
                    attachments=[], components=published.components, author=message.author, edit=message.edit
                )
                await bot.cache_legacy_review_proof(r, path)
                message.edit.assert_awaited_once()

    def test_refreshes_signatures_before_expiration(self):
        at = 100_000
        margin = 2 * bot.PROOF_LINK_REFRESH_SECONDS
        self.assertTrue(bot.proof_link_needs_refresh(URL, at))
        self.assertTrue(bot.proof_link_needs_refresh(URL.replace("ex=1", f"ex={at+margin:x}"), at))
        self.assertFalse(bot.proof_link_needs_refresh(URL.replace("ex=1", f"ex={at+margin+1:x}"), at))

    async def test_oversized_raid_keeps_all_records_and_champion_proofs(self):
        activity = "Chambers of Xeric"
        ref = "archive:" + "a" * 32 + ".png"
        url = FRESH + "a" * 150
        blocks = [
            (variant, [record(id=5*n+rank+1, activity=activity, variant=variant, proof=ref, ms=1000*(rank+1))
                       for rank in range(5)])
            for n, variant in enumerate(bot.RAIDS[activity])
        ]
        with patch.dict(bot.PUBLIC_PROOF_URLS, {ref: url}, clear=True):
            pages = bot.raid_board_page_plan(activity, blocks)
        self.assertGreater(len(pages), 1)
        for index, (label, view) in enumerate(pages):
            if index + 1 < len(pages):
                bot.add_board_page_link(view, pages[index + 1][0],
                                        "https://discord.com/channels/123/456/789")
            self.assertLessEqual(bot.pb_view_text_units(view), bot.PB_PUBLIC_TEXT_LIMIT)
            self.assertLessEqual(len(list(view.walk_children())), 40)
        text = "\n".join(item.content for _, view in pages for item in view.walk_children()
                         if isinstance(item, bot.ui.TextDisplay))
        self.assertEqual(text.count("**05.00**"), len(blocks))
        self.assertEqual(text.count(f"[Proof]({url})"), len(blocks) * 5)
        self.assertEqual(text.count(f"## {activity}"), 1)
        self.assertNotIn(" · PB #", text)
        self.assertNotIn("Empty", text)

    async def test_raid_pages_keep_main_jump_target_and_reuse_existing_continuation(self):
        activity = "The Fractured Archive"
        blocks = [
            (scale, [record(id=n + 1, activity=activity, variant=scale,
                            proof="https://example.com/" + "a" * 1650)])
            for n, scale in enumerate(("Duo", "Trio", "5-Man"))
        ]
        main = SimpleNamespace(id=10, jump_url="https://discord.com/channels/123/456/10")
        main.edit = AsyncMock(return_value=main)
        extra = SimpleNamespace(id=11, jump_url="https://discord.com/channels/123/456/11")
        extra.edit = AsyncMock(return_value=extra)
        channel = SimpleNamespace(id=456, send=AsyncMock(return_value=extra),
                                  fetch_message=AsyncMock(return_value=extra))
        known = {"page": 1, "channel": 456, "message": 11}
        with (patch.object(bot.store, "raid_pages", new=AsyncMock(side_effect=[[], [known]])),
              patch.object(bot.store, "set_raid_page", new=AsyncMock()) as track):
            first = await bot.publish_raid_board_pages(channel, 123, activity, main, blocks, None)
            second = await bot.publish_raid_board_pages(channel, 123, activity, main, blocks, None)
        self.assertEqual(first, [main, extra])
        self.assertEqual(second, first)
        channel.send.assert_awaited_once()
        channel.fetch_message.assert_awaited_once_with(11)
        track.assert_awaited_once_with(123, activity, 1, 456, 11)
        main_text = [item.content for item in main.edit.await_args.kwargs["view"].walk_children()
                     if isinstance(item, bot.ui.TextDisplay)]
        self.assertIn(f"### [5-Man ↓]({extra.jump_url})", main_text)
        extra_text = [item.content for item in extra.edit.await_args.kwargs["view"].walk_children()
                      if isinstance(item, bot.ui.TextDisplay)]
        self.assertIn("### 5-Man", extra_text)
        self.assertFalse(any(activity in item for item in extra_text))

    def test_displaced_raid_page_reflows_only_the_following_board_tail(self):
        raid = "The Fractured Archive"
        roots = {
            bot.PB_FOOTER_KEY: {"message": 150},
            "Theatre of Blood": {"message": 120},
            "Inferno": {"message": 90},
            raid: {"message": 100},
            bot.SNAPSHOT_CONTINUATION_KEY: {"message": 140},
        }
        page = {"activity": raid, "page": 1, "message": 900}
        self.assertEqual(bot.public_board_tail_to_reflow(roots, [page]), [
            raid, "Theatre of Blood", bot.SNAPSHOT_CONTINUATION_KEY, bot.PB_FOOTER_KEY,
        ])
        self.assertEqual(bot.public_board_tail_to_reflow(roots, [dict(page, message=110)]), [])

    async def test_reflow_stages_adjacent_pages_before_switching_live_message_ids(self):
        raid = "The Fractured Archive"
        footer = bot.PB_FOOTER_KEY
        roots = {raid: {"message": 100}, footer: {"message": 150}}
        pages = [{"activity": raid, "page": 1, "message": 900}]
        views = [bot.PBLayoutView(timeout=None), bot.PBLayoutView(timeout=None)]
        for view, label in zip(views, ("## The Fractured Archive", "### 5-Man")):
            view.add_item(bot.ui.TextDisplay(label))
        messages = [SimpleNamespace(id=i, jump_url=f"https://discord.com/channels/123/456/{i}",
                                    edit=AsyncMock(), delete=AsyncMock())
                    for i in range(1000, 1003)]
        switch = AsyncMock()
        channel = SimpleNamespace(id=456, send=AsyncMock(side_effect=messages))
        with (patch.object(bot, "public_tail_views", new=AsyncMock(return_value=[
                  ("Solo", views[0]), ("5-Man", views[1]),
              ])),
              patch.object(bot.store, "switch_public_board_tail", new=switch)):
            out = await bot.publish_public_board_tail(
                channel, 123, [raid, footer], roots, pages, {"channel": 456, "message": 2},
            )
        self.assertEqual(out, {raid: [1000, 1001], footer: [1002]})
        self.assertEqual(channel.send.await_args_list[2].args[0],
                         bot.pb_scroll_to_top_text("https://discord.com/channels/123/456/2"))
        switch.assert_awaited_once_with(123, 456, out, {100, 150, 900})
        self.assertTrue(all(msg.delete.await_count == 0 for msg in messages))

    async def test_reflow_does_not_replace_live_ids_when_staging_fails(self):
        raid = "The Fractured Archive"
        view = bot.PBLayoutView(timeout=None)
        view.add_item(bot.ui.TextDisplay("### 5-Man"))
        staged = SimpleNamespace(id=1000, delete=AsyncMock())
        channel = SimpleNamespace(id=456, send=AsyncMock(side_effect=[staged, RuntimeError("send failed")]))
        with (patch.object(bot, "public_tail_views", new=AsyncMock(return_value=[
                 ("Solo", view), ("5-Man", view),
             ])),
              patch.object(bot.store, "switch_public_board_tail", new=AsyncMock()) as switch):
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                await bot.publish_public_board_tail(
                    channel, 123, [raid], {raid: {"message": 100}}, [],
                    {"channel": 456, "message": 2},
                )
        switch.assert_not_awaited()
        staged.delete.assert_awaited_once()

    async def test_snapshot_overflow_keeps_blue_links_on_every_entry(self):
        proof = "https://example.com/" + "a" * 950
        leaders = {
            activity: [record(id=n * 4 + k, activity=activity, proof=proof)
                       for k in range(1, 4)]
            for n, activity in enumerate(bot.RAIDS)
        }
        recent = [record(id=20 + k, proof=proof) for k in range(3)]
        pages = bot.snapshot_page_plan(leaders, recent)
        self.assertGreater(len(pages), 1)
        for i, (_, view) in enumerate(pages):
            if i + 1 < len(pages):
                bot.add_board_page_link(view, pages[i + 1][0],
                                        "https://discord.com/channels/123/456/789")
            self.assertLessEqual(bot.pb_view_text_units(view), bot.PB_PUBLIC_TEXT_LIMIT)
        text = "\n".join(item.content for _, view in pages for item in view.walk_children()
                         if isinstance(item, bot.ui.TextDisplay))
        self.assertEqual(text.count(f"[Proof]({proof})"), 15)
        self.assertEqual(text.count("### RAID SNAPSHOT"), 1)
        self.assertEqual(text.count("### RECENT PBs"), 1)
        self.assertNotIn("[Recent PBs ↓]", text)
        self.assertNotIn(" · PB #", text)

    async def test_large_board_splits_menus_and_dispatches_real_selected_value(self):
        ref = "archive:" + "c" * 32 + ".png"
        activity = "Chambers of Xeric"
        variants = bot.RAIDS[activity]
        menus = [row.children[0] for row in bot.board_proof_rows([
            (variant, [record(id=5*n+rank+1, activity=activity, variant=variant, proof=ref)
                       for rank in range(5)])
            for n, variant in enumerate(variants)
        ])]
        self.assertEqual([len(menu.item.options) for menu in menus], [25, 15])
        original = menus[1]
        match = original.template.fullmatch(original.custom_id)
        restored = await bot.BoardProofSelect.from_custom_id(None, original.item, match)
        restored._refresh_state(None, {"values": ["26"]})
        with patch.object(bot, "show_record_proof", new=AsyncMock()) as show:
            await restored.callback("interaction")
            show.assert_awaited_once_with("interaction", 26)
