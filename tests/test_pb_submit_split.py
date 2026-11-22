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

    def test_boss_picker_covers_every_boss_including_both_gauntlets(self):
        options = {
            group: [option.value for option in bot.boss_submission_options(group)]
            for group in bot.BOSS_SUBMISSION_GROUPS
        }
        combined = [boss for group in options.values() for boss in group]
        self.assertEqual(set(combined), set(bot.BOSSES))
        self.assertEqual(len(combined), len(set(combined)))
        self.assertTrue(all(len(group) <= 25 for group in options.values()))
        self.assertIn("Gauntlet", options["Bosses"])
        self.assertIn("Corrupted Gauntlet", options["Bosses"])
        self.assertNotIn("Gauntlet / Corrupted Gauntlet", combined)
        picker = bot.BossSubmissionPicker()
        self.assertEqual(
            len([child for child in picker.walk_children()
                 if isinstance(child, bot.ui.Button)]),
            len(bot.BOSS_SUBMISSION_GROUPS)
        )

    def test_boss_modal_has_only_three_modes_and_separate_team_sizes(self):
        modal = bot.BossSubmitModal()
        self.assertEqual(
            [(option.label, option.value) for option in modal.format_select.options],
            [
                ("Standard (no mode)", "__none__"),
                ("Normal", "Normal"),
                ("Awakened", "Awakened"),
            ]
        )
        self.assertEqual(
            [option.value for option in modal.team_size_select.options],
            ["Duo", "Trio", "5-Man"]
        )
        self.assertEqual(len(modal.children), 5)
        self.assertEqual(
            {option.value for option in modal.boss_select.options},
            set(bot.BOSS_SUBMISSION_GROUPS["Bosses"])
        )
        for mode in ("Regular Gauntlet", "Corrupted Gauntlet", "Hard Mode · Trio",
                     "Challenge Mode · Solo", "Expert · 8-Man"):
            self.assertNotIn(mode, [option.value for option in modal.format_select.options])

    async def test_boss_modal_standard_and_special_mode_validation(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock())
        )
        with patch.object(bot, "submit_modal_details", new_callable=AsyncMock) as send:
            standard = bot.BossSubmitModal()
            standard.boss_select._values = ["Vorkath"]
            standard.format_select._values = []
            standard.team_size_select._values = []
            await standard.on_submit(interaction)
            self.assertEqual(send.await_args.args[1:4], ("Bosses", "Vorkath", "Standard"))

            for boss in ("Gauntlet", "Corrupted Gauntlet"):
                send.reset_mock()
                gauntlet = bot.BossSubmitModal()
                gauntlet.boss_select._values = [boss]
                gauntlet.format_select._values = []
                gauntlet.team_size_select._values = []
                await gauntlet.on_submit(interaction)
                self.assertEqual(send.await_args.args[1:4], ("Bosses", boss, "Standard"))

            send.reset_mock()
            dt2 = bot.BossSubmitModal("DT2 Bosses")
            dt2.boss_select._values = ["Vardorvis"]
            dt2.format_select._values = ["Awakened"]
            dt2.team_size_select._values = []
            await dt2.on_submit(interaction)
            self.assertEqual(send.await_args.args[1:4], ("Bosses", "Vardorvis", "Awakened"))

            send.reset_mock()
            nex = bot.BossSubmitModal()
            nex.boss_select._values = ["Nex"]
            nex.format_select._values = []
            nex.team_size_select._values = ["Trio"]
            await nex.on_submit(interaction)
            self.assertEqual(send.await_args.args[1:4], ("Bosses", "Nex", "Trio"))

            send.reset_mock()
            huey = bot.BossSubmitModal()
            huey.boss_select._values = ["The Hueycoatl"]
            huey.format_select._values = []
            huey.team_size_select._values = ["5-Man"]
            await huey.on_submit(interaction)
            self.assertEqual(send.await_args.args[1:4], ("Bosses", "The Hueycoatl", "5-Man"))

            send.reset_mock()
            dt2.format_select._values = []
            await dt2.on_submit(interaction)
            send.assert_not_awaited()
            interaction.response.send_message.assert_awaited()
            self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

            interaction.response.send_message.reset_mock()
            nex.team_size_select._values = []
            await nex.on_submit(interaction)
            send.assert_not_awaited()
            interaction.response.send_message.assert_awaited()

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
