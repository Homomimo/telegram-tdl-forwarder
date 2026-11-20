"""Offline regressions for the follow-up source audit (no Telegram connection)."""
import asyncio
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from bot_fixture import FakeClient, MonitorHarness, SOURCE, TARGET, app


class FollowupAuditTests(MonitorHarness, unittest.IsolatedAsyncioTestCase):
    async def test_two_visible_album_members_are_not_completion_evidence(self):
        q = self.bot.task_queue
        client = self.bot.client
        event = self.event(12, album=80)
        client.get_messages.return_value = self.event(12, 13, album=80).messages
        with patch.object(app, 'MONITOR_ALBUM_WAIT', 0):
            await self.bot.enqueue_monitor_post(event, event.messages, album_member=True)
            await asyncio.sleep(0.01)
        partial = await self.process()
        self.assertEqual(partial.status, app.TaskStatus.PENDING)
        client.forward.assert_not_awaited()
        complete = self.event(12, 13, 14, album=80)
        client.get_messages.return_value = complete.messages
        await self.enqueue(complete)
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        timer, _ = q._retry_timers.popitem()
        timer.cancel()
        q.queue.put_nowait(partial)
        await self.process()
        client.forward.assert_awaited_once()
        self.assertEqual(client.forward.await_args.args[0].id, [12, 13, 14])

    async def test_blocked_progress_is_cancelled_without_blocking_exit(self):
        entered, cancelled, records = asyncio.Event(), asyncio.Event(), []

        async def progress(line):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        argv = [sys.executable, '-u', '-c', 'import time; print(1); time.sleep(.3); print(2)']
        task = asyncio.create_task(self.bot.tdl._run_command(argv, progress, on_record=records.append))
        self.addAsyncCleanup(self._cancel, task)
        await asyncio.wait_for(entered.wait(), 5)
        self.assertEqual(await asyncio.wait_for(task, 5), (0, '1\n2', ''))
        self.assertTrue(cancelled.is_set())
        self.assertEqual(records, ['1', '2'])

    @staticmethod
    async def _cancel(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_definitive_edit_rejection_switches_account_not_message(self):
        source_bot = self.bot.client
        alternate = self.bot.user_client = FakeClient()
        self.bot.user_monitor_running = True
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {12: 28}, 'bot')
        source_bot.edit_message.side_effect = app.errors.MessageAuthorRequiredError(None)
        await self.enqueue(self.event(12), edited=True)
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        alternate.edit_message.assert_awaited_once()
        self.assertEqual(alternate.edit_message.await_args.args[1], 28)
        alternate.forward.assert_not_awaited()
        source_bot.edit_message.assert_awaited_once()

    async def test_repeat_observation_keeps_first_review_and_dedups_legacy_on_restore(self):
        client, q = self.bot.client, self.bot.task_queue
        client.forward.side_effect = TimeoutError('no receipt')
        for severity in ('ERROR', 'WARNING', 'WARNING'):
            await self.enqueue(self.event(12))
            with self.assertLogs(app.logger, severity):
                self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        client.forward.assert_awaited_once()
        count, rows = q.journal.reviews()
        self.assertEqual((count, rows[0]['job_id']), (1, 1))
        # An older installation may already contain redundant rows.
        with q.journal.open() as db:
            db.execute("INSERT INTO jobs(state,payload) SELECT state,payload FROM jobs WHERE id=1")
        await self.bot.stop()
        self.bot = self.make_bot()
        q = self.bot.task_queue
        q._restore()
        self.assertEqual(q.journal.reviews(), (count, rows))

    async def test_invalid_native_receipt_cannot_write_an_edit_mapping(self):
        client = self.bot.client
        event = self.event(5, 6, album=99)
        client.get_messages.return_value = event.messages

        async def send(request):
            return NS(updates=[app.types.UpdateMessageID(id=88, random_id=r) for r in request.random_id])

        client.forward.side_effect = send
        await self.enqueue(event)
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        m = self.bot.monitor_message_map()
        for mid in (5, 6):
            self.assertIsNone(m.get(SOURCE, mid, TARGET))
        self.assertEqual(self.bot.task_queue.journal.reviews()[1][0]['message_ids'], [5, 6])

    async def test_stop_cancels_explicitly_rejected_send_after_timer_moved_it_to_queue(self):
        q = self.bot.task_queue
        self.bot.client.forward.side_effect = app.errors.FloodWaitError(None, capture=1)
        await self.enqueue(self.event(11))
        with self.assertLogs(app.logger, 'WARNING'):
            task = await self.process()
        self.assertEqual(q.journal.restore()[0][1], 'retry_rejected')
        timer, delayed = q._retry_timers.popitem()
        timer.cancel()
        self.assertIs(delayed, task)
        q._schedule_retry(task, 0)
        await asyncio.sleep(0.01)
        self.assertEqual(q.queue.qsize(), 1)
        self.assertFalse(q._retry_timers)
        self.assertEqual(await q.stop_tdl_tasks(), (False, 1))
        self.assertEqual(q.journal.restore(), [])
        self.assertEqual(self.bot.monitor_message_map().copy_state(SOURCE, 11, TARGET), app.TaskStatus.PENDING)
        await self.bot.stop()
        self.bot = self.make_bot()
        q = self.bot.task_queue
        q._restore()
        self.assertEqual(q.journal.reviews(), (0, []))
        self.assertTrue(q.queue.empty())

    async def test_forward_header_queues_numeric_identity_even_with_public_username(self):
        event = self.event(2)
        event.message.forward = NS(from_id=app.types.PeerChannel(SOURCE), channel_post=42,
                                   chat=NS(username='old_username'))
        event.respond = AsyncMock(return_value=None)
        await self.bot.do_forward(event)
        self.assertEqual(self.bot.task_queue.journal.restore()[0][2]['link'], f'https://t.me/c/{SOURCE}/42')

    async def test_tdl_gets_eof_on_stdin_and_timeout_reaps_child(self):
        child = [sys.executable, '-c', 'import sys; print(sys.stdin.read() == "")']
        self.assertEqual(await self.bot.tdl._run_command(child), (0, 'True', ''))
        with patch.object(app, 'TDL_TIMEOUT_SECONDS', 0.3):
            with self.assertRaisesRegex(TimeoutError, 'TDL'):
                await self.bot.tdl._run_command([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.assertFalse(self.bot.tdl.active_processes)

    async def test_acknowledgement_error_does_not_cancel_restart(self):
        event = NS(sender_id=61)
        respond = AsyncMock(side_effect=ConnectionError('status unavailable'))
        with patch.object(app, 'ADMIN_IDS', [61]), self.assertLogs(app.logger, 'WARNING'):
            self.assertTrue(await self.bot.run_admin_action(event, ['/restart'], respond))
        self.assertTrue(self.bot.restart_requested)
        self.bot.client.disconnect.assert_awaited_once()
