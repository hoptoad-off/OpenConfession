import contextlib
from concurrent.futures import ThreadPoolExecutor
import http.cookiejar
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
import uuid

from bot import APIError, Bot, Store, validate_nickname
from dashboard import DashboardServer


class FakeTelegram:
    token = 'fake-token-for-tests'

    def __init__(self):
        self.calls = []
        self.failure = None

    def call(self, method, **params):
        self.calls.append((method, params))
        if self.failure:
            raise self.failure
        if method == 'getFile':
            return {'file_path': 'photos/test.jpg', 'file_size': 3}
        return {'message_id': 100 + len(self.calls)}


def incoming(update_id, text='Привет!', user=42, **extra):
    message = {'from': {'id': user, 'username': 'tester', 'first_name': 'Test', 'last_name': 'User'},
               'chat': {'id': user, 'type': 'private'}, 'message_id': update_id,
               'date': 1000 + update_id, 'text': text, **extra}
    return {'update_id': update_id, 'message': message}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'bot.sqlite3')
        self.api = FakeTelegram()
        self.server = DashboardServer(('127.0.0.1', 0), self.path, 'test-dashboard-password', '@test_channel', self.api)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.cookies = http.cookiejar.CookieJar()
        self.client = build_opener(HTTPCookieProcessor(self.cookies))
        self.csrf = ''

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        hdrs = {'Content-Type': 'application/json', 'X-CSRF-Token': self.csrf}
        hdrs.update(headers or {})
        request = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None, headers=hdrs)
        try:
            response = self.client.open(request)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            body = json.loads(raw) if response.headers.get('Content-Type', '').startswith('application/json') else raw
            return response.status, body, response.headers

    def login(self):
        status, data, headers = self.request('/api/login', {'password': 'test-dashboard-password'})
        self.assertEqual(status, 200)
        self.csrf = data['csrf']
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])

    def test_private_routes_require_login_and_logout_revokes_session(self):
        for path in ('/api/messages', '/api/messages/1', '/api/messages/1/media', '/api/session'):
            self.assertEqual(self.request(path)[0], 401)
        self.assertEqual(self.request('/api/publish', {'text': 'x'})[0], 401)
        self.assertFalse(self.api.calls)
        self.login()
        self.assertEqual(self.request('/api/messages')[0], 200)
        self.assertEqual(self.request('/api/logout', {})[0], 200)
        self.assertEqual(self.request('/api/messages')[0], 401)

    def test_login_throttling(self):
        for _ in range(5):
            self.assertEqual(self.request('/api/login', {'password': 'wrong'})[0], 401)
        self.assertEqual(self.request('/api/login', {'password': 'test-dashboard-password'})[0], 429)

    def test_csrf_cross_origin_and_host_protection(self):
        self.login()
        payload = {'text': 'Never send', 'role': 'Administrator', 'request_id': str(uuid.uuid4())}
        self.assertEqual(self.request('/api/publish', payload, {'X-CSRF-Token': ''})[0], 403)
        self.assertEqual(self.request('/api/publish', payload, {'Origin': 'https://other.example'})[0], 403)
        self.assertEqual(self.request('/api/messages', headers={'Host': 'other.example'})[0], 403)
        self.assertFalse(self.api.calls)

    def test_bot_journal_contains_sender_body_and_processing_status(self):
        with contextlib.closing(Store(self.path).db) as db:
            pass
        store = Store(self.path)
        try:
            bot = Bot(self.api, store, '@test_channel', lambda: 2000)
            bot.handle(incoming(1, 'Hello <script>alert(1)</script>'))
            bot.handle(incoming(2, 'Too soon'))
            bot.handle(incoming(3, '/start'))
            # Duplicate Telegram update must not publish or log twice.
            bot.handle(incoming(1, 'Hello <script>alert(1)</script>'))
        finally:
            store.db.close()
        self.login()
        status, data, _ = self.request('/api/messages')
        self.assertEqual(status, 200)
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['stats']['published'], 1)
        self.assertEqual(data['stats']['senders'], 1)
        self.assertEqual([m['status'] for m in data['items']], ['command', 'rejected', 'published'])
        row = data['items'][-1]
        self.assertEqual(row['sender_id'], 42)
        self.assertEqual(row['sender_username'], 'tester')
        self.assertEqual(row['sender_name'], 'Test User')
        self.assertIn('<script>', row['body'])
        self.assertNotIn('payload_json', row)
        self.assertNotIn('media_file_id', row)
        self.assertTrue(row['channel_url'].startswith('https://t.me/test_channel/'))
        self.assertEqual(self.request('/api/messages?search=42&status=published')[1]['total'], 1)
        self.assertEqual(self.request('/api/messages?search=%27%20OR%201=1--')[1]['total'], 0)

    def test_admin_and_moderator_publish_share_counter_and_deduplicate(self):
        store = Store(self.path)
        try:
            Bot(self.api, store, '@test_channel', lambda: 2000).handle(incoming(1))
        finally:
            store.db.close()
        self.login()
        for number, role in ((2, 'Administrator'), (3, 'Moderator')):
            request = {'text': 'Объявление', 'role': role, 'request_id': str(uuid.uuid4())}
            status, data, _ = self.request('/api/publish', request)
            self.assertEqual(status, 200)
            self.assertEqual(data['message']['status'], 'published')
            self.assertEqual(data['message']['post_number'], number)
            self.assertEqual(self.api.calls[-1][1]['text'], f'Объявление\n\n№{number} — {role}')
            calls = len(self.api.calls)
            self.assertTrue(self.request('/api/publish', request)[1]['duplicate'])
            self.assertEqual(len(self.api.calls), calls)
            request['text'] = 'Changed text'
            self.assertEqual(self.request('/api/publish', request)[0], 400)
        self.assertEqual(self.request('/api/messages')[1]['stats']['published'], 3)

    def test_api_failure_and_unknown_delivery_are_recorded_without_retry(self):
        self.login()
        for code, expected in ((400, 'failed'), (0, 'unknown'), (500, 'unknown')):
            request = {'text': 'test', 'role': 'Moderator', 'request_id': str(uuid.uuid4())}
            self.api.failure = APIError(code)
            self.assertEqual(self.request('/api/publish', request)[1]['message']['status'], expected)
            count = len(self.api.calls)
            self.request('/api/publish', request)
            self.assertEqual(len(self.api.calls), count)

    def test_invalid_role_and_oversized_post_never_publish(self):
        self.login()
        for role, text in (('User', 'text'), ('Administrator', 'x' * 4096), ('Moderator', ' ')):
            payload = {'text': text, 'role': role, 'request_id': str(uuid.uuid4())}
            self.assertEqual(self.request('/api/publish', payload)[0], 400)
        self.assertFalse(self.api.calls)
        self.assertEqual(self.request('/api/messages')[1]['next_number'], 1)
        self.assertEqual(self.request('/api/messages')[1]['total'], 0)

    def test_media_is_authenticated_and_proxied_without_token(self):
        store = Store(self.path)
        try:
            entry = store.journal.incoming(incoming(1, '', photo=[{'file_id': 'photo-id'}]), None, 2000)
        finally:
            store.db.close()
        self.assertEqual(self.request(f'/api/messages/{entry}/media')[0], 401)
        self.login()
        with patch('dashboard.urlopen', return_value=io.BytesIO(b'jpg')):
            status, body, headers = self.request(f'/api/messages/{entry}/media')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'jpg')
        self.assertEqual(headers['Content-Type'], 'image/jpeg')
        self.assertNotIn(self.api.token, str(headers))

    def test_pagination(self):
        store = Store(self.path)
        try:
            for number in range(35):
                store.journal.incoming(incoming(number + 1), None, 2000)
        finally:
            store.db.close()
        self.login()
        self.assertEqual(len(self.request('/api/messages')[1]['items']), 30)
        self.assertEqual(len(self.request('/api/messages?page=2')[1]['items']), 5)
        self.assertEqual(self.request('/api/messages?status=invalid')[0], 400)


class JournalStoreTests(unittest.TestCase):
    def test_reserved_names_and_existing_profile_migration(self):
        for name in ('Administrator', 'moderator', ' ADMINISTRATOR ', 'Ｍｏｄｅｒａｔｏｒ'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_nickname(name)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'bot.sqlite3')
            store = Store(path)
            with store.db:
                store.db.execute("INSERT INTO profiles(user_id,nickname,nickname_key,nickname_changed_at) VALUES (1,'Administrator','administrator',1000)")
            store.db.close()
            store = Store(path)
            try:
                self.assertIsNone(store.profile(1)['nickname'])
                self.assertEqual(store.set_nickname(1, 'Free name', 1001), ('saved', 0))
            finally:
                store.db.close()

    def test_concurrent_admin_and_bot_number_reservations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'bot.sqlite3')
            Store(path).db.close()
            def reserve(index):
                store = Store(path)
                try:
                    if index % 2:
                        return store.next_post_number()
                    record, _ = store.journal.administrative(str(uuid.uuid4()), 'Administrator', 'test', 1000)
                    return record['post_number']
                finally:
                    store.db.close()
            with ThreadPoolExecutor(max_workers=4) as executor:
                numbers = list(executor.map(reserve, range(20)))
            self.assertEqual(sorted(numbers), list(range(1, 21)))

    def test_unknown_bot_publication_is_visible_in_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / 'bot.sqlite3'))
            api = FakeTelegram()
            api.failure = APIError(0)
            try:
                Bot(api, store, '@test_channel', lambda: 2000).handle(incoming(1))
                record = store.journal.list()['items'][0]
                self.assertEqual(record['status'], 'unknown')
                self.assertEqual(record['post_number'], 1)
                self.assertEqual(record['sender_id'], 42)
            finally:
                store.db.close()


if __name__ == '__main__':
    unittest.main()
