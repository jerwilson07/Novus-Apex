import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from PIL import Image, ImageChops, ImageDraw
from podium_image import PLATES, _base_artwork, _foreground_mask, render_podium
from discord.components import _component_factory
from test_pb_ui import record


EMOJIS = {f"pb{i}": f"<:pb{i}:{123456789123456780+i}>" for i in range(1, 4)}


class PodiumTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(bot.ACTIVITY_EMOJI_CACHE, {123: EMOJIS}, clear=True).start()

    def test_top10_uses_inline_trophies_and_muted_counts(self):
        points = [dict(rank=i, member_id=i, points=11-i, firsts=1, seconds=0, thirds=0)
                  for i in range(1, 11)]
        view = bot.Home(points, guild_id=123)
        components = list(view.walk_children())
        self.assertFalse(any(isinstance(item, bot.ui.Section) for item in components))
        standings = "\n".join(item.content for item in components
                              if isinstance(item, bot.ui.TextDisplay))
        for rank in range(1, 4):
            self.assertIn(f"## {EMOJIS[f'pb{rank}']} <@{rank}>", standings)
        for rank in range(4, 11):
            self.assertIn(f"**{rank}.** <@{rank}>", standings)
        counts = f"*{EMOJIS['pb1']} × 1 · {EMOJIS['pb2']} × 0 · {EMOJIS['pb3']} × 0*"
        insets = ["\u200b\u2003\u2003\u2002"] * 3 + ["\u200b\u2003\u2002\u2005"] * 6 + ["\u200b\u2003\u2003\u2005"]
        self.assertEqual(
            [line for line in standings.splitlines() if line.endswith(counts)],
            [f"-# {inset}{counts}" for inset in insets],
        )
        legend = f"-# *Scoring · {EMOJIS['pb1']} +5 pts · {EMOJIS['pb2']} +3 pts · {EMOJIS['pb3']} +1 pt*"
        self.assertTrue(standings.endswith(legend))
        children = view.children[0].children
        display_indices = [i for i, item in enumerate(children) if isinstance(item, bot.ui.TextDisplay)]
        _, podium_index, remaining_index = display_indices
        self.assertEqual(remaining_index, podium_index + 2)
        self.assertIsInstance(children[podium_index + 1], bot.ui.Separator)
        self.assertIn(f"## {EMOJIS['pb3']} <@3>", children[podium_index].content)
        self.assertTrue(children[remaining_index].content.startswith("**4.** <@4>"))
        self.assertLessEqual(len(children), 10)

    def test_missing_trophy_keeps_the_podium_order(self):
        points = [dict(rank=i, member_id=i, points=11-i, firsts=1, seconds=0, thirds=0)
                  for i in range(1, 4)]
        with patch.dict(bot.ACTIVITY_EMOJI_CACHE, {123: {"pb2": EMOJIS["pb2"]}}):
            lines = bot.top10_standings_text(points, 123).splitlines()
        self.assertTrue(lines[0].startswith("## #1 <@1>"))
        self.assertTrue(lines[2].startswith(f"## {EMOJIS['pb2']} <@2>"))
        self.assertTrue(lines[4].startswith("## #3 <@3>"))
        self.assertEqual(len(bot.top10_standings_parts(points, 123)), 1)

    async def test_live_home_embeds_an_image_and_keeps_lower_rows_identical(self):
        guild_id = bot.NOVUS_GUILD_ID
        rows = [dict(rank=i, member_id=i, points=30-i, firsts=5, seconds=2, thirds=1)
                for i in range(1, 11)]
        names = {1: "Alpha", 2: "Bravo", 3: "Charlie"}
        with patch.object(bot, "podium_player_names", new=AsyncMock(return_value=names)):
            view = await bot.Home.for_points(guild_id, rows)
            only_podium = await bot.Home.for_points(guild_id, rows[:3])
        before = bot.Home(rows, guild_id=guild_id)
        self.assertEqual(view.standing_parts, before.standing_parts[1:])
        self.assertEqual(only_podium.standing_parts[0].strip(), before.standing_parts[-1].splitlines()[-1])
        panel = view.children[0]
        self.assertIsInstance(panel.children[3], bot.ui.MediaGallery)
        self.assertTrue(panel.children[3].items[0].media.url.endswith(bot.PODIUM_ANIMATED))
        self.assertIsNone(panel.children[3].items[0].description)
        self.assertNotIn("## <:pb1:", "\n".join(view.standing_parts))
        self.assertEqual(panel.children[-1].to_component_dict(), before.children[0].children[-1].to_component_dict())
        with Image.open(BytesIO(view.podium_image)) as preview:
            self.assertEqual((preview.format, preview.size), ("WEBP", (1674, 942)))
            # Match the banner's aspect so the gallery can fill the card width
            # before it encounters Discord's desktop image height cap.
            self.assertAlmostEqual(preview.width / preview.height, 16 / 9, places=2)
            self.assertEqual((preview.n_frames, preview.info["loop"]), (60, 0))
            first=preview.convert("RGB")
            preview.seek(28)
            middle=preview.convert("RGB")
            self.assertIsNotNone(ImageChops.difference(first,middle).getbbox())
            for box in ((608,545,1064,676),(160,650,590,774),(1088,693,1512,822)):
                self.assertIsNone(ImageChops.difference(first.crop(box),middle.crop(box)).getbbox())
            with Image.open(BytesIO(render_podium(rows[:3],names))) as static:
                diff = ImageChops.difference(first,static.convert("RGB"))
            protected = _foreground_mask().point(lambda alpha: 255 if alpha==255 else 0)
            self.assertIsNone(ImageChops.multiply(diff,protected.convert("RGB")).getbbox())
        self.assertIs(view.podium_image, only_podium.podium_image)
        old_image = SimpleNamespace(filename=bot.PODIUM_FILENAME)
        unrelated_attachment = SimpleNamespace(filename="other.png")
        message = SimpleNamespace(attachments=[old_image, unrelated_attachment])
        edit_files = view.edit_options(message)["attachments"]
        self.assertEqual(edit_files[0], unrelated_attachment)
        self.assertEqual(edit_files[1].filename, bot.PODIUM_ANIMATED)
        self.assertEqual(view.send_options()["file"].filename, bot.PODIUM_ANIMATED)
        self.assertEqual(before.edit_options(message)["attachments"], [unrelated_attachment])

    async def test_third_place_incumbent_stays_on_the_image_and_challenger_is_fourth(self):
        entries = [dict(member_id=i, points=p, firsts=5, seconds=2, thirds=1)
                   for i,p in enumerate((50, 45, 41, 41, 40), 1)]
        rows = bot.rank_pb_points(entries, holders={1: 1, 2: 2, 3: 4})
        self.assertEqual([(r["rank"], r["member_id"]) for r in rows],
                         [(1, 1), (2, 2), (3, 4), (4, 3), (5, 5)])
        with patch.object(bot, "podium_player_names", new=AsyncMock(
                return_value={1: "Alpha", 2: "Bravo", 4: "Incumbent"})) as names:
            view = await bot.Home.for_points(bot.NOVUS_GUILD_ID, rows)
        names.assert_awaited_once_with(bot.NOVUS_GUILD_ID, rows[:3])
        self.assertIsNotNone(view.podium_image)
        self.assertIn("**4.** <@3> · **41 pts**", view.standing_parts[0])

    async def test_podium_holders_are_bootstrapped_by_approval_order_and_survive_restart(self):
        with TemporaryDirectory() as tmp, patch.object(bot, "DBPATH", str(Path(tmp) / "pb.sqlite3")):
            db = bot.Store()
            await db.open()
            g = bot.NOVUS_GUILD_ID
            try:
                challenger = await db.add(g, 10, "Bosses", "Zulrah", "Standard", "New", "10",
                                          2000, "2026-11-17", "https://example.org/new")
                incumbent = await db.add(g, 20, "Bosses", "Vorkath", "Standard", "Holder", "20",
                                         2000, "2026-11-17", "https://example.org/old")
                # The challenger was submitted first, but their approval came later.
                await db.conn.executemany(
                    "UPDATE submissions SET status='approved',updated=? WHERE id=?",
                    [("2026-11-17T15:00:00+00:00", incumbent),
                     ("2026-11-17T15:01:00+00:00", challenger)],
                )
                await db.conn.commit()
                with patch.object(bot, "store", db):
                    rows = await bot.calculate_pb_points(g)
                self.assertEqual([(r["rank"], r["member_id"]) for r in rows], [(1, 20), (2, 10)])
            finally:
                await db.conn.close()

            db = bot.Store()
            await db.open()
            try:
                self.assertEqual(await db.podium_holders(g), {1: 20, 2: 10})
                better = await db.add(g, 10, "Bosses", "Vorkath", "Standard", "New", "10",
                                      1900, "2026-11-17", "https://example.org/better")
                self.assertTrue(await db.change(better, "pending", "approved", reviewer=1))
                self.assertEqual(await db.podium_holders(g), {1: 10, 2: 20})
                await db.clear_guild(g)
                self.assertEqual(await db.podium_holders(g), {})
            finally:
                await db.conn.close()

    def test_podium_cache_reuses_png_and_only_nameplate_text_changes(self):
        rows = [dict(rank=i, member_id=i, points=30-i, firsts=5, seconds=2, thirds=1)
                for i in range(1, 4)]
        names = {1: "Alpha", 2: "Bravo", 3: "Charlie"}
        before = render_podium(rows, names)
        self.assertIs(render_podium([dict(r) for r in rows], dict(names)), before)
        rows[0].update(points=100, seconds=12)
        rows[1].update(points=75, firsts=16, thirds=10)
        names[2] = "Charlie, new name"
        after = render_podium(rows, names)
        self.assertNotEqual(before, after)
        with Image.open(BytesIO(before)) as prior, Image.open(BytesIO(after)) as next_image:
            changes = ImageChops.difference(prior.convert("RGB"), next_image.convert("RGB"))
            self.assertIsNotNone(changes.getbbox())
            erase = ImageDraw.Draw(changes)
            _, anchors = _base_artwork()
            for (x, y, width, _), positions in zip(PLATES, anchors):
                erase.rectangle((x-width//2, y+38, x+width//2, y+110), fill="black")
                for tx, ty in positions:
                    erase.rectangle((tx, ty-30, tx+71, ty+4), fill="black")
            self.assertIsNone(changes.getbbox())

    async def test_clears_alt_only_on_bot_owned_podium_copies(self):
        rows = [dict(rank=i, member_id=i, points=30-i, firsts=5, seconds=2, thirds=1)
                for i in range(1, 11)]
        png = b"original podium image"
        view = bot.Home(rows, guild_id=bot.NOVUS_GUILD_ID, podium=png)
        image_url = f"https://cdn.discordapp.com/attachments/1/2/{bot.PODIUM_FILENAME}?ex=1"
        podium = bot.podium_gallery_tile(view)
        podium.media = image_url
        podium.description = "Historical counts"
        # A different gallery is not part of the cleanup.
        view.children[0].children[0].items[0].description = "Keep banner description"

        def message(v, author=30):
            return SimpleNamespace(
                components=[_component_factory(c) for c in v.to_components()],
                attachments=[], author=SimpleNamespace(id=author),
            )

        async def edit(**kwargs):
            self.assertEqual(kwargs["attachments"][0].fp.read(), png)
            edited = kwargs["view"]
            self.assertTrue(edited.is_finished())
            self.assertFalse(kwargs["allowed_mentions"].users)
            self.assertEqual(edited.children[0].children[0].items[0].description, "Keep banner description")
            return message(edited)

        old = message(view)
        other_author = message(view, author=20)
        old.edit = AsyncMock(side_effect=edit)
        other_author.edit = AsyncMock()
        cdn = AsyncMock(return_value=png)
        client = SimpleNamespace(user=SimpleNamespace(id=30), http=SimpleNamespace(get_from_cdn=cdn))
        with patch.object(bot, "bot", client):
            self.assertIsNone(await bot.clear_podium_badge(other_author))
            self.assertTrue(await bot.clear_podium_badge(old))
            self.assertIsNone(await bot.clear_podium_badge(message(old.edit.await_args.kwargs["view"])))
        cdn.assert_awaited_once_with(image_url)
        other_author.edit.assert_not_called()
        old.edit.assert_awaited_once()

    def test_exact_ties_below_podium_share_ranks_and_keep_cutoff_ties(self):
        def score(member_id, firsts):
            return dict(member_id=member_id, firsts=firsts, seconds=0,
                        thirds=0, points=firsts*5)
        entries = [score(i, 15-i) for i in range(1, 11)]
        entries += [score(20, 14), score(21, 6), score(22, 6), score(23, 5)]
        points = bot.rank_pb_points(entries, holders={1: 20})
        self.assertEqual([p["rank"] for p in points], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10])
        self.assertEqual([p["member_id"] for p in points[:2]], [20, 1])
        text = bot.top10_standings_text(points, 123)
        self.assertEqual(text.count(f"## {EMOJIS['pb1']} <@"), 1)
        self.assertEqual(text.count("**10.**"), 3)
        self.assertNotIn("<@23>", text)

        entries = [score(i, p) for i,p in enumerate((12,11,10,9,8,8,7,6,5,4),1)]
        self.assertEqual([p["rank"] for p in bot.rank_pb_points(entries)],
                         [1, 2, 3, 4, 5, 5, 7, 8, 9, 10])

    def test_boss_and_raid_use_all_custom_podium_icons(self):
        items = [record(id=i, member_ids=str(i), ms=1000*i) for i in range(1, 4)]
        blocks = [("Standard", items)]
        for view in (bot.BossLeaderboard("Araxxor", blocks),
                     bot.RaidLeaderboard("Chambers of Xeric", blocks)):
            text = str(view.to_components())
            for emoji in EMOJIS.values():
                self.assertIn(emoji, text)
            for fallback in ("🏆", "🥈", "🥉"):
                self.assertNotIn(fallback, text)

    def test_novus_trophy_ids_override_stale_emoji_cache(self):
        guild = bot.NOVUS_GUILD_ID
        expected = (
            "<:pb1:1572259759548272752>",
            "<:pb2:1572259827885940928>",
            "<:pb3:1572260053191491634>",
        )
        with patch.dict(bot.ACTIVITY_EMOJI_CACHE, {guild: EMOJIS}):
            self.assertEqual(tuple(bot.podium_icon(rank, guild) for rank in (1, 2, 3)), expected)
            rows = [record(id=i, guild=guild, category="Bosses", activity="Vorkath",
                           variant="Standard", member_ids=str(i), ms=1000*i) for i in (1, 2, 3)]
            view = bot.BossLeaderboard("Vorkath", [("Standard", rows)])
            content = str(view.to_components())
            self.assertTrue(all(emoji in content for emoji in expected))
            self.assertTrue(all(emoji not in content for emoji in EMOJIS.values()))

    def test_snapshot_keeps_all_raid_champions_and_rosters_with_long_links(self):
        leaders = {
            activity: [record(id=10*j+k+1, guild=123, activity=activity,
                              ms=1000*(k+1), proof="https://example.org/"+"a"*390)
                       for k in range(3)]
            for j, activity in enumerate(bot.RAIDS)
        }
        recent = [rows[0] for rows in leaders.values()][:3]
        panel = bot.fit_snapshot_panel(leaders, recent)
        parts = [item.content for item in panel.walk_children()
                 if isinstance(item, bot.ui.TextDisplay)]
        text = "\n".join(parts)
        # Newlines for asserting across components are not sent to Discord.
        self.assertLessEqual(sum(len(part.encode("utf-16-le")) // 2 for part in parts), 4000)
        self.assertEqual(text.count(EMOJIS["pb1"]), sum(map(len, leaders.values())))
        self.assertFalse(any(isinstance(item, bot.ui.Button) for item in panel.walk_children()))
        self.assertEqual(text.splitlines().count(bot.snapshot_team_line(recent[0])),
                         sum(map(len, leaders.values())))
        self.assertNotIn("🏆", text)
        self.assertIn("03.00", text)

    def test_new_announcement_and_legacy_heading(self):
        r = record(guild=123, proof=f"archive:{1:032x}.png")
        card = bot.FirstPlaceAnnouncementCard(r)
        text = str(card.to_components())
        self.assertIn(f"{EMOJIS['pb1']} NEW NOVUS #1", text)
        self.assertIn("pb:proof:view:1", text)
        icons = {rank: EMOJIS[f"pb{rank}"] for rank in range(1, 4)}
        original = "## 🏆 NEW NOVUS #1\n-# PB TIME\n## 🏆 other"
        expected = f"## {icons[1]} NEW NOVUS #1\n-# PB TIME\n## 🏆 other"
        self.assertEqual(bot.replace_legacy_pb_trophies(original, icons), expected)
        older = "## <:novuspb1:1> NEW NOVUS #1\n-# :novuspb2: · <a:novuspb3:3>"
        new = f"## {icons[1]} NEW NOVUS #1\n-# {icons[2]} · {icons[3]}"
        self.assertEqual(bot.replace_legacy_pb_trophies(older, icons), new)
        self.assertEqual(bot.replace_legacy_pb_trophies(new, icons), new)

    async def test_existing_announcement_edit_keeps_proof_and_controls(self):
        row = record(guild=123, proof=f"archive:{1:032x}.png")

        def message(view, author=21):
            return SimpleNamespace(
                id=42, content="", author=SimpleNamespace(id=author),
                components=[_component_factory(c) for c in view.to_components()],
            )

        with patch.dict(bot.ACTIVITY_EMOJI_CACHE, clear=True):
            old = message(bot.FirstPlaceAnnouncementCard(row, leaderboard_url="https://example.org/board"))
        previous_icons = {f"pb{i}": f"<:novuspb{i}:{123456789123456770+i}>"
                          for i in range(1, 4)}
        points = [dict(rank=i, member_id=i, points=11-i, firsts=1, seconds=0, thirds=0)
                  for i in range(1, 4)]
        with patch.dict(bot.ACTIVITY_EMOJI_CACHE, {123: previous_icons}):
            snapshot = message(bot.Home(points, guild_id=123))
            other = message(bot.FirstPlaceAnnouncementCard(row), author=25)
        snapshot.edit = AsyncMock(side_effect=lambda **kwargs: message(kwargs["view"]))
        old.edit = AsyncMock(side_effect=lambda **kwargs: message(kwargs["view"]))
        other.edit = AsyncMock()
        async def history(**kwargs):
            for msg in (old, snapshot, other):
                yield msg

        channel = SimpleNamespace(guild=SimpleNamespace(id=123), history=history)
        service = SimpleNamespace(user=SimpleNamespace(id=21), get_channel=lambda _: channel)
        conn = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
        with (patch.object(bot, "bot", service), patch.object(bot, "GENERAL_CHAT_ID", 17),
              patch.object(bot.store, "one", new=AsyncMock(return_value=None)),
              patch.object(bot.store, "conn", conn),
              patch.object(bot.asyncio, "sleep", new=AsyncMock())):
            await bot.refresh_public_post_trophy_icons(123)
        args = old.edit.await_args.kwargs
        body = str(args["view"].to_components())
        self.assertIn(EMOJIS["pb1"], body)
        self.assertIn("pb:proof:view:1", body)
        self.assertIn("https://example.org/board", body)
        self.assertNotIn("attachments", args)
        snapshot_body = str(snapshot.edit.await_args.kwargs["view"].to_components())
        self.assertTrue(all(emoji in snapshot_body for emoji in EMOJIS.values()))
        self.assertNotIn("novuspb", snapshot_body)
        other.edit.assert_not_called()
