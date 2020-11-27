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
import time
import re
import quopri
import base64
import gzip
import io
import nntplib
import requests
import logging
import subprocess

try:
    import cchardet as chardet # noqa
except ImportError:
    import chardet

from tempfile import mkstemp
from bs4 import BeautifulSoup # noqa
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

from email import charset
charset.add_charset('utf-8', charset.SHORTEST, '8bit') # noqa

# Used for our requests session
REQSESSION = None

__VERSION__ = '1.0'
# Where des marc.info live?
MARCURL = 'https://marc.info'
# Wait this many seconds between requests to marc.info, to avoid triggering
# anti-abuse blocks (and to just be nice)
MARCNICE = 1

# Set our own policy
EMLPOLICY = email.policy.EmailPolicy(utf8=True, cte_type='8bit', max_line_length=None)

logger = logging.getLogger(__name__)


def get_requests_session():
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


def lore_get_message(msgid):
    # See where we're redirected
    rurl = f'https://lore.kernel.org/r/{msgid}'
    rses = get_requests_session()
    resp = rses.head(rurl)
    if resp.status_code < 300 or resp.status_code > 400:
        # Not known on lore
        return None
    # Pop msgid from the end of the redirect
    msgurl = resp.headers['Location'] + 'raw'
    resp.close()
    resp = rses.get(msgurl)
    msg = email.message_from_bytes(resp.content)
    logger.info('    found on lore')
    return msg


# Turned off for now
def patchwork_get_headers(msgid):
    url = f'https://patchwork.kernel.org/api/1.2/patches/'
    params = [
        ('msgid', msgid),
    ]
    rses = get_requests_session()
    resp = rses.get(url, params=params, stream=False)
    if resp.status_code > 200:
        return None

    jj = resp.json()
    if not len(jj):
        return None

    # we only care about one
    p_id = jj[0].get('id')
    resp = rses.get(f'{url}{p_id}', stream=False)
    if resp.status_code > 200:
        return None

    logger.info('    found on patchwork')
    jj = resp.json()
    return jj.get('headers')


def lookaside_fillin(msg):
    wanthdrs = [
        'To',
        'Cc',
        'References',
        'In-Reply-To',
        'User-Agent',
        'X-Mailer',
    ]
    msgid = msg.get('Message-Id').strip('<>')
    lmsg = lore_get_message(msgid)
    if not lmsg:
        return False
        # lmsg = patchwork_get_headers(msgid)
        # if not lmsg:
        #    return False

    for wanthdr in wanthdrs:
        if not msg.get(wanthdr) and lmsg.get(wanthdr):
            msg[wanthdr] = lmsg.get(wanthdr)

    return True


def marc_get_message(marc_list_id, msgnum, listid, toaddr, lookaside):
    rses = get_requests_session()
    url = f'{MARCURL}/?l={marc_list_id}&m={msgnum}&q=mbox'
    logger.info('  grabbing message %s', msgnum)
    resp = rses.get(url)
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
        logger.info('    No message-id, ignored')
        # Can't use it anyway
        return None

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


def check_if_spam(bmsg):
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


def add_msg_to_mbx(msg, mbx, checkspam):
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

        mbx.add(bmsg)
    except: # noqa
        # Throw it out, because life is too short to figure out all possible ways
        # that decades-old email messages make python break.
        logger.info('    corrupted: %s', msg['Subject'])
        return


def marc_get_full_thread(marc_list_id, thread_id):
    cp = 1
    rses = get_requests_session()
    msgnums = list()
    logger.info('Grabbing thread %s', thread_id)
    while True:
        lastpage = True
        np = cp + 1
        nl = f'r={np}&'
        # Be nice
        time.sleep(MARCNICE)
        url = f'{MARCURL}/?t={thread_id}&r={cp}&w=1'
        rsp = rses.get(url)
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


def parse_pipermail_index(pipermail_url):
    logger.info('Grabbing pipermail index from %s', pipermail_url)
    rses = get_requests_session()
    resp = rses.get(pipermail_url)
    index = resp.content

    soup = BeautifulSoup(index, features='lxml')

    mboxes = []
    for tag in soup.find_all('a'):
        # we are looking for a href that ends with .txt.gz
        if 'href' in tag.attrs and tag.attrs['href'].find('.txt') > 1:
            mboxes.append(os.path.join(pipermail_url, tag.attrs['href']))

    return mboxes


def grab_pipermail_archive(pipermail_url, mbx, listid, toaddr, lookaside, checkspam):
    tmpfile = mkstemp('pipermail')[1]
    chunks = pipermail_url.split('/')

    logger.info('  grabbing %s', chunks[-1])
    rses = get_requests_session()
    # XXX: this can be horribly large
    resp = rses.get(pipermail_url, stream=True)
    if resp.content.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(resp.content)) as uncompressed:
            mboxdata = uncompressed.read().decode('utf-8', errors='replace')
    else:
        mboxdata = resp.content.decode('utf-8', errors='replace')

    resp.close()

    # Pipermail does a nasty thing where it doesn't properly handle
    # lines in the body that start with "From ". First, we add ">" to
    # all lines starting with "From " and then fix some of them in the
    # next step.
    logger.info('  demangling %s', chunks[-1])
    regex = r'^From '
    subst = '>From '
    mboxdata = re.sub(regex, subst, mboxdata, 0, flags=re.M)
    regex = r'(<[^>]+) at ([^>]+>)'
    if re.search(regex, mboxdata):
        # Fix pipermail mangling where it changes some email addresses
        # to be ' at ' instead of '@'. This is easiest to do with a
        # handful of regexes than via actual message body manipulation
        # as part of the python's email.message object
        subst = r'\1@\2'
        mboxdata = re.sub(regex, subst, mboxdata, 0, flags=re.M)
        regex = r'^>?(From:? \S+) at (\S+\..*)'
        mboxdata = re.sub(regex, subst, mboxdata, 0, flags=re.M)
    else:
        # Fix any remaining false From escapes
        regex = r'^>(From\s+\S+@\S+\s+\w+\s+\w+\s+\d+\s+\d+:\d+:\d+\s+\d{4})'
        subst = r'\1'
        mboxdata = re.sub(regex, subst, mboxdata, 0, flags=re.M)

    with open(tmpfile, 'wb') as out_fh:
        out_fh.write(mboxdata.encode())

    # Open it now as a mailbox
    tmpmbx = mailbox.mbox(tmpfile)
    for msg in tmpmbx:
        logger.info('    processing: %s', msg.get('Message-Id'))
        fromline = str(msg.get('From', ''))
        if fromline and fromline.find('(') > 0:
            # Fix bogus From: foo@bar.baz (Foo Barski) -> Foo Barski <foo@bar.baz>
            matches = re.search(r'(\S+@\S+\.\S+) \((.*)\)$', fromline)
            if matches:
                gr = matches.groups()
                msg.replace_header('From', f'{gr[1]} <{gr[0]}>')

        if listid:
            msg['List-Id'] = f'<{listid}>'

        if lookaside:
            lookaside_fillin(msg)

        if not msg.get('To'):
            msg['To'] = toaddr

        add_msg_to_mbx(msg, mbx, checkspam)

    tmpmbx.close()
    os.unlink(tmpfile)


def get_marcinfo(args):
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

    thdnums = set()
    msgnums = set()
    for month in months:
        logger.info('Grabbing month %s', month)
        # We may be paginated
        cp = 1
        while True:
            lastpage = True
            # Be nice
            np = cp + 1
            time.sleep(MARCNICE)
            url = f'{MARCURL}/?l={marc_list_id}&b={month}&r={cp}&w=1'
            if cp > 1:
                logger.info('  ... page %s', cp)
            rsp = rses.get(url)
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

    mbx = mailbox.mbox(args.out)
    for thdnum in thdnums:
        tnums = marc_get_full_thread(marc_list_id, thdnum)
        # last message starts the thread
        tnums.reverse()
        irt = None
        for tnum in tnums:
            if tnum in msgnums:
                msgnums.remove(tnum)
            time.sleep(MARCNICE)
            msg = marc_get_message(marc_list_id, tnum, args.listid, args.to, args.lookaside)
            if not msg:
                continue

            if not irt:
                irt = msg.get('Message-Id')
            elif not msg.get('References'):
                msg['References'] = irt
                msg['In-Reply-To'] = irt

            add_msg_to_mbx(msg, mbx, args.checkspam)

    logger.info('Grabbing remaining unthreaded messages')
    for msgnum in msgnums:
        time.sleep(MARCNICE)
        msg = marc_get_message(marc_list_id, msgnum, args.listid, args.to, args.lookaside)
        if not msg:
            continue

        add_msg_to_mbx(msg, mbx, args.checkspam)

    mbx.close()


def get_mailman(args):
    if not args.to:
        args.to = args.listid.replace('.', '@', 1)

    months = parse_pipermail_index(args.url)
    if not months:
        print('Could not find any .txt.gz files listed at %s' % args.url)
        sys.exit(1)
    mbx = mailbox.mbox(args.out)
    for month in months:
        grab_pipermail_archive(month, mbx, args.listid, args.to, args.lookaside, args.checkspam)


def get_nntp(args):
    # Expect in format nntp://news.gmane.org/gmane.linux.network
    logger.info('Connecting to %s', args.url)
    chunks = args.url.split('/')
    server, group = chunks[-2:]
    nntplib._MAXLINE = 1 << 20
    server = nntplib.NNTP(server)
    resp, count, first, last, name = server.group(group)
    total = int(last)

    mbx = mailbox.mbox(args.out)
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
                    msg.add_header('List-Id', f'<{args.listid}')

            add_msg_to_mbx(msg, mbx, args.checkspam)

        except nntplib.NNTPTemporaryError:
            # Ignore one-off article failures -- probably deletes
            pass
        finally:
            aid += 1

    mbx.close()


if __name__ == '__main__':
    import argparse

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

    subparsers = parser.add_subparsers(help='sub-command help', dest='subcmd')

    sp_mm = subparsers.add_parser('mailman', help='Collect mailman archives',
                                  formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    sp_mm.add_argument('-u', '--url', required=True,
                       help='Mailman archive index URL')
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
