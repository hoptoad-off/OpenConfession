import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from start import load_environment

BOT_STUB = '''
import json, os, time
from pathlib import Path
class Store:
    def __init__(self, *args, **kwargs): self.db = self
    def close(self): pass
if __name__ == '__main__':
    Path('bot.started').write_text(json.dumps({'cwd': os.getcwd(), 'token': os.environ['BOT_TOKEN']}))
    try:
        if os.environ.get('TEST_BOT_EXIT') == '1': raise SystemExit(7)
        while True: time.sleep(.1)
    except KeyboardInterrupt:
        pass
    finally:
        Path('bot.stopped').touch()
'''
DASHBOARD_STUB = '''
import os, socket, time
from pathlib import Path
with socket.socket() as sock:
    sock.bind(('127.0.0.1', int(os.environ['DASHBOARD_PORT'])))
    sock.listen()
    Path('dashboard.started').touch()
    try:
        while True: time.sleep(.1)
    except KeyboardInterrupt:
        pass
    finally:
        Path('dashboard.stopped').touch()
'''


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name) / 'project with spaces'
        self.base.mkdir()
        shutil.copyfile(Path(__file__).with_name('start.py'), self.base / 'start.py')
        (self.base / 'bot.py').write_text(BOT_STUB)
        (self.base / 'dashboard.py').write_text(DASHBOARD_STUB)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        (self.base / '.env').write_text(f"BOT_TOKEN='test-token-from-file'\nCHANNEL_ID=@test\n"
                                     f"DASHBOARD_PASSWORD='test password 12345'\nDASHBOARD_PORT={self.port}\n")
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=10)
        self.temp.cleanup()

    def launch(self, **extra):
        env = dict(os.environ, BOT_TOKEN='stale-shell-value', **extra)
        child = subprocess.Popen([sys.executable, str(self.base / 'start.py')],
                                 cwd=self.temp.name, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True)
        self.children.append(child)
        return child

    def wait_for_file(self, name, child):
        deadline = time.monotonic() + 8
        while not (self.base / name).exists():
            if child.poll() is not None:
                self.fail(child.communicate()[0])
            if time.monotonic() > deadline:
                self.fail(f'Timed out waiting for {name}')
            time.sleep(.03)

    def test_start_from_other_directory_loads_env_and_stops_both(self):
        child = self.launch()
        self.wait_for_file('bot.started', child)
        data = json.loads((self.base / 'bot.started').read_text())
        self.assertEqual(data, {'cwd': str(self.base), 'token': 'test-token-from-file'})
        child.send_signal(signal.SIGINT)
        output, _ = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, output)
        self.assertTrue((self.base / 'bot.stopped').exists())
        self.assertTrue((self.base / 'dashboard.stopped').exists())
        self.assertNotIn('test-token-from-file', output)

    def test_bot_failure_stops_dashboard_and_returns_nonzero(self):
        child = self.launch(TEST_BOT_EXIT='1')
        output, _ = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 7, output)
        self.assertTrue((self.base / 'dashboard.stopped').exists())

    def test_occupied_port_does_not_launch_bot(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', self.port))
            sock.listen()
            child = self.launch()
            output, _ = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 1)
        self.assertIn('занят', output)
        self.assertFalse((self.base / 'bot.started').exists())

    def test_second_launch_does_not_disturb_first(self):
        first = self.launch()
        self.wait_for_file('bot.started', first)
        second = self.launch()
        output, _ = second.communicate(timeout=10)
        self.assertEqual(second.returncode, 1)
        self.assertIn('уже работает', output)
        self.assertIsNone(first.poll())
        first.terminate()
        first.communicate(timeout=10)
        self.assertTrue((self.base / 'dashboard.stopped').exists())
        self.assertTrue((self.base / 'bot.stopped').exists())

    def test_invalid_settings_do_not_start_processes(self):
        with (self.base / '.env').open('a') as stream:
            stream.write('MESSAGE_COOLDOWN_SECONDS=-1\n')
        child = self.launch()
        output, _ = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 1)
        self.assertIn('MESSAGE_COOLDOWN_SECONDS', output)
        self.assertFalse((self.base / 'bot.started').exists())
        self.assertFalse((self.base / 'dashboard.started').exists())

    def test_env_is_data_not_shell_code(self):
        env_file = self.base / 'parser.env'
        env_file.write_text("export VALUE='$(touch do-not-create)' # comment\nEMPTY=\nQUOTED='a # b'\n")
        data = load_environment(env_file, {'VALUE': 'old'})
        self.assertEqual(data['VALUE'], '$(touch do-not-create)')
        self.assertEqual(data['EMPTY'], '')
        self.assertEqual(data['QUOTED'], 'a # b')
        self.assertFalse((self.base / 'do-not-create').exists())
        env_file.write_text("TOKEN='private-value\n")
        with self.assertRaises(ValueError) as error:
            load_environment(env_file, {})
        self.assertNotIn('private-value', str(error.exception))


if __name__ == '__main__':
    unittest.main()
