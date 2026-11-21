import asyncio
import json
import os
import runpy
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from bot_fixture import MonitorHarness, SOURCE, TARGET, app, source


class RecoveryTests(MonitorHarness, unittest.IsolatedAsyncioTestCase):
    async def test_hidden_forward_origin_does_not_submit_a_caption_link(self):
        ev = self.event(5)
        ev.respond = AsyncMock(return_value=None)
        ev.message.forward = NS(from_id=app.types.PeerUser(22), channel_post=None)
        await self.bot.do_forward(ev)
        self.assertTrue(self.bot.task_queue.queue.empty())
        self.assertIn('无法确认原帖', ev.respond.await_args.args[0])
        ev.message.forward = None
        await self.bot.do_forward(ev)
        self.assertEqual(self.bot.task_queue.journal.restore()[0][2]['link'], ev.message.text)

    async def test_stop_invalidates_manual_admissions_waiting_for_status(self):
        q = self.bot.task_queue
        for mode in ('clone', 'export_range'):
            with self.subTest(mode=mode):
                entered, resume = asyncio.Event(), asyncio.Event()
                ev = self.event(1)
                ev.sender_id = 61
                msg = NS(id=88, chat_id=ev.chat_id, edit=AsyncMock(), client=self.bot.client)

                async def respond(text):
                    entered.set()
                    await resume.wait()
                    return msg

                ev.respond = respond
                feedback = AsyncMock()
                with patch.object(app, 'ADMIN_IDS', [61]):
                    admission = asyncio.create_task(self.bot.do_forward(ev) if mode == 'clone' else
                        self.bot.run_admin_action(ev, ['/export_range', str(SOURCE), '1', '3'], feedback,
                                                  panel_feedback=True))
                    try:
                        await asyncio.wait_for(entered.wait(), 2)
                        await q.stop_tdl_tasks()
                    finally:
                        resume.set()
                        await admission
                self.assertEqual(q.journal.restore(), [])
                self.assertIn('未入队', msg.edit.await_args.args[0])
                if mode == 'export_range':
                    self.assertIn('未入队', feedback.await_args.args[0])

    async def test_stop_invalidates_resync_waiting_for_map_lock(self):
        q = self.bot.task_queue
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {7: 99})
        ev, feedback = self.event(7), AsyncMock()
        ev.sender_id = 61
        with patch.object(app, 'ADMIN_IDS', [61]):
            async with self.bot._monitor_db_lock:
                admission = asyncio.create_task(self.bot.run_admin_action(ev, ['/resync', str(SOURCE), '7'], feedback))
                await asyncio.sleep(0)  # resync now waits for the same lock
                await q.stop_tdl_tasks()
            await admission
        self.assertEqual(q.journal.restore(), [])
        self.assertIn('未加入同步队列', feedback.await_args.args[0])

    async def test_missing_tdl_binary_is_a_failure_not_an_uncertain_delivery(self):
        q = self.bot.task_queue
        self.bot.tdl.tdl_path = str(Path(app.MONITOR_MAP_FILE).parent / 'no-such-tdl')
        await q.add_task(self.event(9), 'manual', None, 1, target_chat_id=TARGET)
        task = await self.process()
        self.assertEqual(task.status, app.TaskStatus.FAILED)
        self.assertEqual(q.journal.restore(), [])
        self.assertIn('未启动', task.error_msg)
        # An I/O failure after spawn is not proof that nothing was sent.
        with patch.object(self.bot.tdl, '_run_command', side_effect=FileNotFoundError('lost after spawn')):
            ok, detail = await self.bot.tdl.forward_clone('manual', TARGET)
        self.assertFalse(ok)
        self.assertIsInstance(detail, app.UnconfirmedReceipt)

    async def test_cancelled_sending_task_status_agrees_with_review_journal(self):
        q, entered = self.bot.task_queue, asyncio.Event()
        status = NS(edit=AsyncMock())
        self.bot.client.get_messages.return_value = status

        async def sending(*args):
            entered.set()
            await asyncio.Future()

        self.bot.tdl.forward_clone = sending
        await q.add_task(self.event(8), 'manual', 123, 1, target_chat_id=TARGET)
        await q.start()
        await asyncio.wait_for(entered.wait(), 2)
        task = q.current_task
        self.assertEqual(await q.stop_tdl_tasks(), (True, 0))
        self.assertEqual(q.journal.reviews()[0], 1)
        self.assertEqual(task.status, app.TaskStatus.UNCONFIRMED)
        self.assertIn('待核对', status.edit.await_args.args[0])

    async def test_unmapped_edit_is_counted_before_and_after_restart_and_on_clear(self):
        await self.enqueue(self.event(9), edited=True)
        await self.process()
        self.assertEqual(self.bot.task_queue.get_queue_status()['queue_size'], 1)
        q = await self.reopen()
        self.assertEqual(q.get_queue_status()['queue_size'], 1)
        self.assertEqual(await q.stop_tdl_tasks(), (False, 1))
        self.assertEqual(q.journal.restore(), [])
        self.assertEqual(q.get_queue_status()['queue_size'], 0)

    async def test_edit_queued_during_running_edit_is_counted_but_current_is_not_cancelled(self):
        q = self.bot.task_queue
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {11: 99})
        entered, release = asyncio.Event(), asyncio.Event()

        async def editing(*args, **kwargs):
            entered.set()
            await release.wait()

        self.bot.client.edit_message.side_effect = editing
        await self.enqueue(self.event(11), edited=True)
        await q.start()
        await asyncio.wait_for(entered.wait(), 2)
        try:
            await self.enqueue(self.event(11), edited=True)
            self.assertEqual(q.get_queue_status()['queue_size'], 1)
            self.assertEqual(await q.stop_tdl_tasks(), (False, 1))
        finally:
            release.set()
            await asyncio.wait_for(q.queue.join(), 2)
        self.bot.client.edit_message.assert_awaited_once()
        self.assertEqual(q.journal.restore(), [])

    async def reopen(self):
        await self.bot.stop()
        self.bot = self.make_bot()
        self.bot.target_chat_id = TARGET + 1
        self.bot.task_queue._restore()
        return self.bot.task_queue

    async def test_manual_link_scope_survives_restart_and_silent_tdl_is_reviewed(self):
        base = f'https://t.me/c/{SOURCE}/8'
        submitted_target = self.bot.target_chat_id
        for suffix, expected in [('?embed=1&single', '?single'), ('', '')]:
            ev = self.event(8)
            ev.message.text = base + suffix
            ev.respond = AsyncMock(return_value=None)
            await self.bot.do_forward(ev)
            self.assertEqual(self.bot.task_queue.journal.restore()[-1][2]['link'], base + expected)
        for query in ('single&comment=7', 'thread=2&single'):
            self.assertIsNone(self.bot.extract_link(base + '?' + query))
        q = await self.reopen()
        with patch.object(self.bot.tdl, '_run_command', return_value=(0, '', '')) as run:
            for single in (True, False):
                task = await self.process()
                argv = run.await_args.args[0]
                self.assertEqual(argv[argv.index('--from')+1], base)
                self.assertEqual('--single' in argv, single)
                self.assertEqual(task.status, app.TaskStatus.UNCONFIRMED)
                self.assertIsInstance(task.error_msg, app.UnconfirmedReceipt)
                self.assertFalse(self.bot.forward_log.add.call_args.args[2])
                self.assertEqual(task.target_chat_id, submitted_target)
        self.assertEqual(q.journal.reviews()[0], 2)
        q = await self.reopen()
        self.assertTrue(q.queue.empty())
        self.assertEqual(q.journal.reviews()[0], 2)

    async def test_range_single_flag_is_not_single_input_evidence(self):
        output = f'Source({SOURCE}):8 -> Target({TARGET}) error: denied'
        with patch.object(self.bot.tdl, '_run_command', return_value=(0, output, '')) as run:
            for fn, arg, batch in [(self.bot.tdl.forward_from_export, 'export.json', True),
                                   (self.bot.tdl.forward_clone, f'https://t.me/c/{SOURCE}/8?single', False)]:
                ok, detail = await fn(arg, TARGET)
                self.assertFalse(ok)
                self.assertIn('--single', run.await_args.args[0])
                self.assertEqual(isinstance(detail, app.UnconfirmedReceipt), batch)

    async def test_shutdown_with_rejected_flood_request_restores_to_review(self):
        self.bot.client.forward.side_effect = app.errors.FloodWaitError(None, capture=60)
        await self.enqueue(self.event(8))
        task = await self.process()
        original = self.bot.client.forward.await_args.args[0]
        q = await self.reopen()
        self.assertTrue(q.queue.empty())
        self.assertFalse(q._retry_timers)
        count, rows = q.journal.reviews()
        self.assertEqual((count, rows[0]['job_id'], rows[0]['target_chat_id']), (1, task.job_id, TARGET))
        self.assertEqual(rows[0]['ready_at'], task.ready_at)
        m = self.bot.monitor_message_map()
        self.assertEqual(m.db.execute('SELECT random_id FROM monitor_copy_intents').fetchone()[0], original.random_id[0])
        self.bot.client.forward.assert_not_awaited()

    async def test_persistent_read_lock_parks_job_until_restart(self):
        q = self.bot.task_queue
        self.bot.monitor_message_map().db.execute('PRAGMA busy_timeout=0')
        self.bot.report_monitor_status = AsyncMock()
        await self.enqueue(self.event(8))
        locker = sqlite3.connect(app.MONITOR_MAP_FILE)
        try:
            locker.execute('BEGIN EXCLUSIVE')
            for attempt in range(3):
                task = await self.process()
                self.assertEqual(task.status, app.TaskStatus.PENDING)
                self.assertFalse(task.send_started)
                self.assertEqual(task.retries, 0)  # FloodWait and album retries still have their own budget.
                if attempt < 2:
                    timer, waiting = q._retry_timers.popitem()
                    timer.cancel(); q.queue.put_nowait(waiting)
            self.assertFalse(q._retry_timers)
            self.assertEqual(q.get_queue_status()['waiting_for_store'], 1)
            self.assertEqual(q.journal.restore()[0][:2], (task.job_id, 'wait_store'))
            self.bot.client.forward.assert_not_awaited()
        finally:
            locker.close()
        await self.reopen()
        restored = await self.process()
        self.assertEqual((restored.job_id, restored.target_chat_id, restored.status), (task.job_id, TARGET, app.TaskStatus.COMPLETED))
        self.bot.client.forward.assert_awaited_once()

    async def test_write_lock_at_reserve_is_before_send(self):
        q = self.bot.task_queue
        m = self.bot.monitor_message_map()
        m.db.execute('PRAGMA busy_timeout=0')
        await self.enqueue(self.event(9))
        locker = sqlite3.connect(app.MONITOR_MAP_FILE)
        try:
            locker.execute('BEGIN IMMEDIATE')  # Reads work; the reservation transaction cannot begin.
            task = await self.process()
            self.assertEqual(q.journal.restore()[0][1], 'pending')
            self.assertFalse(task.send_started)
            self.assertEqual(m.copy_state(SOURCE, 9, TARGET), app.TaskStatus.PENDING)
            self.bot.client.forward.assert_not_awaited()
        finally:
            locker.close()
        timer, waiting = q._retry_timers.popitem()
        timer.cancel(); q.queue.put_nowait(waiting)
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.bot.client.forward.assert_awaited_once()

    async def test_lock_after_dispatch_is_reviewed_not_retried(self):
        q, client = self.bot.task_queue, self.bot.client
        self.bot.monitor_message_map().db.execute('PRAGMA busy_timeout=0')
        locker = sqlite3.connect(app.MONITOR_MAP_FILE)
        def delivered(request):
            locker.execute('BEGIN EXCLUSIVE')
            return client.forward_response(request)
        client.forward.side_effect = delivered
        try:
            await self.enqueue(self.event(10))
            with self.assertLogs(app.logger, 'ERROR'):
                self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
            self.assertEqual(q.journal.reviews()[0], 1)
            self.assertFalse(q._retry_timers)
        finally:
            locker.close()
        client.forward.assert_awaited_once()
        q = await self.reopen()
        self.assertTrue(q.queue.empty())
        self.assertEqual(q.journal.reviews()[1][0]['message_ids'], [10])

    async def test_precise_cleanup_keeps_gaps_and_legacy_uncertainty(self):
        m = self.bot.monitor_message_map()
        m.remember(SOURCE, TARGET, {100: 800, 102: 802})
        with patch.object(app.time, 'time', return_value=app.time.time()+8*86400):
            m.cleanup_if_due(7)
        S = app.TaskStatus
        self.assertEqual([m.copy_state(SOURCE, i, TARGET) for i in (99, 100, 101, 102)],
                         [S.PENDING, S.COMPLETED, S.PENDING, S.COMPLETED])
        with m.db:
            m.db.execute('INSERT INTO monitor_pruned_through VALUES(?,?,?)', (SOURCE, TARGET, 90))
        self.assertEqual(m.copy_state(SOURCE, 89, TARGET), S.UNCONFIRMED)
        self.assertEqual(m.copy_state(SOURCE, 90, TARGET), S.COMPLETED)
        self.assertEqual(set(m.reserve(SOURCE, TARGET, [89, 90, 99, 100, 101])), {99, 101})

    async def test_restore_album_and_latest_edit_to_admission_target(self):
        for mid in (8, 3, 8):
            ev = self.event(mid, album=55)
            await self.bot.enqueue_monitor_post(ev, ev.messages, album_member=True)
        await self.enqueue(self.event(3, 8, album=55))
        for _ in range(3):
            await self.enqueue(self.event(8), edited=True)
        q = await self.reopen()
        self.assertEqual(q.queue.qsize(), 2)
        self.bot.client.get_messages.side_effect = lambda peer, *, ids: (
            self.event(3, 8, album=55).messages if isinstance(ids, list) else NS(raw_text='caption', entities=None, media=None))
        copy = await self.process()
        self.assertIsNone(copy.event)
        self.assertEqual((copy.message_ids, copy.target_chat_id, copy.status), ([3, 8], TARGET, app.TaskStatus.COMPLETED))
        edit = await self.process()
        self.assertEqual((edit.target_chat_id, edit.status), (TARGET, app.TaskStatus.COMPLETED))
        self.bot.client.edit_message.assert_awaited_once()
        self.assertEqual(q.journal.open().execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)

    async def test_early_edit_survives_restart_and_reconciles(self):
        await self.enqueue(self.event(6), edited=True)
        await self.process()
        q = await self.reopen()
        self.assertTrue(q.queue.empty())
        self.bot.target_chat_id = TARGET
        await self.enqueue(self.event(6))
        await self.process()
        await self.process()
        self.bot.client.edit_message.assert_awaited_once()

    async def test_stop_during_export_restores_unsent_range(self):
        q = self.bot.task_queue
        entered = asyncio.Event()
        async def exporting(*args):
            entered.set()
            await asyncio.Future()
        self.bot.tdl.export_range = AsyncMock(side_effect=exporting)
        await q.add_task(self.event(1), 'range', None, 1, mode='export_range', message_ids=[2, 5],
                         source_chat_id=SOURCE, target_chat_id=TARGET)
        await q.start()
        await asyncio.wait_for(entered.wait(), 2)
        q = await self.reopen()
        self.assertEqual(q.queue.qsize(), 1)
        self.bot.tdl.export_range = AsyncMock(return_value=(True, 'ok'))
        self.bot.tdl.forward_from_export = AsyncMock(return_value=(True, 'ok'))
        await self.process()
        self.assertEqual(self.bot.tdl.forward_from_export.await_args.args[1], TARGET)

    async def test_stop_during_send_only_restores_waiter(self):
        q = self.bot.task_queue
        entered = asyncio.Event()
        async def sending(*args):
            entered.set()
            await asyncio.Future()
        self.bot.tdl.forward_clone = AsyncMock(side_effect=sending)
        for link in ('first', 'waiting'):
            await q.add_task(self.event(1), link, None, 1, target_chat_id=TARGET)
        await q.start()
        await asyncio.wait_for(entered.wait(), 2)
        q = await self.reopen()
        self.assertEqual(q.queue.qsize(), 1)
        count, rows = q.journal.reviews()
        self.assertEqual((count, rows[0]['link']), (1, 'first'))
        self.bot.tdl.forward_clone = AsyncMock(return_value=(True, 'ok'))
        task = await self.process()
        self.assertEqual((task.link, task.target_chat_id), ('waiting', TARGET))

    async def test_edit_received_during_inflight_edit_survives_stop(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {7: 99})
        entered = asyncio.Event()
        async def editing(*args, **kwargs):
            entered.set()
            await asyncio.Future()
        self.bot.client.edit_message.side_effect = editing
        await self.enqueue(self.event(7), edited=True)
        await self.bot.task_queue.start()
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(3):
            await self.enqueue(self.event(7), edited=True)
        q = await self.reopen()
        self.assertEqual(q.journal.reviews()[0], 0)  # Reconcile only the mapped destination, no new delivery.
        self.assertEqual(q.queue.qsize(), 1)
        self.bot.client.get_messages.side_effect = [NS(raw_text='latest', entities=[], media=None),
                                                    NS(raw_text='older', entities=[], media=None)]
        await self.process()
        self.bot.client.edit_message.assert_awaited_once()
        self.bot.client.forward.assert_not_awaited()

    async def test_pre_send_retry_keeps_original_time_and_target(self):
        ev = self.event(10)
        ev.get_input_chat.side_effect = app.errors.FloodWaitError(None, capture=60)
        await self.enqueue(ev)
        task = await self.process()
        self.assertEqual(task.status, app.TaskStatus.PENDING)
        q = await self.reopen()
        self.assertTrue(q.queue.empty())
        restored, = q._retry_timers.values()
        self.assertEqual((restored.ready_at, restored.retries, restored.target_chat_id),
                         (task.ready_at, 1, TARGET))
        self.assertEqual(await q.stop_tdl_tasks(), (False, 1))
        self.assertEqual((await self.reopen()).get_queue_status()['queue_size'], 0)

    async def test_tdlstop_cancels_waiters_without_recovery(self):
        for mid in (5, 7):
            ev = self.event(mid, album=83)
            await self.bot.enqueue_monitor_post(ev, ev.messages, album_member=True)
        await self.enqueue(self.event(5), edited=True)
        self.assertEqual(await self.bot.task_queue.stop_tdl_tasks(), (False, 2))
        q = await self.reopen()
        self.assertTrue(q.queue.empty())
        self.assertEqual(q.journal.reviews(), (0, []))

    async def test_failed_admission_never_claims_queued(self):
        ev = self.event(1)
        status = NS(id=88, chat_id=1, edit=AsyncMock(), client=self.bot.client)
        ev.respond = AsyncMock(return_value=status)
        with patch.object(self.bot.task_queue.journal, 'save', side_effect=sqlite3.OperationalError('disk full')), \
                self.assertLogs(app.logger, 'ERROR'):
            await self.bot.do_forward(ev)
        self.assertIn('未入队', status.edit.await_args.args[0])
        self.assertTrue(self.bot.task_queue.queue.empty())

    async def test_streamed_item_errors_survive_progress_tail_in_both_paths(self):
        async def run(cmd, progress, *, on_record):
            for mid in (4, 19):
                on_record(f'\x1b[31mSource(12):{mid} -> Target(33) error: upload failed\x1b[0m')
            for _ in range(40):
                on_record('progress')
            return 0, 'finished', ''
        with patch.object(self.bot.tdl, '_run_command', side_effect=run):
            for method, arg in ((self.bot.tdl.forward_clone, 'link'), (self.bot.tdl.forward_from_export, 'file')):
                ok, reason = await method(arg, TARGET)
                self.assertFalse(ok)
                self.assertIsInstance(reason, app.UnconfirmedReceipt)
                for expected in ('12/4', '12/19', 'upload failed'):
                    self.assertIn(expected, reason)

    async def test_disabled_fallback_does_not_invoke_tdl(self):
        self.bot.client.forward.side_effect = app.errors.ChatForwardsRestrictedError(None)
        self.bot.tdl.forward_clone = AsyncMock()
        with patch.object(app, 'MONITOR_TDL_FALLBACK', False), self.assertLogs(app.logger, 'ERROR'):
            await self.enqueue(self.event(44))
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        self.bot.tdl.forward_clone.assert_not_awaited()


class HistoryWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_background_writer_coalesces_and_preserves_new_add_during_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            store = app.ForwardLog(path)
            writes = []
            entered, release = threading.Event(), threading.Event()
            original = store._save
            def slow(snapshot):
                self.assertNotEqual(threading.get_ident(), loop_thread)
                writes.append(len(snapshot))
                if len(writes) == 1 or not snapshot:
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('event loop stalled')
                return original(snapshot)
            loop_thread = threading.get_ident()
            with patch.object(store, '_save', side_effect=slow):
                store.add('old', 1, True)
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                for i in range(10):
                    store.add(str(i), 1, True)
                release.set()
                self.assertTrue(await asyncio.to_thread(store.flush))
                self.assertEqual(writes, [1, 11])
                entered.clear()
                release.clear()
                clear = asyncio.create_task(asyncio.to_thread(store.clear))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                store.add('after', 1, True)
                release.set()
                self.assertTrue(await clear)
                self.assertTrue(await asyncio.to_thread(store.flush))
            self.assertEqual([row['from'] for row in json.loads(path.read_bytes())], ['after'])

    async def test_flush_failure_can_be_retried_without_destroying_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            store = app.ForwardLog(path)
            store.add('old', 2, True)
            await asyncio.to_thread(store.flush)
            raw = path.read_bytes()
            with patch.object(app.os, 'replace', side_effect=OSError()), self.assertLogs(app.logger, 'ERROR'):
                store.add('new', 2, False, 'failure')
                self.assertFalse(await asyncio.to_thread(store.flush))
                self.assertTrue(store.write_failed)
                self.assertEqual(path.read_bytes(), raw)
            self.assertTrue(await asyncio.to_thread(store.flush))
            self.assertEqual(len(json.loads(path.read_bytes())), 2)


class DefaultTests(unittest.TestCase):
    def test_unset_empty_and_unrecognized_are_off(self):
        for value, expected in ((None, False), (' ', False), ('0', False), ('typo', False), ('1', True)):
            with patch.dict(os.environ), self.subTest(value=value):
                if value is None:
                    os.environ.pop('MONITOR_TDL_FALLBACK', None)
                else:
                    os.environ['MONITOR_TDL_FALLBACK'] = value
                self.assertEqual(runpy.run_path(str(source))['MONITOR_TDL_FALLBACK'], expected)
