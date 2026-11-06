#!/usr/bin/env python3
#
# List archive collector
#
# This is a tool to collect archive from networked non-mbox sources, such as:
# - mailman (pipermail and hyperkitty)
# - marc.info
# - nntp
#
# After the archives are collected, you can feed them to list-archive-maker.py
#
# Example usage:
#   list-archive-collector.py -o apparmor.mbox -i apparmor.lists.ubuntu.com \
#       -c '[apparmor]' mailman -u https://lists.ubuntu.com/archives/apparmor/
#
# Author:  Konstantin Ryabitsev <konstantin@linuxfoundation.org>
#

import argparse
import calendar
import codecs
import email.header
import email.message
import email.policy
import email.utils
import gzip
import html.parser
import logging
import mailbox
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from typing import Any

import charset_normalizer
import requests
from requests.adapters import HTTPAdapter
from urllib3 import Retry

__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'
__VERSION__ = '2.0'

logger = logging.getLogger(__name__)

# Where does marc.info live?
MARCURL = 'https://marc.info'
# Hitting marc.info more often than this gets you auto-banned
MARC_MIN_NICE = 0.5
# Seconds to wait for a remote server before giving up on a request
HTTP_TIMEOUT = 120

# Write in 8bit, without folding, so messages stay as close to what we
# received as possible.
EMLPOLICY = email.policy.EmailPolicy(utf8=True, cte_type='8bit', max_line_length=None)

# The "From " line that starts each message in an mbox. Pipermail mangles the
# sender address with " at ", and pipermail and marc.info do not escape body
# lines that start with "From ", so we only trust lines with a full date.
FROM_LINE = re.compile(rb'^From \S+(?: at \S+)? +\w{3} \w{3} [ \d]?\d \d\d:\d\d:\d\d \d{4}(?: [+-]\d{4})?\r?\n',
                       re.MULTILINE)

# Pipermail turns user@example.org into "user at example.org". We only undo
# that for text shaped like an address, so code like "<object at 0x1234>" and
# prose like "look at foo.c" survive.
MANGLED_ADDR = rb'([\w.+-]+) at ([\w-]+(?:\.[\w-]+)+)(?![\w.-])'
MANGLED_IN_ANGLES = re.compile(rb'(<(?:mailto:)?)' + MANGLED_ADDR)
MANGLED_ADDR_LINE = re.compile(rb'^(?:>[> ]*)?(?:From|To|Cc|Reply-To): .*$', re.MULTILINE | re.IGNORECASE)

# marc.info mangles "@" and "." in these headers; other headers, like
# Subject, can legitimately contain " () " and " ! ".
MARC_ADDR_HEADERS = {'from', 'to', 'cc', 'reply-to', 'sender', 'message-id', 'in-reply-to', 'references'}

# Headers we copy from the lore.kernel.org copy of a message, if ours lacks them
LOOKASIDE_HEADERS = ['To', 'Cc', 'References', 'In-Reply-To', 'User-Agent', 'X-Mailer']

PIPERMAIL_MONTHS = {name: num for num, name in enumerate(calendar.month_name) if name}

Mailbox = mailbox.mbox | mailbox.Maildir

# Used for our requests session
REQSESSION: requests.Session | None = None


class CollectorError(Exception):
    """A problem that stops collection, with a message for the user."""


class SpamCheckError(CollectorError):
    pass


def get_requests_session() -> requests.Session:
    global REQSESSION
    if REQSESSION is None:
        REQSESSION = requests.session()
        retry = Retry(connect=3, backoff_factor=1)
        adapter = HTTPAdapter(max_retries=retry)
        REQSESSION.mount('http://', adapter)
        REQSESSION.mount('https://', adapter)
        REQSESSION.headers.update({'User-Agent': f'list-archive-collector/{__VERSION__}'})
    return REQSESSION


def http_get(url: str) -> bytes:
    resp = get_requests_session().get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.content


class LinkParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = list()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == 'a':
            href = dict(attrs).get('href')
            if href:
                self.links.append(href)


def get_links(page: bytes) -> list[str]:
    parser = LinkParser()
    parser.feed(page.decode('utf-8', errors='replace'))
    parser.close()
    return parser.links


def clean_header(hdrval: str | None) -> str:
    if hdrval is None:
        return ''

    decoded = ''
    for hstr, hcs in email.header.decode_header(hdrval):
        if isinstance(hstr, str):
            decoded += hstr
            continue
        try:
            decoded += hstr.decode(hcs or 'utf-8', errors='replace')
        except LookupError:
            # Unknown charset name, so take our best shot
            decoded += hstr.decode('utf-8', errors='replace')
    return re.sub(r'\n?\s+', ' ', decoded).strip()


def mime_charset(codec: str) -> str:
    # Python codec names are not always the names mail clients understand
    name = codecs.lookup(codec).name
    if name.startswith('cp125'):
        return f'windows-{name[2:]}'
    if match := re.fullmatch(r'iso8859-(\d+)', name):
        return f'iso-8859-{match.group(1)}'
    return name


def guess_charset(data: bytes) -> str:
    try:
        data.decode('utf-8')
        # Valid UTF-8 with non-ASCII in it is almost never anything else
        return 'utf-8'
    except UnicodeDecodeError:
        pass
    best = charset_normalizer.from_bytes(data).best()
    if best is None:
        # Every byte is valid in latin-1, so nothing gets lost
        return 'iso-8859-1'
    return mime_charset(best.encoding)


def set_missing_charset(msg: email.message.Message) -> None:
    """Declare the charset of 8bit bodies that arrived without a Content-Type."""
    if msg.is_multipart() or msg.get('Content-Type'):
        return
    payload = msg.get_payload(decode=True)
    if not isinstance(payload, bytes) or payload.isascii():
        return
    if not msg.get('MIME-Version'):
        msg['MIME-Version'] = '1.0'
    msg['Content-Type'] = f'text/plain; charset="{guess_charset(payload)}"'
    if not msg.get('Content-Transfer-Encoding'):
        msg['Content-Transfer-Encoding'] = '8bit'


def split_mbox(data: bytes) -> list[bytes]:
    """Split an mbox into raw messages, without their "From " lines."""
    starts = list(FROM_LINE.finditer(data))
    msgs = list()
    for match, nextmatch in zip(starts, starts[1:] + [None]):
        raw = data[match.end():nextmatch.start() if nextmatch else len(data)]
        # The empty line before the next "From " line is a separator
        if raw.endswith(b'\n\n'):
            raw = raw[:-1]
        msgs.append(raw)
    return msgs


def unmangle_pipermail_from(hdrval: bytes, charset: str) -> str | None:
    # Pipermail writes "user at example.org (Real Name)". The real name may be
    # RFC 2047 encoded or raw 8bit in whatever charset the sender used.
    text = clean_header(hdrval.decode(charset, errors='replace'))
    text = re.sub(MANGLED_ADDR.decode(), r'\1@\2', text)
    if match := re.fullmatch(r'(\S+@\S+) \((.*)\)', text):
        name, addr = match.group(2), match.group(1)
    else:
        name, addr = email.utils.parseaddr(text)
    if not addr:
        return None
    return email.utils.formataddr((name, addr))


def parse_pipermail_message(raw: bytes) -> email.message.Message:
    headers, sep, body = raw.partition(b'\n\n')
    # A few raw 8bit bytes in a header are too little to guess a charset
    # from, so use what the whole message tells us.
    charset = 'ascii' if raw.isascii() else guess_charset(raw)
    newheaders = list()
    for line in headers.split(b'\n'):
        if line[:6].lower() == b'from: ':
            fixed = unmangle_pipermail_from(line[6:], charset)
            if fixed is not None:
                line = b'From: ' + fixed.encode()
        newheaders.append(line)
    body = MANGLED_IN_ANGLES.sub(rb'\1\2@\3', body)
    body = MANGLED_ADDR_LINE.sub(lambda match: re.sub(MANGLED_ADDR, rb'\1@\2', match.group(0)), body)

    msg = email.message_from_bytes(b'\n'.join(newheaders) + sep + body)
    irt = msg.get('In-Reply-To')
    if irt and not str(irt).startswith('<'):
        msg.replace_header('In-Reply-To', f'<{irt}>')
    set_missing_charset(msg)
    return msg


def guess_transfer_encoding(body: bytes) -> str | None:
    lines = body.strip().splitlines()
    if not lines:
        return None
    # Base64 bodies are wrapped at a fixed width. Requiring that, or a long
    # single line, keeps short words like "Test" from being "decoded".
    if (all(re.fullmatch(rb'[A-Za-z0-9+/]+={0,2}', line) for line in lines)
            and len({len(line) for line in lines[:-1]}) <= 1
            and (len(lines) > 1 or len(lines[0]) >= 40)
            and sum(len(line) for line in lines) % 4 == 0):
        return 'base64'
    # Quoted-printable never has "=" followed by anything but two hex digits
    # or a line break; plain text and code almost always do.
    if re.search(rb'=[0-9A-F]{2}', body) and not re.search(rb'=(?![0-9A-F]{2}|\r?\n|$)', body):
        return 'quoted-printable'
    return None


def parse_marc_message(raw: bytes, marc_list_id: str, msgnum: str) -> email.message.Message:
    # marc.info breaks MIME by adding the "--" of the delimiter lines to the boundary
    raw = re.sub(rb'(\nContent-Type: multipart/\w+;\s*boundary=")--', rb'\1', raw, count=1)
    msg = email.message_from_bytes(raw)

    newhdrs = list()
    for hdrname, hdrval in msg.raw_items():
        lhdrname = hdrname.lower()
        if lhdrname == 'to':
            # marc.info puts the list name here, which is not an address
            continue
        if lhdrname in MARC_ADDR_HEADERS:
            hdrval = hdrval.replace(' () ', '@').replace(' ! ', '.')
        newhdrs.append((hdrname, hdrval))
    for hdrname in set(msg.keys()):
        del msg[hdrname]
    for hdrname, hdrval in newhdrs:
        msg[hdrname] = hdrval

    if not msg.get('Message-Id'):
        msg['Message-Id'] = f'<marc-{marc_list_id}-{msgnum}@msgid-missing>'

    # marc.info drops Content-Transfer-Encoding, so declare the one the body is in
    if not msg.is_multipart() and not msg.get('Content-Transfer-Encoding'):
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes) and (cte := guess_transfer_encoding(payload)):
            msg['Content-Transfer-Encoding'] = cte
    set_missing_charset(msg)
    return msg


def lore_get_message(msgid: str) -> email.message.Message | None:
    url = f'https://lore.kernel.org/all/{urllib.parse.quote(msgid, safe="@")}/raw'
    resp = get_requests_session().get(url, timeout=HTTP_TIMEOUT)
    if resp.status_code != 200:
        return None
    logger.debug('    found on lore')
    return email.message_from_bytes(resp.content)


def lookaside_fillin(msg: email.message.Message) -> bool:
    lmsg = lore_get_message(str(msg.get('Message-Id', '')).strip().strip('<>'))
    if lmsg is None:
        return False
    for wanthdr in LOOKASIDE_HEADERS:
        if not msg.get(wanthdr) and lmsg.get(wanthdr):
            msg[wanthdr] = lmsg[wanthdr]
    return True


def clean_subject(subject: str, prefixes: list[str]) -> str:
    # Only remove prefixes at the start of the subject, or right after "Re: "
    for prefix in prefixes:
        if subject.startswith(prefix):
            subject = subject[len(prefix):].strip()
        elif subject.startswith(f'Re: {prefix}'):
            subject = 'Re: ' + subject[len(prefix) + 4:].strip()
    return subject


def is_spam(bmsg: bytes) -> bool:
    try:
        result = subprocess.run(['spamc', '-c'], input=bmsg, capture_output=True)
    except OSError as ex:
        raise SpamCheckError(f'Could not run spamc: {ex}') from ex
    # spamc -c exits with 1 for spam; anything else but 0 means the check failed
    if result.returncode not in (0, 1):
        raise SpamCheckError(f'spamc failed with exit code {result.returncode}')
    return result.returncode == 1


class ArchiveWriter:
    """Add the finishing touches to collected messages and write them out."""

    def __init__(self, path: str, as_maildir: bool, listid: str | None = None, toaddr: str | None = None,
                 lookaside: bool = False, checkspam: bool = False, cleansubj: list[str] | None = None) -> None:
        self.mbx: Mailbox = mailbox.Maildir(path) if as_maildir else mailbox.mbox(path)
        self.listid = listid
        self.toaddr = toaddr
        self.lookaside = lookaside
        self.checkspam = checkspam
        self.cleansubj = cleansubj or list()
        self.counts = {'added': 0, 'spam': 0, 'unwritable': 0}

    def add(self, msg: email.message.Message) -> None:
        if self.listid:
            del msg['List-Id']
            msg['List-Id'] = f'<{self.listid}>'
        if self.lookaside:
            lookaside_fillin(msg)
        if self.toaddr and not msg.get('To'):
            msg['To'] = self.toaddr
        if self.cleansubj and msg.get('Subject'):
            oldsubj = clean_header(str(msg['Subject']))
            newsubj = clean_subject(oldsubj, self.cleansubj)
            if newsubj != oldsubj:
                if not newsubj.isascii():
                    newsubj = email.header.Header(newsubj, 'utf-8').encode()
                msg.replace_header('Subject', newsubj)

        subject = clean_header(str(msg.get('Subject', '')))
        try:
            # as_bytes keeps undecodable 8bit content as it was received
            bmsg = msg.as_bytes(policy=EMLPOLICY)
        except Exception:
            # Decades-old mail finds endless ways to break the email module;
            # one bad message must not end the whole collection.
            logger.warning('    unwritable, skipped: %s', subject, exc_info=logger.isEnabledFor(logging.DEBUG))
            self.counts['unwritable'] += 1
            return

        if self.checkspam and is_spam(bmsg):
            logger.info('    spam: %s', subject)
            self.counts['spam'] += 1
            return

        logger.info('    added: %s', subject)
        self.mbx.add(bmsg)
        self.counts['added'] += 1

    def close(self) -> None:
        self.mbx.close()


def pipermail_month_urls(index_url: str, page: bytes) -> list[str]:
    # Without the trailing slash, urljoin would drop the list name
    index_url = index_url.rstrip('/') + '/'
    months = list()
    for href in get_links(page):
        match = re.fullmatch(r'(\d{4})-(\w+)\.txt(?:\.gz)?', href)
        if match and match.group(2) in PIPERMAIL_MONTHS:
            months.append((int(match.group(1)), PIPERMAIL_MONTHS[match.group(2)], href))
    return [urllib.parse.urljoin(index_url, href) for _, _, href in sorted(months)]


def hyperkitty_month_urls(index_url: str, page: bytes) -> list[str]:
    export_url = urllib.parse.urljoin(index_url.rstrip('/') + '/', 'export/export.mbox.gz')
    months = set()
    for href in get_links(page):
        if match := re.search(r'/(\d{4})/(\d{1,2})/$', href):
            months.add((int(match.group(1)), int(match.group(2))))
    urls = list()
    for year, month in sorted(months):
        nextyear, nextmonth = (year + 1, 1) if month == 12 else (year, month + 1)
        urls.append(f'{export_url}?start={year}-{month:02d}-01&end={nextyear}-{nextmonth:02d}-01')
    return urls


def read_archive(source: str) -> bytes:
    if '://' in source:
        logger.info('Grabbing %s', source)
        data = http_get(source)
    else:
        with open(source, 'rb') as fh:
            data = fh.read()
    if data.startswith(b'\x1f\x8b'):
        data = gzip.decompress(data)
    return data


def collect_mailman(url: str, mailman3: bool, writer: ArchiveWriter) -> None:
    if '://' not in url:
        sources = [url]
    elif mailman3:
        sources = hyperkitty_month_urls(url, http_get(url))
    else:
        sources = pipermail_month_urls(url, http_get(url))
    if not sources:
        raise CollectorError(f'Could not find any archives listed at {url}')

    for source in sources:
        try:
            data = read_archive(source)
        except (EOFError, gzip.BadGzipFile) as ex:
            logger.warning('  corrupted archive %s, skipped: %s', source, ex)
            continue
        rawmsgs = split_mbox(data)
        logger.info('  %s messages in %s', len(rawmsgs), source)
        for raw in rawmsgs:
            # Hyperkitty exports real mail, which pipermail's fixes would only damage
            writer.add(email.message_from_bytes(raw) if mailman3 else parse_pipermail_message(raw))


class MarcClient:
    """Fetch marc.info pages without hitting it hard enough to get banned."""

    def __init__(self, session: Any, nice: float) -> None:
        self.session = session
        self.nice = nice
        self.lastget: float | None = None

    def get(self, url: str) -> bytes:
        if self.lastget is not None:
            time.sleep(max(0.0, self.nice - (time.monotonic() - self.lastget)))
        try:
            resp = self.session.get(url, timeout=HTTP_TIMEOUT)
        finally:
            self.lastget = time.monotonic()
        resp.raise_for_status()
        content: bytes = resp.content
        return content


def marc_paginated_links(client: MarcClient, url: str) -> list[str]:
    links = list()
    page = 1
    while True:
        nextpage = f'r={page + 1}&'
        pagelinks = get_links(client.get(url.format(page=page)))
        links += [href for href in pagelinks if nextpage not in href]
        if not any(nextpage in href for href in pagelinks):
            return links
        page += 1
        logger.info('  ... page %s', page)


def marc_numbers(links: list[str], param: str) -> list[str]:
    # Keep the page order, but each number only once
    found = dict()
    for href in links:
        if match := re.search(rf'[?&]{param}=(\d+)\D', href):
            found[match.group(1)] = True
    return list(found)


def collect_marc(client: MarcClient, listname: str, writer: ArchiveWriter) -> None:
    logger.info('Grabbing main index for %s', listname)
    months = sorted(marc_numbers(get_links(client.get(f'{MARCURL}/?l={listname}&w=1')), 'b'))

    havenums = set()
    for oldmsg in writer.mbx:
        if match := re.search(r'm=(\d+)', str(oldmsg.get('X-MARC-Message', ''))):
            havenums.add(match.group(1))
    logger.info('Found %s messages already collected', len(havenums))

    msgnums: list[str] = list()
    thdnums: list[str] = list()
    for month in months:
        logger.info('Grabbing month %s', month)
        links = marc_paginated_links(client, f'{MARCURL}/?l={listname}&b={month}&r={{page}}&w=1')
        msgnums += marc_numbers(links, 'm')
        thdnums += marc_numbers(links, 't')

    def grab(msgnum: str) -> email.message.Message | None:
        logger.info('  grabbing message %s', msgnum)
        try:
            raw = client.get(f'{MARCURL}/?l={listname}&m={msgnum}&q=mbox')
        except requests.RequestException as ex:
            logger.warning('  could not grab message %s: %s', msgnum, ex)
            return None
        return parse_marc_message(raw, listname, msgnum)

    for thdnum in thdnums:
        logger.info('Grabbing thread %s', thdnum)
        links = marc_paginated_links(client, f'{MARCURL}/?t={thdnum}&r={{page}}&w=1')
        # marc.info lists threads newest first, so the last message starts it
        tnums = list(reversed(marc_numbers([href for href in links if listname in href], 'm')))
        irt = None
        for tnum in tnums:
            if tnum in msgnums:
                msgnums.remove(tnum)
            if tnum in havenums:
                logger.debug('Already have %s', tnum)
                continue
            msg = grab(tnum)
            if msg is None:
                continue
            # marc.info often loses the threading headers, so hang replies off the thread start
            if irt is None:
                irt = msg.get('Message-Id')
            elif not msg.get('References'):
                msg['References'] = irt
                msg['In-Reply-To'] = irt
            writer.add(msg)

    logger.info('Grabbing remaining unthreaded messages')
    for msgnum in msgnums:
        if msgnum in havenums:
            logger.debug('Already have %s', msgnum)
            continue
        msg = grab(msgnum)
        if msg is not None:
            writer.add(msg)


def collect_nntp(url: str, writer: ArchiveWriter) -> None:
    try:
        import nntplib
    except ImportError as ex:
        raise CollectorError('nntplib is not available; on Python 3.13+ install standard-nntplib') from ex

    # Expect in format nntp://news.gmane.io/gmane.linux.network
    parsed = urllib.parse.urlsplit(url)
    host, group = parsed.netloc, parsed.path.strip('/')
    logger.info('Connecting to %s', host)
    # Old archives have header lines far longer than nntplib allows by
    # default, and this private limit is the only way to raise it.
    setattr(nntplib, '_MAXLINE', 1 << 20)
    server = nntplib.NNTP(host)
    try:
        _, _, first, last, _ = server.group(group)
        for num in range(int(first), int(last) + 1):
            try:
                _, info = server.article(num)
            except nntplib.NNTPTemporaryError as ex:
                # Usually an expired or deleted article
                logger.debug('  skipping article %s: %s', num, ex)
                continue
            msg = email.message_from_bytes(b'\n'.join(info.lines))
            logger.info('  processing: %s, %s/%s', msg.get('Message-Id'), num, last)
            # gmane keeps the headers it replaced as Original-*
            newhdrs = [(name.removeprefix('Original-'), value) for name, value in msg.raw_items()]
            for hdrname in set(msg.keys()):
                del msg[hdrname]
            for hdrname, hdrval in newhdrs:
                msg[hdrname] = hdrval
            writer.add(msg)
    finally:
        server.quit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Collect external mail archives into a local mbox',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('-i', '--listid',
                        help='List-Id header to inject into the messages')
    parser.add_argument('-t', '--to',
                        help=('Value to put into the To: header, if missing '
                              '(defaults to list-id with first . replaced with @)'))
    parser.add_argument('-k', '--lookaside', action='store_true', default=False,
                        help='Attempt to look up matching lore messages for missing to/cc headers')
    parser.add_argument('-s', '--checkspam', action='store_true', default=False,
                        help='Run spamc to check messages for spam before adding')
    parser.add_argument('-o', '--out', required=True,
                        help='Filename of the mailbox file to write out')
    parser.add_argument('-m', '--as-maildir', action='store_true', default=False,
                        help='Output as maildir instead of mbox')
    # Repeat -c for more strings; nargs='+' would swallow the subcommand name
    parser.add_argument('-c', '--clean-subject', action='append', default=list(),
                        help='Remove this string from subjects (e.g. [listname]), can be repeated')
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Only output errors')
    parser.add_argument('-v', '--verbose', action='store_true', default=False,
                        help='Add more debugging info to the output')

    subparsers = parser.add_subparsers(help='sub-command help', dest='subcmd', required=True)

    sp_mm = subparsers.add_parser('mailman', help='Collect mailman archives',
                                  formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    sp_mm.add_argument('-u', '--url', required=True,
                       help='Mailman archive index URL, or a local archive file')
    sp_mm.add_argument('-3', '--mailman3', action='store_true', default=False,
                       help='This is a mailman3 site')

    sp_marc = subparsers.add_parser('marcinfo', help='Collect marc.info archives',
                                    formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    sp_marc.add_argument('-l', '--listname', required=True,
                         help='Marc.info list name (?l= parameter)')
    sp_marc.add_argument('-n', '--nice', default=1.0, type=float,
                         help='Seconds to sleep between requests')

    sp_nntp = subparsers.add_parser('nntp', help='Collect NNTP archives',
                                    formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    sp_nntp.add_argument('-u', '--url', required=True,
                         help='NNTP url (e.g. nntp://news.gmane.io/gmane.linux.kernel)')
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(format='%(message)s',
                        level=logging.ERROR if args.quiet else logging.DEBUG if args.verbose else logging.INFO)

    if args.subcmd == 'marcinfo' and args.nice < MARC_MIN_NICE:
        parser.error(f'Hitting marc.info every {args.nice}s will get you auto-banned. Try {MARC_MIN_NICE} or above.')
    if args.checkspam and not shutil.which('spamc'):
        parser.error('--checkspam needs spamc, which was not found')

    toaddr = args.to
    if not toaddr and args.listid:
        toaddr = args.listid.replace('.', '@', 1)

    writer = ArchiveWriter(args.out, args.as_maildir, listid=args.listid, toaddr=toaddr,
                           lookaside=args.lookaside, checkspam=args.checkspam, cleansubj=args.clean_subject)
    try:
        if args.subcmd == 'mailman':
            collect_mailman(args.url, args.mailman3, writer)
        elif args.subcmd == 'marcinfo':
            collect_marc(MarcClient(get_requests_session(), args.nice), args.listname, writer)
        else:
            collect_nntp(args.url, writer)
    except (CollectorError, requests.RequestException) as ex:
        logger.critical('%s', ex)
        sys.exit(1)
    finally:
        writer.close()
        logger.info('Wrote %s messages to %s (%s spam, %s unwritable)', writer.counts['added'], args.out,
                    writer.counts['spam'], writer.counts['unwritable'])


if __name__ == '__main__':
    main()
