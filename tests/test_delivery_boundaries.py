"""Offline boundary checks for admission, recovery, receipts, and operations."""
import contextlib
import io
import json
import os
import runpy
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

from bot_fixture import FakeClient, MonitorHarness, SOURCE, TARGET, app, source
from monitor_clone import read_members


class DeliveryBoundaryTests(MonitorHarness, unittest.IsolatedAsyncioTestCase):
    def test_message_links_respect_numeric_peer_and_post_limits(self):
        maximum = 2**31 - 1
        for link in (f'https://t.me/name/{maximum+1}', f'https://t.me/c/{app.MAX_CHANNEL_ID+1}/1',
                     'https://t.me/name/' + '1' * 5000):
            self.assertIsNone(self.bot.extract_link(link), link[:80])
        link = f'https://t.me/c/{app.MAX_CHANNEL_ID}/{maximum}'
        self.assertEqual(self.bot.extract_link(link), link)
        for suffix in ('', '/1'):
            args = ['/export_range', f'https://t.me/c/{app.MAX_CHANNEL_ID+1}{suffix}']
            args += ['2'] if suffix else ['1', '2']
            self.assertIsNotNone(app.parse_export_range_args(args)[3])

    async def test_album_query_stays_inside_wire_id_range(self):
        maximum = 2**31 - 1
        ids = [maximum-1, maximum]
        client = self.bot.client
        client.get_messages.return_value = self.event(*ids, album=5).messages
        posts = await read_members(client, SOURCE, ids, 5)
        self.assertEqual([m.id for m in posts], ids)
        self.assertEqual(max(client.get_messages.await_args.kwargs['ids']), maximum)

    async def test_expiring_media_is_not_converted_to_a_permanent_copy(self):
        client = self.bot.client
        client.forward.side_effect = app.errors.ChatForwardsRestrictedError(None)
        client.upload_file = AsyncMock()
        self.bot.tdl.download_media = AsyncMock()
        now = datetime.now(timezone.utc)
        photo = app.types.Photo(5, 1, b'ref', now, [app.types.PhotoSize('x', 10, 10, 8)], 2)
        doc = app.types.Document(7, 1, b'ref', now, 'text/plain', 8, 2, [])
        media = [app.types.MessageMediaPhoto(photo=photo, ttl_seconds=15),
                 app.types.MessageMediaDocument(document=doc, ttl_seconds=15)]
        with patch.object(app, 'MONITOR_TDL_FALLBACK', True):
            for mid, item in enumerate(media, 5):
                client.get_messages.return_value = [NS(id=mid, grouped_id=None, media=item, raw_text='', entities=[])]
                await self.enqueue(self.event(mid))
                with self.assertLogs(app.logger, 'ERROR'):
                    task = await self.process()
                self.assertEqual(task.status, app.TaskStatus.FAILED)
                self.assertIn('限时', task.error_msg)
                self.assertEqual(self.bot.monitor_message_map().copy_state(SOURCE, mid, TARGET), app.TaskStatus.PENDING)
        self.bot.tdl.download_media.assert_not_awaited()
        client.upload_file.assert_not_awaited()
        self.assertEqual(client.forward.await_count, 2)  # Only the explicitly rejected native forwards.

    async def test_unknown_job_state_is_not_replayed_or_rewritten_on_restore(self):
        q = self.bot.task_queue
        for state in ('sending', 'newer-format'):
            q.journal.save(app.ForwardTask(None, 'manual', None, 1, TARGET), state)
        before = q.journal.restore()
        with self.assertRaisesRegex(RuntimeError, '#2'):
            q._restore()
        self.assertEqual(q.journal.restore(), before)
        self.assertTrue(q.queue.empty())

    async def start_ambiguous_edit(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {7: 91})
        self.bot.client.edit_message.side_effect = TimeoutError('lost acknowledgement')
        await self.enqueue(self.event(7), edited=True)
        return await self.process()

    async def test_cancel_uncertain_edit_keeps_review_but_cancels_timer(self):
        q = self.bot.task_queue
        await self.start_ambiguous_edit()
        self.assertEqual(await q.stop_tdl_tasks(), (False, 1))
        self.assertFalse(q._retry_timers)
        self.assertEqual(q.journal.reviews()[0], 1)
        await self.bot.stop()
        self.bot = self.make_bot()
        self.bot.task_queue._restore()
        self.assertTrue(self.bot.task_queue.queue.empty())
        self.assertEqual(self.bot.task_queue.journal.reviews()[0], 1)

    async def test_cancel_explicitly_rejected_edit_does_not_create_review(self):
        q = self.bot.task_queue
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {7: 91})
        self.bot.client.edit_message.side_effect = app.errors.FloodWaitError(None, capture=60)
        await self.enqueue(self.event(7), edited=True)
        await self.process()
        self.assertEqual(await q.stop_tdl_tasks(), (False, 1))
        self.assertEqual(q.journal.restore(), [])

    async def test_failed_verification_cannot_discard_ambiguous_edit(self):
        q = self.bot.task_queue
        task = await self.start_ambiguous_edit()
        timer, _ = q._retry_timers.popitem()
        timer.cancel()
        q.queue.put_nowait(task)
        self.bot.client.get_messages.return_value = None
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        self.bot.client.edit_message.assert_awaited_once()
        self.assertEqual(q.journal.reviews()[0], 1)

    async def test_failed_reservation_release_preserves_copy_for_review(self):
        self.bot.client.forward.side_effect = app.errors.ChatWriteForbiddenError(None)
        m = self.bot.monitor_message_map()
        await self.enqueue(self.event(9))
        with patch.object(m, 'release_rejected', side_effect=sqlite3.OperationalError('unavailable')), self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        self.assertEqual(self.bot.task_queue.journal.reviews()[1][0]['message_ids'], [9])
        self.bot.client.forward.assert_awaited_once()

    async def test_rolled_back_reservation_is_not_an_uncertain_delivery(self):
        m = self.bot.monitor_message_map()
        await self.enqueue(self.event(8))
        with patch.object(m, 'reserve', side_effect=sqlite3.OperationalError('locked')), self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        self.assertEqual(self.bot.task_queue.journal.restore(), [])
        self.bot.client.forward.assert_not_awaited()

    async def test_clipped_tdl_record_cannot_prove_success(self):
        # The full item-error marker is outside both retained diagnostic ends; no final newline.
        cmd = [sys.executable, '-c', 'import sys; sys.stdout.write("x"*12000+" S(1):8 -> T(2) error: fail "+"y"*12000)']
        ok, detail = await self.bot.tdl._forward_receipt(cmd, None)
        self.assertFalse(ok)
        self.assertIsInstance(detail, app.UnconfirmedReceipt)
        self.assertLess(len(detail), 3000)

    async def test_large_failure_batch_has_bounded_diagnostics(self):
        async def run(cmd, progress, *, on_record):
            for mid in range(1, 700):
                on_record(f'S(1):{mid} -> T(2) error: denied')
            return 0, '', ''
        with patch.object(self.bot.tdl, '_run_command', side_effect=run):
            ok, detail = await self.bot.tdl.forward_clone('manual', TARGET)
        self.assertFalse(ok)
        self.assertIsInstance(detail, app.UnconfirmedReceipt)
        self.assertLess(len(detail), 3000)
        self.assertIn('1/1', detail)

    async def test_large_source_list_preserves_admin_action_receipt(self):
        self.bot.monitor_chat_ids = list(range(SOURCE, SOURCE+600))
        ev, result = NS(sender_id=61), AsyncMock()
        with patch.object(app, 'ADMIN_IDS', [61]):
            await self.bot.run_admin_action(ev, ['/monitor', 'add', str(SOURCE+600)], result)
        text = result.await_args.args[0]
        self.assertIn('已添加', text)
        self.assertIn('601', text)
        self.assertLessEqual(len(text.encode('utf-16-le')), 8192)
        self.assertEqual(len(self.bot.monitor_chat_ids), 601)

    async def test_bot_identity_is_not_accepted_as_user_monitor(self):
        client = FakeClient()
        client.connect = AsyncMock()
        client.get_me = AsyncMock(return_value=NS(bot=True))
        self.bot.user_client = client
        retry = await self.bot._connect_user_monitor(client, initial=True)
        self.assertIsNone(retry)
        self.assertFalse(self.bot.is_user_monitor_active())
        client.disconnect.assert_awaited_once()


class LocalStorageAndSessionTests(unittest.TestCase):
    def test_idle_flush_retries_removal_of_expired_rows_after_read(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'history.json'
            path.write_text(json.dumps([dict(id='7', timestamp=1, date='old', to=2, success=True, **{'from': 'x'})]))
            store = app.ForwardLog(path)
            self.assertEqual(store.get_all(), [])
            with patch.object(store, '_save', return_value=False):
                self.assertFalse(store.flush())
            self.assertTrue(store.flush())
            self.assertEqual(json.loads(path.read_text()), [])

    def test_session_generator_does_not_export_a_bot_login(self):
        client = MagicMock()
        client.get_me.return_value = NS(bot=True)
        client.session.save.return_value = 'not-a-real-session'
        output = io.StringIO()
        with patch.dict(os.environ, API_ID='1', API_HASH='dummy'), patch('telethon.sync.TelegramClient') as cls, contextlib.redirect_stdout(output):
            cls.return_value.__enter__.return_value = client
            with self.assertRaises(SystemExit):
                runpy.run_path(str(source.with_name('gen_telethon_session.py')))
        client.session.save.assert_not_called()
        self.assertNotIn('USER_SESSION_STRING=', output.getvalue())
