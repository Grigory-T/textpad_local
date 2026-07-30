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
        process = subprocess.Popen(
            [sys.executable, 'pad.py'],
            cwd=tmp,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            for _ in range(50):
                try:
                    if request(port, 'GET', '/state')[0] == 200:
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError('test server did not start')

            status, _, body = request(port, 'POST', '/tabs', {
                'json': '1',
                'action': 'create',
                'id': 'agent-api-test',
                'name': 'Agent API Test',
            })
            assert status == 200
            created = json.loads(body)
            assert created['active'] == 'agent-api-test'

            sentence = 'Agent API read and write test passed.'
            status, headers, _ = request(
                port,
                'POST',
                '/?tab=agent-api-test',
                {'text': sentence},
            )
            assert status == 204
            assert int(headers['X-Textpad-Revision']) > 0

            status, _, body = request(port, 'GET', '/state')
            assert status == 200
            state = json.loads(body)
            assert state['contents']['agent-api-test'] == sentence
            first_tab = state['tabs'][0]['id']
            first_text = state['contents'][first_tab]

            status, _, _ = request(
                port,
                'POST',
                '/?tab=does-not-exist',
                {'text': 'must not be written'},
            )
            assert status == 404
            state = json.loads(request(port, 'GET', '/state')[2])
            assert state['contents'][first_tab] == first_text
            assert 'does-not-exist' not in state['contents']
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
            if process.returncode not in (0, -15):
                raise RuntimeError(process.stderr.read())

    print('API tests passed')


if __name__ == '__main__':
    main()
