"""Regression tests for the separate raid-only Novus top ten post."""
import unittest
from unittest.mock import AsyncMock, patch

import bot
from test_pb_ui import record


class RaidsTop10Tests(unittest.IsolatedAsyncioTestCase):
    def test_scoring_counts_only_approved_raid_top_threes_across_all_formats(self):
        rows = [
            record(id=1, status="approved", category="Raids", activity="The Fractured Archive",
                   variant="Solo", member_ids="101", ms=10000),
            record(id=2, status="approved", category="Raids", activity="Chambers of Xeric",
                   variant="Challenge Mode · Solo", member_ids="101", ms=11000),
            record(id=3, status="approved", category="Raids", activity="Tombs of Amascut",
                   variant="Expert · Solo", member_ids="101", ms=12000),
            record(id=4, status="approved", category="Raids", activity="Theatre of Blood",
                   variant="Hard Mode · Trio", member_ids="202,303,404", ms=14000),
            record(id=5, status="approved", category="Raids", activity="Chambers of Xeric",
                   variant="Challenge Mode · Solo", member_ids="202", ms=15000),
            # A boss first-place is worth five overall points but zero raid points.
            record(id=6, status="approved", category="Bosses", activity="Vorkath",
                   variant="Standard", member_ids="202", ms=8000),
        ]
        ranked = bot.raid_pb_points(rows)
        scores = {row["member_id"]: row for row in ranked}
        self.assertEqual((scores[101]["points"], scores[101]["firsts"]), (15, 3))
        self.assertEqual((scores[202]["points"], scores[202]["firsts"], scores[202]["seconds"]), (8, 1, 1))
        self.assertEqual(scores[303]["points"], 5)
        self.assertNotIn("boss", str(scores).lower())

    def test_render_is_its_own_post_with_no_banner_or_buttons(self):
        points = [dict(rank=i, member_id=i, points=20-i, firsts=1, seconds=0, thirds=0)
                  for i in range(1, 11)]
        view = bot.RaidTop10(points, guild_id=123)
        self.assertEqual(len(view.children), 1)
        children = list(view.walk_children())
        text = "\n".join(x.content for x in children if isinstance(x, bot.ui.TextDisplay))
        self.assertIn(bot.PB_RAIDS_TITLE, text)
        self.assertIn("**4.** <@4>", text)
        self.assertIn("**10.** <@10>", text)
        self.assertNotIn(bot.PB_TOP10_TITLE, text)
        self.assertFalse(any(isinstance(x, bot.ui.MediaGallery) for x in children))
        self.assertFalse(any(isinstance(x, bot.ui.ActionRow) for x in children))
        self.assertNotIn(bot.PB_RAIDS_TITLE,
                         str(bot.Home(points, guild_id=bot.NOVUS_GUILD_ID).to_components()))

    async def test_live_raid_podium_replaces_top_three_text_only(self):
        points = [dict(rank=i, member_id=i, points=20-i, firsts=1, seconds=0, thirds=0)
                  for i in range(1, 11)]
        webp = b"RIFF" + b"0000" + b"WEBP" + b"testbytes"
        with (patch.object(bot, "podium_player_names", new=AsyncMock(return_value={
                1: "Alpha", 2: "Bravo", 3: "Charlie"})),
              patch.object(bot, "render_podium_animation", return_value=webp)):
            view = await bot.RaidTop10.for_points(bot.NOVUS_GUILD_ID, points)
        self.assertEqual(view.podium_filename, bot.PODIUM_ANIMATED)
        self.assertTrue(any(isinstance(x, bot.ui.MediaGallery) for x in view.walk_children()))
        text = "\n".join(x.content for x in view.walk_children()
                         if isinstance(x, bot.ui.TextDisplay))
        self.assertNotIn("<@1>", text)
        self.assertIn("**4.** <@4>", text)
        self.assertIn("**10.** <@10>", text)


if __name__ == "__main__":
    unittest.main()
