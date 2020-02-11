#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
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
import urllib.parse
import xml.etree.ElementTree as ET
import gzip

from tempfile import mkstemp
from email import charset
charset.add_charset('utf-8', None)
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
            'list-id',
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


def amify_msg(msg, trailers, ensurediff=False):
    body = get_plain_part(msg, ensurediff=ensurediff)
    if trailers:
        body = git_add_trailers(body, trailers)
    msg.set_payload(body.encode('utf-8'))
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


def get_pi_thread_by_url(t_mbx_url, savefile):
    resp = requests.get(t_mbx_url)
    t_mbox = gzip.decompress(resp.content)
    resp.close()
    with open(savefile, 'wb') as fh:
        logger.debug('Saving %s', savefile)
        fh.write(t_mbox)
    return savefile


def get_pi_thread_by_msgid(msgid, config, cmdargs):
    wantname = cmdargs.wantname
    outdir = cmdargs.outdir
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
    if wantname:
        savefile = os.path.join(outdir, wantname)
    else:
        # Save it into msgid.mbox
        savefile = '%s.t.mbx' % msgid
        savefile = os.path.join(outdir, savefile)

    loc = urllib.parse.urlparse(t_mbx_url)
    if cmdargs.useproject:
        t_mbx_url = '%s://%s/%s/%s/t.mbox.gz' % (
            loc.scheme, loc.netloc, cmdargs.useproject, msgid)
    logger.critical('Grabbing thread from %s', loc.netloc)
    return get_pi_thread_by_url(t_mbx_url, savefile)


def get_plain_part(msg, ensurediff=False):
    # walk until we find the first text/plain part
    mcharset = msg.get_content_charset()
    if not mcharset:
        mcharset = 'utf-8'
    body = None
    for part in msg.walk():
        if part.get_content_type().find('text/plain') < 0:
            continue
        body = part.get_payload(decode=True)
        if body is None:
            continue
        pcharset = part.get_content_charset()
        if not pcharset:
            pcharset = mcharset
        body = body.decode(pcharset, errors='replace')
        if ensurediff and not body_contains_diff(body):
            continue
        break
    return body


def git_add_trailers(payload, trailers):
    cmdargs = ['interpret-trailers']
    output = payload
    if trailers:
        for trailer in set(trailers):
            # Check if this trailer is already in the body
            if payload.find(trailer) < 0:
                logger.info('    Adding trailer: %s', trailer)
                cmdargs += ['--trailer', trailer]
        output = git_run_command(None, args=cmdargs, stdin=payload.encode('utf-8'))
    return output


def get_clean_msgid(msg, header='Message-ID'):
    msgid = None
    raw = msg.get(header)
    if raw:
        matches = re.search(r'<([^>]+)>', raw)
        if matches:
            msgid = matches.groups()[0]
    return msgid


def mbox_to_am(mboxfile, config, cmdargs):
    outdir = cmdargs.outdir
    wantver = cmdargs.wantver
    wantname = cmdargs.wantname
    covertrailers = cmdargs.covertrailers
    mbx = mailbox.mbox(mboxfile)
    count = len(mbx)
    logger.info('Analyzing %s messages in the thread', count)
    am_kept = list()
    slug = None
    cover_keys = dict()
    sorted_keys = [None, None]
    trailer_map = dict()
    cur_vn = None
    vn = None
    multiple_revisions = False
    msgid_map = dict()
    irt_map = dict()
    # Go through the mbox once to build a message map:
    for key, msg in mbx.items():
        msgid = get_clean_msgid(msg)
        irtid = get_clean_msgid(msg, header='In-Reply-To')
        msgid_map[msgid] = key
        if irtid is not None:
            if irtid not in irt_map.keys():
                irt_map[irtid] = list()
            irt_map[irtid].append(key)
    # Go through it slowly now
    for key, msg in mbx.items():
        subj_info = get_subject_info(msg['Subject'])
        logger.debug('Looking at msg %s: %s', key, subj_info['full_subject'])
        body = get_plain_part(msg)
        msgid = get_clean_msgid(msg)
        irtid = get_clean_msgid(msg, header='In-Reply-To')
        has_diffstat = body_contains_diffstat(body)
        has_diff = body_contains_diff(body)

        # if it has no in-reply-to, but other messages I-R-T to it, then
        # it's probably a cover letter that doesn't follow the standard 00/NN notation
        if irtid is None and not has_diff and msgid in irt_map.keys():
            logger.debug('  Probaby a cover letter')
            cover_keys[subj_info['revision']] = key
            continue

        if subj_info['revision_inferred'] and irtid in msgid_map:
            # Grab revision info from the cover letter
            cover_subj_info = get_subject_info(mbx[msgid_map[irtid]]['Subject'])
            subj_info['revision'] = cover_subj_info['revision']
            # Make sure sorted_keys has enough members
            if len(sorted_keys) < subj_info['expected'] + 1:
                sorted_keys = [None] * (subj_info['expected'] + 1)

        new_vn = subj_info['revision']
        if cur_vn is None or new_vn > cur_vn:
            if wantver and wantver != new_vn:
                logger.info('Found series revision: v%s (ignored)', new_vn)
            else:
                logger.info('Found series revision: v%s', new_vn)
            if cur_vn is not None and new_vn > cur_vn:
                multiple_revisions = True
            if wantver is None or wantver == new_vn:
                # Blow away anything we currently have in sorted_keys
                sorted_keys = [None] * (subj_info['expected'] + 1)
                slug = None
            cur_vn = new_vn
        elif vn is None:
            cur_vn = new_vn

        if wantver is not None and wantver != cur_vn:
            logger.debug('  Ignoring v%s: %s', cur_vn, subj_info['full_subject'])
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

        # If the counter is 0, it's definitely the cover letter
        if subj_info['counter'] == 0 and cur_vn not in cover_keys.keys():
            # Found the cover letter
            logger.debug('  Found a cover letter for v%s', cur_vn)
            am_kept.append(key)
            sorted_keys[subj_info['counter']] = key
            cover_keys[cur_vn] = key
            continue

        if has_diff:
            # Do we already have a match for this, though?
            if sorted_keys[subj_info['counter']] is None:
                am_kept.append(key)
                sorted_keys[subj_info['counter']] = key
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

    if multiple_revisions and not wantver:
        logger.info('Will use the latest revision: v%s', vn)
        logger.info('You can pick other revisions using the -vN flag')
    if os.path.exists(am_filename):
        os.unlink(am_filename)
    am_mbx = mailbox.mbox(am_filename)
    logger.info('---')

    # Check if any trailers were sent to the cover letter
    global_trailers = []
    if vn in cover_keys and cover_keys[vn] in trailer_map:
        global_trailers = trailer_map[cover_keys[vn]]

    logger.critical('Writing %s', am_filename)
    have_missing = False
    at = 1
    for key in sorted_keys[1:]:
        if key is None:
            logger.error('  ERROR: missing [%s/%s]!', at, len(sorted_keys)-1)
            have_missing = True
        else:
            msg = mbx[key]
            subject = re.sub(r'\s+', ' ', msg['Subject'])
            logger.info('  %s', subject)
            trailers = []
            if key in trailer_map:
                trailers += trailer_map[key]
            if global_trailers and covertrailers:
                trailers += global_trailers
            msg = amify_msg(msg, trailers, ensurediff=True)
            am_mbx.add(msg)
        at += 1

    logger.critical('Total patches: %s', len(am_mbx))
    if global_trailers and not covertrailers:
        # Warn that some trailers were sent to the cover letter
        logger.critical('---')
        logger.critical('NOTE: Some trailers were sent to the cover letter:')
        for trailer in global_trailers:
            logger.critical('      %s', trailer)
        logger.critical('NOTE: Rerun with -t to apply them to all patches')

    logger.critical('---')
    if have_missing:
        logger.critical('WARNING: Thread incomplete!')

    if vn in cover_keys:
        # Save the cover letter
        cover_msg = amify_msg(mbx[cover_keys[vn]], None, ensurediff=False)
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


def get_subject_info(subject):
    subject = re.sub(r'\s+', ' ', subject).strip()
    subject_info = {
        'full_subject': subject,
        'reply': False,
        'resend': False,
        'rfc': False,
        'revision': 1,
        'revision_inferred': True,
        'counter': 1,
        'expected': 1,
        'prefixes': list(),
        'subject': None,
    }
    # Is it a reply?
    if re.search(r'^\w+:\s*\[', subject):
        subject_info['reply'] = True
        subject = re.sub(r'^\w+:\s*\[', '[', subject)

    # Find all [foo] in the title
    while subject.find('[') == 0:
        matches = re.search(r'^\[([^\]]*)\]', subject)
        for chunk in matches.groups()[0].split():
            if re.search(r'^\d+/\d+$', chunk):
                counters = chunk.split('/')
                subject_info['counter'] = int(counters[0])
                subject_info['expected'] = int(counters[1])
            elif re.search(r'^v\d+$', chunk, re.IGNORECASE):
                subject_info['revision'] = int(chunk[1:])
                subject_info['revision_inferred'] = False
            elif chunk.lower() == 'rfc':
                subject_info['rfc'] = True
            elif chunk.lower() == 'resend':
                subject_info['resend'] = True
            subject_info['prefixes'].append(chunk.lower())
        subject = re.sub(r'^\s*\[[^\]]*\]\s*', '', subject)
    subject_info['subject'] = subject

    return subject_info


def body_contains_diffstat(body):
    if re.search(r'^\s*\d+\sfile.*\d+ insertion.*\d+ deletion', body, re.MULTILINE | re.IGNORECASE):
        return True
    return False


def body_contains_diff(body):
    if re.search(r'^---.*\n\+\+\+', body, re.MULTILINE):
        return True
    return False


def get_newest_series(mboxfile, cmdargs):
    # Open the mbox and find the latest series mentioned in it
    mbx = mailbox.mbox(mboxfile)
    base_msg = None
    latest_revision = None
    seen_msgids = list()
    seen_covers = list()
    for key, msg in mbx.items():
        msgid = get_clean_msgid(msg)
        seen_msgids.append(msgid)
        subj_info = get_subject_info(msg['Subject'])
        # Ignore replies or counters above 1
        if subj_info['reply'] or subj_info['counter'] > 1:
            continue
        if latest_revision is None or subj_info['revision'] > latest_revision:
            # New revision
            latest_revision = subj_info['revision']
            if subj_info['counter'] == 0:
                # And a cover letter, nice. This is the easy case
                base_msg = msg
                seen_covers.append(latest_revision)
                continue
            if subj_info['counter'] == 1:
                if latest_revision not in seen_covers:
                    # A patch/series without a cover letter
                    base_msg = msg

    # Get subject info from base_msg again
    subj_info = get_subject_info(base_msg['Subject'])
    if not len(subj_info['prefixes']):
        logger.debug('Not checking for new revisions: no prefixes on the cover letter.')
        mbx.close()
        return
    base_msgid = get_clean_msgid(base_msg)
    fromeml = email.utils.getaddresses(base_msg.get_all('from', []))[0][1]
    msgdate = email.utils.parsedate_tz(str(base_msg['Date']))
    startdate = time.strftime('%Y%m%d', msgdate[:9])
    listarc = base_msg.get_all('List-Archive')[-1].strip('<>')
    q = 's:"%s" AND f:"%s" AND d:%s..' % (subj_info['subject'], fromeml, startdate)
    queryurl = '%s?%s' % (listarc, urllib.parse.urlencode({'q': q, 'x': 'A', 'o': '-1'}))
    logger.critical('Checking for newer revisions on %s', listarc)
    logger.debug('Query URL: %s', queryurl)
    resp = requests.get(queryurl)
    # try to parse it
    tree = ET.fromstring(resp.content)
    resp.close()
    ns = {'atom': 'http://www.w3.org/2005/Atom'}
    entries = tree.findall('atom:entry', ns)

    for entry in entries:
        title = entry.find('atom:title', ns).text
        subj_info = get_subject_info(title)
        if subj_info['reply'] or subj_info['counter'] > 1:
            logger.debug('Ignoring result (not interesting): %s', title)
            continue
        link = entry.find('atom:link', ns).get('href')
        if subj_info['revision'] < latest_revision:
            logger.debug('Ignoring result (not new revision): %s', title)
            continue
        if link.find('/%s/' % base_msgid) > 0:
            logger.debug('Ignoring result (same thread as ours):%s', title)
            continue
        if subj_info['revision'] == 1 and subj_info['revision'] == latest_revision:
            # Someone sent a separate message with an identical title but no new vX in the subject line
            # It's *probably* a new revision.
            logger.debug('Likely a new revision: %s', title)
        elif subj_info['revision'] > latest_revision:
            logger.debug('Definitely a new revision [v%s]: %s', subj_info['revision'], title)
        else:
            logger.debug('No idea what this is: %s', title)
            continue
        t_mbx_url = '%st.mbox.gz' % link
        savefile = mkstemp('get-lore-mbox')[1]
        nt_mboxfile = get_pi_thread_by_url(t_mbx_url, savefile)
        nt_mbx = mailbox.mbox(nt_mboxfile)
        # Append all of these to the existing mailbox
        new_adds = 0
        for nt_msg in nt_mbx:
            nt_msgid = get_clean_msgid(nt_msg)
            if nt_msgid in seen_msgids:
                logger.debug('Duplicate message, skipping')
                continue
            nt_subject = re.sub(r'\s+', ' ', nt_msg['Subject'])
            logger.debug('Adding: %s', nt_subject)
            new_adds += 1
            mbx.add(nt_msg)
            seen_msgids.append(nt_msgid)
        nt_mbx.close()
        if new_adds:
            logger.info('Added %s messages from thread: %s', new_adds, title)
        logger.debug('Removing temporary %s', nt_mboxfile)
        os.unlink(nt_mboxfile)

    # We close the mbox, since we'll be reopening it later
    mbx.close()


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
        if msgid is None:
            logger.error('Unable to find a valid message-id in stdin.')
            sys.exit(1)
    else:
        msgid = cmdargs.msgid

    msgid = msgid.strip('<>')
    config = get_config_from_git()
    mboxfile = get_pi_thread_by_msgid(msgid, config, cmdargs)

    if mboxfile and cmdargs.checknewer:
        get_newest_series(mboxfile, cmdargs)

    if mboxfile is None:
        return

    if cmdargs.amready:
        # Move it into -thread
        threadmbox = '%s-thread' % mboxfile
        os.rename(mboxfile, threadmbox)
        mbox_to_am(threadmbox, config, cmdargs)
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
    parser.add_argument('-p', '--use-project', dest='useproject', default=None,
                        help='Use a specific project instead of guessing (linux-mm, linux-hardening, etc)')
    parser.add_argument('-c', '--check-newer-revisions', dest='checknewer', action='store_true', default=False,
                        help='Check if newer patch revisions exist')
    parser.add_argument('-a', '--am-ready', dest='amready', action='store_true', default=False,
                        help='Make an mbox ready for git am')
    parser.add_argument('-t', '--apply-cover-trailers', dest='covertrailers', action='store_true', default=False,
                        help='Apply trailers sent to the cover letter to all patches (use with -a)')
    parser.add_argument('-v', '--use-version', dest='wantver', type=int, default=None,
                        help='Get a specific version of the patch/series (use with -a)')
    parser.add_argument('-n', '--mbox-name', dest='wantname', default=None,
                        help='Filename to name the mbox file')
    parser.add_argument('-d', '--debug', action='store_true', default=False,
                        help='Add more debugging info to the output')
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Output critical information only')
    main(parser.parse_args())
