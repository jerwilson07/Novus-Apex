import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import bot
from discord.components import _component_factory
from test_pb_ui import record


def bot_message(view, *, mid=900, author=5):
    return NS(id=mid, author=NS(id=author), attachments=[],
              components=[_component_factory(c) for c in view.to_components()])


class ProofThreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_thread_created_once_in_staff_review_channel(self):
        guild = NS(id=123)
        thread = NS(id=42, parent_id=17, guild=guild,
                    type=bot.discord.ChannelType.private_thread, owner_id=5)
        parent = NS(id=17, guild=guild, threads=[], create_thread=AsyncMock(return_value=thread))

        async def archived(**kwargs):
            self.assertEqual(kwargs, dict(private=True, joined=True, limit=100))
            for t in ():
                yield t

        parent.archived_threads = archived
        service = NS(get_channel=lambda cid: {17: parent, 42: thread}.get(cid), user=NS(id=5))
        with (patch.object(bot, "REVIEW_ID", 17), patch.object(bot, "bot", service),
              patch.object(bot.store, "one", new=AsyncMock(side_effect=[None, {"parent": 17, "thread": 42}])),
              patch.object(bot.store, "save_proof_thread", new=AsyncMock()) as save):
            self.assertIs(await bot.proof_thread_for(123), thread)
            self.assertIs(await bot.proof_thread_for(123), thread)
        parent.create_thread.assert_awaited_once()
        self.assertEqual(parent.create_thread.await_args.kwargs["type"], bot.discord.ChannelType.private_thread)
        self.assertFalse(parent.create_thread.await_args.kwargs["invitable"])
        save.assert_awaited_once_with(123, 17, 42)

    async def test_only_approved_upload_is_copied_once_refresh_reuses_it(self):
        r = record(id=71, status="approved", guild=123, review_channel=17,
                   proof=f"archive:{71:032x}.png")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"{71:032x}.png"
            path.write_bytes(b"example")
            filename = bot.proof_attachment_name(r, path)
            link = f"https://cdn.discordapp.com/attachments/42/501/{filename}?ex=ffffffff"
            message = NS(id=500, author=NS(id=5), attachments=[NS(filename=filename, url=link)])
            thread = NS(id=42, fetch_message=AsyncMock(return_value=message), send=AsyncMock(return_value=message))
            with (patch.object(bot, "REVIEW_ID", 17), patch.object(bot, "bot", NS(user=NS(id=5))),
                  patch.dict(bot.PUBLIC_PROOF_URLS, clear=True),
                  patch.dict(bot.PROOF_THREAD_RETRY_AT, clear=True),
                  patch.object(bot, "proof_thread_for", new=AsyncMock(return_value=thread)),
                  patch.object(bot.store, "one", new=AsyncMock(side_effect=[None, {"thread": 42, "message": 500}])),
                  patch.object(bot.store, "save_proof_message", new=AsyncMock()) as save,
                  patch.object(bot, "count_reachable_proof_images", new=AsyncMock(return_value=1)) as probe):
                self.assertFalse(await bot.cache_proof_from_thread(dict(r, status="pending"), path))
                self.assertTrue(await bot.cache_proof_from_thread(r, path))
                self.assertTrue(await bot.cache_proof_from_thread(r, path))
                self.assertEqual(bot.PUBLIC_PROOF_URLS[r["proof"]], link)
                probe.assert_awaited_once_with([link], allow_video=True)
            thread.send.assert_awaited_once()
            self.assertTrue(thread.send.await_args.kwargs["silent"])
            thread.fetch_message.assert_awaited_once_with(500)
            save.assert_awaited_once_with(123, r["proof"], 42, 500)
            self.assertEqual(path.read_bytes(), b"example")

    async def test_staff_removes_old_tile_only_after_thread_link_is_ready(self):
        r = record(id=71, status="approved", guild=123, review_channel=17, review_message=900,
                   archive_thread=42, proof=f"archive:{71:032x}.png")
        with tempfile.TemporaryDirectory() as tmp, patch.object(bot, "DBPATH", str(Path(tmp) / "test.db")):
            path = bot.archive_path(bot.DBPATH, r["proof"])
            path.parent.mkdir()
            path.write_bytes(b"example")
            filename = bot.proof_attachment_name(r, path)
            old_url = f"https://cdn.discordapp.com/attachments/17/501/{filename}?ex=ffffffff"
            new_url = old_url.replace("/17/501/", "/42/502/")
            old_view = bot.Review(r, archived_filename=filename)
            tile = next(x for x in old_view.walk_children() if isinstance(x, bot.ui.File))
            tile.media.url = old_url
            old = bot_message(old_view)
            old.edit = AsyncMock(return_value=bot_message(bot.Review(r)))
            review = NS(fetch_message=AsyncMock(return_value=old))
            with (patch.object(bot, "REVIEW_ID", 17),
                  patch.object(bot, "bot", NS(user=NS(id=5), get_channel=lambda _: review)),
                  patch.object(bot.store, "all", new=AsyncMock(return_value=[r])),
                  patch.object(bot.store, "mark_proof_button_card", new=AsyncMock()) as mark,
                  patch.object(bot, "cache_proof_from_thread", new=AsyncMock(return_value=False)) as stage,
                  patch.dict(bot.PUBLIC_PROOF_URLS, {r["proof"]: old_url}, clear=True)):
                await bot.restore_staff_proof_cards(123)
                old.edit.assert_not_awaited()
                stage.assert_awaited_once()
                bot.PUBLIC_PROOF_URLS[r["proof"]] = new_url
                await bot.restore_staff_proof_cards(123)
            old.edit.assert_awaited_once()
            mark.assert_awaited_once_with(123, 900)
            self.assertEqual(old.edit.await_args.kwargs["attachments"], [])
            self.assertEqual(path.read_bytes(), b"example")

    async def test_view_proof_used_in_all_staff_states_with_evidence(self):
        for status in ("approved", "pending", "awaiting_proof", "denied"):
            for proof in ("https://example.com/proof.png", f"archive:{1:032x}.png"):
                view = bot.Review(record(status=status, proof=proof))
                buttons = [i for i in view.walk_children() if isinstance(i, bot.ProofButton)]
                self.assertEqual([i.item.label for i in buttons], ["View Proof"])
                self.assertFalse(any(isinstance(i, (bot.ui.File, bot.ui.MediaGallery))
                                     for i in view.walk_children()))
