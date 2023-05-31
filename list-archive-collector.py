#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# List archive collector
#
# This is a tool to collect archive from networked non-mbox sources, such as:
# - mailman
# - marc.info
# - nntp
#
# After the archives are collected, you can feed them to list-archive-maker.py
#
# Author:  Konstantin Ryabitsev <konstantin@linuxfoundation.org>
#

import os
import sys
import mailbox
import email.utils
import email.policy
import email.header
import time
import re
import quopri
import base64
import gzip
import io
import requests
import logging
import subprocess
import argparse

try:
    import cchardet as chardet  # noqa
except ImportError:
    import chardet  # noqa

from tempfile import mkstemp
from bs4 import BeautifulSoup # noqa
from requests.adapters import HTTPAdapter
from urllib3 import Retry

from typing import Optional, Union, List, Set

from email import charset
charset.add_charset('utf-8', None)

# Used for our requests session
REQSESSION = None

__VERSION__ = '1.0'
# Where des marc.info live?
MARCURL = 'https://marc.info'
# Wait this many seconds between requests to marc.info, to avoid triggering
# anti-abuse blocks (and to just be nice)
MARCNICE = 1
LASTMARC = None

# Set our own policy
EMLPOLICY = email.policy.EmailPolicy(utf8=True, cte_type='8bit', max_line_length=None)

logger = logging.getLogger(__name__)


def clean_header(hdrval: str) -> Optional[str]:
    if hdrval is None:
        return ''

    decoded = ''
    for hstr, hcs in email.header.decode_header(hdrval):
        if hcs is None:
            hcs = 'utf-8'
        try:
            decoded += hstr.decode(hcs, errors='replace')
        except LookupError:
            # Try as utf-u
            decoded += hstr.decode('utf-8', errors='replace')
        except (UnicodeDecodeError, AttributeError):
            decoded += hstr
    new_hdrval = re.sub(r'\n?\s+', ' ', decoded)
    return new_hdrval.strip()


def get_requests_session() -> requests.Session:
    global REQSESSION
    if REQSESSION is None:
        REQSESSION = requests.session()
        retry = Retry(connect=3, backoff_factor=1)
        adapter = HTTPAdapter(max_retries=retry)
        REQSESSION.mount('http://', adapter)
        REQSESSION.mount('https://', adapter)
        headers = {
            'User-Agent': f'lore-archive-maker/{__VERSION__}',
        }
        REQSESSION.headers.update(headers)

    return REQSESSION


def lore_get_message(msgid: str) -> email.message.Message:
    # See where we're redirected
    rurl = f'https://lore.kernel.org/r/{msgid}'
    rses = get_requests_session()
    resp = rses.head(rurl)
    if resp.status_code < 300 or resp.status_code > 400:
        # Not known on lore
        raise LookupError
    # Pop msgid from the end of the redirect
    msgurl = resp.headers['Location'] + 'raw'
    resp.close()
    resp = rses.get(msgurl)
    msg = email.message_from_bytes(resp.content)
    logger.info('    found on lore')
    return msg


def lookaside_fillin(msg: email.message.Message) -> bool:
    wanthdrs = [
        'To',
        'Cc',
        'References',
        'In-Reply-To',
        'User-Agent',
        'X-Mailer',
    ]
    msgid = str(msg.get('Message-Id')).strip('<>')
    try:
        lmsg = lore_get_message(msgid)
    except LookupError:
        return False

    for wanthdr in wanthdrs:
        if not msg.get(wanthdr) and lmsg.get(wanthdr):
            msg[wanthdr] = lmsg.get(wanthdr)

    return True


def marc_get_message(marc_list_id: str, msgnum: str, listid: str, toaddr: str,
                     lookaside: bool) -> email.message.Message:
    url = f'{MARCURL}/?l={marc_list_id}&m={msgnum}&q=mbox'
    logger.info('  grabbing message %s', msgnum)
    resp = marc_nice_get(url)
    rawmsg = resp.content
    multipart = False
    if rawmsg.find(b'\nContent-Type: multipart/mixed;') > 0:
        multipart = True
        # marc.info breaks MIME by incorrectly writing boundary headers
        rawmsg = rawmsg.replace(b'\nContent-Type: multipart/mixed; boundary="--',
                                b'\nContent-Type: multipart/mixed; boundary="', 1)
        # We don't need to fix charset for multipart/mixed messages

    msg = email.message_from_bytes(rawmsg)
    if not msg.get('Message-Id'):
        msg['Message-Id'] = f'<marc-{marc_list_id}-{msgnum}@msgid-missing>'

    hdrs = list()

    for hdrname, hdrval in list(msg._headers):  # noqa
        if hdrname == 'To':
            # Useless, we throw it out
            continue
        elif hdrval.find(' () ') and (hdrval.find(' ! ') or hdrval.find('<')):
            # marc.info mangles @ and . in email addresses with
            # the above values. Unmangle them back.
            hdrval = hdrval.replace(' () ', '@').replace(' ! ', '.')
        hdrs.append((hdrname, hdrval))
    msg._headers = hdrs # noqa

    # Marc.info removes content-transfer-encoding headers, so try to figure out
    # what format the raw message is in before trying to add it to the mailbox
    if not multipart:
        payload = msg.get_payload(decode=True)
        # Try to base64 decode it first
        dec = None
        try:
            dec = base64.b64decode(payload, validate=True)
            if dec != payload:
                msg.set_payload(dec)
        except: # noqa
            pass

        if not dec:
            try:
                dec = quopri.decodestring(payload)
                if dec != payload:
                    msg.set_payload(dec)
            except ValueError:
                pass

    if listid:
        msg['List-Id'] = f'<{listid}>'

    if lookaside:
        lookaside_fillin(msg)

    if not msg.get('To'):
        msg['To'] = toaddr

    return msg


def check_if_spam(bmsg: bytes) -> bool:
    if not os.path.exists('/usr/bin/spamc'):
        return False

    logger.info('    checking for spam')
    args = ['/usr/bin/spamc', '-c']
    logger.debug('Running %s' % ' '.join(args))

    pp = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pp.communicate(input=bmsg)
    if pp.returncode == 0:
        return False

    return True


def add_msg_to_mbx(msg: email.message.Message, mbx: Union[mailbox.Mailbox, mailbox.Maildir],
                   checkspam: bool, cleansubj: Optional[List[str]]) -> None:
    if cleansubj:
        for remstr in cleansubj:
            oldsubj = clean_header(msg.get('Subject', ''))
            # We only remove it if it's ^thatsring, or ^Re: thatstring
            if oldsubj.startswith(remstr):
                msg.replace_header('Subject', oldsubj.replace(remstr, '', 1).strip())
            elif oldsubj.startswith(f'Re: {remstr}'):
                msg.replace_header('Subject', oldsubj.replace(f'Re: {remstr}', 'Re:', 1).strip())

    if msg.get_default_type() == 'text/plain':
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                msg.set_charset(chardet.detect(payload)['encoding'])
        except: # noqa
            # This may fail for various reasons having to do with the wonderful world
            # of 8bit content and legacy encodings.
            # Ignore and hope we can still as_string below.
            pass

    try:
        bmsg = msg.as_string(policy=EMLPOLICY).encode()
        if checkspam and check_if_spam(bmsg):
            logger.info('    spam: %s', msg['Subject'])
            return

        logger.info('    added: %s', msg['Subject'])
        mbx.add(bmsg)
    except: # noqa
        # Throw it out, because life is too short to figure out all possible ways
        # that decades-old email messages make python break.
        logger.info('    corrupted: %s', msg['Subject'])
        return


def marc_nice_get(url: str):
    global LASTMARC
    if LASTMARC:
        msleep = MARCNICE - (time.time() - LASTMARC)
    else:
        msleep = MARCNICE
    if msleep > 0:
        time.sleep(msleep)
    rses = get_requests_session()
    rsp = rses.get(url)
    LASTMARC = time.time()
    return rsp


def marc_get_full_thread(marc_list_id: str, thread_id: str) -> List[str]:
    cp = 1
    msgnums = list()
    logger.info('Grabbing thread %s', thread_id)
    while True:
        lastpage = True
        np = cp + 1
        nl = f'r={np}&'
        url = f'{MARCURL}/?t={thread_id}&r={cp}&w=1'
        rsp = marc_nice_get(url)
        soup = BeautifulSoup(rsp.content, features='lxml')
        for tag in soup.find_all('a'):
            href = tag.attrs.get('href')
            if not href:
                continue
            # See if it's a link to the next page
            if href.find(nl) >= 0:
                lastpage = False
                continue
            # Is it from the wrong list?
            if href.find(marc_list_id) < 0:
                continue

            match = re.search(r'm=(\d+)\D', href)
            if match:
                msgnums.append(match.groups()[0])
                continue

        if lastpage:
            break
        cp += 1
        logger.info('\t... page %s', cp)

    return msgnums


def parse_pipermail_index(pipermail_url: str) -> Set[str]:
    logger.info('Grabbing pipermail index from %s', pipermail_url)
    rses = get_requests_session()
    resp = rses.get(pipermail_url)
    index = resp.content

    soup = BeautifulSoup(index, features='lxml')

    mboxes = set()
    for tag in soup.find_all('a'):
        # we are looking for a href that ends with .txt.gz
        if 'href' in tag.attrs and tag.attrs['href'].find('.txt') > 1:
            mboxes.add(os.path.join(pipermail_url, tag.attrs['href']))

    return mboxes


def parse_hyperkitty_index(hyperkitty_url: str) -> Set[str]:
    logger.info('Grabbing hyperkitty index from %s', hyperkitty_url)
    rses = get_requests_session()
    resp = rses.get(hyperkitty_url)
    index = resp.content

    soup = BeautifulSoup(index, features='lxml')

    mboxes = set()
    for tag in soup.find_all('a'):
        # we are looking for a href that has year/month notation
        if 'href' in tag.attrs:
            matches = re.search(r'.*/(\d{4})/(\d{1,2})/', tag.attrs['href'])
            if matches:
                year, month = matches.groups()
                year = int(year)
                month = int(month)
                fromdate = '%d-%02d-01' % (year, month)
                if month == 12:
                    todate = '%d-01-01' % (year+1)
                else:
                    todate = '%d-%02d-01' % (year, month+1)
                archurl = '%s/export/export.mbox.gz?start=%s&end=%s' % (hyperkitty_url.rstrip('/'), fromdate, todate)
                mboxes.add(archurl)

    # return {f'{hyperkitty_url}/export/export.mbox.gz?start=2021-09-01&end=2021-10-01'}
    return mboxes


def grab_pipermail_archive(pipermail_url: str, mbx: Union[mailbox.Mailbox, mailbox.Maildir],
                           args: argparse.Namespace) -> None:
    tmpfile = mkstemp('pipermail')[1]
    chunks = pipermail_url.split('/')
    if pipermail_url[0] == '/':
        with open(pipermail_url, 'rb') as fh:
            if pipermail_url[-3:] == '.gz':
                with gzip.GzipFile(fileobj=fh) as uncompressed:
                    mboxdata = uncompressed.read().decode('utf-8', errors='replace')
            else:
                mboxdata = fh.read().decode('utf-8', errors='replace')
    else:
        logger.info('  grabbing %s', chunks[-1])
        rses = get_requests_session()
        # XXX: this can be horribly large
        try:
            resp = rses.get(pipermail_url, stream=True)
            if resp.content.startswith(b'\x1f\x8b'):
                with gzip.GzipFile(fileobj=io.BytesIO(resp.content)) as uncompressed:
                    mboxdata = uncompressed.read().decode('utf-8', errors='replace')
            else:
                mboxdata = resp.content.decode('utf-8', errors='replace')

            resp.close()
        except EOFError:
            logger.info('  corrupted month: %s, skipped', chunks[-1])
            return

    # Pipermail does a nasty thing where it doesn't properly handle
    # lines in the body that start with "From ". First, we add ">" to
    # all lines starting with "From " and then fix some of them in the
    # next step.
    logger.info('  demangling %s', chunks[-1])
    regex = r'^From '
    subst = '>From '
    mboxdata = re.sub(regex, subst, mboxdata, 0, flags=re.M)
    # Fix pipermail mangling where it changes some email addresses
    # to be ' at ' instead of '@'. This is easiest to do with a
    # handful of regexes than via actual message body manipulation
    # as part of the python's email.message object
    regex = r'(<[^>]+) at ([^>]+>)'
    subst = r'\1@\2'
    mboxdata = re.sub(regex, subst, mboxdata, 0, flags=re.M)
    regex = r'(<[^>]+)\(a\)([^>]+>)'
    subst = r'\1@\2'
    mboxdata = re.sub(regex, subst, mboxdata, 0, flags=re.M)
    regex = r'^>?((?:From|To):? \S+) at (\S+\..*)'
    mboxdata = re.sub(regex, subst, mboxdata, 0, flags=re.M)
    # Fix any remaining false From escapes
    regex = r'^>(From\s+\S+[@-]\S+\s+\w+\s+\w+\s+\d+\s+\d+:\d+:\d+\s+\d{4})'
    subst = r'\1'
    mboxdata = re.sub(regex, subst, mboxdata, 0, flags=re.M)

    with open(tmpfile, 'wb') as out_fh:
        out_fh.write(mboxdata.encode())

    # Open it now as a mailbox
    tmpmbx = mailbox.mbox(tmpfile)
    logger.debug('    %s messages in the archive', len(tmpmbx))
    for mkey in tmpmbx.keys():
        try:
            msg = tmpmbx.get_message(mkey)
        except:  # noqa
            logger.info('    error parsing message %d, skipped', mkey)
            continue

        oldfrom = str(msg.get('From', ''))
        if oldfrom:
            newfrom = clean_header(oldfrom)
            # Fix any leftover at-escaping
            newfrom = newfrom.replace(' at ', '@')
            # Fix bogus From: foo@bar.baz (Foo Barski) -> Foo Barski <foo@bar.baz>
            matches = re.search(r'(\S+@\S+\.\S+) \((.*)\)$', newfrom)
            if matches:
                gr = matches.groups()
                newfrom = f'{gr[1]} <{gr[0]}>'
            if newfrom != oldfrom:
                msg.replace_header('From', newfrom)

        if args.listid:
            msg['List-Id'] = f'<{args.listid}>'

        if args.lookaside:
            lookaside_fillin(msg)

        if not msg.get('To'):
            msg['To'] = args.to
        # Fix in-reply-to
        irt = msg.get('in-reply-to')
        if irt and irt[0] != '<':
            msg.replace_header('In-Reply-To', f'<{irt}>')
        add_msg_to_mbx(msg, mbx, args.checkspam, args.clean_subject)

    tmpmbx.close()
    os.unlink(tmpfile)


def get_marcinfo(args: argparse.Namespace) -> None:
    global MARCNICE

    if args.nice < 0.5:
        logger.critical('Hitting marc.info every %s s will get you auto-banned. Try above 0.5.', args.nice)
        sys.exit(1)
    MARCNICE = args.nice

    if not args.to:
        args.to = args.listid.replace('.', '@', 1)

    marc_list_id = args.listname

    rses = get_requests_session()
    url = f'{MARCURL}/?l={marc_list_id}&w=1'
    logger.info('Grabbing main index for %s', marc_list_id)

    rsp = rses.get(url, stream=False)
    soup = BeautifulSoup(rsp.content, features='lxml')
    months = list()
    for tag in soup.find_all('a'):
        # we are looking for a href that contains
        href = tag.attrs.get('href')
        if not href:
            continue
        match = re.search(r'b=(\d+)\D', href)
        if match:
            months.append(match.groups()[0])

    mbx = get_outbox(args)
    havenums = set()
    if len(mbx):
        for msg in mbx:
            xmarc = msg.get('X-MARC-Message')
            if xmarc:
                match = re.search(r'm=(\d+)', xmarc)
                if match:
                    havenums.add(match.groups()[0])

    logger.info('Found %s messages already in mbox', len(havenums))
    thdnums = set()
    msgnums = set()
    for month in months:
        logger.info('Grabbing month %s', month)
        # We may be paginated
        cp = 1
        while True:
            lastpage = True
            np = cp + 1
            url = f'{MARCURL}/?l={marc_list_id}&b={month}&r={cp}&w=1'
            if cp > 1:
                logger.info('  ... page %s', cp)
            rsp = marc_nice_get(url)
            soup = BeautifulSoup(rsp.content, features='lxml')
            for tag in soup.find_all('a'):
                href = tag.attrs.get('href')
                if not href:
                    continue
                # See if it's a link to the next page
                telltale = f'r={np}&'
                if href.find(telltale) >= 0:
                    lastpage = False
                    continue

                # Is it a message link?
                match = re.search(r'm=(\d+)\D', href)
                if match:
                    msgnums.add(match.groups()[0])
                    continue

                # Is it a thread link?
                match = re.search(r't=(\d+)\D', href)
                if match:
                    thdnums.add(match.groups()[0])
                    continue

            if lastpage:
                break
            cp += 1

    for thdnum in thdnums:
        tnums = marc_get_full_thread(marc_list_id, thdnum)
        # last message starts the thread
        tnums.reverse()
        irt = None
        for tnum in tnums:
            if tnum in havenums:
                logger.info('Already have %s', tnum)
                continue

            if tnum in msgnums:
                msgnums.remove(tnum)
            try:
                msg = marc_get_message(marc_list_id, tnum, args.listid, args.to, args.lookaside)
            except LookupError:
                continue

            if not irt:
                irt = msg.get('Message-Id')
            elif not msg.get('References'):
                msg['References'] = irt
                msg['In-Reply-To'] = irt

            add_msg_to_mbx(msg, mbx, args.checkspam, args.clean_subject)

    logger.info('Grabbing remaining unthreaded messages')
    for msgnum in msgnums:
        if msgnum in havenums:
            logger.info('Already have %s', tnum)
            continue
        try:
            msg = marc_get_message(marc_list_id, msgnum, args.listid, args.to, args.lookaside)
        except LookupError:
            continue
        if not msg:
            continue

        add_msg_to_mbx(msg, mbx, args.checkspam, args.clean_subject)

    mbx.close()


def get_mailman(args: argparse.Namespace) -> None:
    if not args.to:
        args.to = args.listid.replace('.', '@', 1)

    mbx = get_outbox(args)
    if args.url[0] == '/':
        grab_pipermail_archive(args.url, mbx, args)
    else:
        if args.mailman3:
            months = parse_hyperkitty_index(args.url)
        else:
            months = parse_pipermail_index(args.url)
        if not months:
            print('Could not find any .txt.gz files listed at %s' % args.url)
            sys.exit(1)
        for month in months:
            grab_pipermail_archive(month, mbx, args)


def get_nntp(args: argparse.Namespace) -> None:
    import nntplib
    # Expect in format nntp://news.gmane.org/gmane.linux.network
    logger.info('Connecting to %s', args.url)
    chunks = args.url.split('/')
    server, group = chunks[-2:]
    nntplib._MAXLINE = 1 << 20
    server = nntplib.NNTP(server)
    resp, count, first, last, name = server.group(group)
    total = int(last)

    mbx = get_outbox(args)
    aid = 1
    while aid <= total:
        try:
            nresp, nainfo = server.article(aid)
            msg = email.message_from_bytes(b'\n'.join(nainfo[2]))
            logger.info('  processing: %s, %s/%s', msg.get('Message-Id'), aid, total)
            newhdrs = list()
            for hdrname, hdrval in list(msg._headers): # noqa
                if hdrname.find('Original-') == 0:
                    hdrname = hdrname.replace('Original-', '')
                newhdrs.append((hdrname, hdrval))
            msg._headers = newhdrs # noqa
            if args.listid:
                try:
                    msg.replace_header('List-Id', f'<{args.listid}>')
                except KeyError:
                    msg.add_header('List-Id', f'<{args.listid}>')

            add_msg_to_mbx(msg, mbx, args.checkspam, args.clean_subject)

        except nntplib.NNTPTemporaryError:
            # Ignore one-off article failures -- probably deletes
            pass
        finally:
            aid += 1

    mbx.close()


def get_outbox(args: argparse.Namespace) -> Union[mailbox.Mailbox, mailbox.Maildir]:
    if args.as_maildir:
        logger.info('Will output into maildir %s', args.out)
        return mailbox.Maildir(args.out)
    return mailbox.mbox(args.out)


if __name__ == '__main__':
    # noinspection PyTypeChecker
    parser = argparse.ArgumentParser(
        description="Collect external mail archives into a local mbox",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('-d', '--debug', action='store_true', default=False,
                        help='Add more debugging info to the output')
    parser.add_argument('-i', '--listid',
                        help='List-Id header to inject into the messages')
    parser.add_argument('-t', '--to',
                        help='Value to put into the To: header, if missing '
                             '(defaults to list-id with first . replaced with @')
    parser.add_argument('-k', '--lookaside', action='store_true', default=False,
                        help='Attempt to look up matching lore messages for missing to/cc headers')
    parser.add_argument('-s', '--checkspam', action='store_true', default=False,
                        help='Run spamc to check messages for spam before adding')
    parser.add_argument('-o', '--out', required=True,
                        help='Filename of the mailbox file to write out')
    parser.add_argument('-m', '--as-maildir', action='store_true', default=False,
                        help='Output as maildir instead of mbox')
    parser.add_argument('-c', '--clean-subject', nargs='+',
                        help='Remove this string from subjects (e.g. [listname])')

    subparsers = parser.add_subparsers(help='sub-command help', dest='subcmd')

    sp_mm = subparsers.add_parser('mailman', help='Collect mailman archives',
                                  formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    sp_mm.add_argument('-u', '--url', required=True,
                       help='Mailman archive index URL')
    sp_mm.add_argument('-3', '--mailman3', action='store_true', default=False,
                       help='This is a mailman3 site')
    sp_mm.set_defaults(func=get_mailman)

    sp_marc = subparsers.add_parser('marcinfo', help='Collect marc.info archives',
                                    formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    sp_marc.add_argument('-l', '--listname', required=True,
                         help='Marc.info list name (?l= parameter)')
    sp_marc.add_argument('-n', '--nice', default=MARCNICE, type=float,
                         help='Seconds to sleep between requests')
    sp_marc.set_defaults(func=get_marcinfo)

    sp_nntp = subparsers.add_parser('nntp', help='Collect NNTP archives',
                                    formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    sp_nntp.add_argument('-u', '--url', required=True,
                         help='NNTP url (e.g. nntp://news.gmane.com/gmane.linux.kernel')
    sp_nntp.set_defaults(func=get_nntp)

    cmdargs = parser.parse_args()
    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    ch.setFormatter(formatter)

    if cmdargs.debug:
        ch.setLevel(logging.DEBUG)
    else:
        ch.setLevel(logging.INFO)

    logger.addHandler(ch)

    if 'func' not in cmdargs:
        parser.print_help()
        sys.exit(1)

    cmdargs.func(cmdargs)
