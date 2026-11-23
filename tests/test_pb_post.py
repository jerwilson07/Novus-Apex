"""Regression checks for /pbpost displaying the same home panel as #pb-leaderboard."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class PBPostTests(unittest.IsolatedAsyncioTestCase):
    def test_slash_command_registry(self):
        self.assertIsNone(bot.bot.tree.get_command("checkemojis"))
        self.assertIsNotNone(bot.bot.tree.get_command("pbpost"))
        self.assertIsNotNone(bot.bot.tree.get_command("pbpanel"))

    def test_home_panel_includes_banner_top_ten_and_three_buttons(self):
        points = [
            {
                "rank": 1, "member_id": 123, "points": 20,
                "firsts": 2, "seconds": 3, "thirds": 1,
            },
            {
                "rank": 2, "member_id": 456, "points": 8,
                "firsts": 1, "seconds": 1, "thirds": 0,
            },
        ]
        view = bot.Home(points)
        self.assertEqual(len(view.children), 1)
        panel = view.children[0]
        self.assertEqual(int(panel.accent_colour), bot.GOLD)
        self.assertIsInstance(panel.children[0], bot.ui.MediaGallery)
        text = "\n".join(
            child.content for child in panel.children
            if isinstance(child, bot.ui.TextDisplay)
        )
        self.assertEqual(text.splitlines().count(bot.PB_TOP10_TITLE), 1)
        self.assertNotIn("### TOP 10", text)
        self.assertNotIn("PERSONAL BESTS", text)
        self.assertIn("#1 <@123> · **20 pts**", text)
        self.assertIn("#2 <@456> · **8 pts**", text)
        actions = [
            child for child in panel.children
            if isinstance(child, bot.ui.ActionRow)
        ]
        self.assertEqual(len(actions), 1)
        self.assertEqual(
            [button.label for button in actions[0].children],
            ["Submit Raid PB", "Submit Boss PB", "PB Rules", "My PBs"],
        )
        self.assertLessEqual(len(panel.children), 10)

    async def test_pbpost_sends_overall_then_raids_and_preserves_main_board(self):
        overall_message = SimpleNamespace(
            id=100,
            jump_url="https://discord.com/channels/123/456/100",
            delete=AsyncMock(),
        )
        raid_message = SimpleNamespace(
            id=101,
            jump_url="https://discord.com/channels/123/456/101",
            delete=AsyncMock(),
        )
        channel = SimpleNamespace(send=AsyncMock(side_effect=[overall_message,raid_message]))
        points = [
            {"rank":1, "member_id":123, "points":5,
             "firsts":1, "seconds":0, "thirds":0}
        ]
        raid_points = [
            {"rank":1, "member_id":456, "points":3,
             "firsts":0, "seconds":1, "thirds":0}
        ]
        expected_home=bot.Home(points,guild_id=123)
        expected_raid=bot.RaidTop10(raid_points,guild_id=123)
        with (
            patch.object(bot, "calculate_pb_points", new_callable=AsyncMock, return_value=points) as overall_calc,
            patch.object(bot, "calculate_raid_pb_points", new_callable=AsyncMock, return_value=raid_points) as raid_calc,
            patch.object(bot.store, "set_panel", new_callable=AsyncMock) as set_panel,
            patch.object(bot.store, "set_raid_panel", new_callable=AsyncMock) as set_raid,
            patch.object(bot.store, "set_boss_board", new_callable=AsyncMock) as set_boss_board,
            patch.object(bot.store, "clear_boss_boards", new_callable=AsyncMock) as clear_boards,
        ):
            main, raids = await bot.post_current_pb_board(channel, 123)

        self.assertIs(main, overall_message)
        self.assertIs(raids, raid_message)
        overall_calc.assert_awaited_once_with(123)
        raid_calc.assert_awaited_once_with(123)
        self.assertEqual(channel.send.await_count,2)
        sent_views=[call.kwargs["view"] for call in channel.send.await_args_list]
        self.assertEqual(sent_views[0].to_components(),expected_home.to_components())
        self.assertEqual(sent_views[1].to_components(),expected_raid.to_components())
        self.assertEqual(str(sent_views[0].to_components()).count(bot.PB_TOP10_TITLE),1)
        self.assertEqual(str(sent_views[1].to_components()).count(bot.PB_RAIDS_TITLE),1)
        self.assertNotIn(bot.PB_RAIDS_TITLE,str(sent_views[0].to_components()))
        self.assertNotIn(bot.PB_TOP10_TITLE,str(sent_views[1].to_components()))
        for call in channel.send.await_args_list:
            mentions=call.kwargs["allowed_mentions"]
            self.assertFalse(mentions.everyone)
            self.assertFalse(mentions.users)
            self.assertFalse(mentions.roles)
        set_panel.assert_not_awaited()
        set_raid.assert_not_awaited()
        set_boss_board.assert_not_awaited()
        clear_boards.assert_not_awaited()

    async def test_failed_first_send_does_not_touch_existing_board(self):
        channel=SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("send failed")))
        with (
            patch.object(bot.Home,"make",new_callable=AsyncMock,return_value=bot.Home()),
            patch.object(bot.store,"clear_boss_boards",new_callable=AsyncMock) as clear_boards,
        ):
            with self.assertRaises(RuntimeError):
                await bot.post_current_pb_board(channel,123)
        channel.send.assert_awaited_once()
        clear_boards.assert_not_awaited()

    async def test_failed_raids_send_removes_partial_overall_snapshot(self):
        overall_message=SimpleNamespace(id=100,delete=AsyncMock())
        channel=SimpleNamespace(send=AsyncMock(side_effect=[
            overall_message,RuntimeError("raid send failed")
        ]))
        with (
            patch.object(bot.Home,"make",new_callable=AsyncMock,return_value=bot.Home()),
            patch.object(bot.RaidTop10,"make",new_callable=AsyncMock,return_value=bot.RaidTop10()),
            patch.object(bot.store,"set_panel",new_callable=AsyncMock) as set_panel,
            patch.object(bot.store,"set_raid_panel",new_callable=AsyncMock) as set_raid,
        ):
            with self.assertRaises(RuntimeError):
                await bot.post_current_pb_board(channel,123)
        self.assertEqual(channel.send.await_count,2)
        overall_message.delete.assert_awaited_once_with()
        set_panel.assert_not_awaited()
        set_raid.assert_not_awaited()

if __name__ == "__main__":
    unittest.main()
