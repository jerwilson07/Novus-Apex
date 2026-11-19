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
    def test_split_directory_keeps_every_icon_and_link_under_each_messages_limit(self):
        guild = SimpleNamespace(id=bot.NOVUS_GUILD_ID, emojis=[])
        links = {name: f"https://discord.com/channels/{guild.id}/1568280192936443934/{10**18+i}"
                 for i, name in enumerate(bot.PB_DIRECTORY_ORDER)}
        emojis = {emoji.casefold(): f"<:{emoji}:{10**18+i}>"
                  for i, emoji in enumerate(bot.ACTIVITY_EMOJIS.values())}
        points = [dict(rank=i, member_id=10**18+i, points=20-i, firsts=3, seconds=1, thirds=1)
                  for i in range(1, 11)]
        with (patch.object(bot, "GUILD_ID", guild.id),
              patch.object(bot.bot, "get_guild", return_value=guild),
              patch.dict(bot.ACTIVITY_EMOJI_CACHE, {guild.id: emojis})):
            first = bot.Home(points,guild_id=guild.id,directory_links=links)
            second = bot.BossDirectory(links,page=2)
            ordinary = bot.Home(points,guild_id=guild.id)
        def display_text(container):
            return "\n".join(item.content for item in container.children if isinstance(item,bot.ui.TextDisplay))
        top, directory = first.children
        self.assertEqual(display_text(top), display_text(ordinary.children[0]))
        self.assertEqual(len(ordinary.children), 1)
        first_part = display_text(directory)
        second_part = display_text(second.children[0])
        self.assertEqual([s for s in first_part.splitlines() if s.startswith("### ")],
                         ["### Minigames","### Raids","### Slayer Bosses"])
        self.assertEqual([s for s in second_part.splitlines() if s.startswith("### ")],
                         ["### Bosses","### DT2 Bosses"])
        for name,url in links.items():
            comes_first = name in bot.PB_DIRECTORY_FIRST_ACTIVITIES
            part,other = (first_part,second_part) if comes_first else (second_part,first_part)
            icon = bot.NOVUS_ADDED_BOSS_EMOJIS.get(name,emojis[bot.ACTIVITY_EMOJIS[name].casefold()])
            self.assertIn(f"{icon} [{bot.RAID_LABELS.get(name,name)}]({url})",part)
            self.assertNotIn(url,other)
        self.assertLessEqual(bot.pb_view_text_units(first), bot.PB_PUBLIC_TEXT_LIMIT)
        self.assertLessEqual(bot.pb_view_text_units(second), bot.PB_PUBLIC_TEXT_LIMIT)

    def test_team_bosses_use_novus_server_emoji_names(self):
        self.assertEqual(bot.ACTIVITY_EMOJIS["Nex"], "nex")
        self.assertEqual(bot.ACTIVITY_EMOJIS["The Hueycoatl"], "huey")

    def test_full_live_directory_has_all_text_links_and_exact_added_icons(self):
        links = {name: f"https://discord.com/channels/1532817291048124466/1568280192936443934/{10**18+i}"
                 for i, name in enumerate((*bot.BOSSES, *bot.RAIDS))}
        guild = SimpleNamespace(id=bot.NOVUS_GUILD_ID, emojis=[])
        emotes = {emoji.casefold(): f"<:{emoji}:{10**18+i}>"
                  for i, emoji in enumerate(bot.ACTIVITY_EMOJIS.values())}
        with (patch.object(bot, "GUILD_ID", guild.id),
              patch.object(bot.bot, "get_guild", return_value=guild),
              patch.dict(bot.ACTIVITY_EMOJI_CACHE, {guild.id: emotes})):
            view = bot.BossDirectory(links)
        items = list(view.walk_children())
        self.assertFalse(any(isinstance(item, bot.ui.Button) for item in items))
        body = "\n".join(item.content for item in items if isinstance(item, bot.ui.TextDisplay))
        self.assertLessEqual(bot.pb_view_text_units(view), bot.PB_PUBLIC_TEXT_LIMIT)
        for name, url in links.items():
            self.assertIn(f"[{bot.RAID_LABELS.get(name, name)}]({url})", body)
        expected = {"Hespori": "<:hespori:1572891309214142504>",
                    "Amoxliatl": "<:amoxliatl:1572891310992527431>",
                    "Nex": "<:Nex:1533163395136884848>",
                    "The Hueycoatl": "<:Huey:1533163524858183860>"}
        for name, emoji in expected.items():
            self.assertIn(f"{emoji} [{name}]({links[name]})", body)
        boss_lines = body.split("### Bosses\n", 1)[1].split("### DT2 Bosses", 1)[0].splitlines()
        labels = [line.split("[", 1)[1].split("]", 1)[0] for line in boss_lines]
        self.assertEqual(labels, sorted(labels, key=str.casefold))
        self.assertEqual(set(bot.PB_DIRECTORY_ORDER), set(links))

    def test_reflow_interleaves_the_new_boards_and_keeps_the_existing_prefix(self):
        roots = {name: {"message": 10+i} for i, name in enumerate((
            "Inferno", "The Fractured Archive", "Alchemical Hydra", "Corrupted Gauntlet",
            "Mad Angel", "Zulrah", "Duke Sucellus", "Hespori", "Amoxliatl", "Nex",
            "The Hueycoatl", bot.SNAPSHOT_CONTINUATION_KEY, bot.PB_FOOTER_KEY,
        ))}
        expected = [
            "Corrupted Gauntlet", "Hespori", "Mad Angel", "Nex", "The Hueycoatl",
            "Zulrah", "Duke Sucellus", bot.SNAPSHOT_CONTINUATION_KEY, bot.PB_FOOTER_KEY,
        ]
        self.assertEqual(bot.public_board_tail_to_reflow(roots, []), expected)
        for i, name in enumerate(expected, 100):
            roots[name]["message"] = i
        self.assertEqual(bot.public_board_tail_to_reflow(roots, []), [])

    def test_supported_brackets_are_selectable_and_directory_has_all_four(self):
        modal = bot.SubmitModal()
        for select in (modal.target_select, modal.format_select, modal.boss_select):
            self.assertLessEqual(len(select.options), 25)
        self.assertEqual([option.label for option in modal.target_select.options], [*bot.RAIDS, "Boss PB"])
        self.assertFalse(modal.boss_select.required)
        available = {option.value for option in modal.boss_select.options}
        self.assertEqual(available | set(bot.GAUNTLET_FORMATS.values()), set(bot.BOSSES))
        self.assertEqual(set(bot.ADDED_BOSSES), {boss for boss, _, _ in BRACKETS})
        for name in bot.ADDED_BOSSES:
            with self.subTest(boss=name):
                variants = [variant for boss, variant, _ in BRACKETS if boss == name]
                self.assertEqual(bot.BOSSES[name], variants)
                links = {name: "https://example.com/boss"}
                directory = bot.BossDirectory(links)
                self.assertTrue(any(f"[{name}]({links[name]})" in item.content
                                    for item in directory.walk_children()
                                    if isinstance(item, bot.ui.TextDisplay)))
        for name, variant, size in BRACKETS:
            with self.subTest(boss=name, variant=variant):
                self.assertEqual(bot.team_size("Bosses", variant, name), size)
                self.assertEqual(bot.solo_boss_variant(name, variant), size == 1)
                self.assertIn(name, available)
                if size > 1:
                    self.assertIn(variant, [o.value for o in modal.format_select.options])
                    self.assertIn(variant, bot.first_announcement_title(name, variant))
                    self.assertIn(variant, bot.recent_pb_title(name, variant))

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
    async def test_shared_gauntlet_option_preserves_both_pb_boards(self):
        modal = bot.SubmitModal()
        modal.target_select._values = ["boss"]
        modal.details_input._value = "46.80 | 11/11/2026"
        i = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(bot, "submit", new_callable=AsyncMock) as submit:
            modal.boss_select._values = []
            modal.format_select._values = ["__none__"]
            await modal.on_submit(i)
            self.assertIn("Choose which boss", i.response.send_message.await_args.args[0])
            modal.boss_select._values = ["Gauntlet"]
            await modal.on_submit(i)
            self.assertIn("Regular Gauntlet", i.response.send_message.await_args.args[0])
            submit.assert_not_awaited()
            for selected, activity in (("Regular Gauntlet", "Gauntlet"),
                                       ("Corrupted Gauntlet", "Corrupted Gauntlet")):
                modal.format_select._values = [selected]
                await modal.on_submit(i)
                self.assertEqual(submit.await_args.args[1:4], ("Bosses", activity, "Standard"))

    async def test_modal_enforces_roster_sizes_and_saves_the_exact_bracket(self):
        for name, variant, size in BRACKETS:
            with self.subTest(boss=name, variant=variant):
                modal = bot.SubmitModal()
                modal.target_select._values = ["boss"]
                modal.format_select._values = [variant if size > 1 else "__none__"]
                modal.boss_select._values = [name]
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
