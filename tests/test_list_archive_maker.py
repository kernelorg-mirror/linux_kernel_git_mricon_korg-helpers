"""Tests for list-archive-maker.py.

The script's job is to turn subscriber mailboxes into a clean list archive,
so most tests feed it a small mbox and check what lands in the archive and
what lands in the rejects mailbox.
"""

import email.message
import mailbox
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import load_script

lam = load_script('list-archive-maker.py')

LISTID = 'linux-kernel.vger.kernel.org'
LISTADDR = 'linux-kernel@vger.kernel.org'
LIST_RECEIVED = f'from vger.kernel.org by mx.private.example for <{LISTADDR}>; Tue, 14 Mar 2023 10:00:00 +0000'


def make_msg(*headers: tuple[str, str], msgid: str | None = '<one@example.org>',
             listid: str | None = f'Linux Kernel <{LISTID}>',
             date: str | None = 'Sun, 05 Jan 2020 10:00:00 +0000') -> bytes:
    hdrs = [('From', 'Alice <alice@example.org>'), ('Subject', 'Hello')]
    if msgid is not None:
        hdrs.append(('Message-Id', msgid))
    if listid is not None:
        hdrs.append(('List-Id', listid))
    if date is not None:
        hdrs.append(('Date', date))
    hdrs += headers
    return ''.join(f'{name}: {value}\n' for name, value in hdrs).encode() + b'\nBody text\n'


def write_mbox(path: Path, *msgs: bytes) -> Path:
    mbox = mailbox.mbox(path)
    for msg in msgs:
        mbox.add(msg)
    mbox.close()
    return path


def run(tmp_path: Path, *msgs: bytes, listids: list[str] | None = None,
        knownids: Any = None, extrahdrs: list[tuple[str, str]] | None = None) -> dict[str, int]:
    source = write_mbox(tmp_path / 'source.mbox', *msgs)
    outdir = tmp_path / 'out'
    outdir.mkdir(exist_ok=True)
    if knownids is None:
        knownids = lam.KnownMessages()
    counts: dict[str, int] = lam.process_archives(
        [str(source)], lam.MailboxWriter(str(outdir), False), knownids,
        listids if listids is not None else [LISTID], str(tmp_path / 'rejected.mbox'), extrahdrs or [])
    return counts


def archived(tmp_path: Path) -> list[email.message.Message]:
    return [msg for mbx in sorted((tmp_path / 'out').glob('*.mbx')) for msg in mailbox.mbox(mbx)]


def rejected(tmp_path: Path) -> list[email.message.Message]:
    return list(mailbox.mbox(tmp_path / 'rejected.mbox'))


def test_keeps_only_wanted_headers(tmp_path: Path) -> None:
    """Private and noisy headers must not leak into a public archive."""
    run(tmp_path, make_msg(('X-Spam-Status', 'No'), ('Delivered-To', 'me@private.example'),
                           ('DKIM-Signature', 'v=1; d=example.org'), ('List-Unsubscribe', '<mailto:x@y>'),
                           ('X-Developer-Key', 'i=alice'), ('Content-Type', 'text/plain')))
    [msg] = archived(tmp_path)
    assert {name.lower() for name in msg.keys()} == {
        'from', 'subject', 'message-id', 'list-id', 'date', 'dkim-signature', 'list-unsubscribe',
        'x-developer-key', 'content-type'}


def test_keeps_only_received_headers_mentioning_the_list(tmp_path: Path) -> None:
    """Received headers after the list hop describe the subscriber's own mail setup."""
    private = 'from mx.private.example by inbox.private.example for <me@private.example>; Tue, 14 Mar 2023'
    run(tmp_path, make_msg(('Received', private), ('Received', LIST_RECEIVED)))
    [msg] = archived(tmp_path)
    assert msg.get_all('Received') == [LIST_RECEIVED]


def test_archived_message_is_byte_identical_apart_from_headers(tmp_path: Path) -> None:
    """Re-encoding the body would break the DKIM signatures we keep on purpose."""
    body = b'Caf\xe9 au lait\n'
    raw = make_msg(('Content-Type', 'text/plain; charset=iso-8859-1'),
                   ('Content-Transfer-Encoding', '8bit')).replace(b'Body text\n', body)
    run(tmp_path, raw)
    [msg] = archived(tmp_path)
    assert msg['Content-Transfer-Encoding'] == '8bit'
    assert msg.get_payload(decode=True) == body


@pytest.mark.parametrize(('listid', 'listids', 'accepted'), [
    (f'<{LISTID}>', [LISTID], True),
    ('Linux Kernel <LINUX-KERNEL.vger.kernel.org>', [LISTID.upper()], True),
    ('<linux-mm.kvack.org>', [LISTID], False),
    (f'<{LISTID}.example.org>', [LISTID], False),
    ('Linux MM <linux-mm.vger.kernel.org>', ['linux-*.vger.kernel.org'], True),
    ('<linux-mm.kvack.org>', ['linux-*.vger.kernel.org'], False),
    ('<anything.example.org>', [], True),
    (None, [], False),
], ids=['exact', 'case-insensitive', 'other-list', 'not-a-substring-match', 'glob', 'glob-miss',
        'any-list', 'any-list-needs-a-list-id'])
def test_list_id_matching(tmp_path: Path, listid: str | None, listids: list[str], accepted: bool) -> None:
    run(tmp_path, make_msg(listid=listid), listids=listids)
    assert len(archived(tmp_path)) == int(accepted)
    assert len(rejected(tmp_path)) == int(not accepted)


@pytest.mark.parametrize('hdrname', ['To', 'Cc', 'Resent-To', 'X-Mailing-List'])
def test_message_addressed_to_the_list_gets_our_list_id(tmp_path: Path, hdrname: str) -> None:
    """A subscriber may only have the copy of a cross-posted message that came via another list."""
    run(tmp_path, make_msg((hdrname, f'Kernel List <{LISTADDR}>'), listid='Linux MM <linux-mm.kvack.org>'))
    [msg] = archived(tmp_path)
    assert msg.get_all('List-Id') == [f'<{LISTID}>']


def test_list_address_can_be_given_for_nonstandard_list_ids(tmp_path: Path) -> None:
    run(tmp_path, make_msg(('To', 'odd@lists.example.org'), listid=None),
        listids=['odd-list.example.org', 'odd@lists.example.org'])
    [msg] = archived(tmp_path)
    assert msg['List-Id'] == '<odd-list.example.org>'


def test_glob_list_ids_only_match_list_id_headers(tmp_path: Path) -> None:
    """There is no way to know which List-Id a glob should stand for, so we don't guess."""
    run(tmp_path, make_msg(('To', LISTADDR), listid=None), listids=['linux-*.vger.kernel.org'])
    assert archived(tmp_path) == []


def test_to_and_cc_are_merged_and_deduplicated(tmp_path: Path) -> None:
    """Malformed mail can repeat To/Cc headers and addresses; archives need one clean header each."""
    run(tmp_path, make_msg(('To', 'Bob <bob@example.org>'),
                           ('To', 'Carol <carol@example.org>, Bob Again <BOB@example.org>'),
                           ('Cc', f'Bob <bob@example.org>, Dave <dave@example.org>, {LISTADDR}')))
    [msg] = archived(tmp_path)
    assert msg.get_all('To') == ['Bob <bob@example.org>, Carol <carol@example.org>']
    assert msg.get_all('Cc') == [f'Dave <dave@example.org>, {LISTADDR}']


def test_formataddr_drops_unencodable_real_name() -> None:
    assert lam.formataddr(('\udce9t\udce9', 'ete@example.org')) == 'ete@example.org'


def test_extra_headers_are_added(tmp_path: Path) -> None:
    """Extra headers are ours, so they must survive the header filter."""
    run(tmp_path, make_msg(), extrahdrs=[('X-Archive-Source', 'alice')])
    [msg] = archived(tmp_path)
    assert msg['X-Archive-Source'] == 'alice'


@pytest.mark.parametrize(('msg', 'knownids', 'reason'), [
    (make_msg(msgid=None), set(), 'No Message-ID'),
    (make_msg(), {'<one@example.org>'}, 'Duplicate Message-ID'),
    (make_msg(listid='<linux-mm.kvack.org>'), set(), 'No matching List-ID'),
    (make_msg(date=None), set(), 'No usable date'),
    (make_msg(date='not a date'), set(), 'No usable date'),
], ids=['no-msgid', 'duplicate', 'other-list', 'no-date', 'bogus-date'])
def test_rejected_messages_are_kept_unchanged(tmp_path: Path, msg: bytes, knownids: set[str], reason: str) -> None:
    """Rejects are for reviewing the script's decisions, so they must be the original message."""
    run(tmp_path, msg.replace(b'\n\n', b'\nX-Spam-Status: No\n\n', 1), knownids=lam.KnownMessages.from_ids(knownids))
    assert archived(tmp_path) == []
    [rej] = rejected(tmp_path)
    assert rej['X-Import-Rejected-Reason'] == reason
    assert rej['X-Spam-Status'] == 'No'


def test_resent_message_id_is_used_without_message_id(tmp_path: Path) -> None:
    knownids = lam.KnownMessages()
    run(tmp_path, make_msg(('Resent-Message-Id', ' <resent@example.org> '), msgid=None), knownids=knownids)
    assert len(archived(tmp_path)) == 1
    assert knownids.ids() == {'<resent@example.org>'}


@pytest.mark.parametrize(('second_body', 'archived_count'), [
    (b'Body text\n', 1),
    (b'A different message\n', 2),
], ids=['same-message-twice', 'reused-message-id'])
def test_duplicates_across_sources(tmp_path: Path, second_body: bytes, archived_count: int) -> None:
    """Copies of one message are archived once, but senders like Launchpad reuse
    Message-IDs for different messages, and those must all be kept."""
    sources = [str(write_mbox(tmp_path / 'source0.mbox', make_msg())),
               str(write_mbox(tmp_path / 'source1.mbox', make_msg().replace(b'Body text\n', second_body)))]
    (tmp_path / 'out').mkdir()
    knownids = lam.KnownMessages()
    lam.process_archives(sources, lam.MailboxWriter(str(tmp_path / 'out'), False), knownids, [LISTID],
                         str(tmp_path / 'rejected.mbox'), [])
    msgs = archived(tmp_path)
    assert len(msgs) == archived_count
    assert len(rejected(tmp_path)) == 2 - archived_count
    assert '<one@example.org>' in knownids.ids()
    if archived_count == 2:
        # public-inbox files a reused Message-ID's message under the next one,
        # so it must have its own; stable, so re-runs know the message.
        assert msgs[0].get_all('Message-Id') == ['<one@example.org>']
        reused, extra = msgs[1].get_all('Message-Id', [])
        assert reused == '<one@example.org>'
        assert extra.endswith('@msgid-reused>')
        assert extra in knownids.ids()


@pytest.mark.parametrize(('received', 'mbxname'), [
    ([LIST_RECEIVED], '2023-03.mbx'),
    ([], '2020-01.mbx'),
    (['from somewhere; garbage', LIST_RECEIVED], '2023-03.mbx'),
], ids=['received-date', 'date-header', 'skips-bad-received-date'])
def test_messages_are_filed_by_received_month(tmp_path: Path, received: list[str], mbxname: str) -> None:
    """The Date header is set by the sender and is often bogus, so delivery time wins."""
    run(tmp_path, make_msg(*[('Received', value) for value in received]))
    assert [mbx.name for mbx in (tmp_path / 'out').iterdir()] == [mbxname]


def test_counts_only_new_messages(tmp_path: Path) -> None:
    """Archives are appended to, so the count must not include what was already there."""
    outbox = tmp_path / 'out' / '2020-01.mbx'
    outbox.parent.mkdir()
    write_mbox(outbox, make_msg(msgid='<old@example.org>'))
    counts = run(tmp_path, make_msg(msgid='<new1@example.org>'), make_msg(msgid='<new2@example.org>'))
    assert counts == {str(outbox): 2}
    assert len(archived(tmp_path)) == 3


def test_maildir_source_and_output(tmp_path: Path) -> None:
    source = mailbox.Maildir(tmp_path / 'source')
    source.add(make_msg())
    outdir = tmp_path / 'out'
    counts = lam.process_archives([str(tmp_path / 'source')], lam.MailboxWriter(str(outdir), True), lam.KnownMessages(),
                                  [LISTID], None, [])
    assert counts == {str(outdir): 1}
    assert [msg['Message-Id'] for msg in mailbox.Maildir(outdir)] == ['<one@example.org>']


@pytest.mark.parametrize('knownids', [
    {'<b@example.org>', '<a@example.org>'},
    {'<caf\u00e9@example.org>', '<caf\udce9@example.org>'},
], ids=['ascii', 'utf8-and-undecodable-8bit'])
def test_known_ids_round_trip(tmp_path: Path, knownids: set[str]) -> None:
    path = tmp_path / 'known.list'
    lam.save_known_ids(str(path), knownids)
    assert lam.load_known_ids(str(path)) == knownids


def test_known_ids_from_public_inbox_msgmap(tmp_path: Path) -> None:
    """public-inbox stores Message-IDs without the angle brackets."""
    path = tmp_path / 'msgmap.sqlite3'
    with sqlite3.connect(path) as dbconn:
        dbconn.execute('CREATE TABLE msgmap (num INTEGER PRIMARY KEY, mid VARCHAR(255))')
        dbconn.execute("INSERT INTO msgmap (mid) VALUES ('a@example.org')")
    dbconn.close()
    assert lam.load_known_ids(str(path)) == {'<a@example.org>'}


def test_main_records_archived_ids(tmp_path: Path) -> None:
    """A second run over the same sources must not archive anything again."""
    source = write_mbox(tmp_path / 'source.mbox', make_msg())
    known = tmp_path / 'known.list'
    argv = ['-s', str(source), '-e', str(tmp_path / 'out'), '-k', str(known), '-l', LISTID,
            '-x', 'X-Archive-Source: test', '-r', str(tmp_path / 'rejected.mbox')]
    lam.main(argv)
    assert known.read_text() == '<one@example.org>\n'
    [msg] = archived(tmp_path)
    assert msg['X-Archive-Source'] == 'test'

    lam.main(argv)
    assert len(archived(tmp_path)) == 1
    assert len(rejected(tmp_path)) == 1


def inbox_messages(v2path: Path) -> list[email.message.Message]:
    ezpi = pytest.importorskip('ezpi')
    return [email.message_from_bytes(raw) for _, _, raw in ezpi.iter_messages(str(v2path))]


def test_public_inbox_output_skips_what_the_inbox_has(tmp_path: Path) -> None:
    """Backfills often overlap with what the live list already archived."""
    ezpi = pytest.importorskip('ezpi')
    v2path = tmp_path / 'inbox'
    ezpi.add_rfc822_v2(str(v2path), make_msg(msgid='<live@example.org>'))
    source = write_mbox(tmp_path / 'source.mbox', make_msg(msgid='<live@example.org>'),
                       make_msg(('X-Spam-Status', 'No'), msgid='<old@example.org>'))
    argv = ['-s', str(source), '-p', str(v2path), '-l', LISTID, '-r', str(tmp_path / 'rejected.mbox')]
    lam.main(argv)
    lam.main(argv)
    msgs = inbox_messages(v2path)
    assert [msg['Message-Id'] for msg in msgs] == ['<live@example.org>', '<old@example.org>']
    assert msgs[1]['X-Spam-Status'] is None
    assert [rej['X-Import-Rejected-Reason'] for rej in rejected(tmp_path)] == ['Duplicate Message-ID'] * 3


def test_public_inbox_keeps_reused_message_ids(tmp_path: Path) -> None:
    """The inbox knows its messages' bodies, so only real copies count as duplicates."""
    ezpi = pytest.importorskip('ezpi')
    v2path = tmp_path / 'inbox'
    ezpi.add_rfc822_v2(str(v2path), make_msg())
    source = write_mbox(tmp_path / 'source.mbox', make_msg(), make_msg().replace(b'Body text', b'Other text'))
    argv = ['-s', str(source), '-p', str(v2path), '-l', LISTID, '-r', str(tmp_path / 'rejected.mbox')]
    lam.main(argv)
    lam.main(argv)
    msgs = inbox_messages(v2path)
    assert [msg.get_payload() for msg in msgs] == ['Body text\n', 'Other text\n']
    assert len(msgs[1].get_all('Message-Id', [])) == 2


def test_public_inbox_rejects_what_it_cannot_archive(tmp_path: Path) -> None:
    """public-inbox needs a From and a Subject; such messages must not just vanish."""
    pytest.importorskip('ezpi')
    source = write_mbox(tmp_path / 'source.mbox', make_msg().replace(b'Subject: Hello\n', b''))
    lam.main(['-s', str(source), '-p', str(tmp_path / 'inbox'), '-l', LISTID, '-r', str(tmp_path / 'rejected.mbox')])
    assert not (tmp_path / 'inbox').exists() or inbox_messages(tmp_path / 'inbox') == []
    [rej] = rejected(tmp_path)
    assert rej['X-Import-Rejected-Reason'].startswith('Not archivable')
