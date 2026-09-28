"""OpenConfession: single-process Telegram publisher, Python 3.11+."""

import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

COOLDOWN = 15
BURST_LIMIT = 5
BAN_SECONDS = 3600
LOG = logging.getLogger(__name__)


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                next_allowed REAL NOT NULL DEFAULT 0,
                banned_until REAL NOT NULL DEFAULT 0,
                burst_second INTEGER NOT NULL DEFAULT -1,
                burst_count INTEGER NOT NULL DEFAULT 0,
                last_notice REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value INTEGER NOT NULL
            );
        """)

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
            banned_until = now + BAN_SECONDS if count >= BURST_LIMIT else 0
            self.db.execute(
                "UPDATE users SET burst_second=?, burst_count=?, banned_until=? WHERE id=?",
                (sent_second, count, banned_until, user_id),
            )
            if banned_until:
                return 'new_ban', BAN_SECONDS
            if row['next_allowed'] > now:
                return 'cooldown', math.ceil(row['next_allowed'] - now)
            return 'allowed', 0

    def published(self, user_id, now):
        with self.db:
            self.db.execute("UPDATE users SET next_allowed=? WHERE id=?", (now + COOLDOWN, user_id))

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
    def __init__(self, code, retry_after=0):
        super().__init__(f'Telegram API error {code}')
        self.code = code
        self.retry_after = retry_after


class Telegram:
    def __init__(self, token):
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
            except (ValueError, OSError):
                raise APIError(error.code) from None
        except (URLError, TimeoutError, OSError):
            # Do not log the request URL: it contains the bot token.
            raise APIError(0) from None
        if not result.get('ok'):
            raise APIError(result.get('error_code', 0),
                           result.get('parameters', {}).get('retry_after', 0))
        return result['result']


class Bot:
    def __init__(self, api, store, channel, clock=time.time):
        self.api, self.store, self.channel, self.clock = api, store, channel, clock

    def reply(self, chat_id, text):
        try:
            self.api.call('sendMessage', chat_id=chat_id, text=text)
        except APIError as error:
            LOG.warning('Could not send reply (code %s)', error.code)
            if error.retry_after:
                time.sleep(error.retry_after)

    def handle(self, update):
        message = update.get('message')
        if not message or message['chat']['type'] != 'private':
            return
        sender = message.get('from', {})
        if not sender.get('id') or sender.get('is_bot'):
            return
        user_id, chat_id = sender['id'], message['chat']['id']
        now = self.clock()
        status, wait = self.store.check(user_id, message['date'], now)
        if status != 'allowed':
            if self.store.notice_allowed(user_id, now, force=status == 'new_ban'):
                text = (f'Слишком часто! Подождите {wait} сек. перед следующей отправкой.'
                        if status == 'cooldown' else
                        f'Отправка заблокирована за спам. Осталось {wait} сек. '
                        'Блокировка снимается автоматически.')
                self.reply(chat_id, text)
            return
        text = message.get('text', '')
        if text.startswith('/'):
            if self.store.notice_allowed(user_id, now):
                self.reply(chat_id, 'Пришлите сообщение — я опубликую его в канале без модерации. '
                           'Интервал: 15 секунд. За 5 сообщений в одну секунду — блокировка на час. '
                           'Команды не публикуются. Альбомы отправляйте отдельными файлами.')
            return
        if message.get('media_group_id'):
            if self.store.notice_allowed(user_id, now):
                self.reply(chat_id, 'Альбомы пока не поддерживаются. Отправьте один файл с подписью.')
            return
        try:
            self.api.call('copyMessage', chat_id=self.channel,
                          from_chat_id=chat_id, message_id=message['message_id'])
        except APIError as error:
            LOG.warning('Publication failed (code %s)', error.code)
            if error.code == 0 or error.code >= 500:
                self.store.published(user_id, self.clock())
                self.reply(chat_id, 'Не удалось подтвердить публикацию из-за сбоя связи. '
                           'Проверьте канал перед повторной отправкой.')
            else:
                self.reply(chat_id, 'Не удалось опубликовать сообщение. Попробуйте позже '
                           'или отправьте обычный текст.')
            if error.retry_after:
                time.sleep(error.retry_after)
            return
        self.store.published(user_id, self.clock())
        self.reply(chat_id, 'Сообщение опубликовано! Следующее можно отправить через 15 секунд.')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token, channel = os.environ.get('BOT_TOKEN'), os.environ.get('CHANNEL_ID')
    if not token or not channel:
        raise SystemExit('Задайте BOT_TOKEN и CHANNEL_ID в переменных окружения.')
    store = Store(os.environ.get('DATABASE_PATH', 'data/bot.sqlite3'))
    api = Telegram(token)
    try:
        me = api.call('getMe')
        hook = api.call('getWebhookInfo')
        if hook.get('url'):
            raise SystemExit('У бота включён webhook. Отключите его перед запуском polling.')
        chat = api.call('getChat', chat_id=channel)
        member = api.call('getChatMember', chat_id=channel, user_id=me['id'])
        if chat['type'] != 'channel' or not member.get('can_post_messages'):
            raise SystemExit('Добавьте бота администратором канала с правом публикации.')
    except APIError as error:
        raise SystemExit(f'Не удалось проверить настройки: код {error.code}.') from None
    bot = Bot(api, store, channel)
    LOG.info('Bot started')
    while True:
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
