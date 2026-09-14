import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/review.py'


class LifecycleTest(unittest.TestCase):
    def start(self, cleanup=60, idle=600):
        directory = tempfile.TemporaryDirectory(prefix='review-lifecycle-')
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.start_at(self.directory, cleanup, idle)

    def start_at(self, directory, cleanup=60, idle=600):
        self.process = subprocess.Popen(
            [sys.executable, str(SCRIPT), 'serve', '--session', str(directory),
             '--cleanup-seconds', str(cleanup), '--idle-seconds', str(idle)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        process = self.process
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        line = process.stdout.readline()
        self.assertTrue(line, '服务未启动')
        self.connection = json.loads((directory / 'connection.json').read_text())
        self.port = int(self.connection['base'].rsplit(':', 1)[1])

    def api(self, action, body, ui=False):
        token = self.connection['url'].split('#')[1] if ui else self.connection['control_token']
        request = Request(self.connection['base'] + '/api/' + action,
                          data=json.dumps(body).encode(),
                          headers={'Authorization': 'Bearer ' + token})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def publish(self):
        return self.api('publish', {'id': 'theme', 'question': '虚构配色选择',
                                   'options': [{'id': 'dark', 'label': '深色'}]})

    def assert_closed(self):
        self.assertEqual(self.process.wait(timeout=5), 0)
        with socket.socket() as connection:
            connection.settimeout(1)
            self.assertNotEqual(connection.connect_ex(('127.0.0.1', self.port)), 0)

    def test_stop_auth_revision_pending_and_restart(self):
        self.start()
        self.publish()
        for revision, ui, code in [(1, True, 401), (1, False, 400)]:
            with self.assertRaises(HTTPError) as error:
                self.api('stop', {'revision': revision}, ui=ui)
            self.assertEqual(error.exception.code, code)
        self.api('answer', {'revision': 1, 'choices': ['dark']}, ui=True)
        with self.assertRaises(HTTPError):
            self.api('stop', {'revision': 0})
        self.assertTrue(self.api('stop', {'revision': 1})['stopping'])
        self.assert_closed()
        saved = json.loads((self.directory / 'state.json').read_text())
        self.assertEqual(saved['answers'][0]['choices'], ['dark'])
        self.start_at(self.directory)
        self.assertEqual(self.publish()['revision'], 2)
        self.api('answer', {'revision': 2, 'cancelled': True})
        self.api('stop', {'revision': 2})
        self.assert_closed()

    def test_terminal_fallback_and_pending_ask_stays_alive(self):
        self.start(cleanup=0.1)
        self.publish()
        self.api('ask', {'revision': 1, 'option_id': 'dark', 'prompt': '虚构追问'})
        time.sleep(1.2)
        self.assertIsNone(self.process.poll())
        self.api('answer', {'revision': 1, 'choices': ['dark']})
        self.assert_closed()
        saved = json.loads((self.directory / 'state.json').read_text())
        self.assertEqual(saved['status'], 'answered')
        self.assertEqual(saved['asks'][0]['status'], 'cancelled')

    def test_idle_timeout_releases_port_without_selecting(self):
        self.start(cleanup=0.1, idle=1)
        self.publish()
        self.assert_closed()
        saved = json.loads((self.directory / 'state.json').read_text())
        self.assertEqual(saved['status'], 'timed_out')
        self.assertEqual(saved['answers'], [])


if __name__ == '__main__':
    unittest.main()
