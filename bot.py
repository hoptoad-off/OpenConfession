"""OpenConfession: single-process Telegram publisher, Python 3.11+."""

import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from http.client import HTTPException
from journal import Journal

DEFAULT_MESSAGE_COOLDOWN = 15
BURST_LIMIT = 5
SPAM_PAUSE_SECONDS = 5
SPAM_WARNING_SECONDS = 30 * 60
BAN_SECONDS = 3600
NICKNAME_COOLDOWN = 24 * 60 * 60
NICKNAME_BUTTON = '✏️ Изменить ник'
CANCEL_BUTTON = 'Отмена'
MAIN_KEYBOARD = {'keyboard': [[{'text': NICKNAME_BUTTON}]],
                 'resize_keyboard': True, 'is_persistent': True}
CANCEL_KEYBOARD = {'keyboard': [[{'text': CANCEL_BUTTON}]], 'resize_keyboard': True}
LOG = logging.getLogger(__name__)
RESERVED_NICKNAMES = {'administrator', 'moderator'}


def validate_nickname(value):
    nickname = unicodedata.normalize('NFC', value.strip())
    if not 1 <= len(nickname) <= 32 or not nickname.isprintable():
        raise ValueError('Ник должен содержать от 1 до 32 символов, без переносов строк и скрытых символов.')
    key = ' '.join(unicodedata.normalize('NFKC', nickname).casefold().split())
    if key in RESERVED_NICKNAMES:
        raise ValueError('Ники Administrator и Moderator доступны только администрации.')
    return nickname, key


def telegram_length(value):
    return len(value.encode('utf-16-le')) // 2


def hours_and_minutes(seconds):
    hours, minutes = divmod(math.ceil(max(0, seconds) / 60), 60)
    return f'{hours} ч. {minutes} мин.'


class Store:
    def __init__(self, path, cooldown=DEFAULT_MESSAGE_COOLDOWN):
        self.cooldown = cooldown
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                next_allowed REAL NOT NULL DEFAULT 0,
                banned_until REAL NOT NULL DEFAULT 0,
                burst_second INTEGER NOT NULL DEFAULT -1,
                burst_count INTEGER NOT NULL DEFAULT 0,
                last_notice REAL NOT NULL DEFAULT 0,
                spam_warned_until REAL NOT NULL DEFAULT 0,
                spam_pause_until REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                nickname TEXT,
                nickname_key TEXT UNIQUE,
                nickname_changed_at REAL,
                awaiting_nickname INTEGER NOT NULL DEFAULT 0
            );
        """)
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(users)')}
        with self.db:
            for column in ('spam_warned_until', 'spam_pause_until'):
                if column not in columns:
                    self.db.execute(f'ALTER TABLE users ADD COLUMN {column} REAL NOT NULL DEFAULT 0')
            self.db.execute('''UPDATE profiles SET nickname=NULL, nickname_key=NULL,
                               nickname_changed_at=NULL, awaiting_nickname=0
                               WHERE nickname_key IN ('administrator', 'moderator')''')
        self.journal = Journal(self.db)

    def profile(self, user_id):
        row = self.db.execute('SELECT * FROM profiles WHERE user_id=?', (user_id,)).fetchone()
        return row if row is not None else {
            'nickname': None, 'nickname_changed_at': None, 'awaiting_nickname': 0}

    def nickname_wait(self, user_id, now):
        changed_at = self.profile(user_id)['nickname_changed_at']
        return max(0, math.ceil(changed_at + NICKNAME_COOLDOWN - now)) if changed_at is not None else 0

    def await_nickname(self, user_id, waiting):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO profiles(user_id) VALUES (?)', (user_id,))
            self.db.execute('UPDATE profiles SET awaiting_nickname=? WHERE user_id=?',
                            (int(waiting), user_id))

    def set_nickname(self, user_id, value, now):
        nickname, key = validate_nickname(value)
        try:
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO profiles(user_id) VALUES (?)', (user_id,))
                if self.profile(user_id)['nickname'] == nickname:
                    self.db.execute('UPDATE profiles SET awaiting_nickname=0 WHERE user_id=?', (user_id,))
                    return 'unchanged', 0
                wait = self.nickname_wait(user_id, now)
                if wait:
                    return 'locked', wait
                self.db.execute('''UPDATE profiles SET nickname=?, nickname_key=?,
                                   nickname_changed_at=?, awaiting_nickname=0 WHERE user_id=?''',
                                (nickname, key, now, user_id))
                return 'saved', 0
        except sqlite3.IntegrityError:
            return 'taken', 0

    def next_post_number(self, reserve=True):
        if not reserve:
            row = self.db.execute("SELECT value FROM settings WHERE key='post_number'").fetchone()
            return row['value'] + 1 if row else 1
        # Reserve before contacting Telegram: uncertain deliveries must not reuse a number.
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('post_number', 0)")
            self.db.execute("UPDATE settings SET value=value+1 WHERE key='post_number'")
            return self.db.execute("SELECT value FROM settings WHERE key='post_number'").fetchone()['value']

    def check(self, user_id, sent_second, now):
        """Count all incoming attempts, including commands and rejected messages.

        Telegram dates have second precision. Use the source timestamp so slow
        outgoing API calls do not hide a burst already queued at Telegram.
        """
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (user_id,))
            row = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if row['banned_until'] > now:
                return 'banned', math.ceil(row['banned_until'] - now)
            count = row['burst_count'] + 1 if sent_second == row['burst_second'] else 1
            if count >= BURST_LIMIT:
                # Start a fresh group of attempts; attempt six alone must not cause a ban.
                if row['spam_warned_until'] > now:
                    self.db.execute('''UPDATE users SET burst_second=?, burst_count=0,
                                       banned_until=?, spam_warned_until=0, spam_pause_until=0 WHERE id=?''',
                                    (sent_second, now + BAN_SECONDS, user_id))
                    return 'new_ban', BAN_SECONDS
                self.db.execute('''UPDATE users SET burst_second=?, burst_count=0, banned_until=0,
                                   spam_warned_until=?, spam_pause_until=? WHERE id=?''',
                                (sent_second, now + SPAM_WARNING_SECONDS, now + SPAM_PAUSE_SECONDS, user_id))
                return 'warning', SPAM_PAUSE_SECONDS
            self.db.execute(
                "UPDATE users SET burst_second=?, burst_count=?, banned_until=0 WHERE id=?",
                (sent_second, count, user_id),
            )
            if row['spam_pause_until'] > now:
                return 'spam_cooldown', math.ceil(row['spam_pause_until'] - now)
            if row['next_allowed'] > now:
                return 'cooldown', math.ceil(row['next_allowed'] - now)
            return 'allowed', 0

    def published(self, user_id, now):
        with self.db:
            self.db.execute("UPDATE users SET next_allowed=? WHERE id=?", (now + self.cooldown, user_id))

    def notice_allowed(self, user_id, now, force=False):
        with self.db:
            row = self.db.execute("SELECT last_notice FROM users WHERE id=?", (user_id,)).fetchone()
            if not force and now - row['last_notice'] < 2:
                return False
            self.db.execute("UPDATE users SET last_notice=? WHERE id=?", (now, user_id))
            return True

    def offset(self):
        row = self.db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()
        return row['value'] if row else 0

    def acknowledge(self, update_id):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('offset', ?)", (update_id + 1,))


class APIError(Exception):
    def __init__(self, code, retry_after=0, description=''):
        super().__init__(f'Telegram API error {code}')
        self.code = code
        self.retry_after = retry_after
        self.description = description


class Telegram:
    def __init__(self, token):
        self.token = token
        self.base = f'https://api.telegram.org/bot{token}/'

    def call(self, method, **params):
        request = Request(self.base + method, data=json.dumps(params).encode(),
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=40) as response:
                result = json.load(response)
        except HTTPError as error:
            try:
                result = json.load(error)
            except (ValueError, OSError, HTTPException):
                raise APIError(error.code) from None
        except (URLError, TimeoutError, OSError, HTTPException, ValueError):
            # Do not log the request URL: it contains the bot token.
            raise APIError(0) from None
        if not isinstance(result, dict) or (result.get('ok') and 'result' not in result):
            raise APIError(0)
        if not result.get('ok'):
            raise APIError(result.get('error_code', 0),
                           result.get('parameters', {}).get('retry_after', 0),
                           str(result.get('description', '')).replace(self.token, '[REDACTED]'))
        return result['result']


class Bot:
    def __init__(self, api, store, channel, clock=time.time):
        self.api, self.store, self.channel, self.clock = api, store, channel, clock

    def reply(self, chat_id, text, keyboard=None):
        try:
            self.api.call('sendMessage', chat_id=chat_id, text=text,
                          reply_markup=MAIN_KEYBOARD if keyboard is None else keyboard)
        except APIError as error:
            LOG.warning('Could not send reply (code %s)', error.code)
            if error.retry_after:
                time.sleep(error.retry_after)

    def choose_nickname(self, user_id, chat_id, value=None):
        now = self.clock()
        if value is None:
            wait = self.store.nickname_wait(user_id, now)
            if wait:
                self.reply(chat_id, f'Ник можно менять раз в 24 часа. Осталось {hours_and_minutes(wait)}')
                return
            self.store.await_nickname(user_id, True)
            nickname = self.store.profile(user_id)['nickname']
            current = f'Сейчас ваш ник: {nickname}.' if nickname else 'Ник пока не задан.'
            self.reply(chat_id, f'{current}\nВведите новый ник — от 1 до 32 символов. '
                       'Он будет виден под публикациями. Следующая смена — через 24 часа. '
                       'Это сообщение не будет опубликовано.', CANCEL_KEYBOARD)
            return
        self.store.await_nickname(user_id, True)
        try:
            status, wait = self.store.set_nickname(user_id, value, now)
        except ValueError as error:
            self.reply(chat_id, str(error) + ' Введите другой ник или нажмите «Отмена».', CANCEL_KEYBOARD)
            return
        if status == 'taken':
            self.reply(chat_id, 'Этот ник уже занят. Введите другой ник или нажмите «Отмена».', CANCEL_KEYBOARD)
        elif status == 'locked':
            self.store.await_nickname(user_id, False)
            self.reply(chat_id, f'Ник можно менять раз в 24 часа. Осталось {hours_and_minutes(wait)}')
        else:
            nickname = self.store.profile(user_id)['nickname']
            self.reply(chat_id, f'Ваш ник: {nickname}. Теперь отправьте сообщение для публикации. '
                       'Сменить ник можно раз в 24 часа.')

    def handle(self, update):
        message = update.get('message')
        if not message or message['chat']['type'] != 'private':
            return
        sender = message.get('from', {})
        if not sender.get('id') or sender.get('is_bot'):
            return
        entry = self.store.journal.incoming(update, self.store.profile(sender['id'])['nickname'], self.clock())
        if entry is None:
            return
        try:
            self.handle_logged(message, entry)
        except Exception:
            record = self.store.journal.get(entry)
            if record['status'] not in ('published', 'partial'):
                self.store.journal.finish(entry, 'unknown' if record['status'] == 'sending' else 'failed',
                                          'Обработка прервана. Результат требует проверки.')
            raise

    def handle_logged(self, message, entry):
        sender = message['from']
        user_id, chat_id = sender['id'], message['chat']['id']
        now = self.clock()
        status, wait = self.store.check(user_id, message['date'], now)
        if status in ('warning', 'spam_cooldown'):
            self.store.journal.finish(entry, 'warning' if status == 'warning' else 'rejected', status)
            if self.store.notice_allowed(user_id, now, force=status == 'warning'):
                text = (f'Предупреждение за спам. Подождите {wait} сек. '
                        'Повторный спам в течение 30 минут приведёт к блокировке на 1 час.'
                        if status == 'warning' else
                        f'Подождите {wait} сек. перед следующей отправкой.')
                self.reply(chat_id, text)
            return
        if status in ('banned', 'new_ban'):
            self.store.journal.finish(entry, 'banned', status)
            if self.store.notice_allowed(user_id, now, force=status == 'new_ban'):
                text = (f'Отправка заблокирована за спам. Осталось {hours_and_minutes(wait)} '
                        'Блокировка снимается автоматически.')
                self.reply(chat_id, text)
            return
        text = message.get('text', '')
        command = text.split(maxsplit=1)[0].split('@', 1)[0].lower() if text.startswith('/') else ''
        if text == CANCEL_BUTTON or command == '/cancel':
            self.store.journal.finish(entry, 'profile', 'Отмена ввода ника')
            self.store.await_nickname(user_id, False)
            self.reply(chat_id, 'Ввод ника отменён. Можно отправить сообщение для публикации.')
            return
        if text == NICKNAME_BUTTON or command == '/nick':
            self.store.journal.finish(entry, 'profile', 'Настройка ника')
            parts = text.split(maxsplit=1)
            value = parts[1] if command == '/nick' and len(parts) > 1 else None
            self.choose_nickname(user_id, chat_id, value)
            return
        if text.startswith('/'):
            self.store.journal.finish(entry, 'command', command)
            self.store.await_nickname(user_id, False)
            if self.store.notice_allowed(user_id, now):
                self.reply(chat_id, 'Пришлите сообщение — я опубликую его в канале без модерации. '
                           f'Интервал: {self.store.cooldown} сек. За 5 сообщений в одну секунду — '
                           'предупреждение и пауза 5 сек. Повторный спам в течение 30 минут — блокировка на час. '
                           'Нажмите «✏️ Изменить ник» или отправьте /nick Ваш ник. '
                           'Ник: 1–32 символа, должен быть свободен; смена раз в 24 часа. '
                           'Без своего ника в подписи будет только номер сообщения. '
                           'Сообщения и данные отправителя доступны администрации в журнале. '
                           'Команды не публикуются. Альбомы отправляйте отдельными файлами.')
            return
        if self.store.profile(user_id)['awaiting_nickname']:
            self.store.journal.finish(entry, 'profile', 'Ввод ника')
            if not text or message.get('media_group_id'):
                self.reply(chat_id, 'Пришлите ник обычным текстом или нажмите «Отмена».', CANCEL_KEYBOARD)
            else:
                self.choose_nickname(user_id, chat_id, text)
            return
        if status == 'cooldown':
            self.store.journal.finish(entry, 'rejected', 'cooldown')
            if self.store.notice_allowed(user_id, now):
                self.reply(chat_id, f'Подождите {wait} сек. перед следующей отправкой.')
            return
        if message.get('media_group_id'):
            self.store.journal.finish(entry, 'rejected', 'Альбомы не поддерживаются')
            if self.store.notice_allowed(user_id, now):
                self.reply(chat_id, 'Альбомы пока не поддерживаются. Отправьте один файл с подписью.')
            return
        nickname = self.store.profile(user_id)['nickname']
        number = self.store.next_post_number(reserve=False)
        footer = f'№{number} — {nickname}' if nickname else f'№{number}'
        captionable = any(kind in message for kind in ('photo', 'video', 'animation', 'audio', 'voice', 'document'))
        content = text if text else message.get('caption', '')
        signed = f'{content}\n\n{footer}' if content else footer
        if (text or captionable) and telegram_length(signed) > (4096 if text else 1024):
            self.store.journal.finish(entry, 'rejected', 'Слишком длинное сообщение')
            limit = (4096 if text else 1024) - telegram_length('\n\n' + footer)
            self.reply(chat_id, f'Сообщение с подписью слишком длинное. Сократите текст до {limit} '
                       'символов (эмодзи могут занимать два символа) и отправьте заново.')
            return
        # Dashboard publications use the same counter: reserve atomically and rebuild the footer.
        number = self.store.next_post_number()
        footer = f'№{number} — {nickname}' if nickname else f'№{number}'
        signed = f'{content}\n\n{footer}' if content else footer
        if (text or captionable) and telegram_length(signed) > (4096 if text else 1024):
            self.store.journal.finish(entry, 'rejected', 'Слишком длинное сообщение')
            self.reply(chat_id, 'Сообщение с подписью слишком длинное. Сократите текст и отправьте заново.')
            return
        self.store.journal.finish(entry, 'sending', post_number=number)
        media_published = False
        channel_message_id = None
        try:
            if text:
                result = self.api.call('sendMessage', chat_id=self.channel, text=signed,
                              entities=message.get('entities', []),
                              link_preview_options={'is_disabled': True})
                channel_message_id = result['message_id']
            elif captionable:
                result = self.api.call('copyMessage', chat_id=self.channel,
                              from_chat_id=chat_id, message_id=message['message_id'],
                              caption=signed, caption_entities=message.get('caption_entities', []),
                              show_caption_above_media=False)
                channel_message_id = result['message_id']
            else:
                copied = self.api.call('copyMessage', chat_id=self.channel,
                                       from_chat_id=chat_id, message_id=message['message_id'])
                media_published = True
                channel_message_id = copied['message_id']
                self.store.published(user_id, self.clock())
                self.api.call('sendMessage', chat_id=self.channel, text=footer,
                              reply_parameters={'message_id': copied['message_id']})
        except APIError as error:
            delivery = 'partial' if media_published else 'unknown' if error.code == 0 or error.code >= 500 else 'failed'
            self.store.journal.finish(entry, delivery, f'Telegram API: {error.code}',
                                      channel_message_id=channel_message_id)
            LOG.warning('Publication failed (code %s)', error.code)
            if media_published:
                self.reply(chat_id, 'Вложение опубликовано, но не удалось подтвердить отправку подписи. '
                           'Проверьте канал; не отправляйте вложение повторно.')
            elif error.code == 0 or error.code >= 500:
                self.store.published(user_id, self.clock())
                self.reply(chat_id, 'Не удалось подтвердить публикацию из-за сбоя связи. '
                           'Проверьте канал перед повторной отправкой.')
            else:
                self.reply(chat_id, 'Не удалось опубликовать сообщение. Попробуйте позже '
                           'или отправьте обычный текст.')
            if error.retry_after:
                time.sleep(error.retry_after)
            return
        self.store.journal.finish(entry, 'published', channel_message_id=channel_message_id)
        self.store.published(user_id, self.clock())
        self.reply(chat_id, f'Сообщение №{number} опубликовано!')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token, channel = os.environ.get('BOT_TOKEN'), os.environ.get('CHANNEL_ID')
    if not token or not channel:
        raise SystemExit('Задайте BOT_TOKEN и CHANNEL_ID в переменных окружения.')
    try:
        cooldown = int(os.environ.get('MESSAGE_COOLDOWN_SECONDS', str(DEFAULT_MESSAGE_COOLDOWN)))
        if cooldown < 0:
            raise ValueError
    except ValueError:
        raise SystemExit('MESSAGE_COOLDOWN_SECONDS должен быть целым числом секунд не меньше 0.') from None
    store = Store(os.environ.get('DATABASE_PATH', 'data/bot.sqlite3'), cooldown=cooldown)
    api = Telegram(token)
    stage = 'getMe'
    try:
        me = api.call('getMe')
        stage = 'getWebhookInfo'
        hook = api.call('getWebhookInfo')
        if hook.get('url'):
            raise SystemExit('У бота включён webhook. Отключите его перед запуском polling.')
        stage = 'getChat'
        chat = api.call('getChat', chat_id=channel)
        stage = 'getChatMember'
        member = api.call('getChatMember', chat_id=channel, user_id=me['id'])
        if chat['type'] != 'channel' or not member.get('can_post_messages'):
            raise SystemExit('Добавьте бота администратором канала с правом публикации.')
    except APIError as error:
        detail = f' {error.description}' if error.description else ''
        hint = ''
        if stage == 'getChat':
            hint = ' Проверьте CHANNEL_ID: @имя_канала или числовой ID -100…; бот должен быть добавлен в канал.'
        elif stage == 'getChatMember':
            hint = ' Добавьте именно этого бота администратором канала с правом публикации.'
        raise SystemExit(f'Не удалось проверить настройки ({stage}): код {error.code}.{detail}{hint}') from None
    bot = Bot(api, store, channel)
    LOG.info('Bot started')
    while True:
        store.journal.heartbeat()
        try:
            updates = api.call('getUpdates', offset=store.offset(), timeout=30,
                               allowed_updates=['message'])
        except APIError as error:
            if error.code in (401, 409):
                raise SystemExit(f'Polling остановлен: код {error.code}. Проверьте токен и другие экземпляры.') from None
            LOG.warning('Polling failed (code %s)', error.code)
            time.sleep(max(5, error.retry_after))
            continue
        for update in updates:
            # Persist BEFORE sending: a crash must not repost the same confession.
            # This deliberately prefers possible loss over automatic duplicates.
            store.acknowledge(update['update_id'])
            bot.handle(update)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
