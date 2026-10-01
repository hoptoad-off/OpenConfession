"""Shared SQLite message journal for the bot and the local dashboard."""
import json
import time

MEDIA_TYPES = ('photo', 'video', 'animation', 'audio', 'voice', 'document', 'sticker',
               'video_note', 'contact', 'location', 'venue', 'poll', 'dice')
STATUSES = ('received', 'sending', 'published', 'rejected', 'warning', 'banned',
            'command', 'profile', 'failed', 'unknown', 'partial')


class Journal:
    def __init__(self, db):
        self.db = db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                update_id INTEGER UNIQUE,
                request_key TEXT UNIQUE,
                origin TEXT NOT NULL,
                source_chat_id INTEGER,
                source_message_id INTEGER,
                sender_id INTEGER,
                sender_username TEXT,
                sender_name TEXT,
                nickname TEXT,
                body TEXT NOT NULL DEFAULT '',
                media_type TEXT NOT NULL DEFAULT 'text',
                media_file_id TEXT,
                media_file_name TEXT,
                payload_json TEXT,
                created_at REAL NOT NULL,
                sent_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'received',
                reason TEXT NOT NULL DEFAULT '',
                post_number INTEGER UNIQUE,
                channel_message_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS messages_status_id ON messages(status, id DESC);
            CREATE INDEX IF NOT EXISTS messages_sender_id ON messages(sender_id, id DESC);
        ''')

    def incoming(self, update, nickname, now):
        message = update['message']
        sender = message['from']
        kind = next((kind for kind in MEDIA_TYPES if kind in message),
                    'text' if 'text' in message else 'other')
        media = message.get(kind, {})
        if isinstance(media, list):
            media = media[-1] if media else {}
        if not isinstance(media, dict):
            media = {}
        with self.db:
            cursor = self.db.execute('''INSERT OR IGNORE INTO messages
                (update_id, origin, source_chat_id, source_message_id, sender_id, sender_username,
                 sender_name, nickname, body, media_type, media_file_id, media_file_name,
                 payload_json, created_at, sent_at)
                VALUES (?, 'telegram', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (update.get('update_id'), message['chat']['id'], message['message_id'], sender['id'],
                 sender.get('username'), ' '.join(filter(None, (sender.get('first_name'), sender.get('last_name')))),
                 nickname, message.get('text', message.get('caption', '')), kind,
                 media.get('file_id'), media.get('file_name'), json.dumps(message, ensure_ascii=False),
                 now, message['date']))
            return cursor.lastrowid if cursor.rowcount else None

    def finish(self, entry_id, status, reason='', post_number=None, channel_message_id=None):
        if status not in STATUSES:
            raise ValueError('Unknown journal status')
        with self.db:
            self.db.execute('''UPDATE messages SET status=?, reason=?,
                post_number=COALESCE(?, post_number), channel_message_id=COALESCE(?, channel_message_id)
                WHERE id=?''', (status, reason, post_number, channel_message_id, entry_id))

    def get(self, entry_id):
        row = self.db.execute('SELECT * FROM messages WHERE id=?', (entry_id,)).fetchone()
        return dict(row) if row is not None else None

    def request(self, request_key):
        row = self.db.execute('SELECT * FROM messages WHERE request_key=?', (request_key,)).fetchone()
        return dict(row) if row is not None else None

    def administrative(self, request_key, role, body, now):
        if role not in ('Administrator', 'Moderator'):
            raise ValueError('Выберите Administrator или Moderator.')
        with self.db:
            cursor = self.db.execute('''INSERT OR IGNORE INTO messages
                (request_key, origin, nickname, sender_name, body, created_at, sent_at, status)
                VALUES (?, 'dashboard', ?, ?, ?, ?, ?, 'sending')''',
                (request_key, role, role, body, now, now))
            if not cursor.rowcount:
                previous = self.request(request_key)
                if previous['nickname'] != role or previous['body'] != body:
                    raise ValueError('Этот запрос уже использован для другого сообщения.')
                return previous, False
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('post_number', 0)")
            self.db.execute("UPDATE settings SET value=value+1 WHERE key='post_number'")
            number = self.db.execute("SELECT value FROM settings WHERE key='post_number'").fetchone()[0]
            signed = f'{body}\n\n№{number} — {role}'
            if len(signed.encode('utf-16-le')) // 2 > 4096:
                raise ValueError('Текст с подписью превышает лимит Telegram. Сократите сообщение.')
            self.db.execute('UPDATE messages SET post_number=? WHERE id=?', (number, cursor.lastrowid))
            return self.get(cursor.lastrowid), True

    def list(self, search='', status='', page=1, page_size=30):
        where, args = [], []
        if search:
            needle = '%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            where.append('(' + ' OR '.join(f"{col} LIKE ? ESCAPE '\\'" for col in
                         ('body', 'sender_username', 'sender_name', 'nickname', 'CAST(sender_id AS TEXT)',
                          'CAST(post_number AS TEXT)')) + ')')
            args.extend([needle] * 6)
        if status:
            if status not in STATUSES:
                raise ValueError('Неизвестный статус.')
            where.append('status=?')
            args.append(status)
        clause = ' WHERE ' + ' AND '.join(where) if where else ''
        total = self.db.execute('SELECT COUNT(*) FROM messages' + clause, args).fetchone()[0]
        rows = self.db.execute('SELECT * FROM messages' + clause + ' ORDER BY id DESC LIMIT ? OFFSET ?',
                               [*args, page_size, (page - 1) * page_size]).fetchall()
        return {'items': [dict(row) for row in rows], 'total': total, 'page': page, 'page_size': page_size}

    def stats(self):
        counts = dict(self.db.execute('SELECT status, COUNT(*) FROM messages GROUP BY status').fetchall())
        return {'total': sum(counts.values()), 'published': counts.get('published', 0),
                'rejected': sum(counts.get(key, 0) for key in ('rejected', 'warning', 'banned')),
                'senders': self.db.execute('SELECT COUNT(DISTINCT sender_id) FROM messages').fetchone()[0]}

    def heartbeat(self):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('bot_heartbeat', ?)", (int(time.time()),))
