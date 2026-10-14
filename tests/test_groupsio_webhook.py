"""Tests for groupsio-webhook.py."""

import email
import hashlib
import hmac
import json
from configparser import ConfigParser
from pathlib import Path
from typing import Any

import pytest
from conftest import load_script

falcon_testing = pytest.importorskip('falcon.testing')
ezpi = pytest.importorskip('ezpi')
gw = load_script('groupsio-webhook.py')

PSK = 'sekrit'
MOUNT = '/groupsio_webhook'

MESSAGE = """\
From: Alice Example <alice@example.org>
To: devel@groups.io
Subject: Hello there
Message-Id: <hello@example.org>
List-Id: <something.else>

Body text
"""


def make_config(tmp_path: Path, **main: str) -> ConfigParser:
    config = ConfigParser()
    config['main'] = {
        'groupsio_psk': PSK,
        'inboxdir': str(tmp_path / 'inboxes'),
        'emerg_dir': str(tmp_path / 'emerg'),
    }
    config['main'].update(main)
    (tmp_path / 'emerg').mkdir(exist_ok=True)
    return config


def make_client(config: ConfigParser) -> Any:
    return falcon_testing.TestClient(gw.create_app(config, MOUNT))


def make_payload(groupaddr: str = 'devel@groups.io', message: str = MESSAGE) -> bytes:
    return json.dumps({
        'action': 'sent_message',
        'msg_num': 42,
        'group': {'email_address': groupaddr, 'group_url': 'https://groups.io/g/devel'},
        'message': message,
    }).encode()


def post(client: Any, body: bytes) -> Any:
    signature = hmac.new(PSK.encode(), body, hashlib.sha256).hexdigest()
    return client.simulate_post(MOUNT, body=body, headers={'X-Groupsio-Signature': signature})


def read_inbox(path: Path) -> list[email.message.Message]:
    return [email.message_from_bytes(raw) for _, _, raw in ezpi.iter_messages(str(path))]


def emerg_files(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / 'emerg').iterdir())


def test_writes_message_to_auto_created_inbox(tmp_path: Path) -> None:
    resp = post(make_client(make_config(tmp_path)), make_payload())

    assert resp.status_code == 200
    msgs = read_inbox(tmp_path / 'inboxes' / 'devel.groups.io')
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg['Subject'] == 'Hello there'
    assert msg.get_all('List-Id') == ['<devel.groups.io>']
    assert msg['X-Groupsio-URL'] == 'https://groups.io/g/devel/message/42'
    assert 'for <devel@groups.io>' in msg['X-Webhook-Received']
    assert msg.get_payload() == 'Body text\n'
    assert emerg_files(tmp_path) == []


def test_per_list_inbox_overrides_inboxdir(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config['devel@groups.io'] = {'inbox': str(tmp_path / 'custom')}

    assert post(make_client(config), make_payload()).status_code == 200
    assert len(read_inbox(tmp_path / 'custom')) == 1
    assert not (tmp_path / 'inboxes').exists()


@pytest.mark.parametrize(
    'body, signature, status',
    [
        (make_payload(), None, 401),
        (make_payload(), '0' * 64, 401),
        (b'', 'sign', 400),
        (b'{not json', 'sign', 400),
    ],
    ids=['unsigned', 'bad-signature', 'empty', 'bad-json'],
)
def test_rejects_bad_requests(tmp_path: Path, body: bytes, signature: str | None, status: int) -> None:
    headers = {}
    if signature == 'sign':
        signature = hmac.new(PSK.encode(), body, hashlib.sha256).hexdigest()
    if signature:
        headers['X-Groupsio-Signature'] = signature
    resp = make_client(make_config(tmp_path)).simulate_post(MOUNT, body=body, headers=headers)

    assert resp.status_code == status
    assert not (tmp_path / 'inboxes').exists()


def test_skips_payload_without_message(tmp_path: Path) -> None:
    body = json.dumps({'action': 'new_member', 'group': {'email_address': 'devel@groups.io'}}).encode()

    assert post(make_client(make_config(tmp_path)), body).status_code == 200
    assert not (tmp_path / 'inboxes').exists()
    assert emerg_files(tmp_path) == []


@pytest.mark.parametrize(
    'main, payload, status, reason',
    [
        ({'inboxdir': ''}, make_payload(), 200, 'noinbox'),
        ({}, make_payload(groupaddr='../etc@groups.io'), 200, 'noinbox'),
        ({}, make_payload(message='From: a@example.org\n\nno subject\n'), 200, 'invalid'),
        ({}, json.dumps({'msg_num': 1, 'message': MESSAGE}).encode(), 400, 'malformed'),
    ],
    ids=['no-inboxdir', 'path-traversal', 'no-subject', 'no-group'],
)
def test_saves_unarchivable_payloads(
    tmp_path: Path, main: dict[str, str], payload: bytes, status: int, reason: str
) -> None:
    resp = post(make_client(make_config(tmp_path, **main)), payload)

    assert resp.status_code == status
    saved = emerg_files(tmp_path)
    assert len(saved) == 1
    assert saved[0].name.endswith(f'-{reason}.json')
    # The exact signed payload is kept so it can be replayed later.
    assert saved[0].read_bytes() == payload
    assert not (tmp_path / 'etc.groups.io').exists()


def test_write_failure_saves_payload_and_asks_for_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError('Could not obtain v2 inbox lock')

    monkeypatch.setattr(gw.ezpi, 'add_rfc822_v2', fail)
    resp = post(make_client(make_config(tmp_path)), make_payload())

    assert resp.status_code == 500
    assert [p.name.endswith('-failed.json') for p in emerg_files(tmp_path)] == [True]


def test_missing_emerg_dir_does_not_crash(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config = make_config(tmp_path, inboxdir='', emerg_dir=str(tmp_path / 'nonexistent'))

    assert post(make_client(config), make_payload()).status_code == 200
    assert 'payload was not saved' in caplog.text


@pytest.mark.parametrize(
    'main, error',
    [({'groupsio_psk': ''}, 'groupsio_psk'), ({'auto_epoch': 'weekly'}, 'auto_epoch')],
    ids=['no-psk', 'bad-epoch'],
)
def test_rejects_bad_config(tmp_path: Path, main: dict[str, str], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        gw.create_app(make_config(tmp_path, **main))
