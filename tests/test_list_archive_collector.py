"""Tests for list-archive-collector.py.

The collector turns archives that were never meant to be re-imported
(pipermail text dumps, marc.info pages, NNTP) back into usable mail, so most
tests feed it the mangled forms those sources really produce and check what
comes out. Network access is replaced with fakes.
"""

import base64
import collections
import gzip
import mailbox
import subprocess
import sys
import types
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from conftest import load_script

pytest.importorskip('requests')
lac = load_script('list-archive-collector.py')

LISTID = 'apparmor.lists.ubuntu.com'
LISTADDR = 'apparmor@lists.ubuntu.com'

PIPERMAIL_MONTH = b"""\
From alice at example.org  Thu Mar 16 06:08:16 1995
From: alice at example.org (Alice Example)
Date: Thu, 16 Mar 1995 06:08:16 +0000
Subject: [apparmor] First
Message-ID: <first@example.org>

From the start, this line was part of the body.
Thanks to Bob <bob at example.org> and <mailto:carol at example.org>.
Signed-off-by: Dave <dave.d at mail.example.org>
>>> Dummy()
<class __main__.Dummy at 0x00F4CE70>
Look at foo.c for details.

From bob at example.org  Thu Mar 16 07:00:00 1995
From: bob at example.org (Bob)
Date: Thu, 16 Mar 1995 07:00:00 +0000
Subject: Re: [apparmor] First
In-Reply-To: first@example.org
References: <first@example.org>
Message-ID: <second@example.org>

> From: alice at example.org
> Cc: carol at example.org, dave at example.org
Reply body.
"""

MARC_MSG = b"""\
From linux-kernel  Tue Jan 31 23:59:36 2023
From: Roman Gushchin <roman.gushchin () linux ! dev>
Date: Tue, 31 Jan 2023 23:59:36 +0000
To: linux-kernel
Subject: Re: [PATCH] mm: wow ! that () works
Message-Id: <Y9mraBHucYdnHXiS () P9FQF9L96D ! corp ! robot ! car>
X-MARC-Message: https://marc.info/?l=linux-kernel&m=167520957802119

"""


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise lac.requests.HTTPError(f'{self.status_code}')


class FakeSession:
    """Serves canned pages and remembers what was asked for."""

    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = pages
        self.requested: list[str] = list()

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requested.append(url)
        if url in self.pages:
            return FakeResponse(self.pages[url])
        return FakeResponse(b'Not found', 404)


def text_of(msg: Message) -> str:
    payload = msg.get_payload(decode=True)
    assert isinstance(payload, bytes)
    return payload.decode(msg.get_content_charset() or 'ascii')


def decoded(hdrval: str) -> str:
    return str(make_header(decode_header(hdrval)))


def read_mbox(path: Path) -> list[Message]:
    return list(mailbox.mbox(path))


def test_get_links() -> None:
    html = b'<html><a href="one.txt">1</a><A HREF="two.txt">2</A><a name="x">no</a></html>'
    assert lac.get_links(html) == ['one.txt', 'two.txt']


def test_pipermail_months_are_in_date_order() -> None:
    """Months must be fetched in order, so the output mbox is chronological."""
    html = (b'<a href="2010-July.txt.gz">a</a><a href="2010-June.txt.gz">b</a>'
            b'<a href="2011-January.txt">c</a><a href="thread.html">d</a>')
    base = 'https://lists.ubuntu.com/archives/apparmor'
    assert lac.pipermail_month_urls(base, html) == [
        f'{base}/2010-June.txt.gz', f'{base}/2010-July.txt.gz', f'{base}/2011-January.txt']


def test_hyperkitty_months_are_in_date_order() -> None:
    base = 'https://lists.example.org/archives/list/foo@example.org/'
    html = (b'<a href="/archives/list/foo@example.org/2022/1/">a</a>'
            b'<a href="/archives/list/foo@example.org/2021/12/">b</a>'
            b'<a href="/archives/list/foo@example.org/2021/12/">b again</a>')
    export = 'https://lists.example.org/archives/list/foo@example.org/export/export.mbox.gz'
    assert lac.hyperkitty_month_urls(base, html) == [
        f'{export}?start=2021-12-01&end=2022-01-01', f'{export}?start=2022-01-01&end=2022-02-01']


def test_split_mbox_only_splits_on_real_from_lines() -> None:
    """Pipermail does not escape body lines starting with "From "."""
    msgs = lac.split_mbox(PIPERMAIL_MONTH)
    assert len(msgs) == 2
    assert b'From the start, this line was part of the body.' in msgs[0]


def test_split_mbox_understands_python_from_lines() -> None:
    data = b'From MAILER-DAEMON Wed Sep  1 12:00:00 2021\nSubject: one\n\nx\n\n' \
           b'From carol@example.org Thu Sep  2 12:00:00 2021\nSubject: two\n\ny\n'
    assert [msg.split(b'\n')[0] for msg in lac.split_mbox(data)] == [b'Subject: one', b'Subject: two']


@pytest.mark.parametrize(('fromhdr', 'expected'), [
    (b'alice at example.org (Alice Example)', 'Alice Example <alice@example.org>'),
    (b'Alice Example <alice at example.org>', 'Alice Example <alice@example.org>'),
    (b'alice at example.org', 'alice@example.org'),
    (b'alice at example.org (=?iso-8859-1?q?J=F6rg?=)', 'Jörg <alice@example.org>'),
    (b'martin at v.loewis.de (Martin v. L\xf6wis)', 'Martin v. Löwis <martin@v.loewis.de>'),
], ids=['name-in-parens', 'name-first', 'bare', 'encoded-name', 'raw-latin1-name'])
def test_pipermail_from_is_unmangled(fromhdr: bytes, expected: str) -> None:
    msg = lac.parse_pipermail_message(b'From: ' + fromhdr + b'\nSubject: x\nMessage-ID: <x@y>\n\nbody\n')
    assert decoded(msg['From']) == expected


def test_pipermail_body_addresses_are_unmangled() -> None:
    """Trailers like Signed-off-by must have real addresses, but code and prose must survive."""
    first, second = (lac.parse_pipermail_message(raw) for raw in lac.split_mbox(PIPERMAIL_MONTH))
    body = text_of(first)
    assert 'From the start, this line was part of the body.' in body
    assert 'Bob <bob@example.org> and <mailto:carol@example.org>' in body
    assert 'Signed-off-by: Dave <dave.d@mail.example.org>' in body
    assert '<class __main__.Dummy at 0x00F4CE70>' in body
    assert 'Look at foo.c for details.' in body
    assert '> From: alice@example.org\n> Cc: carol@example.org, dave@example.org\n' in text_of(second)


def test_pipermail_in_reply_to_gets_brackets() -> None:
    second = lac.parse_pipermail_message(lac.split_mbox(PIPERMAIL_MONTH)[1])
    assert second['In-Reply-To'] == '<first@example.org>'


def test_pipermail_charset_is_detected_from_the_original_bytes() -> None:
    """Pipermail text archives drop Content-Type; decoding them as UTF-8 first destroys the text."""
    body = 'Martin v. Löwis wrote: Schöne Grüße aus Köln, wir müssen über die Änderungen reden.\n'.encode('latin-1')
    msg = lac.parse_pipermail_message(b'From: m at example.org\nSubject: x\nMessage-ID: <x@y>\n\n' + body)
    assert msg.get_payload(decode=True) == body
    assert 'Schöne Grüße aus Köln' in text_of(msg)


def test_ascii_message_gets_no_content_type() -> None:
    msg = lac.parse_pipermail_message(b'From: m at example.org\nSubject: x\nMessage-ID: <x@y>\n\nplain\n')
    assert msg['Content-Type'] is None


def test_marc_unmangles_only_address_headers() -> None:
    msg = lac.parse_marc_message(MARC_MSG + b'body\n', 'linux-kernel', '167520957802119')
    assert msg['From'] == 'Roman Gushchin <roman.gushchin@linux.dev>'
    assert msg['Message-Id'] == '<Y9mraBHucYdnHXiS@P9FQF9L96D.corp.robot.car>'
    assert msg['Subject'] == 'Re: [PATCH] mm: wow ! that () works'
    # marc.info puts the list name into To, which is not an address
    assert msg['To'] is None


def test_marc_message_without_message_id_gets_a_stable_one() -> None:
    raw = MARC_MSG.replace(b'Message-Id: <Y9mraBHucYdnHXiS () P9FQF9L96D ! corp ! robot ! car>\n', b'')
    msg = lac.parse_marc_message(raw + b'body\n', 'linux-kernel', '42')
    assert msg['Message-Id'] == '<marc-linux-kernel-42@msgid-missing>'


QP_BODY = b'caf=C3=A9 au lait, and a soft =\nbreak\n'
B64_TEXT = 'Hello there, this is a message body that marc.info served base64 encoded.\nSecond line.\n'


@pytest.mark.parametrize(('body', 'expected'), [
    (base64.encodebytes(B64_TEXT.encode()), B64_TEXT),
    (QP_BODY, 'café au lait, and a soft break\n'),
    (b'a = b;\nint x =\n    5;\n', 'a = b;\nint x =\n    5;\n'),
    (b'Test\n', 'Test\n'),
], ids=['base64', 'quoted-printable', 'code-with-equals', 'word-that-is-valid-base64'])
def test_marc_transfer_encoding_is_guessed(body: bytes, expected: str) -> None:
    """marc.info drops Content-Transfer-Encoding, but guessing wrong would corrupt plain text."""
    msg = lac.parse_marc_message(MARC_MSG + body, 'linux-kernel', '1')
    assert text_of(msg) == expected


def test_marc_multipart_boundary_is_repaired() -> None:
    raw = MARC_MSG.replace(b'\n\n', b'\nContent-Type: multipart/mixed; boundary="--XYZ"\n\n', 1) + (
        b'--XYZ\nContent-Type: text/plain\n\npart one\n--XYZ\nContent-Type: text/plain\n\npart two\n--XYZ--\n')
    msg = lac.parse_marc_message(raw, 'linux-kernel', '1')
    assert [text_of(part) for part in msg.get_payload()] == ['part one', 'part two']


@pytest.mark.parametrize(('subject', 'expected'), [
    ('[apparmor] Hello', 'Hello'),
    ('Re: [apparmor] Hello', 'Re: Hello'),
    ('Hello [apparmor]', 'Hello [apparmor]'),
    ('[other] Hello', '[other] Hello'),
    ('[apparmor] [ann] Hello', 'Hello'),
])
def test_clean_subject(subject: str, expected: str) -> None:
    assert lac.clean_subject(subject, ['[apparmor]', '[ann]']) == expected


@pytest.mark.parametrize(('returncode', 'spam'), [(0, False), (1, True)])
def test_is_spam(monkeypatch: pytest.MonkeyPatch, returncode: int, spam: bool) -> None:
    monkeypatch.setattr(lac.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, returncode))
    assert lac.is_spam(b'msg') == spam


def test_spam_check_failure_is_not_spam_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """If spamd is down, dropping every message as spam would silently empty the archive."""
    monkeypatch.setattr(lac.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 69))
    with pytest.raises(lac.SpamCheckError):
        lac.is_spam(b'msg')


def test_lookaside_fills_in_missing_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession({'https://lore.kernel.org/all/a%2Fb@example.org/raw':
                           b'To: list@example.org\nCc: someone@example.org\nSubject: x\n\nbody\n'})
    monkeypatch.setattr(lac, 'get_requests_session', lambda: session)
    msg = Message()
    msg['Message-Id'] = '<a/b@example.org>'
    msg['Cc'] = 'kept@example.org'
    assert lac.lookaside_fillin(msg)
    assert msg['To'] == 'list@example.org'
    assert msg['Cc'] == 'kept@example.org'


def test_lookaside_without_lore_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lac, 'get_requests_session', lambda: FakeSession({}))
    msg = Message()
    msg['Message-Id'] = '<missing@example.org>'
    assert not lac.lookaside_fillin(msg)
    assert msg['To'] is None


def test_missing_message_id_is_stable(tmp_path: Path) -> None:
    """Real posts can lack a Message-ID; the one we make must not change
    between runs, or every re-run would archive them again."""
    raw = b'From: john at example.org (John)\nSubject: [PATCH] fix\nDate: Tue, 3 Jan 2012 04:23:44 -0800\n\npatch\n'
    out = tmp_path / 'out.mbox'
    writer = lac.ArchiveWriter(str(out), as_maildir=False, listid=LISTID)
    for msg in (raw, raw, raw.replace(b'patch', b'other patch')):
        writer.add(lac.parse_pipermail_message(msg))
    writer.close()
    first, again, other = (msg['Message-Id'] for msg in read_mbox(out))
    assert first == again != other
    assert first.endswith('@msgid-missing>')


def write_gz(path: Path, data: bytes) -> Path:
    with gzip.open(path, 'wb') as fh:
        fh.write(data)
    return path


def test_main_collects_local_pipermail_archive(tmp_path: Path) -> None:
    out = tmp_path / 'out.mbox'
    month = write_gz(tmp_path / '1995-March.txt.gz', PIPERMAIL_MONTH)
    lac.main(['-o', str(out), '-i', LISTID, '-c', '[apparmor]', 'mailman', '-u', str(month)])
    first, second = read_mbox(out)
    assert first['From'] == 'Alice Example <alice@example.org>'
    assert [msg['Subject'] for msg in (first, second)] == ['First', 'Re: First']
    assert [msg['List-Id'] for msg in (first, second)] == [f'<{LISTID}>'] * 2
    assert [msg['To'] for msg in (first, second)] == [LISTADDR] * 2


def test_main_collects_hyperkitty_export_unchanged(tmp_path: Path) -> None:
    """Mailman 3 exports are real mail; pipermail's fixes would only damage them."""
    out = tmp_path / 'out.mbox'
    month = write_gz(tmp_path / 'export.mbox.gz', (
        b'From carol@example.org Wed Sep  1 12:00:00 2021\n'
        b'From: Carol <carol@example.org>\nTo: list@example.org\nSubject: Hi\nMessage-ID: <c@example.org>\n'
        b'Content-Type: text/plain; charset="iso-8859-1"\nContent-Transfer-Encoding: 8bit\n\n'
        b'Not an address: <bob at example.org>\nCaf\xe9\n'))
    lac.main(['-o', str(out), 'mailman', '-3', '-u', str(month)])
    [msg] = read_mbox(out)
    assert msg.get_payload(decode=True) == b'Not an address: <bob at example.org>\nCaf\xe9\n'
    assert msg['Content-Type'] == 'text/plain; charset="iso-8859-1"'


MARCURL = 'https://marc.info'


def marc_msg(msgnum: str, msgid: str) -> bytes:
    return (f'From lkml  Tue Jan 31 23:59:36 2023\nFrom: A <a () example ! org>\nSubject: m{msgnum}\n'
            f'Message-Id: <{msgid}>\nX-MARC-Message: {MARCURL}/?l=lkml&m={msgnum}\n\nbody\n').encode()


def test_marc_collects_new_messages_and_threads(tmp_path: Path) -> None:
    """Already collected messages are skipped, and thread replies point at their thread."""
    out = tmp_path / 'out.mbox'
    existing = mailbox.mbox(out)
    existing.add(marc_msg('111', 'old@example.org'))
    existing.close()
    session = FakeSession({
        f'{MARCURL}/?l=lkml&w=1': b'<a href="?l=lkml&r=1&b=202301&w=1">Jan</a>',
        f'{MARCURL}/?l=lkml&b=202301&r=1&w=1': (b'<a href="?l=lkml&m=111&w=2">a</a><a href="?l=lkml&m=222&w=2">b</a>'
                                                 b'<a href="?t=999&r=1&w=1">thread</a>'),
        # marc.info lists threads newest first
        f'{MARCURL}/?t=999&r=1&w=1': b'<a href="?l=lkml&m=444&w=2">reply</a><a href="?l=lkml&m=333&w=2">root</a>',
        f'{MARCURL}/?l=lkml&m=222&q=mbox': marc_msg('222', 'single@example.org'),
        f'{MARCURL}/?l=lkml&m=333&q=mbox': marc_msg('333', 'root@example.org'),
        f'{MARCURL}/?l=lkml&m=444&q=mbox': marc_msg('444', 'reply@example.org'),
    })
    writer = lac.ArchiveWriter(str(out), as_maildir=False, listid=LISTID, toaddr=LISTADDR)
    lac.collect_marc(lac.MarcClient(session, nice=0), 'lkml', writer)
    writer.close()

    assert f'{MARCURL}/?l=lkml&m=111&q=mbox' not in session.requested
    msgs = {msg['Message-Id']: msg for msg in read_mbox(out)}
    assert sorted(msgs) == ['<old@example.org>', '<reply@example.org>', '<root@example.org>', '<single@example.org>']
    assert msgs['<reply@example.org>']['In-Reply-To'] == '<root@example.org>'
    assert msgs['<single@example.org>']['From'] == 'A <a@example.org>'
    assert msgs['<single@example.org>']['To'] == LISTADDR


ArticleInfo = collections.namedtuple('ArticleInfo', ['number', 'message_id', 'lines'])


def test_nntp_starts_at_the_first_article(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Groups often start far above 1; asking for every lower number wastes a request each."""
    class TemporaryError(Exception):
        pass

    asked: list[int] = list()

    class FakeNNTP:
        def __init__(self, host: str) -> None:
            assert host == 'news.example.org'

        def group(self, name: str) -> tuple[str, int, int, int, str]:
            return '211', 3, 5, 7, name

        def article(self, num: int) -> tuple[str, ArticleInfo]:
            asked.append(num)
            if num == 6:
                raise TemporaryError('423 no such article')
            lines = [b'From: a@example.org', b'Subject: s', f'Message-Id: <{num}@example.org>'.encode(),
                     b'Original-To: list@example.org', b'', b'body']
            return '220', ArticleInfo(num, f'<{num}@example.org>', lines)

        def quit(self) -> None:
            pass

    fake = types.ModuleType('nntplib')
    setattr(fake, 'NNTP', FakeNNTP)
    setattr(fake, 'NNTPTemporaryError', TemporaryError)
    monkeypatch.setitem(sys.modules, 'nntplib', fake)

    out = tmp_path / 'out.mbox'
    writer = lac.ArchiveWriter(str(out), as_maildir=False, listid=LISTID)
    lac.collect_nntp('nntp://news.example.org/gmane.example', writer)
    writer.close()

    assert asked == [5, 6, 7]
    msgs = read_mbox(out)
    assert [msg['Message-Id'] for msg in msgs] == ['<5@example.org>', '<7@example.org>']
    assert msgs[0]['To'] == 'list@example.org'
    assert msgs[0]['List-Id'] == f'<{LISTID}>'
