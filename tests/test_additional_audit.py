"""Five follow-up passes: offline regressions for album, retry, edit, TDL and shutdown."""
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from bot_fixture import FakeClient, MonitorHarness, SOURCE, TARGET, app


class AdditionalAuditTests(MonitorHarness, unittest.IsolatedAsyncioTestCase):
    async def restart_queue(self):
        await self.bot.stop()
        self.bot = self.make_bot()
        self.bot.task_queue._restore()
        return self.bot.task_queue

    async def test_mixed_album_is_reviewed_immediately_without_splitting(self):
        event = self.event(12, 13, 14, album=80)
        m = self.bot.monitor_message_map()
        m.remember(SOURCE, TARGET, {12: 90})
        m.reserve(SOURCE, TARGET, [13])  # An older attempt could have published this member.
        await self.enqueue(event)
        with self.assertLogs(app.logger, 'WARNING'):
            task = await self.process()
        self.assertEqual(task.status, app.TaskStatus.UNCONFIRMED)
        count, rows = self.bot.task_queue.journal.reviews()
        self.assertEqual((count, rows[0]['message_ids']), (1, [13, 14]))
        self.assertEqual(m.copy_state(SOURCE, 14, TARGET), app.TaskStatus.PENDING)
        self.bot.client.forward.assert_not_awaited()
        q = await self.restart_queue()
        self.assertTrue(q.queue.empty())
        self.assertEqual(q.journal.reviews(), (count, rows))

    async def test_smaller_album_event_cannot_erase_full_snapshot(self):
        q, client = self.bot.task_queue, self.bot.client
        complete = self.event(12, 13, 14, album=80)
        partial = self.event(12, 13, album=80)
        client.get_messages.return_value = partial.messages
        await self.enqueue(complete)
        task = q.queue.get_nowait()
        task.retries = 2
        with self.assertLogs(app.logger, 'WARNING'):
            await q._process_task(task)
        q.queue.task_done()
        self.assertTrue(task.waiting_for_album)
        client.get_messages.return_value = complete.messages
        await self.enqueue(partial)
        self.assertEqual(q.journal.restore()[0][2]['album_event_ids'], [12, 13, 14])
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        client.forward.assert_awaited_once()
        self.assertEqual(client.forward.await_args.args[0].id, [12, 13, 14])

    async def schedule_rejected_copy(self):
        self.bot.client.forward.side_effect = app.errors.FloodWaitError(None, capture=60)
        await self.enqueue(self.event(11))
        with self.assertLogs(app.logger, 'WARNING'):
            return await self.process()

    async def test_release_failure_during_stop_keeps_recovery_handle(self):
        await self.schedule_rejected_copy()
        m = self.bot.monitor_message_map()
        with patch.object(m, 'release_rejected', side_effect=sqlite3.OperationalError('map is locked')):
            with self.assertLogs(app.logger, 'ERROR'):
                self.assertEqual(await self.bot.task_queue.stop_tdl_tasks(), (False, 1))
        self.assertEqual(m.copy_state(SOURCE, 11, TARGET), app.TaskStatus.UNCONFIRMED)
        self.bot.client.forward.assert_awaited_once()
        q = await self.restart_queue()
        self.assertTrue(q.queue.empty())
        self.assertEqual(q.journal.reviews()[1][0]['message_ids'], [11])

    async def test_crash_between_release_and_journal_delete_does_not_leave_false_review(self):
        await self.schedule_rejected_copy()
        q = self.bot.task_queue
        with patch.object(q.journal, 'discard', side_effect=sqlite3.OperationalError('journal unavailable')):
            with self.assertLogs(app.logger, 'ERROR'):
                self.assertEqual(await q.stop_tdl_tasks(), (False, 1))
        self.assertEqual(q.journal.restore()[0][1], 'retry_rejected')
        self.assertEqual(self.bot.monitor_message_map().copy_state(SOURCE, 11, TARGET), app.TaskStatus.PENDING)
        q = await self.restart_queue()
        self.assertTrue(q.queue.empty())
        self.assertEqual(q.journal.restore(), [])

    async def test_rejected_retry_marks_sending_before_dispatch(self):
        task = await self.schedule_rejected_copy()
        q = self.bot.task_queue
        timer, _ = q._retry_timers.popitem()
        timer.cancel()
        entered = asyncio.Event()
        states = []

        async def block(request):
            states.append(q.journal.restore()[0][1])
            entered.set()
            await asyncio.Future()

        self.bot.client.forward.side_effect = block
        q.queue.put_nowait(task)
        await q.start()
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(states, ['sending'])
        q = await self.restart_queue()
        self.assertTrue(q.queue.empty())
        self.assertEqual(q.journal.reviews()[1][0]['message_ids'], [11])

    async def test_lost_cloned_attachment_does_not_commit_a_text_only_mapping(self):
        m, client = self.bot.monitor_message_map(), self.bot.client
        old_key = 'photo:100:False'
        m.remember(SOURCE, TARGET, {11: 50}, 'bot', {11: old_key})
        client.get_messages.return_value = NS(raw_text='now text only', entities=[], media=None)
        await self.enqueue(self.event(11), edited=True)
        with self.assertLogs(app.logger, 'ERROR'):
            task = await self.process()
        self.assertEqual(task.status, app.TaskStatus.FAILED)
        self.assertIn('源帖附件已移除', task.error_msg)
        self.assertEqual(m.cloned_media_key(SOURCE, 11, TARGET), old_key)
        client.edit_message.assert_not_awaited()
        client.forward.assert_not_awaited()

    async def test_tdl_timeout_releases_inherited_output_pipes(self):
        # A descendant deliberately holds both output streams, even after its parent exits.
        # It does not execute TDL or call Telegram and exits when the test directory is removed.
        holder = ('import pathlib, sys, time\n'
                  'p = pathlib.Path(sys.argv[1])\n'
                  'for _ in range(200):\n'
                  '    if not p.exists(): break\n'
                  '    time.sleep(0.05)\n')
        parent = ('import subprocess, sys\n'
                  'subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]], '
                  'stdout=sys.stdout, stderr=sys.stderr)\n'
                  'print("descendant started", flush=True)\n')
        with tempfile.TemporaryDirectory() as keepalive:
            argv = [sys.executable, '-c', parent, holder, keepalive]
            with patch.object(app, 'TDL_TIMEOUT_SECONDS', 1), patch.object(app, 'TDL_REAP_GRACE_SECONDS', 0.2):
                command = asyncio.create_task(self.bot.tdl._run_command(argv))
                try:
                    done, _ = await asyncio.wait({command}, timeout=4)
                    self.assertIn(command, done, 'inherited handles held the dispatcher past the timeout')
                    with self.assertRaisesRegex(TimeoutError, 'descendant started'):
                        command.result()
                finally:
                    # Release the fixture even if the assertion fails against an older implementation.
                    Path(keepalive).rmdir()
                    await asyncio.gather(command, return_exceptions=True)
        self.assertFalse(self.bot.tdl.active_processes)

    async def test_shutdown_stops_worker_before_alert_or_client_disconnect(self):
        q = self.bot.task_queue
        user = self.bot.user_client = FakeClient()
        entered, cancelled, alert_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def publish(request):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async def notify(text):
            alert_started.set()
            await asyncio.Future()

        async def disconnect():
            self.assertTrue(cancelled.is_set())
            self.assertTrue(q.worker_task.done())

        user.forward.side_effect = publish
        user.disconnect.side_effect = disconnect
        self.bot.user_monitor_running = True
        event = self.event(8)
        event.client = user
        await self.enqueue(event)
        await self.enqueue(self.event(9))
        await q.start()
        await asyncio.wait_for(entered.wait(), 2)
        with patch.object(self.bot, 'notify_admins', side_effect=notify), patch.object(app, 'MONITOR_ALERT_WINDOW', 0):
            self.bot._admission_failure_count = 1
            self.bot._admission_alert = asyncio.create_task(self.bot._report_admission_failures())
            with self.assertLogs(app.logger, 'ERROR'):
                await asyncio.wait_for(alert_started.wait(), 2)
            await asyncio.wait_for(self.bot.stop(), 2)
        user.forward.assert_awaited_once()
        user.disconnect.assert_awaited_once()
        self.assertTrue(cancelled.is_set())
        q = await self.restart_queue()
        self.assertEqual(q.journal.reviews()[1][0]['message_ids'], [8])
        self.assertEqual(q.queue.get_nowait().message_ids, [9])
        q.queue.task_done()
