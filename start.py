#!/usr/bin/env python3
"""Start the bot and dashboard together, loading the adjacent .env file."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent


def load_environment(path, environ=None):
    env = dict(os.environ if environ is None else environ)
    if path.exists():
        for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
            try:
                parts = shlex.split(line, comments=True)
            except ValueError:
                raise ValueError(f'Ошибка кавычек в .env, строка {number}.') from None
            if parts and parts[0] == 'export':
                parts = parts[1:]
            if not parts:
                continue
            if len(parts) != 1 or '=' not in parts[0]:
                raise ValueError(f'Ожидается KEY=value в .env, строка {number}.')
            key, value = parts[0].split('=', 1)
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                raise ValueError(f'Некорректное имя настройки в .env, строка {number}.')
            env[key] = value
    env['PYTHONUNBUFFERED'] = '1'
    return env


def validate_environment(env):
    for key in ('BOT_TOKEN', 'CHANNEL_ID', 'DASHBOARD_PASSWORD'):
        if not env.get(key):
            raise ValueError(f'Задайте {key} в .env рядом со start.py.')
    if env['BOT_TOKEN'] == 'replace_with_botfather_token':
        raise ValueError('Замените пример BOT_TOKEN в .env на токен вашего бота.')
    if len(env['DASHBOARD_PASSWORD']) < 12 or env['DASHBOARD_PASSWORD'] == 'replace_with_a_long_random_password':
        raise ValueError('DASHBOARD_PASSWORD должен быть вашим паролем длиной не менее 12 символов.')
    try:
        port = int(env.get('DASHBOARD_PORT', '8080'))
        if not 1024 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError('DASHBOARD_PORT должен быть числом от 1024 до 65535.') from None
    try:
        if int(env.get('MESSAGE_COOLDOWN_SECONDS', '15')) < 0:
            raise ValueError
    except ValueError:
        raise ValueError('MESSAGE_COOLDOWN_SECONDS должен быть целым числом не меньше 0.') from None
    return port


@contextmanager
def instance_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        try:
            if os.name == 'posix':
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                handle.write(b'0')
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ValueError('Общий запуск уже работает. Сначала остановите его через Ctrl+C.') from None
        yield
        # Closing the handle releases the lock, including when startup raises an error.


def stop_children(children):
    for _, child in children:
        if child.poll() is None:
            try:
                child.send_signal(signal.SIGINT) if os.name == 'posix' else child.terminate()
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5
    for _, child in children:
        try:
            child.wait(timeout=max(.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def run_services(base, env, port):
    # Fail before starting polling when the dashboard port is already occupied.
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            raise ValueError(f'Порт {port} занят. Остановите отдельно запущенный dashboard '
                             'или измените DASHBOARD_PORT в .env.') from None
    children, stopping = [], False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        dashboard = subprocess.Popen([sys.executable, '-u', str(base / 'dashboard.py')],
                                     cwd=base, env=env, start_new_session=os.name == 'posix')
        children.append(('Dashboard', dashboard))
        deadline = time.monotonic() + 15
        while not stopping:
            if dashboard.poll() is not None:
                print('Dashboard не запустился. Бот не запускался.', file=sys.stderr, flush=True)
                return dashboard.returncode or 1
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError('Dashboard не открыл порт за 15 секунд. Проверьте сообщения выше.')
                time.sleep(.1)
        if stopping:
            return 0
        children.append(('Бот', subprocess.Popen([sys.executable, '-u', str(base / 'bot.py')],
                                                cwd=base, env=env, start_new_session=os.name == 'posix')))
        print(f'Бот и dashboard запущены. Панель: http://127.0.0.1:{port}', flush=True)
        print('Ctrl+C останавливает оба процесса.', flush=True)
        while not stopping:
            for name, child in children:
                code = child.poll()
                if code is not None:
                    print(f'{name} завершился (код {code}). Останавливаю второй процесс.', flush=True)
                    return code if code > 0 else 1
            time.sleep(.2)
        return 0
    finally:
        stop_children(children)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    try:
        env = load_environment(BASE / '.env')
        port = validate_environment(env)
        with instance_lock(BASE / 'data' / 'launcher.lock'):
            # Upgrade SQLite once, before the two processes open it concurrently.
            from bot import Store
            path = Path(env.get('DATABASE_PATH', 'data/bot.sqlite3'))
            if not path.is_absolute():
                path = BASE / path
            Store(str(path), cooldown=int(env.get('MESSAGE_COOLDOWN_SECONDS', '15'))).db.close()
            return run_services(BASE, env, port)
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
