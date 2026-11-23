"""Regression checks for direct raid/boss dropdown modals and optional proof."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class SubmissionSplitTests(unittest.IsolatedAsyncioTestCase):
    def test_home_buttons_present_two_category_actions(self):
        view=bot.Home()
        labels=[button.label for row in view.children[0].children
                if isinstance(row,bot.ui.ActionRow) for button in row.children]
        self.assertEqual(labels,["Submit Raid PB","Submit Boss PB","PB Rules","My PBs"])

    async def test_home_buttons_open_modals_directly(self):
        view=bot.Home()
        row=next(row for row in view.children[0].children
                 if isinstance(row,bot.ui.ActionRow))
        response=SimpleNamespace(send_modal=AsyncMock(),send_message=AsyncMock())
        i=SimpleNamespace(response=response)
        await row.children[0].callback(i)
        self.assertIsInstance(response.send_modal.await_args.args[0],bot.AllRaidSubmitModal)
        await row.children[1].callback(i)
        self.assertIsInstance(response.send_modal.await_args.args[0],bot.BossSubmitModal)
        response.send_message.assert_not_awaited()

    def test_raid_modal_has_every_raid_and_variant_with_optional_proof(self):
        modal=bot.AllRaidSubmitModal()
        expected={(activity,variant) for activity,variants in bot.RAIDS.items()
                  for variant in variants}
        options={tuple(option.value.split("|||",1)) for option in modal.raid_select.options}
        self.assertEqual(options,expected)
        self.assertLessEqual(len(modal.raid_select.options),25)
        self.assertEqual(len(modal.children),4)
        self.assertFalse(hasattr(modal,"boss_select"))
        self.assertIsInstance(modal.proof_upload,bot.ui.FileUpload)
        self.assertFalse(modal.proof_upload.required)
        self.assertFalse(modal.details_input.placeholder)

    def test_existing_per_raid_shortcut_has_optional_proof(self):
        for activity,formats in bot.RAIDS.items():
            modal=bot.RaidSubmitModal(activity)
            self.assertEqual([option.value for option in modal.format_select.options],formats)
            self.assertEqual(len(modal.children),4)
            self.assertIsInstance(modal.proof_upload,bot.ui.FileUpload)
            self.assertFalse(modal.details_input.placeholder)

    def test_boss_modal_contains_all_dt2_variants_and_no_mode_dropdown(self):
        modal=bot.BossSubmitModal()
        standard={option.value for option in modal.boss_select.options}
        special={option.value for option in modal.special_boss_select.options}
        regular={activity for activity,variants in bot.BOSSES.items()
                 if variants==["Standard"]}
        dt2={f"{activity}|||{mode}" for activity,variants in bot.BOSSES.items()
             if set(variants)=={"Normal","Awakened"} for mode in variants}
        team={f"{activity}|||{size}" for activity,formats in bot.TEAM_BOSS_SIZES.items()
              for size in formats}
        self.assertEqual(standard,regular)
        self.assertEqual(special,dt2|team)
        self.assertIn("Gauntlet",standard)
        self.assertIn("Corrupted Gauntlet",standard)
        self.assertLessEqual(len(modal.boss_select.options),25)
        self.assertLessEqual(len(modal.special_boss_select.options),25)
        self.assertFalse(hasattr(modal,"format_select"))
        self.assertEqual(len(modal.children),5)
        self.assertFalse(modal.details_input.placeholder)
        self.assertIsInstance(modal.proof_upload,bot.ui.FileUpload)
        self.assertFalse(modal.proof_upload.required)

    async def test_boss_modal_resolves_standard_dt2_nex_and_huey(self):
        i=SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(bot,"submit_modal_details",new_callable=AsyncMock) as send:
            async def check(standard,special,expected):
                modal=bot.BossSubmitModal()
                modal.boss_select._values=[standard] if standard else []
                modal.special_boss_select._values=[special] if special else []
                await modal.on_submit(i)
                self.assertEqual(send.await_args.args[1:4],expected)
                self.assertIs(send.await_args.args[6],modal.proof_upload)
                send.reset_mock()

            await check("Vorkath",None,("Bosses","Vorkath","Standard"))
            await check("Gauntlet",None,("Bosses","Gauntlet","Standard"))
            await check("Corrupted Gauntlet",None,
                        ("Bosses","Corrupted Gauntlet","Standard"))
            await check(None,"Vardorvis|||Normal",("Bosses","Vardorvis","Normal"))
            await check(None,"Vardorvis|||Awakened",("Bosses","Vardorvis","Awakened"))
            await check(None,"Nex|||Trio",("Bosses","Nex","Trio"))
            await check(None,"The Hueycoatl|||5-Man",
                        ("Bosses","The Hueycoatl","5-Man"))

            modal=bot.BossSubmitModal()
            modal.boss_select._values=["Gauntlet"]
            modal.special_boss_select._values=["Nex|||Duo"]
            await modal.on_submit(i)
            send.assert_not_awaited()
            i.response.send_message.assert_awaited()
            self.assertTrue(i.response.send_message.await_args.kwargs["ephemeral"])

    async def test_raid_modal_preserves_variant_and_proof_field(self):
        i=SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        modal=bot.AllRaidSubmitModal()
        modal.raid_select._values=["Theatre of Blood|||Hard Mode · Trio"]
        with patch.object(bot,"submit_modal_details",new_callable=AsyncMock) as send:
            await modal.on_submit(i)
        self.assertEqual(send.await_args.args[1:4],
                         ("Raids","Theatre of Blood","Hard Mode · Trio"))
        self.assertIs(send.await_args.args[6],modal.proof_upload)

    async def test_optional_proof_included_in_submission_when_present(self):
        response=SimpleNamespace(send_message=AsyncMock())
        i=SimpleNamespace(response=response)
        details=SimpleNamespace(value="14:32.60")
        members=SimpleNamespace(values=[SimpleNamespace(id=123)])
        attachment=SimpleNamespace(
            content_type="image/png",size=120000,
            url="https://cdn.discordapp.com/attachments/111/222/proof.png"
        )
        upload=SimpleNamespace(values=[attachment])
        with (
            patch.object(bot,"discord_attachment_key",return_value=("111","222","proof.png")),
            patch.object(bot,"submit",new_callable=AsyncMock) as submit
        ):
            await bot.submit_modal_details(i,"Bosses","Vorkath","Standard",details,members,upload)
            self.assertEqual(submit.await_args.kwargs["proof"],attachment.url)
            submit.reset_mock()
            upload.values=[]
            await bot.submit_modal_details(i,"Bosses","Vorkath","Standard",details,members,upload)
            self.assertEqual(submit.await_args.kwargs["proof"],"")


if __name__=="__main__":
    unittest.main()
