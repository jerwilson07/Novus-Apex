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

    def test_top10_uses_inline_trophies_and_muted_counts(self):
        points = [dict(rank=i, member_id=i, points=11-i, firsts=1, seconds=0, thirds=0)
                  for i in range(1, 11)]
        view = bot.Home(points, guild_id=123)
        components = list(view.walk_children())
        self.assertFalse(any(isinstance(item, bot.ui.Section) for item in components))
        standings = "\n".join(item.content for item in components
                              if isinstance(item, bot.ui.TextDisplay))
        for rank in range(1, 4):
            self.assertIn(f"### {EMOJIS[f'novuspb{rank}']} <@{rank}>", standings)
        for rank in range(4, 11):
            self.assertIn(f"**{rank}.** <@{rank}>", standings)
        counts = f"*{EMOJIS['novuspb1']} × 1 · {EMOJIS['novuspb2']} × 0 · {EMOJIS['novuspb3']} × 0*"
        insets = ["\u200b\u2003\u2003\u2002"] * 3 + ["\u200b\u2003\u2002\u2005"] * 6 + ["\u200b\u2003\u2003\u2005"]
        self.assertEqual(
            [line for line in standings.splitlines() if line.endswith(counts)],
            [f"-# {inset}{counts}" for inset in insets],
        )
        legend = f"-# *Scoring · {EMOJIS['novuspb1']} +5 pts · {EMOJIS['novuspb2']} +3 pts · {EMOJIS['novuspb3']} +1 pt*"
        self.assertTrue(standings.endswith(legend))
        children = view.children[0].children
        display_indices = [i for i, item in enumerate(children) if isinstance(item, bot.ui.TextDisplay)]
        _, podium_index, remaining_index = display_indices
        self.assertEqual(remaining_index, podium_index + 2)
        self.assertIsInstance(children[podium_index + 1], bot.ui.Separator)
        self.assertIn(f"### {EMOJIS['novuspb3']} <@3>", children[podium_index].content)
        self.assertTrue(children[remaining_index].content.startswith("**4.** <@4>"))
        self.assertLessEqual(len(children), 10)

    def test_missing_trophy_keeps_the_podium_order(self):
        points = [dict(rank=i, member_id=i, points=11-i, firsts=1, seconds=0, thirds=0)
                  for i in range(1, 4)]
        with patch.dict(bot.ACTIVITY_EMOJI_CACHE, {123: {"novuspb2": EMOJIS["novuspb2"]}}):
            lines = bot.top10_standings_text(points, 123).splitlines()
        self.assertTrue(lines[0].startswith("### #1 <@1>"))
        self.assertTrue(lines[2].startswith(f"### {EMOJIS['novuspb2']} <@2>"))
        self.assertTrue(lines[4].startswith("### #3 <@3>"))
        self.assertEqual(len(bot.top10_standings_parts(points, 123)), 1)

    def test_exact_ties_share_ranks_and_keep_cutoff_ties(self):
        def score(member_id, firsts):
            return dict(member_id=member_id, firsts=firsts, seconds=0,
                        thirds=0, points=firsts*5)
        entries = [score(i, 15-i) for i in range(1, 11)]
        entries += [score(20, 14), score(21, 6), score(22, 6), score(23, 5)]
        points = bot.rank_pb_points(entries)
        self.assertEqual([p["rank"] for p in points], [1, 1, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10])
        text = bot.top10_standings_text(points, 123)
        self.assertEqual(text.count(f"### {EMOJIS['novuspb1']} <@"), 2)
        self.assertEqual(text.count("**10.**"), 3)
        self.assertNotIn("<@23>", text)

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
            "<:novuspb1:1572157972702953563>",
            "<:novuspb2:1572155458423361637>",
            "<:novuspb3:1572155471606054982>",
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
        self.assertEqual(text.count(EMOJIS["novuspb1"]), sum(map(len, leaders.values())))
        self.assertFalse(any(isinstance(item, bot.ui.Button) for item in panel.walk_children()))
        self.assertEqual(text.splitlines().count(bot.snapshot_team_line(recent[0])),
                         sum(map(len, leaders.values())))
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
        older = "## <:novuspb1:1> NEW NOVUS #1\n## <:novuspb2:2> other"
        new = f"## {EMOJIS['novuspb1']} NEW NOVUS #1\n## <:novuspb2:2> other"
        self.assertEqual(bot.replace_first_trophy_heading(older, EMOJIS["novuspb1"]), new)
        self.assertEqual(bot.replace_first_trophy_heading(older.replace("<:novuspb1:1>", ":novuspb1:"), EMOJIS["novuspb1"]), new)
        self.assertEqual(bot.replace_first_trophy_heading(new, EMOJIS["novuspb1"]), new)

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
