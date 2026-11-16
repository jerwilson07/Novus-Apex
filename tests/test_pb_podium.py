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

    def test_top10_uses_custom_emojis_for_only_first_three(self):
        points = [dict(member_id=i, points=11-i, firsts=1, seconds=0, thirds=0)
                  for i in range(1, 11)]
        content = str(bot.Home(points, guild_id=123).to_components())
        for emoji in EMOJIS.values():
            self.assertEqual(content.count(emoji), 1)
        for i in range(4, 11):
            self.assertIn(f"**{i}.** <@{i}>", content)
        self.assertEqual(bot.podium_icon(1, 999), "🏆")

    def test_only_top_three_are_larger_and_scoring_follows_the_last_entry(self):
        points = [dict(member_id=i, points=11-i, firsts=1, seconds=0, thirds=0)
                  for i in range(1, 11)]
        view = bot.Home(points, guild_id=123)
        standings = next(item.content for item in view.walk_children()
                         if isinstance(item, bot.ui.TextDisplay) and " · **" in item.content)
        lines = standings.splitlines()
        for i in range(1, 4):
            self.assertTrue(lines[i-1].startswith(f"### {EMOJIS[f'novuspb{i}']} <@{i}>"))
        for i in range(4, 11):
            self.assertTrue(lines[i-1].startswith(f"**{i}.** <@{i}>"))
        self.assertEqual(lines[10:], ["", "-# 1st = 5 pts · 2nd = 3 pts · 3rd = 1 pt"])

    def test_boss_champions_use_custom_icon_but_lower_and_raids_unchanged(self):
        items = [record(id=i, member_ids=str(i), ms=1000*i) for i in range(1, 4)]
        blocks = [("Standard", items)]
        boss = str(bot.BossLeaderboard("Araxxor", blocks).to_components())
        raid = str(bot.RaidLeaderboard("Chambers of Xeric", blocks).to_components())
        self.assertIn(EMOJIS["novuspb1"], boss)
        self.assertNotIn(EMOJIS["novuspb2"], boss)
        self.assertNotIn(EMOJIS["novuspb3"], boss)
        self.assertIn("🥈", boss)
        self.assertIn("🥉", boss)
        self.assertIn("🏆", raid)
        self.assertNotIn(EMOJIS["novuspb1"], raid)

    def test_novus_boss_does_not_fall_back_to_gold_trophy(self):
        guild = bot.NOVUS_GUILD_ID
        row = record(guild=guild, category="Bosses", activity="Vorkath", variant="Standard")
        view = bot.BossLeaderboard("Vorkath", [("Standard", [row])])
        self.assertNotIn("🏆", str(view.to_components()))
        with patch.dict(bot.ACTIVITY_EMOJI_CACHE, {guild: EMOJIS}):
            view = bot.BossLeaderboard("Vorkath", [("Standard", [row])])
            self.assertIn(EMOJIS["novuspb1"], str(view.to_components()))

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
