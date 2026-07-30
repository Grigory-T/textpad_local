#!/usr/bin/env python3
from contextlib import closing
from http.client import HTTPConnection
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parent


def start_server(tmp):
    return subprocess.Popen(
        [sys.executable, 'pad.py'],
        cwd=tmp,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def stop_server(process):
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
    if process.returncode not in (0, -15):
        raise RuntimeError(process.stderr.read())


def request(port, method, path, fields=None):
    body = urlencode(fields).encode() if fields is not None else None
    headers = {'Content-Type': 'application/x-www-form-urlencoded'} if body else {}
    connection = HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def wait_for_server(port):
    for _ in range(50):
        try:
            if request(port, 'GET', '/state')[0] == 200:
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError('test server did not start')


def state(port):
    status, _, body = request(port, 'GET', '/state')
    assert status == 200
    return json.loads(body)


def main():
    with closing(socket.socket()) as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]

    with tempfile.TemporaryDirectory(prefix='textpad-local-api-test-') as tmp:
        tmp_path = Path(tmp)
        shutil.copy2(ROOT / 'pad.py', tmp_path / 'pad.py')
        (tmp_path / 'config.py').write_text(
            f"HOST = '127.0.0.1'\nPORT = {port}\nMIRROR_DIR = './mirror'\n",
            encoding='utf-8',
        )
        process = start_server(tmp)
        try:
            wait_for_server(port)
            initial = state(port)
            first_tab = initial['tabs'][0]['id']
            first_text = initial['contents'][first_tab]

            for path in ('/', '/?tab=', '/?tab=does-not-exist'):
                status, _, _ = request(
                    port,
                    'POST',
                    path,
                    {'text': 'must not be written'},
                )
                assert status == 404
                assert state(port)['contents'][first_tab] == first_text

            status, _, body = request(port, 'POST', '/tabs', {
                'json': '1',
                'action': 'create',
                'id': 'agent-api-test',
                'name': 'Agent API Test',
            })
            assert status == 200
            created = json.loads(body)
            assert created['active'] == 'agent-api-test'

            status, _, body = request(port, 'POST', '/tabs', {
                'json': '1',
                'action': 'create',
                'id': 'agent-api-test',
                'name': 'Agent API Test',
                'after': first_tab,
            })
            assert status == 200
            duplicate = json.loads(body)
            assert duplicate['active'] == 'agent-api-test-2'
            ids = [tab['id'] for tab in duplicate['tabs']]
            assert ids.index('agent-api-test-2') == ids.index(first_tab) + 1

            sentence = (
                'First line\n'
                'Unicode: Привет, 你好, café, 😀\n'
                'Reserved form characters: & = + % ? #\n'
                'Trailing spaces stay here:  \n'
            )
            status, headers, _ = request(
                port,
                'POST',
                '/?tab=agent-api-test',
                {'text': sentence},
            )
            assert status == 204
            assert int(headers['X-Textpad-Revision']) > 0
            assert state(port)['contents']['agent-api-test'] == sentence

            large_text = '\n'.join(
                f'{line:04d}: ' + ('x' * 248)
                for line in range(1024)
            )
            status, _, _ = request(
                port,
                'POST',
                '/?tab=agent-api-test-2',
                {'text': large_text},
            )
            assert status == 204
            assert state(port)['contents']['agent-api-test-2'] == large_text

            status, _, _ = request(
                port,
                'POST',
                '/?tab=agent-api-test-2',
                {'text': ''},
            )
            assert status == 204
            assert state(port)['contents']['agent-api-test-2'] == ''

            status, _, _ = request(
                port,
                'POST',
                '/?tab=agent-api-test-2',
                {'text': large_text},
            )
            assert status == 204

            current = state(port)
            assert current['contents'][first_tab] == first_text
            assert 'does-not-exist' not in current['contents']
            assert (tmp_path / 'tabs.json').read_bytes() == (
                tmp_path / 'tabs.json.bak'
            ).read_bytes()
            for index, tab in enumerate(current['tabs'], 1):
                mirror = next((tmp_path / 'mirror').glob(f'{index}. *.txt'))
                assert mirror.read_text(encoding='utf-8') == current['contents'][tab['id']]

            stop_server(process)
            process = start_server(tmp)
            wait_for_server(port)
            persisted = state(port)
            assert persisted['contents']['agent-api-test'] == sentence
            assert persisted['contents']['agent-api-test-2'] == large_text
            assert persisted['contents'][first_tab] == first_text
        finally:
            if process.poll() is None:
                stop_server(process)

    print('API tests passed')


if __name__ == '__main__':
    main()
