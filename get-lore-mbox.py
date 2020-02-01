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

import requests
import gzip

logger = logging.getLogger('get-lore-mbox')

_DEFAULT_CONFIG = {
    'midmask': 'https://lore.kernel.org/r/%s',
    'linkmask': 'https://lore.kernel.org/r/%s',
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


def get_pi_thread_by_msgid(msgid, config, outdir='.'):
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
    # Save it into msgid.mbox
    savefile = '%s.t.mbx' % msgid
    savefile = os.path.join(outdir, savefile)
    with open(savefile, 'wb') as fh:
        fh.write(t_mbox)
        logger.info('Saved thread into %s', savefile)
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


def git_add_trailers(msg, trailers):
    cmdargs = ['interpret-trailers']
    payload = msg.get_payload(decode=True)
    for trailer in trailers:
        logger.info('    Adding trailer: %s', trailer)
        cmdargs += ['--trailer', trailer]
        output = git_run_command(None, args=cmdargs, stdin=payload)
        msg.set_payload(output.encode('utf-8'))
    return msg


def get_clean_msgid(msg, header='Message-ID'):
    msgid = msg.get(header)
    if msgid:
        msgid = msg.get(header).strip().strip('<>')
    return msgid


def mbox_to_am(mboxfile, config, outdir='.', wantver=None):
    mbx = mailbox.mbox(mboxfile)
    count = len(mbx)
    logger.info('Analyzing %s messages in the thread', count)
    am_kept = list()
    msgid_map = dict()
    slug = None
    sorted_keys = [None, None]
    trailer_map = dict()
    expected_count = 1
    vn = None
    for key, msg in mbx.items():
        msgid = get_clean_msgid(msg)
        msgid_map[msgid] = key
        subject = re.sub(r'\s+', ' ', msg['Subject'])

        # Start by looking at prefixes in the subject
        matches = re.search(r'\[PATCH([^\]]+)\]', subject, re.IGNORECASE)
        if not matches:
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

        if vn is None or new_vn > vn:
            if new_vn != 1:
                logger.info('Found new series version: v%s', new_vn)
            if wantver is None or wantver == new_vn:
                # Blow away anything we currently have in sorted_keys
                sorted_keys = [None] * (expected_count + 1)
                slug = None
            vn = new_vn
        elif vn is None:
            vn = new_vn

        if wantver is not None and wantver != vn:
            logger.info('  Ignoring v%s: %s', vn, subject)
            continue

        # We use a "slug" for mbox name, based on the subject
        if not slug:
            slug = re.sub(r'\s*\[.*?\]\s*', '', subject).lower()
            slug = re.sub(r'\W+', '_', slug).strip('_')
            if vn != 1:
                slug = 'v%s_%s' % (vn, slug)
        body = get_plain_part(msg)
        logger.info('  Processing: %s', subject)
        if re.search(r'\D0+/\d+', subject) and sorted_keys[0] is None:
            am_kept.append(key)
            sorted_keys[cur_count] = key
            continue
            # skip to the next message
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
        trailer_map[irt_key] = matches.groups()

    if not len(am_kept):
        logger.critical('Did not find any patches to save')
        return None

    am_filename = os.path.join(outdir, '%s.mbx' % slug)

    if os.path.exists(am_filename):
        os.unlink(am_filename)
    am_mbx = mailbox.mbox(am_filename)
    at = 0
    logger.critical('---')
    logger.critical('Writing %s', am_filename)
    have_missing = False
    for key in sorted_keys:
        if key is None:
            if at != 0:
                logger.error('  ERROR: missing [%s/%s]!', at, expected_count)
                have_missing = True
        else:
            msg = mbx[key]
            subject = re.sub(r'\s+', ' ', msg['Subject'])
            logger.critical('  %s', subject)
            if key in trailer_map:
                msg = git_add_trailers(msg, trailer_map[key])
                if msg['Content-Transfer-Encoding'] == 'base64':
                    msg.replace_header('Content-Transfer-Encoding', '8bit')
            am_mbx.add(msg)
        at += 1

    logger.critical('---')
    if have_missing:
        logger.critical('WARNING: Thread incomplete, please check above!')
    top_msgid = get_clean_msgid(am_mbx[0])
    linkurl = config['linkmask'] % top_msgid
    logger.critical('Link: %s', linkurl)
    # Look for base-commit line in the first message
    body = get_plain_part(am_mbx[0])
    matches = re.search(r'^\s*base-commit: (\S+)\s*$', body, re.MULTILINE)
    if matches:
        base_commit = matches.groups()[0]
        logger.critical('Base-commit included, you can branch using:')
        logger.critical('  git checkout -b %s %s', slug, base_commit)
        logger.critical('  git am %s', am_filename)
    else:
        logger.critical('No base-commit information included, sorry.')
        logger.critical('  git checkout -b %s master', slug)
        logger.critical('  git am %s', am_filename)
    am_mbx.close()

    return am_filename


def main(cmdargs):
    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    ch.setFormatter(formatter)

    if cmdargs.quiet:
        ch.setLevel(logging.CRITICAL)
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
    mboxfile = get_pi_thread_by_msgid(msgid, config, outdir=cmdargs.outdir)
    if mboxfile and cmdargs.amready:
        mbox_to_am(mboxfile, config, outdir=cmdargs.outdir, wantver=cmdargs.version)


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
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Only output errors to the stdout')
    main(parser.parse_args())
