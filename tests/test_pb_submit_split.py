"""Regression tests for the raid/boss-specific PB submission controls."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class SubmissionSplitTests(unittest.IsolatedAsyncioTestCase):
    def test_home_has_four_buttons_with_separate_submission_paths(self):
        home = bot.Home()
        buttons = [x.label for item in home.children[0].children
                   if isinstance(item, bot.ui.ActionRow) for x in item.children]
        self.assertEqual(buttons, [
            "Submit Raid PB", "Submit Boss PB", "PB Rules", "My PBs"
        ])

    def test_raid_picker_has_only_four_raid_buttons(self):
        picker = bot.RaidSubmissionPicker()
        buttons = [x.label for x in picker.walk_children()
                   if isinstance(x, bot.ui.Button)]
        self.assertEqual(buttons, list(bot.RAID_LABELS.values()))

    def test_each_raid_modal_has_only_formats_for_its_own_raid(self):
        for activity, formats in bot.RAIDS.items():
            with self.subTest(activity=activity):
                modal = bot.RaidSubmitModal(activity)
                self.assertEqual(
                    [x.value for x in modal.format_select.options],
                    formats,
                )
                self.assertEqual(len(modal.children), 3)
                self.assertEqual(modal.details_input.placeholder, "30.60 or 14:32.60")
                self.assertFalse(hasattr(modal, "boss_select"))

    def test_boss_modal_excludes_raid_format_combinations(self):
        modal = bot.BossSubmitModal()
        labels = {x.value for x in modal.format_select.options}
        self.assertEqual(labels, {
            "__none__", "Normal", "Awakened", "Regular Gauntlet",
            "Corrupted Gauntlet", "Duo", "Trio", "5-Man",
        })
        self.assertEqual(len(modal.children), 4)
        self.assertLessEqual(len(modal.boss_select.options), 25)
        self.assertNotIn("Hard Mode · Trio", labels)
        self.assertNotIn("Challenge Mode · Solo", labels)
        self.assertNotIn("Expert · 8-Man", labels)

    async def test_boss_modal_standard_and_special_mode_validation(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock())
        )
        with patch.object(bot, "submit_modal_details", new_callable=AsyncMock) as send:
            standard = bot.BossSubmitModal()
            standard.boss_select._values = ["Vorkath"]
            standard.format_select._values = []
            await standard.on_submit(interaction)
            self.assertEqual(send.await_args.args[1:4], ("Bosses", "Vorkath", "Standard"))

            send.reset_mock()
            gauntlet = bot.BossSubmitModal()
            gauntlet.boss_select._values = ["Gauntlet"]
            gauntlet.format_select._values = ["Corrupted Gauntlet"]
            await gauntlet.on_submit(interaction)
            self.assertEqual(send.await_args.args[1:4], ("Bosses", "Corrupted Gauntlet", "Standard"))

            send.reset_mock()
            dt2 = bot.BossSubmitModal()
            dt2.boss_select._values = ["Vardorvis"]
            dt2.format_select._values = []
            await dt2.on_submit(interaction)
            send.assert_not_awaited()
            interaction.response.send_message.assert_awaited()
            self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_raid_modal_preserves_categorized_submission(self):
        i = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        modal = bot.RaidSubmitModal("Theatre of Blood")
        modal.format_select._values = ["Hard Mode · Trio"]
        with patch.object(bot, "submit_modal_details", new_callable=AsyncMock) as send:
            await modal.on_submit(i)
        self.assertEqual(send.await_args.args[1:4],
                         ("Raids", "Theatre of Blood", "Hard Mode · Trio"))


if __name__ == "__main__":
    unittest.main()
