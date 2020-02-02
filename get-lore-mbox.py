#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import os
import sys
import argparse
import mailbox
import email
import email.message
import email.utils
import subprocess
import logging
import re
import fnmatch
import time

import requests
import gzip

logger = logging.getLogger('get-lore-mbox')

_DEFAULT_CONFIG = {
    'midmask': 'https://lore.kernel.org/r/%s',
    'linkmask': 'https://lore.kernel.org/r/%s',
}

WANTHDRS = {'sender',
            'from',
            'to',
            'cc',
            'subject',
            'date',
            'message-id',
            'resent-message-id',
            'reply-to',
            'in-reply-to',
            'references',
            'mime-*',
            'list-id',
            'content-*',
            'errors-to',
            'x-mailing-list',
            'resent-to',
            }


def git_get_command_lines(gitdir, args):
    out = git_run_command(gitdir, args)
    lines = list()
    if out:
        for line in out.split('\n'):
            if line == '':
                continue
            lines.append(line)

    return lines


def git_run_command(gitdir, args, stdin=None, logstderr=False):
    cmdargs = ['git', '--no-pager']
    if gitdir:
        cmdargs += ['--git-dir', gitdir]
    cmdargs += args

    logger.debug('Running %s' % ' '.join(cmdargs))

    if stdin:
        (output, error) = subprocess.Popen(cmdargs, stdout=subprocess.PIPE,
                                           stdin=subprocess.PIPE,
                                           stderr=subprocess.PIPE).communicate(input=stdin)
    else:
        (output, error) = subprocess.Popen(cmdargs, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE).communicate()

    output = output.strip().decode('utf-8', errors='replace')

    if logstderr and len(error.strip()):
        logger.debug('Stderr: %s', error.decode('utf-8', errors='replace'))

    return output


def amify_msg(msg, trailers):
    payload = msg.get_payload(decode=True)
    if trailers:
        payload = git_add_trailers(payload, trailers)
    msg.set_payload(payload)
    # Clean up headers
    newhdrs = []
    for hdrname, hdrval in list(msg._headers):
        lhdrname = hdrname.lower()
        wanthdr = False
        for hdrmatch in WANTHDRS:
            if fnmatch.fnmatch(lhdrname, hdrmatch):
                wanthdr = True
                break
        if wanthdr:
            if lhdrname == 'content-transfer-encoding':
                hdrval = '8bit'
            newhdrs.append((hdrname, hdrval))
    msg._headers = newhdrs
    return msg


def get_config_from_git():
    gitconfig = _DEFAULT_CONFIG
    args = ['config', '-z', '--get-regexp', r'get-lore-mbox\..*']
    out = git_run_command(None, args)
    if not out:
        return gitconfig

    for line in out.split('\x00'):
        if not line:
            continue
        key, value = line.split('\n', 1)
        try:
            chunks = key.split('.')
            cfgkey = chunks[-1]
            gitconfig[cfgkey] = value
        except ValueError:
            logger.debug('Ignoring git config entry %s', line)

    return gitconfig


def get_msgid_from_stdin():
    if not sys.stdin.isatty():
        message = email.message_from_string(sys.stdin.read())
        return message.get('Message-ID', None)
    logger.error('Error: pipe a message or pass msgid as parameter')
    sys.exit(1)


def get_pi_thread_by_msgid(msgid, config, outdir='.', wantname=None):
    # Grab the head from lore, to see where we are redirected
    midmask = config['midmask'] % msgid
    logger.info('Looking up %s', midmask)
    resp = requests.head(midmask)
    if resp.status_code < 300 or resp.status_code > 400:
        logger.critical('That message-id is not known.')
        return None
    canonical = resp.headers['Location'].rstrip('/')
    resp.close()
    t_mbx_url = '%s/t.mbox.gz' % canonical
    logger.critical('Grabbing thread from %s', t_mbx_url)
    resp = requests.get(t_mbx_url)
    t_mbox = gzip.decompress(resp.content)
    if wantname:
        savefile = os.path.join(outdir, wantname)
    else:
        # Save it into msgid.mbox
        savefile = '%s.t.mbx' % msgid
        savefile = os.path.join(outdir, savefile)
    with open(savefile, 'wb') as fh:
        logger.debug('Saving %s', savefile)
        fh.write(t_mbox)
    return savefile


def get_plain_part(msg):
    # walk until we find the first text/plain part
    body = None
    for part in msg.walk():
        if part.get_content_type().find('text/plain') < 0:
            continue
        body = part.get_payload(decode=True)
        if body is None:
            continue

        body = body.decode('utf-8', errors='replace')
    return body


def git_add_trailers(payload, trailers):
    cmdargs = ['interpret-trailers']
    output = payload.decode('utf-8')
    for trailer in trailers:
        # Check if this trailer is already in the body
        if output.find(trailer) < 0:
            logger.info('    Adding trailer: %s', trailer)
            cmdargs += ['--trailer', trailer]
        output = git_run_command(None, args=cmdargs, stdin=output.encode('utf-8'))
    return output


def get_clean_msgid(msg, header='Message-ID'):
    msgid = msg.get(header)
    if msgid:
        msgid = msg.get(header).strip().strip('<>')
    return msgid


def mbox_to_am(mboxfile, config, outdir='.', wantver=None, wantname=None):
    mbx = mailbox.mbox(mboxfile)
    count = len(mbx)
    logger.info('Analyzing %s messages in the thread', count)
    am_kept = list()
    msgid_map = dict()
    slug = None
    cover_keys = dict()
    sorted_keys = [None, None]
    trailer_map = dict()
    expected_count = 1
    cur_vn = None
    vn = None
    for key, msg in mbx.items():
        msgid = get_clean_msgid(msg)
        msgid_map[msgid] = key
        subject = re.sub(r'\s+', ' ', msg['Subject'])
        logger.debug('Looking at msg %s: %s', key, subject)

        # Start by looking at prefixes in the subject
        matches = re.search(r'\[PATCH([^\]]*)\]', subject, re.IGNORECASE)
        if not matches:
            # if the key is 0, it may be a cover letter
            if key == 0:
                body = get_plain_part(msg)
                if re.search(r'^\s*\d+\sfile.*\d+ insertion.*\d+ deletion', body, re.MULTILINE | re.IGNORECASE):
                    # Looks like a cover letter, so keep it in mind, unless we find
                    # something else better suited to be a cover letter
                    logger.debug('  Probaby a cover letter')
                    cover_keys[1] = key
            # Ignoring this message
            continue
        cur_count = 1
        expected_count = 1
        new_vn = 1
        for prefix in matches.groups()[0].split():
            # Does it match \d/\d?
            if re.search(r'\d/\d', prefix):
                cur, expected = prefix.split('/')
                cur_count = int(cur)
                expected_count = int(expected)
            # Is does it have a v\d?
            matches = re.search(r'v(\d+)', prefix)
            if matches:
                new_vn = int(matches.groups()[0])

        if cur_vn is None or new_vn > cur_vn:
            if new_vn != 1:
                logger.debug('Found new series version: v%s', new_vn)
            if wantver is None or wantver == new_vn:
                # Blow away anything we currently have in sorted_keys
                sorted_keys = [None] * (expected_count + 1)
                slug = None
            cur_vn = new_vn
        elif vn is None:
            cur_vn = new_vn

        if wantver is not None and wantver != cur_vn:
            logger.debug('  Ignoring v%s: %s', cur_vn, subject)
            continue

        vn = cur_vn

        # We use a "slug" for mbox name, based on the date and author
        if not slug:
            msgdate = email.utils.parsedate_tz(str(msg['Date']))
            prefix = time.strftime('%Y%m%d', msgdate[:9])
            authorline = email.utils.getaddresses(msg.get_all('from', []))[0]
            if authorline[0]:
                author = re.sub(r'\W+', '_', authorline[0]).strip('_').lower()
            else:
                author = re.sub(r'\W+', '_', authorline[1]).strip('_').lower()
            slug = '%s_%s' % (prefix, author)
            if cur_vn != 1:
                slug = 'v%s_%s' % (cur_vn, slug)
        logger.debug('  Processing: %s', subject)

        # If the count is 00/NN, it's the cover letter
        if cur_count == 0 and cur_vn not in cover_keys.keys():
            # Found the cover letter
            logger.debug('  Found a cover letter for v%s', cur_vn)
            cover_keys[cur_vn] = key
            continue

        body = get_plain_part(msg)
        # Do we have a '^---' followed by '^+++' in the body anywhere?
        if re.search(r'^---.*\n\+\+\+', body, re.MULTILINE):
            # Contains a diff
            # Do we already have a match for this, though?
            if sorted_keys[cur_count] is None:
                am_kept.append(key)
                sorted_keys[cur_count] = key
                continue
        # Do we have something that looks like a new trailer?
        matches = re.search(r'^\s*([\w-]+: .*<\S+>)\s*$', body, re.MULTILINE)
        if not matches:
            continue
        # Where do we need to stick them?
        irt_key = 0
        irt_id = get_clean_msgid(msg, header='In-Reply-To')
        if irt_id and irt_id in msgid_map:
            irt_key = msgid_map[irt_id]
        if irt_key not in trailer_map:
            trailer_map[irt_key] = list()
        trailer_map[irt_key] += matches.groups()

    if not len(am_kept):
        logger.info('Did not find any patches to save')
        return None

    if not wantname:
        am_filename = os.path.join(outdir, '%s.mbx' % slug)
        am_cover = os.path.join(outdir, '%s.cover' % slug)
    else:
        am_filename = os.path.join(outdir, wantname)
        am_cover = os.path.join(outdir, '%s.cover' % wantname)
        if wantname.find('.') < 0:
            slug = wantname
        else:
            slug = '.'.join(wantname.split('.')[:-1])

    if os.path.exists(am_filename):
        os.unlink(am_filename)
    am_mbx = mailbox.mbox(am_filename)
    logger.info('---')

    logger.critical('Writing %s', am_filename)
    have_missing = False
    at = 1
    for key in sorted_keys[1:]:
        if key is None:
            logger.error('  ERROR: missing [%s/%s]!', at, expected_count)
            have_missing = True
        else:
            msg = mbx[key]
            subject = re.sub(r'\s+', ' ', msg['Subject'])
            logger.info('  %s', subject)
            if key in trailer_map:
                trailers = trailer_map[key]
            else:
                trailers = None
            msg = amify_msg(msg, trailers)
            am_mbx.add(msg)
        at += 1

    logger.critical('Total patches: %s', len(am_mbx))
    logger.critical('---')
    if have_missing:
        logger.critical('WARNING: Thread incomplete!')

    if vn in cover_keys:
        # Save the cover letter
        cover_msg = amify_msg(mbx[cover_keys[vn]], None)
        with open(am_cover, 'w') as fh:
            fh.write(cover_msg.as_string())
        logger.critical('Cover: %s', am_cover)
        first_body = get_plain_part(cover_msg)
    else:
        first_body = get_plain_part(am_mbx[0])

    top_msgid = get_clean_msgid(am_mbx[0])
    linkurl = config['linkmask'] % top_msgid
    logger.critical('Link: %s', linkurl)

    base_commit = None
    matches = re.search(r'base-commit: .*?([0-9a-f]+)', first_body, re.MULTILINE)
    if matches:
        base_commit = matches.groups()[0]
    else:
        # Try a more relaxed search
        matches = re.search(r'based on .*?([0-9a-f]{40})', first_body, re.MULTILINE)
        if matches:
            base_commit = matches.groups()[0]

    if base_commit:
        logger.critical('Base-commit included, you can branch using:')
        logger.critical('  git checkout -b %s %s', slug, base_commit)
        logger.critical('  git am %s', am_filename)
    else:
        logger.info('No base-commit information included, sorry.')
        logger.info('  git checkout -b %s master', slug)
        logger.info('  git am %s', am_filename)
    am_mbx.close()

    return am_filename


def main(cmdargs):
    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    ch.setFormatter(formatter)

    if cmdargs.quiet:
        ch.setLevel(logging.CRITICAL)
    elif cmdargs.debug:
        ch.setLevel(logging.DEBUG)
    else:
        ch.setLevel(logging.INFO)

    logger.addHandler(ch)

    if not cmdargs.msgid:
        logger.debug('Getting Message-ID from stdin')
        msgid = get_msgid_from_stdin()
    else:
        msgid = cmdargs.msgid

    msgid = msgid.strip('<>')
    config = get_config_from_git()
    mboxfile = get_pi_thread_by_msgid(msgid, config, outdir=cmdargs.outdir, wantname=cmdargs.wantname)
    if mboxfile and cmdargs.amready:
        # Move it into -thread
        threadmbox = '%s-thread' % mboxfile
        os.rename(mboxfile, threadmbox)
        mbox_to_am(threadmbox, config, outdir=cmdargs.outdir, wantver=cmdargs.version,
                   wantname=cmdargs.wantname)
        os.unlink(threadmbox)
    else:
        mbx = mailbox.mbox(mboxfile)
        logger.critical('Saved %s', mboxfile)
        logger.critical('%s messages in the thread', len(mbx))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('msgid', nargs='?',
                        help='Message ID to process, or pipe a raw message')
    parser.add_argument('-o', '--outdir', default='.',
                        help='Output into this directory')
    parser.add_argument('-a', '--am-ready', dest='amready', action='store_true', default=False,
                        help='Make an mbox ready for git am')
    parser.add_argument('-v', '--version', type=int, default=None,
                        help='Get a specific version of the patch/series (use with -a)')
    parser.add_argument('-n', '--mbox-name', dest='wantname', default=None,
                        help='Filename to name the mbox file')
    parser.add_argument('-d', '--debug', action='store_true', default=False,
                        help='Add more debugging info to the output')
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Be very terse outputting to the stdout')
    main(parser.parse_args())
