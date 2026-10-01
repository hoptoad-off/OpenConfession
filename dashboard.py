"""Password-protected dashboard. Runs locally alongside bot.py, sharing SQLite."""
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit, quote
from urllib.request import urlopen
import uuid

from bot import APIError, Store, Telegram

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).with_name('dashboard_static')
SESSION_SECONDS = 8 * 3600
MAX_MEDIA_SIZE = 20 * 1024 * 1024


def public_message(row, channel, detail=False):
    result = {key: value for key, value in row.items()
              if key not in ('payload_json', 'media_file_id', 'request_key')}
    result['has_media'] = bool(row['media_file_id'])
    result['channel_url'] = None
    if row['channel_message_id']:
        if channel.startswith('@'):
            result['channel_url'] = f'https://t.me/{quote(channel[1:], safe="")}/{row["channel_message_id"]}'
        elif channel.startswith('-100') and channel[1:].isdigit():
            result['channel_url'] = f'https://t.me/c/{channel[4:]}/{row["channel_message_id"]}'
    if detail and row['payload_json']:
        message = json.loads(row['payload_json'])
        result['details'] = {key: message[key] for key in ('contact', 'location', 'venue', 'poll', 'dice') if key in message}
    return result


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, database_path, password, channel, api):
        if not ipaddress.ip_address(address[0]).is_loopback:
            raise ValueError('Dashboard должен слушать локальный адрес. Для удалённого доступа используйте SSH-туннель.')
        self.database_path, self.channel, self.api = str(database_path), channel, api
        self.password_digest = hashlib.sha256(password.encode()).digest()
        self.sessions, self.login_attempts = {}, {}
        self.session_lock = threading.Lock()
        # Complete schema upgrades before accepting concurrent requests.
        store = Store(self.database_path)
        store.db.close()
        super().__init__(address, Handler)

    def new_store(self):
        return Store(self.database_path)


class Handler(BaseHTTPRequestHandler):
    server_version = 'OpenConfession'

    def log_message(self, format, *args):
        # Never log POST bodies, cookies, passwords or media URLs containing a bot token.
        LOG.info('%s %s', self.command, self.path.split('?', 1)[0])

    def send(self, status, body, content_type='application/json; charset=utf-8', cookie=None, attachment=False):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        if attachment:
            self.send_header('Content-Disposition', 'attachment; filename="attachment"')
        self.end_headers()
        self.wfile.write(body)

    def error(self, status, message):
        self.send(status, {'error': message})

    def host_allowed(self):
        port = self.server.server_port
        allowed = {f'127.0.0.1:{port}', f'localhost:{port}'}
        host = self.headers.get('Host', '')
        if host not in allowed:
            self.error(403, 'Недопустимый адрес запроса.')
            return False
        origin = self.headers.get('Origin')
        if origin and origin != f'http://{host}':
            self.error(403, 'Запрос с другого сайта отклонён.')
            return False
        return True

    def session(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie', ''))
            token = cookies['oc_session'].value if 'oc_session' in cookies else ''
        except Exception:
            return None
        with self.server.session_lock:
            now = time.time()
            self.server.sessions = {key: value for key, value in self.server.sessions.items() if value['expires'] > now}
            session = self.server.sessions.get(token)
            return (token, session) if session else None

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise ValueError('Требуется JSON-запрос.')
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 65536:
            raise ValueError('Недопустимый размер запроса.')
        try:
            value = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            raise ValueError('Некорректный JSON.') from None
        if not isinstance(value, dict):
            raise ValueError('Требуется JSON-объект.')
        return value

    def do_GET(self):
        try:
            self.get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, OverflowError):
            self.error(400, 'Проверьте параметры запроса.')
        except Exception:
            LOG.error('Dashboard GET failed', exc_info=False)
            self.error(500, 'Не удалось загрузить данные. Попробуйте обновить страницу.')

    def get(self):
        if not self.host_allowed():
            return
        url = urlsplit(self.path)
        assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                  '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                  '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if url.path in assets:
            name, mime = assets[url.path]
            self.send(200, (STATIC / name).read_bytes(), mime)
            return
        session = self.session()
        if not session:
            self.error(401, 'Войдите в панель управления.')
            return
        if url.path == '/api/session':
            self.send(200, {'csrf': session[1]['csrf'], 'channel': self.server.channel})
            return
        store = self.server.new_store()
        try:
            if url.path == '/api/messages':
                query = parse_qs(url.query)
                page = max(1, min(1000000, int(query.get('page', ['1'])[0])))
                data = store.journal.list(query.get('search', [''])[0][:200], query.get('status', [''])[0], page)
                data['items'] = [public_message(row, self.server.channel) for row in data['items']]
                data['stats'] = store.journal.stats()
                heartbeat = store.db.execute("SELECT value FROM settings WHERE key='bot_heartbeat'").fetchone()
                data['bot_online'] = bool(heartbeat and time.time() - heartbeat[0] < 90)
                data['next_number'] = store.next_post_number(reserve=False)
                self.send(200, data)
                return
            parts = url.path.strip('/').split('/')
            if len(parts) in (3, 4) and parts[:2] == ['api', 'messages'] and parts[2].isdigit():
                record = store.journal.get(int(parts[2]))
                if record is None:
                    self.error(404, 'Сообщение не найдено.')
                elif len(parts) == 3:
                    self.send(200, public_message(record, self.server.channel, detail=True))
                elif parts[3] == 'media':
                    self.media(record)
                else:
                    self.error(404, 'Страница не найдена.')
                return
            self.error(404, 'Страница не найдена.')
        finally:
            store.db.close()

    def media(self, record):
        if not record['media_file_id']:
            self.error(404, 'У сообщения нет доступного файла.')
            return
        try:
            media = self.server.api.call('getFile', file_id=record['media_file_id'])
            path = media.get('file_path', '')
            if not path or '..' in path.split('/') or path.startswith('/') or media.get('file_size', 0) > MAX_MEDIA_SIZE:
                self.error(413, 'Файл недоступен для просмотра. Откройте публикацию в Telegram.')
                return
            remote = 'https://api.telegram.org/file/bot' + self.server.api.token + '/' + quote(path, safe='/')
            with urlopen(remote, timeout=30) as response:
                data = response.read(MAX_MEDIA_SIZE + 1)
            if len(data) > MAX_MEDIA_SIZE:
                self.error(413, 'Файл слишком большой для просмотра.')
                return
        except Exception:
            self.error(502, 'Не удалось получить вложение из Telegram.')
            return
        suffix = Path(path).suffix.lower()
        mime = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
                '.mp4': 'video/mp4', '.webm': 'video/webm', '.gif': 'image/gif',
                '.ogg': 'audio/ogg', '.oga': 'audio/ogg', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4'}.get(suffix)
        if record['media_type'] == 'document':
            mime = None
        self.send(200, data, mime or 'application/octet-stream', attachment=mime is None)

    def do_POST(self):
        try:
            self.post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, OverflowError) as error:
            self.error(400, str(error))
        except Exception:
            LOG.error('Dashboard POST failed', exc_info=False)
            self.error(500, 'Операция не подтверждена. Обновите журнал перед повторной отправкой.')

    def post(self):
        if not self.host_allowed():
            return
        path = urlsplit(self.path).path
        data = self.body()
        if path == '/api/login':
            self.login(data)
            return
        session = self.session()
        if not session:
            self.error(401, 'Войдите в панель управления.')
            return
        if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), session[1]['csrf']):
            self.error(403, 'Сессия устарела. Обновите страницу.')
            return
        if path == '/api/logout':
            with self.server.session_lock:
                self.server.sessions.pop(session[0], None)
            self.send(200, {'ok': True}, cookie='oc_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
        elif path == '/api/publish':
            self.publish(data)
        else:
            self.error(404, 'Страница не найдена.')

    def login(self, data):
        password = data.get('password')
        if not isinstance(password, str) or len(password) > 1024:
            raise ValueError('Введите пароль.')
        ip, now = self.client_address[0], time.time()
        with self.server.session_lock:
            attempts = [t for t in self.server.login_attempts.get(ip, []) if t > now - 300]
            if len(attempts) >= 5:
                self.error(429, 'Слишком много попыток входа. Повторите через 5 минут.')
                return
            digest = hashlib.sha256(password.encode()).digest()
            if not secrets.compare_digest(digest, self.server.password_digest):
                self.server.login_attempts[ip] = [*attempts, now]
                self.error(401, 'Неверный пароль.')
                return
            self.server.login_attempts.pop(ip, None)
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.server.sessions = {key: value for key, value in self.server.sessions.items() if value['expires'] > now}
            if len(self.server.sessions) >= 100:
                self.server.sessions.pop(next(iter(self.server.sessions)))
            self.server.sessions[token] = {'expires': now + SESSION_SECONDS, 'csrf': csrf}
        self.send(200, {'csrf': csrf, 'channel': self.server.channel},
                  cookie=f'oc_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}')

    def publish(self, data):
        role, body, request_key = data.get('role'), data.get('text'), data.get('request_id')
        if role not in ('Administrator', 'Moderator'):
            raise ValueError('Выберите Administrator или Moderator.')
        if not isinstance(body, str) or not body.strip() or len(body) > 4096:
            raise ValueError('Введите текст сообщения, не более 4096 символов.')
        if not isinstance(request_key, str):
            raise ValueError('Нет идентификатора отправки. Обновите страницу.')
        request_key = str(uuid.UUID(request_key))
        store = self.server.new_store()
        try:
            record, created = store.journal.administrative(request_key, role, body.strip(), time.time())
            if created:
                signed = f'{record["body"]}\n\n№{record["post_number"]} — {role}'
                try:
                    result = self.server.api.call('sendMessage', chat_id=self.server.channel, text=signed,
                                                  link_preview_options={'is_disabled': True})
                except APIError as error:
                    status = 'unknown' if error.code == 0 or error.code >= 500 else 'failed'
                    store.journal.finish(record['id'], status, f'Telegram API: {error.code}')
                except Exception:
                    store.journal.finish(record['id'], 'unknown', 'Не удалось подтвердить результат публикации.')
                else:
                    store.journal.finish(record['id'], 'published', channel_message_id=result['message_id'])
                record = store.journal.get(record['id'])
            self.send(200, {'message': public_message(record, self.server.channel), 'duplicate': not created})
        finally:
            store.db.close()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token, channel, password = (os.environ.get(key, '') for key in ('BOT_TOKEN', 'CHANNEL_ID', 'DASHBOARD_PASSWORD'))
    if not token or not channel:
        raise SystemExit('Задайте BOT_TOKEN и CHANNEL_ID.')
    if len(password) < 12 or password == 'replace_with_a_long_random_password':
        raise SystemExit('Задайте DASHBOARD_PASSWORD: собственный пароль длиной не менее 12 символов.')
    try:
        port = int(os.environ.get('DASHBOARD_PORT', '8080'))
        if not 1024 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit('DASHBOARD_PORT должен быть числом от 1024 до 65535.') from None
    server = DashboardServer(('127.0.0.1', port), os.environ.get('DATABASE_PATH', 'data/bot.sqlite3'),
                             password, channel, Telegram(token))
    LOG.info('Dashboard: http://127.0.0.1:%s', port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
