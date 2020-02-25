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
import email.header
import email.policy
import subprocess
import logging
import re
import fnmatch
import time

import requests
import urllib.parse
import xml.etree.ElementTree
import gzip

from tempfile import mkstemp
from email import charset
charset.add_charset('utf-8', None)
emlpolicy = email.policy.EmailPolicy(utf8=True, cte_type='8bit', max_line_length=None)
logger = logging.getLogger('get-lore-mbox')

VERSION = '0.2.9'

# You can use bash-style globbing here
WANTHDRS = [
    'sender',
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
]

# You can use bash-style globbing here
# end with '*' to include any other trailers
# You can change the default in your ~/.gitconfig, e.g.:
# [get-lore-mbox]
#   # remember to end with ,*
#   trailer-order=link*,fixes*,cc*,reported*,suggested*,original*,co-*,tested*,reviewed*,acked*,signed-off*,*
DEFAULT_TRAILER_ORDER = 'fixes*,reported*,suggested*,original*,co-*,signed-off*,tested*,reviewed*,acked*,cc*,link*,*'

DEFAULT_CONFIG = {
    'midmask': 'https://lore.kernel.org/r/%s',
    'linkmask': 'https://lore.kernel.org/r/%s',
    'trailer-order': DEFAULT_TRAILER_ORDER,
}


class LoreMailbox:
    def __init__(self):
        self.msgid_map = dict()
        self.series = dict()
        self.followups = list()
        self.unknowns = list()

    def __repr__(self):
        out = list()
        for key, lser in self.series.items():
            out.append(str(lser))
        out.append('--- Followups ---')
        for lmsg in self.followups:
            out.append('  %s' % lmsg.full_subject)
        out.append('--- Unknowns ---')
        for lmsg in self.unknowns:
            out.append('  %s' % lmsg.full_subject)

        return '\n'.join(out)

    def get_by_msgid(self, msgid):
        if msgid in self.msgid_map:
            return self.msgid_map[msgid]
        return None

    def get_series(self, revision=None):
        if revision is None:
            if not len(self.series):
                return None
            # Use the highest revision
            revision = max(self.series.keys())
        elif revision not in self.series.keys():
            return None

        lser = self.series[revision]

        # Is it empty?
        empty = True
        for lmsg in lser.patches:
            if lmsg is not None:
                empty = False
                break
        if empty:
            logger.critical('All patches in series v%s are missing.', lser.revision)
            return None

        # Do we have a cover letter for it?
        if not lser.has_cover:
            # Let's find the first patch with an in-reply-to and see if that
            # is our cover letter
            for member in lser.patches:
                if member is not None and member.in_reply_to is not None:
                    potential = self.get_by_msgid(member.in_reply_to)
                    if potential is not None and potential.has_diffstat and not potential.has_diff:
                        # This is *probably* the cover letter
                        lser.patches[0] = potential
                        lser.has_cover = True
                        break

        # Do we have any follow-ups?
        for fmsg in self.followups:
            logger.debug('Analyzing follow-up: %s', fmsg.full_subject)
            # If there are no trailers in this one, ignore it
            if not len(fmsg.trailers):
                continue
            # if it's for the wrong revision, ignore it
            if lser.revision != fmsg.revision:
                continue
            # Go up through the follow-ups and tally up trailers until
            # we either run out of in-reply-tos, or we find a patch in
            # our series
            if fmsg.in_reply_to is None:
                # Check if there's something matching in References
                refs = fmsg.msg.get('References', '')
                pmsg = None
                for ref in refs.split():
                    refid = ref.strip('<>')
                    if refid in self.msgid_map and refid != fmsg.msgid:
                        pmsg = self.msgid_map[refid]
                        break
                if pmsg is None:
                    # Can't find the message we're replying to here
                    continue
            else:
                pmsg = self.msgid_map[fmsg.in_reply_to]

            trailers = fmsg.trailers
            lvl = 1
            while True:
                logger.debug('%sParent: %s', ' ' * lvl, pmsg.full_subject)
                logger.debug('%sTrailers:', ' ' * lvl)
                for trailer in set(trailers):
                    logger.debug('%s%s: %s', ' ' * (lvl+1), trailer[0], trailer[1])
                found = False
                if lser.revision != pmsg.revision:
                    break
                for lmsg in lser.patches:
                    if lmsg is not None and lmsg.msgid == pmsg.msgid:
                        # Confirmed, this is our parent patch
                        lmsg.followup_trailers += trailers
                        found = True
                        break
                if found:
                    break
                elif pmsg.in_reply_to and pmsg.in_reply_to in self.msgid_map:
                    lvl += 1
                    trailers += pmsg.trailers
                    pmsg = self.msgid_map[pmsg.in_reply_to]
                else:
                    break

        return lser

    def add_message(self, msg):
        lmsg = LoreMessage(msg)
        logger.debug('Looking at: %s', lmsg.full_subject)
        self.msgid_map[lmsg.msgid] = lmsg

        if lmsg.has_diff or lmsg.has_diffstat:
            if lmsg.revision not in self.series:
                self.series[lmsg.revision] = LoreSeries(lmsg.revision, lmsg.expected)
                if len(self.series) > 1:
                    logger.info('Found new series v%s', lmsg.revision)
            if lmsg.has_diff:
                self.series[lmsg.revision].add_patch(lmsg)
            elif lmsg.counter == 0 and lmsg.has_diffstat:
                # Bona-fide cover letter
                self.series[lmsg.revision].add_cover(lmsg)
            elif lmsg.reply:
                # We'll figure out where this belongs later
                self.followups.append(lmsg)
        elif lmsg.reply:
            self.followups.append(lmsg)
        else:
            self.unknowns.append(lmsg)


class LoreSeries:
    def __init__(self, revision, expected):
        self.revision = revision
        self.expected = expected
        self.patches = [None] * (expected+1)
        self.followups = list()
        self.complete = False
        self.has_cover = False

    def __repr__(self):
        out = list()
        if self.has_cover:
            out.append('- Series: [v%s] %s' % (self.revision, self.patches[0].subject))
        elif self.patches[1] is not None:
            out.append('- Series: [v%s] %s' % (self.revision, self.patches[1].subject))
        else:
            out.append('- Series: [v%s] (untitled)' % self.revision)

        out.append('  revision: %s' % self.revision)
        out.append('  expected: %s' % self.expected)
        out.append('  complete: %s' % self.complete)
        out.append('  has_cover: %s' % self.has_cover)
        out.append('  patches:')
        at = 0
        for member in self.patches:
            if member is not None:
                out.append('    [%s/%s] %s' % (at, self.expected, member.subject))
                if member.followup_trailers:
                    out.append('       Add: %s' % ', '.join(member.followup_trailers))
            else:
                out.append('    [%s/%s] MISSING' % (at, self.expected))
            at += 1

        return '\n'.join(out)

    def add_patch(self, lmsg):
        while len(self.patches) < lmsg.expected + 1:
            self.patches.append(None)
        self.expected = lmsg.expected
        if self.patches[lmsg.counter] is not None:
            # Okay, weird, is the one in there a reply?
            omsg = self.patches[lmsg.counter]
            if omsg.reply:
                # Replace that one with this one
                self.patches[lmsg.counter] = lmsg
        else:
            self.patches[lmsg.counter] = lmsg
        self.complete = not (None in self.patches[1:])

    def add_cover(self, lmsg):
        self.add_patch(lmsg)
        self.has_cover = True

    def get_slug(self):
        # Find the first non-None entry
        lmsg = None
        for lmsg in self.patches:
            if lmsg is not None:
                break

        if lmsg is None:
            return 'undefined'

        prefix = time.strftime('%Y%m%d', lmsg.date[:9])
        authorline = email.utils.getaddresses(lmsg.msg.get_all('from', []))[0]
        author = re.sub(r'\W+', '_', authorline[1]).strip('_').lower()
        slug = '%s_%s' % (prefix, author)
        if self.revision != 1:
            slug = 'v%s_%s' % (self.revision, slug)

        return slug

    def save_am_mbox(self, outfile, covertrailers, trailer_order=None, addmysob=False, addlink=False, linkmask=None):
        if os.path.exists(outfile):
            os.unlink(outfile)
        usercfg = dict()
        if addmysob:
            usercfg = get_config_from_git(r'user\..*')
            if 'name' not in usercfg or 'email' not in usercfg:
                logger.critical('WARNING: Unable to add your Signed-off-by: git returned no user.name or user.email')
                addmysob = False

        mbx = mailbox.mbox(outfile)
        logger.info('---')
        logger.critical('Writing %s', outfile)
        at = 1
        for lmsg in self.patches[1:]:
            if lmsg is not None:
                if self.has_cover and covertrailers and self.patches[0].followup_trailers:
                    lmsg.followup_trailers += self.patches[0].followup_trailers
                if addmysob:
                    lmsg.followup_trailers.append(('Signed-off-by', '%s <%s>' % (usercfg['name'], usercfg['email'])))
                if addlink:
                    lmsg.followup_trailers.append(('Link', linkmask % lmsg.msgid))
                logger.info('  %s', lmsg.full_subject)
                msg = lmsg.get_am_message(trailer_order=trailer_order)
                # Pass a policy that avoids most legacy encoding horrors
                mbx.add(msg.as_bytes(policy=emlpolicy))
            else:
                logger.error('  ERROR: missing [%s/%s]!', at, self.expected)
            at += 1
        return mbx

    def save_cover(self, outfile):
        cover_msg = self.patches[0].get_am_message(add_trailers=False, trailer_order=None)
        with open(outfile, 'w') as fh:
            fh.write(cover_msg.as_string(policy=emlpolicy))
        logger.critical('Cover: %s', outfile)


class LoreMessage:
    def __init__(self, msg):
        self.msg = msg
        self.msgid = None

        # Subject-based info
        self.lsubject = None
        self.full_subject = None
        self.subject = None
        self.reply = False
        self.revision = 1
        self.counter = 1
        self.expected = 1
        self.revision_inferred = True
        self.counters_inferred = True

        # Header-based info
        self.in_reply_to = None
        self.fromname = None
        self.fromemail = None
        self.date = None

        # Body and body-based info
        self.body = None
        self.has_diff = False
        self.has_diffstat = False
        self.trailers = list()
        self.followup_trailers = list()

        self.msgid = LoreMessage.get_clean_msgid(self.msg)
        self.lsubject = LoreSubject(msg['Subject'])
        # Copy them into this object for convenience
        self.full_subject = self.lsubject.full_subject
        self.subject = self.lsubject.subject
        self.reply = self.lsubject.reply
        self.revision = self.lsubject.revision
        self.counter = self.lsubject.counter
        self.expected = self.lsubject.expected
        self.revision_inferred = self.lsubject.revision_inferred
        self.counters_inferred = self.lsubject.counters_inferred

        # Handle [PATCH 6/5]
        if self.counter > self.expected:
            self.expected = self.counter

        self.in_reply_to = LoreMessage.get_clean_msgid(self.msg, header='In-Reply-To')

        try:
            fromdata = email.utils.getaddresses(self.msg.get_all('from', []))[0]
            self.fromname = fromdata[0]
            self.fromemail = fromdata[1]
        except IndexError:
            pass

        self.date = email.utils.parsedate_tz(str(self.msg['Date']))

        diffre = re.compile(r'^(---.*\n\+\+\+|GIT binary patch)', re.M | re.I)
        diffstatre = re.compile(r'^\s*\d+ file.*\d+ (insertion|deletion)', re.M | re.I)

        # walk until we find the first text/plain part
        mcharset = self.msg.get_content_charset()
        if not mcharset:
            mcharset = 'utf-8'

        for part in msg.walk():
            cte = part.get_content_type()
            if cte.find('/plain') < 0 and cte.find('/x-patch') < 0:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            pcharset = part.get_content_charset()
            if not pcharset:
                pcharset = mcharset
            payload = payload.decode(pcharset, errors='replace')
            if self.body is None:
                self.body = payload
                continue
            # If we already found a body, but we now find something that contains a diff,
            # then we prefer this part
            if diffre.search(payload):
                self.body = payload

        if diffstatre.search(self.body):
            self.has_diffstat = True
        if diffre.search(self.body):
            self.has_diff = True

        # We only pay attention to trailers that are sent in reply
        if self.reply:
            # Do we have something that looks like a person-trailer?
            matches = re.findall(r'^\s*([\w-]+):[ \t]+(.*<\S+>)\s*$', self.body, re.MULTILINE)
            if matches:
                # Does the email in the trailer match what's in the From?
                for tname, tvalue in matches:
                    namedata = email.utils.getaddresses([tvalue])[0]
                    if namedata[1].lower() == self.fromemail.lower():
                        self.trailers.append((tname, tvalue))

    def __repr__(self):
        out = list()
        out.append('msgid: %s' % self.msgid)
        out.append(str(self.lsubject))

        out.append('  fromname: %s' % self.fromname)
        out.append('  fromemail: %s' % self.fromemail)
        out.append('  date: %s' % str(self.date))
        out.append('  in_reply_to: %s' % self.in_reply_to)

        # Header-based info
        out.append('  --- begin body ---')
        for line in self.body.split('\n'):
            out.append('  |%s' % line)
        out.append('  --- end body ---')

        # Body and body-based info
        out.append('  has_diff: %s' % self.has_diff)
        out.append('  has_diffstat: %s' % self.has_diffstat)
        out.append('  --- begin my trailers ---')
        for trailer in self.trailers:
            out.append('  |%s' % str(trailer))
        out.append('  --- begin followup trailers ---')
        for trailer in self.followup_trailers:
            out.append('  |%s' % str(trailer))
        out.append('  --- end trailers ---')

        return '\n'.join(out)

    @staticmethod
    def clean_header(hdrval):
        uval = hdrval.replace('\n', ' ')
        new_hdrval = re.sub(r'\s+', ' ', uval)
        return new_hdrval.strip()

    @staticmethod
    def get_clean_msgid(msg, header='Message-Id'):
        msgid = None
        raw = msg.get(header)
        if raw:
            matches = re.search(r'<([^>]+)>', LoreMessage.clean_header(raw))
            if matches:
                msgid = matches.groups()[0]
        return msgid

    def fix_trailers(self, trailer_order=None):
        bodylines = self.body.split('\n')
        # Get existing trailers
        # 1. Find the first ---
        # 2. Go backwards and grab everything matching ^[\w-]+:\s.*$ until a blank line
        fixlines = list()
        trailersdone = False
        for line in bodylines:
            if trailersdone:
                fixlines.append(line)
                continue

            if line.strip() == '---':
                # Start going backwards in fixlines
                btrailers = list()
                for rline in reversed(fixlines):
                    if not len(rline.strip()):
                        break
                    matches = re.search(r'^([\w-]+):\s+(.*)', rline)
                    if not matches:
                        break
                    fixlines.pop()
                    btrailers.append(matches.groups())

                # Now we add mix-in trailers
                trailers = btrailers + self.followup_trailers
                added = list()
                if trailer_order is None:
                    trailer_order = DEFAULT_TRAILER_ORDER
                for trailermatch in trailer_order:
                    for trailer in trailers:
                        if trailer in added:
                            continue
                        if fnmatch.fnmatch(trailer[0].lower(), trailermatch.strip()):
                            fixlines.append('%s: %s' % trailer)
                            if trailer not in btrailers:
                                logger.info('    Added: %s: %s' % trailer)
                            else:
                                logger.debug('     Kept: %s: %s' % trailer)
                            added.append(trailer)
                trailersdone = True
            fixlines.append(line)
        self.body = '\n'.join(fixlines)

    def get_am_message(self, add_trailers=True, trailer_order=None):
        if add_trailers:
            self.fix_trailers(trailer_order=trailer_order)
        am_body = self.body
        am_msg = email.message.EmailMessage()
        am_msg.set_payload(am_body.encode('utf-8'))
        # Clean up headers
        for hdrname, hdrval in self.msg.items():
            lhdrname = hdrname.lower()
            wanthdr = False
            for hdrmatch in WANTHDRS:
                if fnmatch.fnmatch(lhdrname, hdrmatch):
                    wanthdr = True
                    break
            if wanthdr:
                new_hdrval = LoreMessage.clean_header(hdrval)
                # noinspection PyBroadException
                try:
                    am_msg.add_header(hdrname, new_hdrval)
                except:
                    # A broad except to handle any potential weird header conditions
                    pass
        am_msg.set_charset('utf-8')
        return am_msg


class LoreSubject:
    def __init__(self, subject):
        # Subject-based info
        self.full_subject = None
        self.subject = None
        self.reply = False
        self.resend = False
        self.patch = False
        self.rfc = False
        self.revision = 1
        self.counter = 1
        self.expected = 1
        self.revision_inferred = True
        self.counters_inferred = True
        self.prefixes = list()

        subject = re.sub(r'\s+', ' ', LoreMessage.clean_header(subject)).strip()
        # Remove any leading [] that don't have "patch", "resend" or "rfc" in them
        while True:
            oldsubj = subject
            subject = re.sub(r'^\s*\[[^\]]*\]\s*(\[[^\]]*(:?patch|resend|rfc).*)', '\\1', subject, flags=re.IGNORECASE)
            if oldsubj == subject:
                break

        # Remove any brackets inside brackets
        while True:
            oldsubj = subject
            subject = re.sub(r'^\s*\[([^\]]*)\[([^\]]*)\]', '[\\1\\2]', subject)
            subject = re.sub(r'^\s*\[([^\]]*)\]([^\]]*)\]', '[\\1\\2]', subject)
            if oldsubj == subject:
                break

        self.full_subject = subject
        # Is it a reply?
        if re.search(r'^\w+:\s*\[', subject):
            self.reply = True
            subject = re.sub(r'^\w+:\s*\[', '[', subject)

        # Find all [foo] in the title
        while subject.find('[') == 0:
            matches = re.search(r'^\[([^\]]*)\]', subject)
            for chunk in matches.groups()[0].split():
                # Remove any trailing commas or semicolons
                chunk = chunk.strip(',;')
                if re.search(r'^\d{1,3}/\d{1,3}$', chunk):
                    counters = chunk.split('/')
                    self.counter = int(counters[0])
                    self.expected = int(counters[1])
                    self.counters_inferred = False
                elif re.search(r'^v\d+$', chunk, re.IGNORECASE):
                    self.revision = int(chunk[1:])
                    self.revision_inferred = False
                elif chunk.lower().find('rfc') == 0:
                    self.rfc = True
                elif chunk.lower().find('resend') == 0:
                    self.resend = True
                elif chunk.lower().find('patch') == 0:
                    self.patch = True
                self.prefixes.append(chunk.lower())
            subject = re.sub(r'^\s*\[[^\]]*\]\s*', '', subject)
        self.subject = subject

    def __repr__(self):
        out = list()
        out.append('  full_subject: %s' % self.full_subject)
        out.append('  subject: %s' % self.subject)
        out.append('  reply: %s' % self.reply)
        out.append('  resend: %s' % self.resend)
        out.append('  patch: %s' % self.patch)
        out.append('  rfc: %s' % self.rfc)
        out.append('  revision: %s' % self.revision)
        out.append('  revision_inferred: %s' % self.revision_inferred)
        out.append('  counter: %s' % self.counter)
        out.append('  expected: %s' % self.expected)
        out.append('  counters_inferred: %s' % self.counters_inferred)
        out.append('  prefixes: %s' % ', '.join(self.prefixes))

        return '\n'.join(out)


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


def get_config_from_git(regexp, defaults=None):
    args = ['config', '-z', '--get-regexp', regexp]
    out = git_run_command(None, args)
    gitconfig = defaults
    if not gitconfig:
        gitconfig = dict()
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


def get_pi_thread_by_url(t_mbx_url, savefile, session):
    resp = session.get(t_mbx_url)
    if resp.status_code != 200:
        logger.critical('Server returned an error: %s', resp.status_code)
        return None
    t_mbox = gzip.decompress(resp.content)
    resp.close()
    if not len(t_mbox):
        logger.critical('No messages found for that query')
        return None
    with open(savefile, 'wb') as fh:
        logger.debug('Saving %s', savefile)
        fh.write(t_mbox)
    return savefile


def get_pi_thread_by_msgid(msgid, config, cmdargs, session):
    wantname = cmdargs.wantname
    outdir = cmdargs.outdir
    # Grab the head from lore, to see where we are redirected
    midmask = config['midmask'] % msgid
    logger.info('Looking up %s', midmask)
    resp = session.head(midmask)
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
        logger.debug('Modifying query to use %s', cmdargs.useproject)
        t_mbx_url = '%s://%s/%s/%s/t.mbox.gz' % (
            loc.scheme, loc.netloc, cmdargs.useproject, msgid)
        logger.debug('Will query: %s', t_mbx_url)
    logger.critical('Grabbing thread from %s', loc.netloc)
    return get_pi_thread_by_url(t_mbx_url, savefile, session)


def mbox_to_am(mboxfile, config, cmdargs):
    outdir = cmdargs.outdir
    wantver = cmdargs.wantver
    wantname = cmdargs.wantname
    covertrailers = cmdargs.covertrailers
    mbx = mailbox.mbox(mboxfile)
    count = len(mbx)
    logger.info('Analyzing %s messages in the thread', count)
    lmbx = LoreMailbox()
    # Go through the mbox once to populate base series
    for key, msg in mbx.items():
        lmbx.add_message(msg)

    lser = lmbx.get_series(revision=wantver)
    if lser is None and wantver is None:
        logger.critical('No patches found.')
        return
    if lser is None:
        logger.critical('Unable to find revision %s', wantver)
        return
    if len(lmbx.series) > 1 and not wantver:
        logger.info('Will use the latest revision: v%s', lser.revision)
        logger.info('You can pick other revisions using the -vN flag')

    if wantname:
        slug = wantname
        if wantname.find('.') > -1:
            slug = '.'.join(wantname.split('.')[:-1])
    else:
        slug = lser.get_slug()

    am_filename = os.path.join(outdir, '%s.mbx' % slug)
    am_cover = os.path.join(outdir, '%s.cover' % slug)

    am_mbx = lser.save_am_mbox(am_filename, covertrailers, trailer_order=config['trailer-order'],
                               addmysob=cmdargs.addmysob, addlink=cmdargs.addlink,
                               linkmask=config['linkmask'])
    logger.info('---')

    logger.critical('Total patches: %s', len(am_mbx))
    if lser.has_cover and lser.patches[0].followup_trailers and not covertrailers:
        # Warn that some trailers were sent to the cover letter
        logger.critical('---')
        logger.critical('NOTE: Some trailers were sent to the cover letter:')
        for trailer in lser.patches[0].followup_trailers:
            logger.critical('      %s: %s', trailer[0], trailer[1])
        logger.critical('NOTE: Rerun with -t to apply them to all patches')

    logger.critical('---')
    if not lser.complete:
        logger.critical('WARNING: Thread incomplete!')

    if lser.has_cover:
        lser.save_cover(am_cover)

    top_msgid = None
    first_body = None
    for lmsg in lser.patches:
        if lmsg is not None:
            first_body = lmsg.body
            top_msgid = lmsg.msgid
            break
    if top_msgid is None:
        logger.critical('Could not find any patches in the series.')
        return

    linkurl = config['linkmask'] % top_msgid
    if cmdargs.quiltready:
        q_dirname = os.path.join(outdir, '%s.patches' % slug)
        am_mbox_to_quilt(am_mbx, q_dirname)
        logger.critical('Quilt: %s', q_dirname)

    logger.critical(' Link: %s', linkurl)

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
        logger.critical(' Base: %s', base_commit)
        logger.critical('       git checkout -b %s %s', slug, base_commit)
        logger.critical('       git am %s', am_filename)
    else:
        logger.critical(' Base: not found, sorry')
        logger.critical('       git checkout -b %s master', slug)
        logger.critical('       git am %s', am_filename)

    am_mbx.close()

    return am_filename


def am_mbox_to_quilt(am_mbx, q_dirname):
    if os.path.exists(q_dirname):
        logger.critical('ERROR: Directory %s exists, not saving quilt patches', q_dirname)
        return
    os.mkdir(q_dirname, 0o755)
    patch_filenames = list()
    for key, msg in am_mbx.items():
        # Run each message through git mailinfo
        msg_out = mkstemp(suffix=None, prefix=None, dir=q_dirname)
        patch_out = mkstemp(suffix=None, prefix=None, dir=q_dirname)
        cmdargs = ['mailinfo', '--encoding=UTF-8', msg_out[1], patch_out[1]]
        info = git_run_command(None, cmdargs, msg.as_bytes(policy=emlpolicy))
        if not len(info.strip()):
            logger.critical('ERROR: Could not get mailinfo from patch %s', msg['Subject'])
            continue
        patchinfo = dict()
        for line in info.split('\n'):
            chunks = line.split(':',  1)
            patchinfo[chunks[0]] = chunks[1]

        slug = re.sub(r'\W+', '_', patchinfo['Subject']).strip('_').lower()
        patch_filename = '%04d_%s.patch' % (key+1, slug)
        patch_filenames.append(patch_filename)
        quilt_out = os.path.join(q_dirname, patch_filename)
        with open(quilt_out, 'wb') as fh:
            line = 'From: %s <%s>\n' % (patchinfo['Author'].strip(), patchinfo['Email'].strip())
            fh.write(line.encode('utf-8'))
            line = 'Subject: %s\n' % patchinfo['Subject'].strip()
            fh.write(line.encode('utf-8'))
            line = 'Date: %s\n' % patchinfo['Date'].strip()
            fh.write(line.encode('utf-8'))
            fh.write('\n'.encode('utf-8'))
            with open(msg_out[1], 'r') as mfh:
                fh.write(mfh.read().encode('utf-8'))
            with open(patch_out[1], 'r') as pfh:
                fh.write(pfh.read().encode('utf-8'))
        logger.debug('  Wrote: %s', patch_filename)
        os.unlink(msg_out[1])
        os.unlink(patch_out[1])
    # Write the series file
    with open(os.path.join(q_dirname, 'series'), 'w') as sfh:
        for patch_filename in patch_filenames:
            sfh.write('%s\n' % patch_filename)


def get_newest_series(mboxfile, session):
    # Open the mbox and find the latest series mentioned in it
    mbx = mailbox.mbox(mboxfile)
    base_msg = None
    latest_revision = None
    seen_msgids = list()
    seen_covers = list()
    for key, msg in mbx.items():
        msgid = LoreMessage.get_clean_msgid(msg)
        seen_msgids.append(msgid)
        lsub = LoreSubject(msg['Subject'])
        # Ignore replies or counters above 1
        if lsub.reply or lsub.counter > 1:
            continue
        if latest_revision is None or lsub.revision > latest_revision:
            # New revision
            latest_revision = lsub.revision
            if lsub.counter == 0:
                # And a cover letter, nice. This is the easy case
                base_msg = msg
                seen_covers.append(latest_revision)
                continue
            if lsub.counter == 1:
                if latest_revision not in seen_covers:
                    # A patch/series without a cover letter
                    base_msg = msg

    # Get subject info from base_msg again
    lsub = LoreSubject(base_msg['Subject'])
    if not len(lsub.prefixes):
        logger.debug('Not checking for new revisions: no prefixes on the cover letter.')
        mbx.close()
        return
    base_msgid = LoreMessage.get_clean_msgid(base_msg)
    fromeml = email.utils.getaddresses(base_msg.get_all('from', []))[0][1]
    msgdate = email.utils.parsedate_tz(str(base_msg['Date']))
    startdate = time.strftime('%Y%m%d', msgdate[:9])
    listarc = base_msg.get_all('List-Archive')[-1].strip('<>')
    q = 's:"%s" AND f:"%s" AND d:%s..' % (lsub.subject.replace('"', ''), fromeml, startdate)
    queryurl = '%s?%s' % (listarc, urllib.parse.urlencode({'q': q, 'x': 'A', 'o': '-1'}))
    logger.critical('Checking for newer revisions on %s', listarc)
    logger.debug('Query URL: %s', queryurl)
    resp = session.get(queryurl)
    # try to parse it
    tree = xml.etree.ElementTree.fromstring(resp.content)
    resp.close()
    ns = {'atom': 'http://www.w3.org/2005/Atom'}
    entries = tree.findall('atom:entry', ns)

    for entry in entries:
        title = entry.find('atom:title', ns).text
        lsub = LoreSubject(title)
        if lsub.reply or lsub.counter > 1:
            logger.debug('Ignoring result (not interesting): %s', title)
            continue
        link = entry.find('atom:link', ns).get('href')
        if lsub.revision < latest_revision:
            logger.debug('Ignoring result (not new revision): %s', title)
            continue
        if link.find('/%s/' % base_msgid) > 0:
            logger.debug('Ignoring result (same thread as ours):%s', title)
            continue
        if lsub.revision == 1 and lsub.revision == latest_revision:
            # Someone sent a separate message with an identical title but no new vX in the subject line
            # It's *probably* a new revision.
            logger.debug('Likely a new revision: %s', title)
        elif lsub.revision > latest_revision:
            logger.debug('Definitely a new revision [v%s]: %s', lsub.revision, title)
        else:
            logger.debug('No idea what this is: %s', title)
            continue
        t_mbx_url = '%st.mbox.gz' % link
        savefile = mkstemp('get-lore-mbox')[1]
        nt_mboxfile = get_pi_thread_by_url(t_mbx_url, savefile, session)
        nt_mbx = mailbox.mbox(nt_mboxfile)
        # Append all of these to the existing mailbox
        new_adds = 0
        for nt_msg in nt_mbx:
            nt_msgid = LoreMessage.get_clean_msgid(nt_msg)
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
    # Handle the case when someone pastes a full URL to the message
    matches = re.search(r'^https?://[^/]+/([^/]+)/([^/]+@[^/]+)', msgid, re.IGNORECASE)
    if matches:
        chunks = matches.groups()
        msgid = chunks[1]
        # Infer the project name from the URL, if possible
        if chunks[0] != 'r':
            cmdargs.useproject = chunks[0]

    config = get_config_from_git(r'get-lore-mbox\..*', defaults=DEFAULT_CONFIG)
    config['trailer-order'] = config['trailer-order'].split(',')

    session = requests.session()
    session.headers.update({'User-Agent': 'get-lore-mbox/%s' % VERSION})
    mboxfile = get_pi_thread_by_msgid(msgid, config, cmdargs, session)

    if mboxfile and cmdargs.checknewer:
        get_newest_series(mboxfile, session)

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
    parser.add_argument('-s', '--add-my-sob', dest='addmysob', action='store_true', default=False,
                        help='Add your own signed-off-by to every patch (use with -a)')
    parser.add_argument('-l', '--add-link', dest='addlink', action='store_true', default=False,
                        help='Add a lore.kernel.org/r/ link to every patch (use with -a)')
    parser.add_argument('-n', '--mbox-name', dest='wantname', default=None,
                        help='Filename to name the mbox file')
    parser.add_argument('-Q', '--quilt-ready', dest='quiltready', action='store_true', default=False,
                        help='Save mbox patches in a quilt-ready folder (use with -a)')
    parser.add_argument('-d', '--debug', action='store_true', default=False,
                        help='Add more debugging info to the output')
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Output critical information only')
    main(parser.parse_args())
