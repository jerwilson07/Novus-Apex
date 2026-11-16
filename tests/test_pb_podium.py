import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from discord.components import _component_factory
from test_pb_ui import record


EMOJIS = {f"novuspb{i}": f"<:novuspb{i}:{123456789123456780+i}>" for i in range(1, 4)}


class PodiumTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(bot.ACTIVITY_EMOJI_CACHE, {123: EMOJIS}, clear=True).start()

    def test_top10_uses_large_trophy_art_for_only_first_three(self):
        points = [dict(member_id=i, points=11-i, firsts=1, seconds=0, thirds=0)
                  for i in range(1, 11)]
        view = bot.Home(points, guild_id=123)
        sections = [item for item in view.walk_children() if isinstance(item, bot.ui.Section)]
        self.assertEqual(len(sections), 3)
        for rank, section in enumerate(sections, 1):
            self.assertIsInstance(section.accessory, bot.ui.Thumbnail)
            self.assertEqual(section.accessory.media.url,
                             f"https://cdn.discordapp.com/emojis/{123456789123456780+rank}.png?size=128")
            self.assertTrue(section.children[0].content.startswith(f"### {rank}. <@{rank}>"))
        standings = "\n".join(item.content for item in view.walk_children()
                              if isinstance(item, bot.ui.TextDisplay))
        for rank in range(4, 11):
            self.assertIn(f"**{rank}.** <@{rank}>", standings)
        self.assertEqual(standings.count("*(1 first · 0 second · 0 third)*"), 10)
        self.assertTrue(standings.endswith("-# *1st = 5 pts · 2nd = 3 pts · 3rd = 1 pt*"))
        self.assertLessEqual(len(view.children[0].children), 10)

    def test_missing_trophy_keeps_the_podium_order(self):
        points = [dict(member_id=i, points=11-i, firsts=1, seconds=0, thirds=0)
                  for i in range(1, 4)]
        with patch.dict(bot.ACTIVITY_EMOJI_CACHE, {123: {"novuspb2": EMOJIS["novuspb2"]}}):
            view = bot.Home(points, guild_id=123)
        lines = [item.content for item in view.walk_children()
                 if isinstance(item, bot.ui.TextDisplay) and "<@" in item.content]
        self.assertTrue(lines[0].startswith("### #1 <@1>"))
        self.assertTrue(lines[1].startswith("### 2. <@2>"))
        self.assertTrue(lines[2].startswith("### 🥉 <@3>"))
        self.assertIsNone(bot.podium_trophy_url(4, 123))

    def test_boss_and_raid_champions_use_custom_icon_without_changing_lower_ranks(self):
        items = [record(id=i, member_ids=str(i), ms=1000*i) for i in range(1, 4)]
        blocks = [("Standard", items)]
        boss = str(bot.BossLeaderboard("Araxxor", blocks).to_components())
        raid = str(bot.RaidLeaderboard("Chambers of Xeric", blocks).to_components())
        self.assertIn(EMOJIS["novuspb1"], boss)
        self.assertNotIn(EMOJIS["novuspb2"], boss)
        self.assertNotIn(EMOJIS["novuspb3"], boss)
        self.assertIn("🥈", boss)
        self.assertIn("🥉", boss)
        self.assertNotIn("🏆", raid)
        self.assertIn(EMOJIS["novuspb1"], raid)

    def test_novus_boss_does_not_fall_back_to_gold_trophy(self):
        guild = bot.NOVUS_GUILD_ID
        row = record(guild=guild, category="Bosses", activity="Vorkath", variant="Standard")
        view = bot.BossLeaderboard("Vorkath", [("Standard", [row])])
        self.assertNotIn("🏆", str(view.to_components()))
        with patch.dict(bot.ACTIVITY_EMOJI_CACHE, {guild: EMOJIS}):
            view = bot.BossLeaderboard("Vorkath", [("Standard", [row])])
            self.assertIn(EMOJIS["novuspb1"], str(view.to_components()))

    def test_snapshot_keeps_all_raid_champions_and_badges_with_long_links(self):
        leaders = {
            activity: [record(id=10*j+k+1, guild=123, activity=activity,
                              ms=1000*(k+1), proof="https://example.org/"+"a"*390)
                       for k in range(3)]
            for j, activity in enumerate(bot.RAIDS)
        }
        recent = [rows[0] for rows in leaders.values()][:3]
        panel = bot.fit_snapshot_panel(leaders, recent)
        text = "\n".join(item.content for item in panel.walk_children()
                         if isinstance(item, bot.ui.TextDisplay))
        self.assertLessEqual(len(text.encode("utf-16-le"))//2, 4000)
        self.assertEqual(text.count(EMOJIS["novuspb1"]), len(bot.RAIDS))
        self.assertEqual(sum(isinstance(item, bot.ui.Section) and item.accessory.label=="NOVUS #1"
                             for item in panel.walk_children()), len(bot.RAIDS))
        self.assertNotIn("🏆", text)
        self.assertIn("0:03.00", text)

    def test_new_announcement_and_legacy_heading(self):
        r = record(guild=123, proof=f"archive:{1:032x}.png")
        card = bot.FirstPlaceAnnouncementCard(r)
        text = str(card.to_components())
        self.assertIn(f"{EMOJIS['novuspb1']} NEW NOVUS #1", text)
        self.assertIn("pb:proof:view:1", text)
        original = "## 🏆 NEW NOVUS #1\n-# PB TIME\n## 🏆 other"
        expected = f"## {EMOJIS['novuspb1']} NEW NOVUS #1\n-# PB TIME\n## 🏆 other"
        self.assertEqual(bot.replace_first_trophy_heading(original, EMOJIS["novuspb1"]), expected)

    async def test_existing_announcement_edit_keeps_proof_and_controls(self):
        row = record(guild=123, proof=f"archive:{1:032x}.png")

        def message(view, author=21):
            return SimpleNamespace(
                id=42, content="", author=SimpleNamespace(id=author),
                components=[_component_factory(c) for c in view.to_components()],
            )

        with patch.dict(bot.ACTIVITY_EMOJI_CACHE, clear=True):
            old = message(bot.FirstPlaceAnnouncementCard(row, leaderboard_url="https://example.org/board"))
            other = message(bot.FirstPlaceAnnouncementCard(row), author=25)
        old.edit = AsyncMock(side_effect=lambda **kwargs: message(kwargs["view"]))
        other.edit = AsyncMock()
        async def history(**kwargs):
            for msg in (old, other):
                yield msg

        channel = SimpleNamespace(guild=SimpleNamespace(id=123), history=history)
        service = SimpleNamespace(user=SimpleNamespace(id=21), get_channel=lambda _: channel)
        conn = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
        with (patch.object(bot, "bot", service), patch.object(bot, "GENERAL_CHAT_ID", 17),
              patch.object(bot.store, "one", new=AsyncMock(return_value=None)),
              patch.object(bot.store, "conn", conn),
              patch.object(bot.asyncio, "sleep", new=AsyncMock())):
            await bot.refresh_first_announcement_trophy_icons(123)
        args = old.edit.await_args.kwargs
        body = str(args["view"].to_components())
        self.assertIn(EMOJIS["novuspb1"], body)
        self.assertIn("pb:proof:view:1", body)
        self.assertIn("https://example.org/board", body)
        self.assertNotIn("attachments", args)
        other.edit.assert_not_called()
