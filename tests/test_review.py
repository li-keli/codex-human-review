import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('review', Path(__file__).resolve().parents[1] / 'scripts/review.py')
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='codex-review-test-')
        self.addCleanup(self.tmp.cleanup)
        self.state = review.Review(Path(self.tmp.name))
        self.q = {'id': 'q1', 'question': '模拟决定', 'options': [{'id': 'a', 'label': '方案 A'}, {'id': 'b', 'label': '方案 B'}]}
        self.state.update('publish', self.q)

    def answer(self, **kw):
        return self.state.update('answer', {'revision': 1, 'choices': ['a'], **kw})

    def test_roundtrip_and_restart(self):
        self.answer(note='实际意见')
        restored = review.Review(Path(self.tmp.name))
        self.assertEqual(restored.data['answers'][0]['note'], '实际意见')
        self.assertEqual(restored.data['answers'][0]['labels'], ['方案 A'])
        restored.update('publish', {**self.q, 'id': 'q2'})
        self.assertEqual(restored.data['revision'], 2)
        with self.assertRaises(ValueError):
            restored.update('answer', {'revision': 1, 'choices': ['a']})

    def test_duplicate_submission(self):
        self.answer()
        with self.assertRaises(ValueError):
            self.answer()
        self.assertEqual(len(self.state.data['answers']), 1)

    def test_pending_cannot_overwrite_or_finish(self):
        for action, body in [('publish', self.q), ('finish', {})]:
            with self.assertRaises(ValueError):
                self.state.update(action, body)
        self.assertEqual(self.state.data['status'], 'pending')

    def test_invalid_answers_stay_pending(self):
        for data in [{'choices': []}, {'choices': ['a','b']}, {'choices': ['z']}, {'choices': ['a','a']}, {'choices': 1}]:
            with self.assertRaises(ValueError):
                self.answer(**data)
        self.assertEqual(self.state.data['status'], 'pending')
        self.assertEqual(self.state.data['answers'], [])

    def test_free_text(self):
        self.answer(choices=[], note='采用第三种方案')
        self.assertEqual(self.state.data['status'], 'answered')

    def test_pause_and_republish(self):
        self.answer(choices=[], cancelled=True)
        self.assertEqual(self.state.data['status'], 'paused')
        self.state.update('publish', self.q)
        self.assertEqual(self.state.data['status'], 'pending')

    def test_multi_and_finish(self):
        self.answer()
        self.state.update('publish', {**self.q, 'type': 'multi'})
        self.answer(revision=2, choices=['a','b'])
        self.state.update('finish', {'summary': '测试结束'})
        self.assertEqual(self.state.data['status'], 'completed')

    def test_malformed_table(self):
        with self.assertRaises(ValueError):
            review.validate({**self.q, 'table': {'headers': ['a'], 'rows': [['b','c']]}})

    def test_ask_roundtrip_and_followup(self):
        self.assertFalse(self.state.has_event())
        self.state.update('ask', {'revision': 1, 'option_id': 'a', 'prompt': '有什么风险？'})
        self.assertTrue(self.state.has_event())
        ask = self.state.data['asks'][0]
        self.state.update('respond', {'ask_id': ask['id'], 'response': '当前会话给出的实际解释'})
        self.assertFalse(self.state.has_event())
        self.state.update('ask', {'revision': 1, 'option_id': 'a', 'prompt': '继续解释成本'})
        self.assertEqual(len(self.state.data['asks']), 2)
        self.assertEqual(self.state.data['answers'], [])
        self.assertEqual(self.state.data['status'], 'pending')

    def test_ask_rejects_invalid_duplicate_and_late_reply(self):
        for body in [{'revision': 0, 'option_id': 'a', 'prompt': '风险'},
                     {'revision': 1, 'option_id': 'x', 'prompt': '风险'},
                     {'revision': 1, 'option_id': 'a', 'prompt': ''}]:
            with self.assertRaises(ValueError):
                self.state.update('ask', body)
        self.state.update('ask', {'revision': 1, 'option_id': 'a', 'prompt': '风险'})
        ask_id = self.state.data['asks'][0]['id']
        with self.assertRaises(ValueError):
            self.state.update('ask', {'revision': 1, 'option_id': 'b', 'prompt': '成本'})
        self.answer()
        with self.assertRaises(ValueError):
            self.state.update('respond', {'ask_id': ask_id, 'response': '过期回答'})
        self.assertEqual(self.state.data['asks'][0]['status'], 'cancelled')

    def test_ask_history_survives_restart(self):
        self.state.update('ask', {'revision': 1, 'option_id': 'b', 'prompt': '适用场景'})
        restored = review.Review(Path(self.tmp.name))
        self.assertEqual(restored.data['asks'][0]['prompt'], '适用场景')
        self.assertTrue(restored.has_event())

    def test_idle_timeout_and_activity(self):
        clock = [1000.0]
        directory = Path(self.tmp.name) / 'timer'
        directory.mkdir()
        state = review.Review(directory, now=lambda: clock[0])
        state.update('publish', self.q)
        self.assertEqual(state.data['deadline'], 1600)
        clock[0] = 1599
        state.update('activity', {'revision': 1})
        self.assertEqual(state.data['deadline'], 2199)
        clock[0] = 2198
        self.assertFalse(state.expire())
        clock[0] = 2199
        self.assertTrue(state.expire())
        self.assertTrue(state.has_event())
        self.assertEqual(state.data['status'], 'timed_out')
        self.assertEqual(state.data['answers'], [])
        for action, body in [('activity', {'revision':1}), ('answer', {'revision':1,'choices':['a']}), ('finish', {})]:
            with self.assertRaises(ValueError):
                state.update(action, body)

    def test_background_read_and_model_reply_do_not_extend_timeout(self):
        clock = [1000.0]
        directory = Path(self.tmp.name) / 'timer'
        directory.mkdir()
        state = review.Review(directory, now=lambda: clock[0])
        state.update('publish', self.q)
        state.update('ask', {'revision':1,'option_id':'a','prompt':'风险'})
        ask_id = state.data['asks'][0]['id']
        clock[0] = 1500
        state.has_event()
        state.update('respond', {'ask_id':ask_id,'response':'解释'})
        self.assertEqual(state.data['deadline'], 1600)
        clock[0] = 1600
        state.expire()
        self.assertEqual(state.data['status'], 'timed_out')

    def test_timeout_cancels_ask_and_can_resume(self):
        clock = [1000.0]
        directory = Path(self.tmp.name) / 'timer'
        directory.mkdir()
        state = review.Review(directory, idle_seconds=10, now=lambda: clock[0])
        state.update('publish', self.q)
        state.update('ask', {'revision':1,'option_id':'a','prompt':'风险'})
        clock[0] = 1010
        state.expire()
        self.assertEqual(state.data['asks'][0]['status'], 'cancelled')
        state.update('publish', self.q)
        self.assertEqual(state.data['revision'], 2)
        self.assertEqual(state.data['deadline'], 1020)

    def test_notification_once_per_revision_and_no_timer_reset(self):
        self.state.notifier = Mock(return_value={'status': 'submitted'})
        deadline = self.state.data['deadline']
        self.state.update('notify', {})
        self.state.update('notify', {})
        self.state.notifier.assert_called_once()
        self.assertEqual(self.state.data['deadline'], deadline)
        restored = review.Review(Path(self.tmp.name), notifier=self.state.notifier)
        restored.update('notify', {})
        self.state.notifier.assert_called_once()
        self.answer()
        with self.assertRaises(ValueError):
            self.state.update('notify', {})
        self.state.update('publish', self.q)
        self.state.update('notify', {})
        self.assertEqual(self.state.notifier.call_count, 2)

    def test_notification_failure_preserves_question(self):
        self.state.notifier = Mock(return_value={'status': 'failed', 'message': '不可用'})
        self.state.update('notify', {})
        self.assertEqual(self.state.data['status'], 'pending')
        self.assertEqual(self.state.data['notification']['status'], 'failed')

    def test_failed_save_does_not_commit_answer_or_timeout(self):
        before = json.loads(self.state.path.read_text())
        with patch.object(review, 'save', side_effect=OSError('磁盘不可写')):
            with self.assertRaises(OSError):
                self.answer()
            self.assertEqual(self.state.data, before)
            self.assertFalse(self.state.has_event())
            self.state.now = lambda: before['deadline'] + 1
            with self.assertRaises(OSError):
                self.state.expire()
            self.assertEqual(self.state.data, before)
        self.assertTrue(self.state.expire())

    def test_notification_does_not_block_answer_or_overwrite_new_state(self):
        entered, release = threading.Event(), threading.Event()
        def notifier(q):
            entered.set()
            release.wait(2)
            return {'status': 'submitted'}
        self.state.notifier = notifier
        worker = threading.Thread(target=lambda: self.state.update('notify', {}))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.state.lock.acquire(timeout=0.2))
            try:
                self.answer()
                self.state.update('publish', {**self.q, 'id': 'q2'})
            finally:
                self.state.lock.release()
        finally:
            release.set()
            worker.join(2)
        self.assertEqual(self.state.data['revision'], 2)
        self.assertNotIn('notification', self.state.data)

    def test_activity_is_small_and_restores_after_restart(self):
        version = self.state.data['version']
        before = self.state.path.read_bytes()
        self.state.now = lambda: self.state.data['deadline'] - 10
        self.state.update('activity', {'revision': 1})
        self.assertEqual(before, self.state.path.read_bytes())
        self.assertLess(self.state.activity_path.stat().st_size, 150)
        restored = review.Review(Path(self.tmp.name))
        self.assertEqual(restored.data['deadline'], self.state.data['deadline'])
        self.assertTrue(self.state.snapshot(str(version))['unchanged'])
        self.answer()
        self.assertNotIn('unchanged', self.state.snapshot(str(version)))

    def test_failed_activity_does_not_extend_deadline(self):
        deadline = self.state.data['deadline']
        with patch.object(review, 'save', side_effect=OSError('磁盘不可写')):
            with self.assertRaises(OSError):
                self.state.update('activity', {'revision': 1})
        self.assertEqual(deadline, self.state.data['deadline'])

    def test_ui_snapshot_limits_history_and_is_detached(self):
        for i in range(25):
            self.state.update('answer', {'revision': self.state.data['revision'], 'choices': ['a']})
            self.state.update('publish', {**self.q, 'id': str(i)})
        snapshot = self.state.snapshot('-1')
        self.assertEqual(len(snapshot['answers']), 20)
        snapshot['question']['id'] = 'changed'
        self.assertNotEqual(self.state.data['question']['id'], 'changed')
        self.assertEqual(len(self.state.snapshot()['answers']), 25)

    def test_http_connection_budget_and_timeout(self):
        # 不监听端口，验证接收连接后的限制与释放。
        server = review.BoundedHTTPServer(('127.0.0.1', 0), review.BaseHTTPRequestHandler,
                                          bind_and_activate=False)
        try:
            request = Mock()
            with patch.object(review.ThreadingHTTPServer, 'process_request'):
                for _ in range(16):
                    server.process_request(request, ('127.0.0.1', 1))
                request.settimeout.assert_called_with(5)
                with patch.object(server, 'shutdown_request') as shutdown:
                    server.process_request(request, ('127.0.0.1', 2))
                    shutdown.assert_called_once()
            with patch.object(review.ThreadingHTTPServer, 'process_request_thread'):
                server.process_request_thread(request, ('127.0.0.1', 1))
            self.assertTrue(server.slots.acquire(blocking=False))
        finally:
            server.server_close()

    def test_notification_click_targets_exact_thread(self):
        question = {**self.q, 'question': '引号 " 和 $(命令) 都是普通文本'}
        thread_id = '00000000-0000-4000-8000-000000000001'
        with patch.object(review.sys, 'platform', 'darwin'), \
             patch.object(review.shutil, 'which', return_value='/test/terminal-notifier'), \
             patch.object(review.subprocess, 'run') as run:
            run.return_value = Mock(returncode=0)
            result = review.notify_macos(question, thread_id)
            self.assertEqual(result['status'], 'submitted')
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index('-open') + 1], 'codex://threads/' + thread_id)
            self.assertIn(question['question'], argv[argv.index('-message') + 1])
            self.assertNotIn('-sender', argv)
            self.assertNotIn('-execute', argv)
            self.assertNotIn('shell', run.call_args.kwargs)

    def test_notification_rejects_missing_or_untrusted_target(self):
        with patch.object(review.sys, 'platform', 'darwin'), \
             patch.object(review.subprocess, 'run') as run:
            for target in (None, '', 'https://example.com', 'id; open /tmp'):
                self.assertEqual(review.notify_macos(self.q, target)['status'], 'failed')
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
