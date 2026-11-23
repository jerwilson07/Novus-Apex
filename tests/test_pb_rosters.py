import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bot
from test_pb_ui import record


def view_text(view):
    def walk(item):
        return [getattr(item, "content", ""), *(
            s for child in getattr(item, "children", ()) for s in walk(child)
        )]
    return "\n".join(walk(view))


class PBRosterTests(unittest.TestCase):
    def test_historical_duo_is_visible_in_public_second_place_and_approved_review(self):
        ezra = record(id=35, guild=bot.NOVUS_GUILD_ID, category="Bosses",
                      activity="Royal Titans", variant="Standard", ms=46800,
                      pb_date="2026-11-11", status="approved", members="07 Ezra",
                      member_ids="707", submitter=808)
        leader = dict(ezra, id=40, ms=37200, member_ids="222,333")
        board = bot.BossLeaderboard("Royal Titans", [("Standard", [leader, ezra])])
        self.assertIn("**46.80** · <@707> · What A Piety", view_text(board))
        self.assertIn("<@707> · What A Piety", view_text(bot.Review(ezra)))
        with patch.object(bot, "royal_titans_35_partner_id", 808):
            self.assertEqual(bot.public_members(ezra), "<@707> · <@808>")
        self.assertEqual(bot.public_members(dict(ezra, guild=123)), "<@707>")
        self.assertEqual(bot.public_members(dict(ezra, member_ids="707,808")), "<@707> · <@808>")

    def test_team_roster_shared_by_other_bosses_raids_and_announcements(self):
        team = record(activity="Yama", category="Bosses", variant="Standard",
                      members="Player One, Player Two", member_ids="111,222",
                      submitter=333, ms=35000)
        self.assertIn("<@111> · <@222>", view_text(bot.BossLeaderboard("Yama", [("Standard", [team])])))
        self.assertIn("<@111> · <@222>", view_text(bot.Review(team)))
        self.assertIn("<@111> · <@222>", bot.first_place_announcement(team, None))
        self.assertNotIn("<@333>", bot.public_members(team))
        self.assertEqual(bot.public_members(dict(team, member_ids="111")), "Player One · Player Two")
        raid = record(activity="Tombs of Amascut", variant="Expert · 8-Man",
                      member_ids="1,2,3,4,5,6,7,8")
        self.assertIn(" · ".join(f"<@{n}>" for n in range(1, 9)),
                      view_text(bot.RaidLeaderboard("Tombs of Amascut", [("Expert · 8-Man", [raid])])))


 
class StaffReviewBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_review_is_edited_in_place_without_record_changes(self):
        import asyncio
        row = record(
            guild=123, id=81, category="Raids", activity="The Fractured Archive",
            variant="6-Man", members="One, Two, Three, Four, Five, Six",
            member_ids="101,202,303,404,505,606", submitter=999,
            review_channel=55, review_message=77, status="approved"
        )
        existing = SimpleNamespace(
            author=SimpleNamespace(id=444), id=77, attachments=[],
            edit=AsyncMock()
        )
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=existing))
        fake_bot = SimpleNamespace(
            guilds=[SimpleNamespace(id=123)],
            user=SimpleNamespace(id=444),
            get_channel=lambda channel_id: channel
        )
        fake_db = SimpleNamespace(
            one=AsyncMock(return_value=None),
            all=AsyncMock(return_value=[row]),
            lock=asyncio.Lock(),
            conn=SimpleNamespace(execute=AsyncMock(), commit=AsyncMock()),
        )
        old_card = "Submitted by <@999>\\n-# SUBMITTED PB\\n**<@101> · <@202>** · **14:32.60**"
        new_card = bot.Review(row)
        with (
            patch.object(bot, "bot", fake_bot),
            patch.object(bot, "store", fake_db),
            patch.object(bot, "old_announcement_text", return_value=old_card),
            patch.object(bot.ui.LayoutView, "from_message", return_value=SimpleNamespace(
                walk_children=lambda: iter(())
            )),
            patch.object(bot.Review, "make", new_callable=AsyncMock, return_value=new_card),
            patch.object(bot.asyncio, "sleep", new_callable=AsyncMock),
        ):
            await bot.refresh_existing_staff_review_cards_once()

        existing.edit.assert_awaited_once()
        view = existing.edit.await_args.kwargs["view"]
        text = view_text(view)
        self.assertIn("Submitted by <@101> · <@202> · <@303> · <@404> · <@505> · <@606>", text)
        self.assertNotIn("Submitted by <@999>", text)
        submitted = text.split("-# SUBMITTED PB", 1)[1].split("-# ", 1)[0]
        self.assertIn("14:32.60", submitted)
        self.assertNotIn("<@101>", submitted)
        self.assertFalse(existing.edit.await_args.kwargs.get("attachments"))
        fake_db.conn.execute.assert_awaited_once()
        self.assertIn("staff_review_full_roster_time_only_v1", fake_db.conn.execute.await_args.args[1][0])
        fake_db.conn.commit.assert_awaited_once()


class PBRosterSubmitTests(unittest.IsolatedAsyncioTestCase):
    async def test_historical_mention_only_resolves_verified_named_submitter(self):
        ezra = record(id=35, guild=bot.NOVUS_GUILD_ID, category="Bosses",
                      activity="Royal Titans", variant="Standard", ms=46800,
                      pb_date="2026-11-11", members="07 Ezra", member_ids="707", submitter=808)
        member = SimpleNamespace(id=808, name="not-the-teammate", global_name=None,
                                 display_name="Someone Else")
        guild = SimpleNamespace(id=bot.NOVUS_GUILD_ID, get_member=Mock(return_value=None),
                                fetch_member=AsyncMock(return_value=member))
        with (patch.object(bot, "store", SimpleNamespace(one=AsyncMock(return_value=ezra))),
              patch.object(bot, "royal_titans_35_partner_id", None)):
            await bot.resolve_royal_titans_35_partner(guild)
            self.assertEqual(bot.public_members(ezra), "<@707> · What A Piety")
            member.display_name = "What A Piety"
            await bot.resolve_royal_titans_35_partner(guild)
            self.assertEqual(bot.public_members(ezra), "<@707> · <@808>")

    async def test_team_boss_modal_saves_solo_and_duo_to_the_same_board(self):
        for activity in sorted(bot.NON_SOLO_BOSSES):
            with self.subTest(activity=activity):
                i = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=999),
                                    response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                                    followup=SimpleNamespace(send=AsyncMock()))
                one = SimpleNamespace(id=111, display_name="One")
                two = SimpleNamespace(id=222, display_name="Two")
                three = SimpleNamespace(id=333, display_name="Three")
                db = SimpleNamespace(add=AsyncMock(return_value=7), attach_review=AsyncMock(),
                                     one=AsyncMock(return_value=record()))
                channel = SimpleNamespace(id=1, send=AsyncMock(return_value=SimpleNamespace(id=2)))
                with (patch.object(bot, "store", db), patch.object(bot.bot, "get_channel", return_value=channel),
                      patch.object(bot.Review, "make", new_callable=AsyncMock),
                      patch.object(bot, "datetime", wraps=datetime) as clock):
                    clock.now.return_value = datetime(2026, 11, 13, tzinfo=timezone.utc)
                    for players in ([one, two, three], [one], [one, two]):
                        await bot.submit(i, "Bosses", activity, "Standard", "0:46.80",
                                         "11/11/2026", players,
                                         proof="https://example.com/proof.png")
                    self.assertIn("no more than two",
                                  i.response.send_message.await_args.args[0])
                    self.assertEqual(db.add.await_count, 2)
                    self.assertEqual([call.args[2:7] for call in db.add.await_args_list], [
                        ("Bosses", activity, "Standard", "One", "111"),
                        ("Bosses", activity, "Standard", "One, Two", "111,222"),
                    ])

    async def test_separate_modals_keep_raid_formats_and_dt2_modes(self):
        i = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        boss = bot.BossSubmitModal()
        boss.boss_select._values = []
        boss.special_boss_select._values = ["Vardorvis|||Awakened"]
        raid = bot.AllRaidSubmitModal()
        raid.raid_select._values = ["The Fractured Archive|||Duo"]
        with patch.object(bot, "submit_modal_details", new_callable=AsyncMock) as submit:
            await boss.on_submit(i)
            self.assertEqual(submit.await_args.args[1:4],
                             ("Bosses", "Vardorvis", "Awakened"))
            submit.reset_mock()
            await raid.on_submit(i)
            self.assertEqual(submit.await_args.args[1:4],
                             ("Raids", "The Fractured Archive", "Duo"))
