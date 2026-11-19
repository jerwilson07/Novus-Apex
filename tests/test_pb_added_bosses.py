"""Registration, validation, and publication for the added boss brackets."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import bot
from test_pb_ui import record


BRACKETS = (
    ("Hespori", "Standard", 1),
    ("Amoxliatl", "Standard", 1),
    ("Nex", "Duo", 2),
    ("Nex", "Trio", 3),
    ("The Hueycoatl", "Trio", 3),
    ("The Hueycoatl", "5-Man", 5),
)


class AddedBossesLayoutTests(TestCase):
    def test_team_bosses_use_novus_server_emoji_names(self):
        self.assertEqual(bot.ACTIVITY_EMOJIS["Nex"], "nex")
        self.assertEqual(bot.ACTIVITY_EMOJIS["The Hueycoatl"], "huey")

    def test_live_directory_bosses_are_alphabetical_link_buttons_with_exact_emoji_ids(self):
        links = {name: f"https://discord.com/channels/1532817291048124466/1568280192936443934/{10**18+i}"
                 for i, name in enumerate((*bot.BOSSES, *bot.RAIDS))}
        guild = SimpleNamespace(id=bot.NOVUS_GUILD_ID, emojis=[])
        with (patch.object(bot, "GUILD_ID", guild.id),
              patch.object(bot.bot, "get_guild", return_value=guild)):
            view = bot.BossDirectory(links)
        buttons = [item for item in view.walk_children() if isinstance(item, bot.ui.Button)]
        labels = [button.label for button in buttons]
        self.assertEqual(labels, sorted(labels, key=str.casefold))
        self.assertIn("### Bosses", [item.content for item in view.walk_children()
                                     if isinstance(item, bot.ui.TextDisplay)])
        self.assertLessEqual(bot.pb_view_text_units(view), bot.PB_PUBLIC_TEXT_LIMIT)
        self.assertLessEqual(len(list(view.walk_children())), 40)
        expected = {"Hespori": 1572891309214142504, "Amoxliatl": 1572891310992527431,
                    "Nex": 1533163395136884848, "The Hueycoatl": 1533163524858183860}
        for button in buttons:
            self.assertEqual(button.url, links[button.label])
            if button.label in expected:
                self.assertEqual(button.emoji.id, expected[button.label])

    def test_supported_brackets_are_selectable_and_directory_has_all_four(self):
        modal = bot.SubmitModal()
        for select in (modal.target_select, modal.format_select, modal.boss_select):
            self.assertLessEqual(len(select.options), 25)
        self.assertEqual(set(bot.ADDED_BOSSES), {boss for boss, _, _ in BRACKETS})
        for name in bot.ADDED_BOSSES:
            with self.subTest(boss=name):
                variants = [variant for boss, variant, _ in BRACKETS if boss == name]
                self.assertEqual(bot.BOSSES[name], variants)
                links = {name: "https://example.com/boss"}
                directory = bot.BossDirectory(links)
                self.assertTrue(any(item.label == name and item.url == links[name]
                                    for item in directory.walk_children()
                                    if isinstance(item, bot.ui.Button)))
        for name, variant, size in BRACKETS:
            with self.subTest(boss=name, variant=variant):
                self.assertEqual(bot.team_size("Bosses", variant, name), size)
                self.assertEqual(bot.solo_boss_variant(name, variant), size == 1)
                if size > 1:
                    self.assertIn(f"boss|||{name}", [o.value for o in modal.target_select.options])
                    self.assertIn(variant, [o.value for o in modal.format_select.options])
                    self.assertIn(variant, bot.first_announcement_title(name, variant))
                    self.assertIn(variant, bot.recent_pb_title(name, variant))
                else:
                    self.assertIn(name, [o.value for o in modal.boss_select.options])

    def test_team_boards_fit_with_full_rosters_and_long_proof_links(self):
        for name, variants in bot.TEAM_BOSS_SIZES.items():
            with self.subTest(boss=name):
                blocks = [(variant, [
                    record(id=vi*5+i, category="Bosses", activity=name, variant=variant,
                           member_ids=",".join(str(100000000000000000+i*10+m) for m in range(size)),
                           proof=f"https://example.com/{vi}/{i}/"+("a"*1000))
                    for i in range(5)
                ]) for vi, (variant, size) in enumerate(variants.items())]
                board = bot.linked_board_view(bot.BossLeaderboard, name, blocks)
                self.assertLessEqual(bot.pb_view_text_units(board), bot.PB_PUBLIC_TEXT_LIMIT)
                self.assertLessEqual(len(board.children[0].children), 10)
                contents = [item.content for item in board.walk_children() if isinstance(item, bot.ui.TextDisplay)]
                self.assertTrue(all(f"### {variant}" in contents for variant in variants))
                self.assertIn("<@100000000000000000>", "\n".join(contents))
                if name == "The Hueycoatl":
                    self.assertIn("<@100000000000000004>", "\n".join(contents))

    def test_new_boards_only_move_the_snapshot_and_footer_to_the_end(self):
        roots = {
            "Inferno": {"message": 90},
            bot.SNAPSHOT_CONTINUATION_KEY: {"message": 100},
            bot.PB_FOOTER_KEY: {"message": 110},
            "Nex": {"message": 120},
            "The Hueycoatl": {"message": 130},
        }
        closing = [bot.SNAPSHOT_CONTINUATION_KEY, bot.PB_FOOTER_KEY]
        self.assertEqual(bot.public_board_tail_to_reflow(roots, []), closing)
        roots.update({key: {"message": 140+i} for i, key in enumerate(closing)})
        self.assertEqual(bot.public_board_tail_to_reflow(roots, []), [])


class AddedBossesIntegrationTests(IsolatedAsyncioTestCase):
    async def test_modal_enforces_roster_sizes_and_saves_the_exact_bracket(self):
        for name, variant, size in BRACKETS:
            with self.subTest(boss=name, variant=variant):
                modal = bot.SubmitModal()
                modal.target_select._values = [f"boss|||{name}" if size > 1 else "boss"]
                modal.format_select._values = [variant if size > 1 else "__none__"]
                modal.boss_select._values = ["__none__" if size > 1 else name]
                modal.details_input._value = "46.80 | 11/11/2026"
                i = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=999),
                                    response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                                    followup=SimpleNamespace(send=AsyncMock()))
                db = SimpleNamespace(add=AsyncMock(return_value=7), attach_review=AsyncMock(),
                                     one=AsyncMock(return_value=record()))
                channel = SimpleNamespace(id=1, send=AsyncMock(return_value=SimpleNamespace(id=2)))
                players = [SimpleNamespace(id=100+k, display_name=f"Player {k}") for k in range(size+1)]
                with (patch.object(bot, "store", db), patch.object(bot.bot, "get_channel", return_value=channel),
                      patch.object(bot.Review, "make", new_callable=AsyncMock),
                      patch.object(bot, "datetime", wraps=datetime) as clock):
                    clock.now.return_value = datetime(2026, 11, 19, tzinfo=timezone.utc)
                    modal.members_select._values = players
                    await modal.on_submit(i)
                    self.assertIn(f"Expected {size}", i.response.send_message.await_args.args[0])
                    db.add.assert_not_awaited()
                    modal.members_select._values = players[:size]
                    await modal.on_submit(i)
                    db.add.assert_awaited_once()
                    self.assertEqual(db.add.await_args.args[2:5], ("Bosses", name, variant))
                    self.assertEqual(db.add.await_args.args[6], ",".join(str(p.id) for p in players[:size]))

    async def test_only_missing_new_boards_are_published(self):
        tracked = [{"activity": "Inferno"}, {"activity": "Hespori"}, {"activity": "Nex"}]
        published = tracked + [{"activity": "Amoxliatl"}, {"activity": "The Hueycoatl"}]
        channel = SimpleNamespace(id=7)
        db = SimpleNamespace(set_boss_board=AsyncMock(), boss_boards=AsyncMock(return_value=published))
        with (patch.object(bot, "store", db),
              patch.object(bot, "write_linked_board",
                           new=AsyncMock(side_effect=[SimpleNamespace(id=8), SimpleNamespace(id=9)])) as write):
            self.assertEqual(await bot.publish_missing_added_boss_boards(channel, 123, tracked), published)
            self.assertEqual([call.args[2] for call in write.await_args_list], ["Amoxliatl", "The Hueycoatl"])
            self.assertTrue(all(call.kwargs["silent"] for call in write.await_args_list))
            self.assertEqual([call.args for call in db.set_boss_board.await_args_list],
                             [(123, "Amoxliatl", 7, 8), (123, "The Hueycoatl", 7, 9)])
