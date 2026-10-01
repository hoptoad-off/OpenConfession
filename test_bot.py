import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from bot import (APIError, Bot, Store, Telegram, main, NICKNAME_BUTTON,
                 CANCEL_BUTTON, NICKNAME_COOLDOWN, validate_nickname)


class StartupDiagnosticsTests(unittest.TestCase):
    def test_message_cooldown_configuration(self):
        for setting, expected in ((None, 15), ('30', 30), ('0', 0)):
            env = {'BOT_TOKEN': 'fake', 'CHANNEL_ID': '@channel', 'DATABASE_PATH': 'test.sqlite3'}
            if setting is not None:
                env['MESSAGE_COOLDOWN_SECONDS'] = setting
            with self.subTest(setting=setting), patch.dict('os.environ', env, clear=True), \
                 patch('bot.Store') as store, patch('bot.Telegram.call', side_effect=APIError(401)):
                with self.assertRaises(SystemExit):
                    main()
                store.assert_called_once_with('test.sqlite3', cooldown=expected)

    def test_invalid_cooldown_fails_before_database_or_network(self):
        for setting in ('-1', '1.5', 'abc', ''):
            env = {'BOT_TOKEN': 'fake', 'CHANNEL_ID': '@channel', 'MESSAGE_COOLDOWN_SECONDS': setting}
            with self.subTest(setting=setting), patch.dict('os.environ', env, clear=True), \
                 patch('bot.Store') as store, patch('bot.Telegram.call') as api:
                with self.assertRaisesRegex(SystemExit, 'MESSAGE_COOLDOWN_SECONDS'):
                    main()
                store.assert_not_called()
                api.assert_not_called()

    def test_http_error_preserves_description_and_redacts_token(self):
        token = 'test-secret-token'
        body = json.dumps({'ok': False, 'error_code': 400,
                           'description': 'Bad Request: ' + token,
                           'parameters': {'retry_after': 3}}).encode()
        error = HTTPError('https://example.invalid', 400, 'Bad Request', {}, io.BytesIO(body))
        with patch('bot.urlopen', side_effect=error):
            with self.assertRaises(APIError) as caught:
                Telegram(token).call('getChat', chat_id='invalid')
        error.close()
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(caught.exception.retry_after, 3)
        self.assertEqual(caught.exception.description, 'Bad Request: [REDACTED]')

    def test_startup_error_identifies_failed_request(self):
        cases = [
            ([{'id': 1}, {}, APIError(400, description='Bad Request: chat not found')],
             'getChat', 'CHANNEL_ID', 'chat not found'),
            ([{'id': 1}, {}, {'type': 'channel'},
              APIError(400, description='Bad Request: member list is inaccessible')],
             'getChatMember', 'администратором', 'member list is inaccessible'),
        ]
        for responses, method, hint, description in cases:
            with self.subTest(method=method), \
                 patch.dict('os.environ', {'BOT_TOKEN': 'fake', 'CHANNEL_ID': '@channel'}), \
                 patch('bot.Store'), patch('bot.Telegram.call', side_effect=responses):
                with self.assertRaises(SystemExit) as caught:
                    main()
            message = str(caught.exception)
            self.assertIn(method, message)
            self.assertIn(hint, message)
            self.assertIn(description, message)


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.failure = None

    def call(self, method, **params):
        self.calls.append((method, params))
        if params.get('chat_id') == '@channel' and self.failure:
            raise self.failure
        return {'message_id': len(self.calls)}


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
        return [params for method, params in self.api.calls if params.get('chat_id') == '@channel']

    def test_button_nickname_and_signed_publication(self):
        self.message(text='/start')
        self.assertEqual(self.api.calls[-1][1]['reply_markup']['keyboard'][0][0]['text'], NICKNAME_BUTTON)
        self.now += 2
        self.message(text=NICKNAME_BUTTON)
        self.message(text='Bill Edon')
        self.assertFalse(self.copies())
        self.assertFalse(self.store.profile(1)['awaiting_nickname'])
        self.message(text='Всем привет, как вы?')
        self.assertEqual(self.copies()[0]['text'], 'Всем привет, как вы?\n\n№1 — Bill Edon')
        self.assertNotIn('reply_markup', self.copies()[0])

    def test_nickname_length_unicode_and_plain_text(self):
        self.message(text=NICKNAME_BUTTON)
        self.message(text='Я' * 33)
        self.assertIsNone(self.store.profile(1)['nickname'])
        self.assertTrue(self.store.profile(1)['awaiting_nickname'])
        self.now += 2
        self.message(text='Я' * 32)
        self.assertEqual(self.store.profile(1)['nickname'], 'Я' * 32)
        for invalid in ('', '   ', 'a\nb', 'a\u202eb'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_nickname(invalid)
        self.assertEqual(validate_nickname('<Bill & Edon>')[0], '<Bill & Edon>')

    def test_taken_nickname_does_not_consume_change_or_publish(self):
        self.message(text='/nick Bill Edon')
        self.message(user=2, text=NICKNAME_BUTTON)
        self.message(user=2, text='bill edon')
        self.assertIn('занят', self.api.calls[-1][1]['text'])
        self.assertIsNone(self.store.profile(2)['nickname_changed_at'])
        self.assertTrue(self.store.profile(2)['awaiting_nickname'])
        self.message(user=2, text='Other')
        self.assertEqual(self.store.profile(2)['nickname'], 'Other')
        self.assertFalse(self.copies())

    def test_nickname_change_exact_24h_boundary(self):
        self.assertEqual(self.store.set_nickname(1, 'First', 0), ('saved', 0))
        self.assertEqual(self.store.set_nickname(1, 'Second', NICKNAME_COOLDOWN - .1), ('locked', 1))
        self.assertEqual(self.store.set_nickname(1, 'Second', NICKNAME_COOLDOWN), ('saved', 0))
        self.assertEqual(self.store.set_nickname(2, 'First', NICKNAME_COOLDOWN), ('saved', 0))

    def test_saving_same_nickname_does_not_extend_lock(self):
        self.store.set_nickname(1, 'Bill', 1000)
        self.assertEqual(self.store.set_nickname(1, 'Bill', 2000), ('unchanged', 0))
        self.assertEqual(self.store.profile(1)['nickname_changed_at'], 1000)

    def test_upgrade_existing_database_preserves_state(self):
        legacy_path = str(Path(self.temp.name) / 'legacy.sqlite3')
        db = sqlite3.connect(legacy_path)
        db.executescript('''
            CREATE TABLE users (id INTEGER PRIMARY KEY, next_allowed REAL NOT NULL DEFAULT 0,
                banned_until REAL NOT NULL DEFAULT 0, burst_second INTEGER NOT NULL DEFAULT -1,
                burst_count INTEGER NOT NULL DEFAULT 0, last_notice REAL NOT NULL DEFAULT 0);
            CREATE TABLE settings (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            INSERT INTO users(id, banned_until) VALUES (1, 9999);
            INSERT INTO settings VALUES ('offset', 123);
        ''')
        db.close()
        upgraded = Store(legacy_path)
        try:
            self.assertEqual(upgraded.offset(), 123)
            self.assertEqual(upgraded.check(1, 1000, 1000), ('banned', 8999))
            self.assertEqual(upgraded.set_nickname(1, 'Bill', 1000), ('saved', 0))
            self.assertEqual(upgraded.next_post_number(), 1)
        finally:
            upgraded.db.close()

    def test_taken_name_after_24h_preserves_old_name_and_timer(self):
        self.store.set_nickname(1, 'First', self.now)
        self.store.set_nickname(2, 'Second', self.now)
        self.now += NICKNAME_COOLDOWN
        self.message(text='/nick second')
        self.assertEqual(self.store.profile(1)['nickname'], 'First')
        self.message(text='Third')
        self.assertEqual(self.store.profile(1)['nickname'], 'Third')

    def test_nickname_key_normalizes_case_spaces_and_unicode(self):
        self.store.set_nickname(1, 'Bill Edon', self.now)
        for nickname in ('BILL EDON', 'bill  edon', 'Ｂｉｌｌ Ｅｄｏｎ'):
            self.assertEqual(self.store.set_nickname(2, nickname, self.now), ('taken', 0))
        self.store.set_nickname(3, 'Café', self.now)
        self.assertEqual(self.store.set_nickname(4, 'Cafe\u0301', self.now), ('taken', 0))

    def test_cancel_and_media_during_nickname_entry(self):
        self.message(text=NICKNAME_BUTTON)
        self.message(text='', photo=[{'file_id': 'x'}])
        self.assertTrue(self.store.profile(1)['awaiting_nickname'])
        self.assertFalse(self.copies())
        self.message(text=CANCEL_BUTTON)
        self.message(text='publish')
        self.assertEqual(self.copies()[0]['text'], 'publish\n\n№1')

    def test_nickname_button_works_during_publication_cooldown(self):
        self.message()
        self.message(text=NICKNAME_BUTTON)
        self.message(text='Bill')
        self.assertEqual(self.store.profile(1)['nickname'], 'Bill')
        self.assertEqual(len(self.copies()), 1)

    def test_locked_button_does_not_start_nickname_entry(self):
        self.message(text='/nick Bill')
        self.message(text=NICKNAME_BUTTON)
        self.assertIn('24 часа', self.api.calls[-1][1]['text'])
        self.assertIn('Осталось 24 ч. 0 мин.', self.api.calls[-1][1]['text'])
        self.assertFalse(self.store.profile(1)['awaiting_nickname'])
        self.now += 21 * 3600 + 47 * 60
        self.message(text='/nick Other')
        self.assertIn('Осталось 2 ч. 13 мин.', self.api.calls[-1][1]['text'])
        self.assertFalse(self.store.profile(1)['awaiting_nickname'])

    def test_profiles_numbers_and_pending_entry_survive_restart(self):
        self.store.set_nickname(1, 'Bill', self.now)
        self.store.await_nickname(2, True)
        self.message()
        self.store.db.close()
        self.store = Store(self.path)
        self.bot = Bot(self.api, self.store, '@channel', lambda: self.now)
        self.assertTrue(self.store.profile(2)['awaiting_nickname'])
        self.assertEqual(self.store.set_nickname(2, 'BILL', self.now), ('taken', 0))
        self.assertEqual(self.store.nickname_wait(1, self.now), NICKNAME_COOLDOWN)
        self.now += 15
        self.message()
        self.assertTrue(self.copies()[-1]['text'].endswith('№2 — Bill'))

    def test_uncertain_delivery_never_reuses_number(self):
        self.api.failure = APIError(0)
        self.message()
        self.api.failure = None
        self.now += 15
        self.message()
        self.assertTrue(self.copies()[0]['text'].endswith('№1'))
        self.assertTrue(self.copies()[1]['text'].endswith('№2'))

    def test_text_and_media_preserve_entities_before_footer(self):
        self.store.set_nickname(1, '<Bill & Edon>', self.now)
        entities = [{'type': 'bold', 'offset': 0, 'length': 2}]
        self.message(text='😀 hello', entities=entities)
        self.assertEqual(self.copies()[-1]['entities'], entities)
        self.assertTrue(self.copies()[-1]['text'].endswith('№1 — <Bill & Edon>'))
        self.assertNotIn('parse_mode', self.copies()[-1])
        self.now += 15
        self.message(text='', photo=[{'file_id': 'x'}], caption='😀 photo', caption_entities=entities)
        self.assertEqual(self.copies()[-1]['caption'], '😀 photo\n\n№2 — <Bill & Edon>')
        self.assertEqual(self.copies()[-1]['caption_entities'], entities)
        self.assertFalse(self.copies()[-1]['show_caption_above_media'])

    def test_supported_caption_media_all_get_footer(self):
        for kind in ('photo', 'video', 'animation', 'audio', 'voice', 'document'):
            with self.subTest(kind=kind):
                self.message(text='', **{kind: {'file_id': 'x'}})
                self.assertRegex(self.copies()[-1]['caption'], r'^№[0-9]+$')
                self.now += 15

    def test_sticker_gets_separate_footer_reply(self):
        self.message(text='', sticker={'file_id': 'x'})
        channel_calls = [(method, params) for method, params in self.api.calls
                         if params.get('chat_id') == '@channel']
        self.assertEqual(channel_calls[0][0], 'copyMessage')
        self.assertEqual(channel_calls[1][1]['text'], '№1')
        self.assertEqual(channel_calls[1][1]['reply_parameters'], {'message_id': 1})

    def test_footer_failure_after_media_keeps_cooldown(self):
        original = self.api.call
        def call(method, **params):
            if method == 'sendMessage' and params.get('chat_id') == '@channel':
                raise APIError(429)
            return original(method, **params)
        with patch.object(self.api, 'call', side_effect=call):
            self.message(text='', sticker={'file_id': 'x'})
        self.assertIn('Вложение опубликовано', self.api.calls[-1][1]['text'])
        self.assertEqual(self.store.check(1, 1001, 1001)[0], 'cooldown')

    def test_long_text_and_caption_rejected_without_truncation(self):
        for message in ({'text': 'x' * 4096}, {'text': '😀' * 2048},
                        {'text': '', 'photo': [{'file_id': 'x'}], 'caption': 'x' * 1024}):
            with self.subTest(message_type='text' if message['text'] else 'photo'):
                self.message(**message)
                self.assertFalse(self.copies())
                self.assertIn('слишком длинное', self.api.calls[-1][1]['text'])
                self.now += 2
        self.message(text='Short')
        self.assertTrue(self.copies()[0]['text'].endswith('№1'))

    def test_signed_text_and_caption_at_exact_limits(self):
        footer = '\n\n№1'
        self.message(text='x' * (4096 - len(footer)))
        self.assertEqual(len(self.copies()[0]['text']), 4096)
        self.now += 15
        self.message(text='', photo=[{'file_id': 'x'}], caption='x' * (1024 - len(footer)))
        self.assertEqual(len(self.copies()[-1]['caption']), 1024)

    def test_publish_and_exact_cooldown_boundary(self):
        self.message()
        self.assertEqual(self.api.calls[-1][1]['text'], 'Сообщение №1 опубликовано!')
        self.now = 1014.1
        self.message()
        self.assertEqual(len(self.copies()), 1)
        self.assertIn('1 сек.', self.api.calls[-1][1]['text'])
        self.assertEqual(self.api.calls[-1][1]['text'], 'Подождите 1 сек. перед следующей отправкой.')
        self.now = 1015
        self.message()
        self.assertEqual(len(self.copies()), 2)

    def test_custom_cooldown_and_help(self):
        self.store.db.close()
        self.store = Store(self.path, cooldown=30)
        self.bot = Bot(self.api, self.store, '@channel', lambda: self.now)
        self.message(text='/start')
        self.assertIn('Интервал: 30 сек.', self.api.calls[-1][1]['text'])
        self.message()
        self.now += 29.1
        self.message()
        self.assertEqual(len(self.copies()), 1)
        self.assertEqual(self.api.calls[-1][1]['text'], 'Подождите 1 сек. перед следующей отправкой.')
        self.now = 1030
        self.message()
        self.assertEqual(len(self.copies()), 2)

    def test_zero_cooldown_still_enforces_spam_ban(self):
        self.store.db.close()
        self.store = Store(self.path, cooldown=0)
        self.bot = Bot(self.api, self.store, '@channel', lambda: self.now)
        for _ in range(5):
            self.message()
        self.assertEqual(len(self.copies()), 4)
        self.assertIn('Предупреждение за спам', self.api.calls[-1][1]['text'])
        for _ in range(5):
            self.message()
        self.assertEqual(len(self.copies()), 4)
        self.assertEqual(self.store.check(1, 1000, self.now)[0], 'banned')

    def test_burst_bans_even_rejected_messages_and_expires(self):
        for _ in range(10):
            self.message()
        self.assertIn('Осталось 1 ч. 0 мин.', self.api.calls[-1][1]['text'])
        self.assertEqual(self.store.check(1, 1000, 1000), ('banned', 3600))
        self.now = 4599
        self.message()
        self.assertIn('Осталось 0 ч. 1 мин.', self.api.calls[-1][1]['text'])
        self.assertEqual(len(self.copies()), 1)
        self.now = 4600
        self.message()
        self.assertEqual(len(self.copies()), 2)

    def test_slow_api_does_not_hide_source_burst(self):
        for i in range(5):
            self.now = 1000 + i * 2
            self.message(date=1000)
        self.assertIn('Предупреждение за спам', self.api.calls[-1][1]['text'])
        for i in range(5):
            self.now = 1010 + i * 2
            self.message(date=1001)
        self.assertEqual(self.store.check(1, 1001, self.now)[0], 'banned')

    def test_users_are_independent(self):
        for _ in range(10):
            self.message()
        self.message(user=2)
        self.assertEqual(len(self.copies()), 2)

    def test_ban_cooldown_and_offset_survive_restart(self):
        self.message(user=2)
        for _ in range(10):
            self.message()
        self.store.acknowledge(42)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.offset(), 43)
        self.assertEqual(self.store.check(1, 1001, 1001), ('banned', 3599))
        self.assertEqual(self.store.check(2, 1001, 1001), ('cooldown', 14))

    def test_command_spam_is_counted(self):
        for _ in range(10):
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
        self.assertLessEqual(len(self.api.calls), 5)

    def test_first_burst_warns_and_pause_expires_after_five_seconds(self):
        for _ in range(5):
            result = self.store.check(1, 1000, 1000)
        self.assertEqual(result, ('warning', 5))
        # Attempt six is not itself a second burst and must not extend the pause.
        self.assertEqual(self.store.check(1, 1000, 1000), ('spam_cooldown', 5))
        self.assertEqual(self.store.check(1, 1004, 1004.1), ('spam_cooldown', 1))
        self.assertEqual(self.store.check(1, 1005, 1005), ('allowed', 0))

    def test_second_burst_inside_window_bans_but_exact_expiry_warns(self):
        for user, second_time, expected in ((1, 2799.9, ('new_ban', 3600)),
                                             (2, 2800, ('warning', 5))):
            with self.subTest(user=user):
                for _ in range(5):
                    self.store.check(user, 1000, 1000)
                for _ in range(5):
                    result = self.store.check(user, int(second_time), second_time)
                self.assertEqual(result, expected)

    def test_warning_and_pause_survive_restart(self):
        for _ in range(5):
            self.store.check(1, 1000, 1000)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.check(1, 1001, 1001), ('spam_cooldown', 4))
        self.assertEqual(self.store.check(1, 1005, 1005), ('allowed', 0))
        for _ in range(5):
            result = self.store.check(1, 1100, 1100)
        self.assertEqual(result, ('new_ban', 3600))

    def test_warning_does_not_shorten_normal_cooldown(self):
        for _ in range(5):
            self.message()
        self.now = 1005
        self.message()
        self.assertEqual(len(self.copies()), 1)
        self.assertEqual(self.api.calls[-1][1]['text'], 'Подождите 10 сек. перед следующей отправкой.')
        self.now = 1015
        self.message()
        self.assertEqual(len(self.copies()), 2)

    def test_spam_pause_blocks_nickname_commands_without_changing_profile(self):
        for _ in range(5):
            self.message(text='/start')
        self.now += 2
        self.message(text='/nick Bill')
        self.assertIsNone(self.store.profile(1)['nickname'])
        self.assertIn('Подождите 3 сек.', self.api.calls[-1][1]['text'])
        self.now += 3
        self.message(text='/nick Bill')
        self.assertEqual(self.store.profile(1)['nickname'], 'Bill')

    def test_attempts_during_ban_do_not_extend_it(self):
        for _ in range(10):
            self.store.check(1, 1000, 1000)
        for _ in range(10):
            self.assertEqual(self.store.check(1, 4599, 4599), ('banned', 1))
        self.assertEqual(self.store.check(1, 4600, 4600), ('allowed', 0))
        for _ in range(5):
            result = self.store.check(1, 4601, 4601)
        self.assertEqual(result, ('warning', 5))


if __name__ == '__main__':
    unittest.main()
