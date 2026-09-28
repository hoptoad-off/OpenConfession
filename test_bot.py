import tempfile
import unittest
from pathlib import Path

from bot import APIError, Bot, Store


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.failure = None

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == 'copyMessage' and self.failure:
            raise self.failure
        return {}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'state.sqlite3')
        self.store = Store(self.path)
        self.api = FakeAPI()
        self.now = 1000.0
        self.bot = Bot(self.api, self.store, '@channel', lambda: self.now)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def message(self, user=1, **extra):
        message = {'from': {'id': user}, 'chat': {'id': user, 'type': 'private'},
                   'message_id': 10, 'date': int(self.now), 'text': 'Признание'}
        message.update(extra)
        self.bot.handle({'message': message})

    def copies(self):
        return [params for method, params in self.api.calls if method == 'copyMessage']

    def test_publish_and_exact_cooldown_boundary(self):
        self.message()
        self.now = 1014.1
        self.message()
        self.assertEqual(len(self.copies()), 1)
        self.assertIn('1 сек.', self.api.calls[-1][1]['text'])
        self.now = 1015
        self.message()
        self.assertEqual(len(self.copies()), 2)

    def test_burst_bans_even_rejected_messages_and_expires(self):
        for _ in range(5):
            self.message()
        self.assertEqual(self.store.check(1, 1000, 1000), ('banned', 3600))
        self.now = 4599
        self.message()
        self.assertEqual(len(self.copies()), 1)
        self.now = 4600
        self.message()
        self.assertEqual(len(self.copies()), 2)

    def test_slow_api_does_not_hide_source_burst(self):
        for i in range(5):
            self.now = 1000 + i * 2
            self.message(date=1000)
        self.assertEqual(self.store.check(1, 1001, self.now)[0], 'banned')

    def test_users_are_independent(self):
        for _ in range(5):
            self.message()
        self.message(user=2)
        self.assertEqual(len(self.copies()), 2)

    def test_ban_cooldown_and_offset_survive_restart(self):
        self.message(user=2)
        for _ in range(5):
            self.message()
        self.store.acknowledge(42)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.offset(), 43)
        self.assertEqual(self.store.check(1, 1001, 1001), ('banned', 3599))
        self.assertEqual(self.store.check(2, 1001, 1001), ('cooldown', 14))

    def test_command_spam_is_counted(self):
        for _ in range(5):
            self.message(text='/start')
        self.assertFalse(self.copies())
        self.assertEqual(self.store.check(1, 1001, 1001)[0], 'banned')

    def test_group_and_edited_messages_ignored(self):
        self.message(chat={'id': -1, 'type': 'group'})
        self.bot.handle({'edited_message': {'text': 'edit'}})
        self.assertFalse(self.copies())

    def test_failed_publication_does_not_start_cooldown(self):
        self.api.failure = APIError(400)
        self.message()
        self.assertEqual(self.store.check(1, 1001, 1001)[0], 'allowed')

    def test_unknown_delivery_starts_cooldown_without_retry(self):
        self.api.failure = APIError(0)
        self.message()
        self.assertEqual(len(self.copies()), 1)
        self.assertEqual(self.store.check(1, 1001, 1001)[0], 'cooldown')

    def test_album_is_not_partially_published(self):
        self.message(media_group_id='album')
        self.assertFalse(self.copies())

    def test_blocked_flood_does_not_create_reply_flood(self):
        for _ in range(100):
            self.message()
        self.assertLessEqual(len(self.api.calls), 4)


if __name__ == '__main__':
    unittest.main()
