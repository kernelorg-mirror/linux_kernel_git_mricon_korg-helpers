#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# This bot automatically recognizes when patchwork-tracked patches
# are applied to git repositories and marks them as "Accepted." It can
# additionally send mail notifications to the maintainers and to the
# patch submitters.
#
# It runs from a cronjob, but can be also run from post-update hooks with
# extra wrappers. For more details, consult:
#
# https://korg.wiki.kernel.org/userdoc/pwbot
#
#

__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import os
import sys
import argparse
import smtplib
import subprocess
import sqlite3
import logging
import hashlib
import re
import requests
import datetime
import netrc

import ruamel.yaml # noqa

from email.mime.text import MIMEText
from email.header import Header
from email.utils import formatdate, getaddresses, make_msgid

from fcntl import lockf, LOCK_EX, LOCK_NB

from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

from string import Template

import xmlrpc.client as xmlrpclib

# Send all email 8-bit, this is not 1999
from email import charset
charset.add_charset('utf-8', charset.SHORTEST)

__VERSION__ = '2.0'
DB_VERSION = 1
REST_API_VERSION = '1.2'
HUNK_RE = re.compile(r'^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@')
FILENAME_RE = re.compile(r'^(---|\+\+\+) (\S+)')
REST_PER_PAGE = 100

CONFIG = None

NOMAIL = False
DRYRUN = False
MAILHOST = 'localhost'
DOMAIN = None
CACHEDIR = os.path.expanduser('~/.cache/git-patchwork-bot')

_project_cache = dict()
_server_cache = dict()

logger = logging.getLogger('gitpwcron')


# Lifted from patchwork pwclient
class Transport(xmlrpclib.SafeTransport):

    def __init__(self, url):
        xmlrpclib.SafeTransport.__init__(self)
        self.credentials = None
        self.host = None
        self.proxy = None
        self.scheme = url.split('://', 1)[0]
        self.https = url.startswith('https')
        if self.https:
            self.proxy = os.environ.get('https_proxy')
        else:
            self.proxy = os.environ.get('http_proxy')
        if self.proxy:
            self.https = self.proxy.startswith('https')

    def make_connection(self, host):
        self.host = host
        if self.proxy:
            host = self.proxy.split('://', 1)[-1].rstrip('/')
        try:
            nc = netrc.netrc()
            auths = nc.authenticators(host)
            if auths:
                login, account, password = auths
                host = f'{login}:{password}@{host}'
        except FileNotFoundError:
            pass
        if self.https:
            return xmlrpclib.SafeTransport.make_connection(self, host)
        else:
            return xmlrpclib.Transport.make_connection(self, host)

    def send_request(self, host, handler, request_body, debug):
        handler = '%s://%s%s' % (self.scheme, host, handler)
        return xmlrpclib.Transport.send_request(self, host, handler, request_body, debug)


class Restmaker:
    def __init__(self, server):
        self.server = server
        self.url = '/'.join((server.rstrip('/'), 'api', REST_API_VERSION))

        self.series_url = '/'.join((self.url, 'series'))
        self.patches_url = '/'.join((self.url, 'patches'))
        self.covers_url = '/'.join((self.url, 'covers'))

        # Simple local cache
        self._patches = dict()

        self.session = requests.session()
        retry = Retry(connect=3, backoff_factor=0.5)
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        headers = {
            'User-Agent': f'git-patchwork-bot/{__VERSION__}',
        }
        apitoken = CONFIG['patchworks'][server].get('apitoken', None)
        if apitoken:
            headers['Authorization'] = f'Token {apitoken}'
        self.session.headers.update(headers)

    def get_cover(self, cover_id):
        try:
            logger.debug('Grabbing cover %d', cover_id)
            url = '/'.join((self.covers_url, str(cover_id), ''))
            logger.debug('url=%s', url)
            rsp = self.session.get(url, stream=False)
            rsp.raise_for_status()
            return rsp.json()
        except requests.exceptions.RequestException as ex:
            logger.info('REST error: %s', ex)
            return None

    def get_patch(self, patch_id):
        if patch_id not in self._patches:
            try:
                logger.debug('Grabbing patch %d', patch_id)
                url = '/'.join((self.patches_url, str(patch_id), ''))
                logger.debug('url=%s', url)
                rsp = self.session.get(url, stream=False)
                rsp.raise_for_status()
                self._patches[patch_id] = rsp.json()
            except requests.exceptions.RequestException as ex:
                logger.info('REST error: %s', ex)
                self._patches[patch_id] = None

        return self._patches[patch_id]

    def get_series(self, series_id):
        try:
            logger.debug('Grabbing series %d', series_id)
            url = '/'.join((self.series_url, str(series_id), ''))
            logger.debug('url=%s', url)
            rsp = self.session.get(url, stream=False)
            rsp.raise_for_status()
        except requests.exceptions.RequestException as ex:
            logger.info('REST error: %s', ex)
            return None

        return rsp.json()

    def get_patch_list(self, params):
        try:
            logger.debug('Grabbing patch list with params=%s', params)
            rsp = self.session.get(self.patches_url, params=params, stream=False)
            rsp.raise_for_status()
        except requests.exceptions.RequestException as ex:
            logger.info('REST error: %s', ex)
            return None

        return rsp.json()

    def get_series_list(self, params):
        try:
            logger.debug('Grabbing series with params=%s', params)
            rsp = self.session.get(self.series_url, params=params, stream=False)
            rsp.raise_for_status()
        except requests.exceptions.RequestException as ex:
            logger.info('REST error: %s', ex)
            return None

        return rsp.json()

    def update_patch(self, patch_id, state=None, archived=False, commit_ref=None):
        # Clear it out of the cache
        if patch_id in self._patches:
            del self._patches[patch_id]

        try:
            logger.debug('Updating patch %d:', patch_id)
            url = '/'.join((self.patches_url, str(patch_id), ''))
            logger.debug('url=%s', url)
            data = list()
            if state is not None:
                logger.debug('  state=%s', state)
                data.append(('state', state))
            if archived:
                logger.debug('  archived=True')
                data.append(('archived', True))
            if commit_ref is not None:
                logger.debug('  commit_ref=%s', commit_ref)
                data.append(('commit_ref', commit_ref))

            rsp = self.session.patch(url, data=data, stream=False)
            rsp.raise_for_status()
        except requests.exceptions.RequestException as ex:
            logger.info('REST error: %s', ex)
            return None

        return rsp.json()


def get_patchwork_patches_by_project_id_hash(rpc, project_id, pwhash):
    logger.debug('Looking up %s', pwhash)
    try:
        patches = rpc.patch_list({'project_id': project_id, 'hash': pwhash, 'archived': False})
    except xmlrpclib.Fault as ex:
        logger.debug('Got a Fault: %s', ex.faultString)
        return None

    if not patches:
        logger.debug('No match for hash=%s', pwhash)
        return None

    return [patch['id'] for patch in patches]


def get_patchwork_pull_requests_by_project(rm, project, fromstate):
    page = 0
    pagedata = list()
    prs = set()
    more = True
    while True:
        if not pagedata and more:
            page += 1
            params = [
                ('project', project),
                ('archived', 'false'),
                ('state', fromstate),
                ('order', '-date'),
                ('page', page),
                ('q', 'PULL'),
                ('per_page', REST_PER_PAGE),
            ]
            logger.debug('Processing page %s', page)

            pagedata = rm.get_patch_list(params)
            if not pagedata or len(pagedata) < REST_PER_PAGE:
                more = False

        if not pagedata:
            logger.debug('Finished processing all patches')
            break

        entry = pagedata.pop()
        pull_url = entry.get('pull_url')
        if pull_url:
            patch_id = entry.get('id')
            logger.info('Found pull request: %s (%s)', pull_url, patch_id)
            chunks = pull_url.split()
            pull_host = chunks[0]
            if len(chunks) > 1:
                pull_refname = chunks[1]
            else:
                pull_refname = 'master'

            prs.add((pull_host, pull_refname, patch_id))

    return prs


def project_by_name(pname):
    global _project_cache
    global _server_cache

    if not pname:
        return None

    if pname not in _project_cache:
        # Find patchwork definition containing this project
        server = None
        pconfig = None
        for defurl in CONFIG['patchworks']:
            if pname in CONFIG['patchworks'][defurl]['projects']:
                server = defurl
                pconfig = CONFIG['patchworks'][defurl]['projects'][pname]
                break
        if not server:
            logger.critical('Could not find project matching %s in config', pname)
            sys.exit(1)

        if server not in _server_cache:
            rm = Restmaker(server)
            _project_cache[server] = dict()
            url = '%s/xmlrpc/' % server
            transport = Transport(url)

            try:
                rpc = xmlrpclib.Server(url, transport=transport)
            except (IOError, OSError):
                logger.info('Unable to connect to %s', url)
                sys.exit(1)

            plist = rpc.project_list('', 0)
            _server_cache[server] = (rm, rpc, plist)
        else:
            rm, rpc, plist = _server_cache[server]

        found = False
        for project in plist:
            if project['linkname'].lower().startswith(pname.lower()):
                logger.debug('project lookup: linkname=%s, server=%s, id=%d', pname, server, project['id'])
                _project_cache[pname] = (project, rm, rpc, pconfig)
                found = True
                break
        if not found:
            logger.info('Could not find project matching %s on server %s', pname, server)
            return None

    return _project_cache[pname]


def db_save_meta(c):
    c.execute('DELETE FROM meta')
    c.execute('''INSERT INTO meta VALUES(?)''', (DB_VERSION,))


def db_save_repo_heads(c, heads):
    c.execute('DELETE FROM heads')
    for refname, commit_id in heads:
        c.execute('''INSERT INTO heads VALUES(?,?)''', (refname, commit_id))


def db_get_repo_heads(c):
    return c.execute('SELECT refname, commit_id FROM heads').fetchall()


def db_init_common_sqlite_db(c):
    c.execute('''
        CREATE TABLE meta (
            version INTEGER
        )''')
    db_save_meta(c)


def db_init_cache_sqlite_db(c):
    logger.info('Initializing new sqlite3 db with metadata version %s', DB_VERSION)
    db_init_common_sqlite_db(c)
    c.execute('''
        CREATE TABLE revs (
            rev TEXT NOT NULL,
            patchwork_id TEXT NOT NULL,
            git_id TEXT NOT NULL,
            created DATE
        )''')
    c.execute('''CREATE UNIQUE INDEX idx_rev ON revs(rev)''')


def db_init_pw_sqlite_db(c):
    logger.info('Initializing new sqlite3 db with metadata version %s', DB_VERSION)
    db_init_common_sqlite_db(c)
    c.execute('''
        CREATE TABLE heads (
            refname TEXT,
            commit_id TEXT
        )''')


def git_get_command_lines(gitdir, args):
    out = git_run_command(gitdir, args)
    lines = list()
    if out:
        for line in out.split('\n'):
            if line == '':
                continue
            lines.append(line)

    return lines


def git_run_command(gitdir, args, stdin=None):
    args = ['git', '--no-pager', '--git-dir', gitdir] + args

    logger.debug('Running %s' % ' '.join(args))

    if stdin is None:
        (output, error) = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()
    else:
        pp = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (output, error) = pp.communicate(input=stdin.encode('utf-8'))

    output = output.strip().decode('utf-8', errors='replace')
    if len(error.strip()):
        logger.debug('Stderr: %s', error.decode('utf-8', errors='replace'))

    return output


def git_get_repo_heads(gitdir):
    refs = list()
    lines = git_get_command_lines(gitdir, ['show-ref', '--heads'])
    if lines is not None:
        for line in lines:
            (commit_id, refname) = line.split()
            refs.append((refname, commit_id))
    return refs


def git_get_new_revs(gitdir, db_heads, git_heads, committers, merges=False):
    newrevs = dict()
    if committers:
        logger.debug('filtering by committers=%s', committers)
    for db_refrow in list(db_heads):
        if db_refrow in git_heads:
            logger.debug('No changes in %s', db_refrow[0])
            continue

        (refname, db_commit_id) = db_refrow
        # Find a matching one in git
        git_commit_id = None
        for git_refrow in git_heads:
            if git_refrow[0] == refname:
                git_commit_id = git_refrow[1]
                break
        if git_commit_id is None:
            # Looks like this head is gone from git
            db_heads.remove(db_refrow)
            continue

        if db_commit_id == git_commit_id:
            # No changes in this head
            continue

        rev_range = '%s..%s' % (db_commit_id, git_commit_id)
        args = ['log', '--pretty=%H:%cn:%ce:%s', '--reverse']
        if not merges:
            args += ['--no-merges']

        args += [rev_range, refname]

        lines = git_get_command_lines(gitdir, args)
        if not lines:
            continue

        revs = list()

        for line in lines:
            (commit_id, cn, ce, logmsg) = line.split(':', 3)
            if committers and ce not in committers:
                logger.debug('Skipping %s, ce=%s', commit_id, ce)
                continue
            if len(cn):
                committer = '%s <%s>' % (cn, ce)
            else:
                committer = ce
            logger.debug('commit_id=%s, committer=%s, subject=%s', commit_id, committer, logmsg)
            revs.append((commit_id, logmsg, committer))

        if revs:
            newrevs[refname] = revs

    return newrevs


def git_get_rev_diff(gitdir, rev):
    args = ['diff', '%s~..%s' % (rev, rev)]
    return git_run_command(gitdir, args)


def git_get_patch_id(diff):
    args = ['patch-id', '--stable']
    out = git_run_command('', args, stdin=diff)
    logger.debug('out=%s', out)
    if not out:
        return None
    return out.split()[0]


def get_patchwork_hash(diff):
    """Generate a hash from a diff. Lifted verbatim from patchwork."""

    # normalise spaces
    diff = diff.replace('\r', '')
    diff = diff.strip() + '\n'

    prefixes = ['-', '+', ' ']
    hashed = hashlib.sha1()

    for line in diff.split('\n'):
        if len(line) <= 0:
            continue

        hunk_match = HUNK_RE.match(line)
        filename_match = FILENAME_RE.match(line)

        if filename_match:
            # normalise -p1 top-directories
            if filename_match.group(1) == '---':
                filename = 'a/'
            else:
                filename = 'b/'
            filename += '/'.join(filename_match.group(2).split('/')[1:])

            line = filename_match.group(1) + ' ' + filename
        elif hunk_match:
            # remove line numbers, but leave line counts
            def fn(x):
                if not x:
                    return 1
                return int(x)
            line_nos = list(map(fn, hunk_match.groups()))
            line = '@@ -%d +%d @@' % tuple(line_nos)
        elif line[0] in prefixes:
            # if we have a +, - or context line, leave as-is
            pass
        else:
            # other lines are ignored
            continue

        hashed.update((line + '\n').encode('utf-8'))

    return hashed.hexdigest()


def listify(obj):
    if isinstance(obj, list):
        return list(obj)
    return [obj]


def send_summary(serieslist, committers, to_state, refname, pname, rs, hs):
    logger.info('Preparing summary')
    # we send summaries by project, so the project name is going to be all the same

    count = 0
    summary = list()
    for sdata in serieslist:
        count += 1
        logger.debug('Summarizing: %s', sdata.get('name'))

        # If we have a cover letter, then the reference is the msgid of the cover letter,
        # else the reference is the msgid of the first patch
        patches = sdata.get('patches')

        submitter = sdata.get('submitter')

        if len(patches) == 1:
            summary.append('Patch: %s' % sdata.get('name'))
        else:
            summary.append('Series: %s' % sdata.get('name'))

        summary.append('  Submitter: %s <%s>' % (submitter.get('name'), submitter.get('email')))
        pid = patches[0].get('id')
        if pid in committers:
            summary.append('  Committer: %s' % committers[pid])
        summary.append('  Patchwork: %s' % sdata.get('web_url'))

        if sdata.get('cover_letter'):
            msgid = sdata.get('cover_letter').get('msgid').strip('<>')
        else:
            msgid = patches[0].get('msgid').strip('<>')

        link = 'https://lore.kernel.org/r/%s' % msgid
        summary.append('  Lore link: %s' % link)

        if len(patches) > 1:
            summary.append('    Patches: %s' % patches[0].get('name'))
            for patch in patches[1:]:
                count += 1
                summary.append('             %s' % patch.get('name'))
        summary.append('')

    bodytpt = Template(CONFIG['templates']['summary'])
    params = {
        'newstate': to_state,
        'treename': rs['treename'],
        'refname': refname.replace('refs/heads/', '', 1),
        'summary': '\n'.join(summary),
        'total': count,
        'signature': CONFIG['templates']['signature'],
    }
    body = bodytpt.safe_substitute(params)

    project, rm, rpc, pconfig = project_by_name(pname)
    tweaks = get_tweaks(pconfig, hs)

    msg = MIMEText(body, _charset='utf-8')
    msg.replace_header('Content-Transfer-Encoding', '8bit')

    msg['Subject'] = Header('Patchwork summary for: %s' % pname)
    msg['From'] = Header(tweaks['from'])
    msg['Message-Id'] = make_msgid('git-patchwork-summary', domain=DOMAIN)
    msg['Date'] = formatdate(localtime=True)

    targets = listify(tweaks['summaryto'])
    msg['To'] = Header(', '.join(targets))
    if 'alwayscc' in tweaks:
        msg['Cc'] = Header(', '.join(listify(tweaks['alwayscc'])))
        targets.append(listify(tweaks['alwayscc']))
    if 'alwaysbcc' in tweaks:
        targets.append(listify(tweaks['alwaysbcc']))

    if not NOMAIL:
        logger.debug('Message follows')
        logger.debug(msg.as_string())
        logger.info('Sending summary to: %s', msg['To'])

        smtp = smtplib.SMTP(MAILHOST)
        smtp.sendmail(tweaks['from'], targets, msg.as_bytes())
        smtp.close()
    else:
        logger.info('Would have sent the following:')
        logger.info('------------------------------')
        logger.info(msg.as_string())
        logger.info('------------------------------')

    return str(msg['Message-Id'])


def get_tweaks(pconfig, hconfig):
    fields = ['from', 'summaryto', 'onlyto', 'neverto', 'onlyifcc', 'neverifcc',
              'alwayscc', 'alwaysbcc', 'cclist', 'ccall']
    bubbled = dict()
    for field in fields:
        if field in hconfig:
            bubbled[field] = hconfig[field]
            continue
        if field in pconfig:
            bubbled[field] = pconfig[field]
    return bubbled


def notify_submitters(serieslist, committers, refname, revs, pname, rs, hs):
    logger.info('Sending submitter notifications')
    project, rm, rpc, pconfig = project_by_name(pname)

    tweaks = get_tweaks(pconfig, hs)

    for sdata in serieslist:
        # If we have a cover letter, then the reference is the msgid of the cover letter,
        # else the reference is the msgid of the first patch
        patches = sdata.get('patches')
        is_pull_request = False
        if sdata.get('cover_letter'):
            reference = sdata.get('cover_letter').get('msgid')
            fullcover = rm.get_cover(sdata.get('cover_letter').get('id'))
            headers = {k.lower(): v for k, v in fullcover.get('headers').items()}
            content = fullcover.get('content')
        else:
            reference = patches[0].get('msgid')
            fullpatch = rm.get_patch(patches[0].get('id'))
            headers = {k.lower(): v for k, v in fullpatch.get('headers').items()}
            content = fullpatch.get('content')
            if fullpatch.get('pull_url'):
                is_pull_request = True

        submitter = sdata.get('submitter')
        project = sdata.get('project')

        if 'neverto' in tweaks:
            neverto = listify(tweaks['neverto'])
            if submitter.get('email') in neverto:
                logger.debug('Skipping neverto address:%s', submitter.get('email'))
                continue

        ccs = list()
        cchdr = headers.get('cc')
        if cchdr:
            ccs = [chunk[1] for chunk in getaddresses(listify(cchdr))]
        tos = list()
        tohdr = headers.get('to')
        if tohdr:
            tos = [chunk[1] for chunk in getaddresses(listify(tohdr))]

        xpb = headers.get('x-patchwork-bot')
        logger.debug('X-Patchwork-Bot=%s', xpb)
        # If X-Patchwork-Bot header is set to "notify" we always notify
        if xpb != 'notify':
            # Use cc-based notification logic
            if 'onlyifcc' in tweaks:
                match = None
                for chunk in listify(tweaks['onlyifcc']):
                    if chunk in ccs:
                        match = chunk
                        break
                if match is None:
                    logger.debug('Skipping %s due to onlyifcc=%s', submitter.get('email'), tweaks['onlyifcc'])
                    continue

            if ccs and 'neverifcc' in tweaks:
                match = None
                for chunk in listify(tweaks['neverifcc']):
                    if chunk in ccs:
                        match = chunk
                        break
                if match is not None:
                    logger.debug('Skipping %s due to neverifcc=%s', submitter.get('email'), tweaks['neverifcc'])
                    continue

        logger.debug('Preparing a notification for %s', submitter.get('email'))
        if is_pull_request:
            reqtype = 'pull request'
        elif len(sdata.get('patches')) > 1:
            reqtype = 'series'
        else:
            reqtype = 'patch'

        trimquote = list()
        if content:
            qcount = 0
            for cline in content.split('\n'):
                # Quote the first paragraph only and then [snip] if we quoted more than 5 lines
                if qcount > 5 and (not len(cline.strip()) or cline.strip().find('---') == 0):
                    trimquote.append('> ')
                    trimquote.append('> [...]')
                    break
                trimquote.append('> %s' % cline.rstrip())
                qcount += 1

        summary = list()
        committer = 'unknown committer'
        for patch in patches:
            summary.append('  - %s' % patch.get('name'))
            pid = patch.get('id')
            committer = committers.get(pid, 'unknown committer')
            if 'commitlink' in rs:
                summary.append('    %s' % (rs['commitlink'] % revs[pid]))

        bodytpt = Template(CONFIG['templates']['submitter'])
        params = {
            'reqtype': reqtype,
            'treename': rs['treename'],
            'refname': refname.replace('refs/heads/', '', 1),
            'committer': committer,
            'sentdate': str(headers.get('date')),
            'trimquote': '\n'.join(trimquote),
            'summary': '\n'.join(summary),
            'signature': CONFIG['templates']['signature'],
        }

        body = bodytpt.safe_substitute(params)

        msg = MIMEText(body, _charset='utf-8')
        msg.replace_header('Content-Transfer-Encoding', '8bit')

        msg['Subject'] = Header('Re: %s' % headers.get('subject'))
        msg['From'] = Header(tweaks['from'])
        msg['Message-Id'] = make_msgid('git-patchwork-notify', domain=DOMAIN)
        msg['Date'] = formatdate(localtime=True)
        msg['References'] = Header(reference)
        msg['In-Reply-To'] = Header(reference)

        if 'onlyto' in tweaks:
            targets = listify(tweaks['onlyto'])
            msg['To'] = '%s <%s>' % (submitter.get('name'), targets[0])
        else:
            targets = [submitter.get('email')]
            msg['To'] = Header('%s <%s>' % (submitter.get('name'), submitter.get('email')))

            ccaddrs = list()
            if tweaks.get('alwayscc'):
                ccaddrs += listify(tweaks['alwayscc'])
                targets += ccaddrs
            if tweaks.get('cclist'):
                ccaddrs.append(project.get('list_email'))
                targets.append(project.get('list_email'))
            if tweaks.get('ccall'):
                for addr in tos + ccs:
                    if addr not in targets:
                        targets.append(addr)
                        ccaddrs.append(addr)

            if 'alwaysbcc' in tweaks:
                targets += listify(tweaks['alwaysbcc'])

            if len(ccaddrs):
                msg['Cc'] = ', '.join(ccaddrs)

        if not NOMAIL:
            logger.debug('Message follows')
            logger.debug(msg.as_string())
            logger.info('Notifying %s', submitter.get('email'))

            smtp = smtplib.SMTP(MAILHOST)
            smtp.sendmail(tweaks['from'], targets, msg.as_bytes())
            smtp.close()
        else:
            logger.info('Would have sent the following:')
            logger.info('------------------------------')
            logger.info(msg.as_string())
            logger.info('------------------------------')


def housekeeping(pname):
    project, rm, rpc, pconfig = project_by_name(pname)
    if 'housekeeping' not in pconfig:
        return

    project_id = project['id']

    logger.info('Running housekeeping for %s', pname)
    hconfig = pconfig['housekeeping']
    cutoffdays = 90
    report = ''

    if 'autosupersede' in hconfig:
        logger.info('Getting series from %s/%s', rm.server, pname)
        try:
            cutoffdays = int(hconfig['autosupersede'])
        except ValueError:
            pass

        cutoffdate = datetime.datetime.now() - datetime.timedelta(days=cutoffdays)
        logger.debug('cutoffdate=%s', cutoffdate)
        series = dict()
        page = 0
        pagedata = list()
        lastpage = False
        while True:
            if not pagedata and not lastpage:
                page += 1
                logger.debug('  grabbing page %d', page)
                params = [
                    ('project', project_id),
                    ('order', '-date'),
                    ('page', page),
                    ('per_page', REST_PER_PAGE)
                ]
                pagedata = rm.get_series_list(params)

            if not pagedata:
                # Got them all?
                logger.debug('Finished processing all series')
                break

            entry = pagedata.pop()
            # Did we go too far back?
            s_date = entry.get('date')
            series_date = datetime.datetime.strptime(s_date, "%Y-%m-%dT%H:%M:%S")
            if series_date < cutoffdate:
                lastpage = True
                logger.debug('Went too far back, stopping at %s', series_date)
                continue

            s_id = entry.get('id')
            s_name = entry.get('name')
            if s_name is None:
                # Ignoring this one, because we must have a name
                continue

            # Remove any [foo] from the front, for best matching.
            # Usually, patchwork strips these, but not always.
            s_name = re.sub(r'^\[.*?]\s*', '', s_name)

            ver = entry.get('version')
            subm_id = entry.get('submitter').get('id')
            patches = list()
            for patch in entry.get('patches'):
                patches.append(patch.get('id'))

            if not patches:
                # Not sure how we can have a series without patches, but ok
                continue

            received_all = entry.get('received_all')
            if (subm_id, s_name) not in series:
                series[(subm_id, s_name)] = dict()

            series[(subm_id, s_name)][series_date] = {
                'id': id,
                'patches': patches,
                'complete': received_all,
                'date': s_date,
                'rev': ver,
            }
            logger.debug('Processed id=%s (%s)', s_id, s_name)

        for key, items in series.items():
            if len(items) < 2:
                # Not a redundant series
                continue

            subm_id, subject = key
            versions = list(items.keys())
            versions.sort()
            latest_version = versions.pop()
            logger.debug('%s: latest_version: %s', subject, items[latest_version]['date'])
            if not items[latest_version]['complete']:
                logger.debug('Skipping this series, because it is not complete')
                continue

            sreport = list()
            logger.info('Checking: [v%s] %s (%s)', items[latest_version]['rev'], subject,
                        items[latest_version]['date'])
            for v in versions:
                rev = items[v]['rev']
                s_date = items[v]['date']
                patch_id = items[v]['patches'][0]
                patch = rm.get_patch(patch_id)
                if not patch:
                    # Huh, what happened?
                    continue
                state = patch.get('state')
                if state != 'superseded':
                    logger.info('  Marking series as superseded: [v%s] %s (%s)', rev, subject, s_date)
                    sreport.append('  Superseding: [v%s] %s (%s):' % (rev, subject, s_date))
                    # Yes, we need to supersede these patches
                    for patch_id in items[v]['patches']:
                        logger.info('    Superseding patch: %d', patch_id)
                        patch = rm.get_patch(patch_id)
                        patch_title = patch.get('name')
                        current_state = patch.get('state')
                        if current_state == 'superseded':
                            logger.info('    Patch already set to superseded, skipping')
                            continue
                        sreport.append('    %s' % patch_title)
                        if not DRYRUN:
                            rm.update_patch(patch_id, state='superseded')
                        else:
                            logger.info('    Dryrun: Not actually setting state')

            if sreport:
                report += 'Latest series: [v%s] %s (%s)\n' % (items[latest_version]['rev'], subject,
                                                              items[latest_version]['date'])
                report += '\n'.join(sreport)
                report += '\n\n'

    if 'autoarchive' in hconfig:
        logger.info('Auto-archiving old patches in %s/%s', rm.server, pname)
        try:
            cutoffdays = int(hconfig['autoarchive'])
        except ValueError:
            pass

        cutoffdate = datetime.datetime.now() - datetime.timedelta(days=cutoffdays)
        logger.debug('cutoffdate=%s', cutoffdate)

        page = 0
        seen = set()
        pagedata = list()
        lastpage = False
        archived = 0
        while True:
            if not pagedata and not lastpage:
                if archived:
                    logger.info('Archived %d patches, grabbing next page', archived)
                params = [
                    ('project', project_id),
                    ('archived', 'false'),
                    ('state', 'new'),
                    ('order', 'date'),
                    ('per_page', REST_PER_PAGE)
                ]

                if DRYRUN:
                    # We don't need pagination if we're not in dryrun, because
                    # once we archive the patches, they don't show up in this
                    # query any more.
                    page += 1
                    params.append(('page', page))

                pagedata = rm.get_patch_list(params)

            if not pagedata:
                logger.debug('Finished processing all patches')
                break

            entry = pagedata.pop()
            # Did we go too far forward?
            patch_date = datetime.datetime.strptime(entry.get('date'), "%Y-%m-%dT%H:%M:%S")
            if patch_date >= cutoffdate:
                # mark that we're on the last page
                lastpage = True
                continue

            patch_id = entry.get('id')
            if patch_id in seen:
                # If the archived setting isn't actually sticking on the server for
                # some reason, then we are in for an infinite loop. Recognize this
                # and quit when that happens.
                logger.info('Setting to archived is not working, exiting loop.')
                break

            seen.add(patch_id)
            archived += 1
            if not DRYRUN:
                rm.update_patch(patch_id, archived=True)
            else:
                logger.info('  Dryrun: Not actually archiving')

        if archived:
            logger.info('Archived %d total patches', archived)

        if not report:
            return

        if 'summaryto' not in pconfig:
            logger.info('Report follows')
            logger.info('------------------------------')
            logger.info(report)
            logger.info('------------------------------')
            logger.debug('summaryto not set, not sending report')
            return

        report += '\n-- \n' + CONFIG['templates']['signature']

        msg = MIMEText(report, _charset='utf-8')
        msg.replace_header('Content-Transfer-Encoding', '8bit')

        msg['Subject'] = 'Patchwork housekeeping for: %s' % pname
        msg['From'] = pconfig['from']
        msg['Message-Id'] = make_msgid('git-patchwork-housekeeping', domain=DOMAIN)
        msg['Date'] = formatdate(localtime=True)

        targets = listify(pconfig['summaryto'])
        msg['To'] = ', '.join(targets)
        if 'alwayscc' in pconfig:
            msg['Cc'] = ', '.join(listify(pconfig['alwayscc']))
            targets += listify(pconfig['alwayscc'])
        if 'alwaysbcc' in pconfig:
            targets += listify(pconfig['alwaysbcc'])

        if not NOMAIL:
            logger.debug('Message follows')
            logger.debug(msg.as_string())
            logger.info('Sending housekeeping summary to: %s', msg['To'])

            smtp = smtplib.SMTP(MAILHOST)
            smtp.sendmail(pconfig['from'], targets, msg.as_bytes())
            smtp.close()
        else:
            logger.info('Would have sent the following:')
            logger.info('------------------------------')
            logger.info(msg.as_string())
            logger.info('------------------------------')


def pwrun(repo, rsettings):
    git_heads = git_get_repo_heads(repo)
    if not git_heads:
        logger.info('Could not get the latest ref in %s', repo)
        sys.exit(1)

    # Do we have a pw.db there yet?
    dbpath = os.path.join(repo, 'pw.db')
    db_exists = os.path.isfile(dbpath)
    dbconn = sqlite3.connect(dbpath, sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    c = dbconn.cursor()

    if not db_exists:
        db_init_pw_sqlite_db(c)
        db_save_repo_heads(c, git_heads)
        # Exit early
        dbconn.commit()
        return

    db_heads = db_get_repo_heads(c)
    committers = rsettings.get('committers', list())

    newrevs = git_get_new_revs(repo, db_heads, git_heads, committers=committers, merges=True)
    if not newrevs:
        logger.debug('No new revs in %s', repo)
        return

    rcdbpath = os.path.join(CACHEDIR, 'revcache.db')
    rcdb_exists = os.path.isfile(rcdbpath)
    rcdbconn = sqlite3.connect(rcdbpath, sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    rc = rcdbconn.cursor()

    if not rcdb_exists:
        db_init_cache_sqlite_db(rc)
    else:
        rc.execute('''DELETE FROM revs WHERE created > datetime('now', ?)''', ('-30 days',))

    count = 0
    for pname, psettings in rsettings['projects'].items():
        rpwhashes = dict()
        rgithashes = dict()
        wantstates = list()
        have_prs = False
        for refname, revlines in newrevs.items():
            found = False
            for wanthead, hsettings in psettings.items():
                if refname.endswith(wanthead):
                    found = True
                    if 'fromstate' in hsettings:
                        wantstates += hsettings['fromstate']
                    break
            if not found:
                logger.debug('Skipping ref %s (not wanted)')
                continue

            rpwhashes[refname] = set()
            logger.debug('Looking at %s', refname)
            for rev, logline, committer in revlines:
                if logline.find('Merge') == 0 and logline.find('://') > 0:
                    have_prs = True
                    rpwhashes[refname].add((rev, logline, committer, None))
                    continue

                hits = rc.execute('SELECT patchwork_id, git_id FROM revs WHERE rev=?', (rev,)).fetchall()
                if not hits:
                    diff = git_get_rev_diff(repo, rev)
                    pwhash = get_patchwork_hash(diff)
                    git_patch_id = git_get_patch_id(diff)
                    if pwhash and git_patch_id:
                        rc.execute('''INSERT INTO revs
                                      VALUES (?, ?, ?, datetime('now'))''', (rev, pwhash, git_patch_id))
                else:
                    pwhash = hits[0][0]
                    git_patch_id = hits[0][1]

                rgithashes[git_patch_id] = rev
                if pwhash:
                    rpwhashes[refname].add((rev, logline, committer, pwhash))

        if not wantstates:
            wantstates = ['new', 'under-review']

        logger.debug('wantstates=%s', wantstates)

        logger.info(' project : %s', pname)
        project, rm, rpc, pconfig = project_by_name(pname)
        project_id = project['id']

        if have_prs:
            logger.info('PR merge commit found, loading up pull requests')
            # Find all from states we're interested in
            prs = get_patchwork_pull_requests_by_project(rm, project_id, wantstates)
        else:
            prs = set()

        for refname, hashpairs in rpwhashes.items():
            logger.info('Analyzing %d revisions in %s', len(hashpairs), refname)
            # Get our settings
            hsettings = None
            for wanthead, hsettings in psettings.items():
                if refname.endswith(wanthead):
                    break
            # Patchwork lowercases state name and replaces spaces with dashes
            to_state = hsettings['tostate'].lower().replace(' ', '-')
            fromstate = list()
            for fs in hsettings.get('fromstate', list()):
                fromstate.append(fs.lower().replace(' ', '-'))
            if not fromstate:
                fromstate = list(wantstates)

            # We create patch_id->rev mapping first
            revs = dict()
            committers = dict()
            for rev, logline, committer, pwhash in hashpairs:
                if have_prs and pwhash is None:
                    if logline.find(' of ') > 0:
                        matches = re.search(r'Merge\s\S+\s[\'\"](\S+)[\'\"]\sof\s(\w+://\S+)', logline)
                        if not matches:
                            continue
                        m_refname = matches.group(1)
                        m_host = matches.group(2)
                    elif logline.find('://') > 0:
                        matches = re.search(r'Merge\s(\w+://\S+)', logline)
                        if not matches:
                            continue
                        m_refname = 'master'
                        m_host = matches.group(1)
                    else:
                        continue

                    logger.debug('Looking for ref %s host %s', m_refname, m_host)

                    for pull_host, pull_refname, patch_id in prs:
                        if pull_host.find(m_host) > -1 and pull_refname.find(m_refname) > -1:
                            logger.info('Found matching pull request in %s (id: %s)', logline, patch_id)
                            revs[patch_id] = rev
                            committers[patch_id] = committer
                            break
                    continue

                # Do we have a matching hash on the server?
                logger.debug('Matching: %s', logline)
                # Theoretically, should only return one, but we play it safe and
                # handle for multiple matches.
                patch_ids = get_patchwork_patches_by_project_id_hash(rpc, project_id, pwhash)
                if not patch_ids:
                    continue

                for patch_id in patch_ids:
                    pdata = rm.get_patch(patch_id)
                    if not pdata:
                        logger.debug('Ignoring patch_id=%d due to REST error', patch_id)
                        continue
                    if pdata.get('state') not in fromstate:
                        logger.debug('Ignoring patch_id=%d due to state=%s', patch_id, pdata.get('state'))
                        continue
                    revs[patch_id] = rev
                    committers[patch_id] = committer

            # Now we iterate through it
            updated_series = list()
            done_patches = set()
            for patch_id in list(revs.keys()):
                if patch_id in done_patches:
                    # we've already updated this series
                    logger.debug('Already applied %d as part of previous series', patch_id)
                    continue
                pdata = rm.get_patch(patch_id)
                serieslist = pdata.get('series', None)
                if not serieslist:
                    # This is probably from the time before patchwork-2 migration.
                    # We'll just ignore those.
                    logger.debug('A patch without an associated series? Woah.')
                    continue

                for series in serieslist:
                    series_id = series.get('id')
                    sdata = rm.get_series(series_id)
                    if not sdata.get('received_all'):
                        logger.debug('Series %d is incomplete, skipping', series_id)
                        continue
                    update_queue = list()
                    for spatch in sdata.get('patches'):
                        spatch_id = spatch.get('id')
                        spdata = rm.get_patch(spatch_id)

                        rev = None
                        if spatch_id in revs:
                            rev = revs[spatch_id]
                        else:
                            # try to use the more fuzzy git-patch-id matching
                            spatch_hash = git_get_patch_id(spdata.get('diff'))
                            if spatch_hash is not None and spatch_hash in rgithashes:
                                logger.debug('Matched via git-patch-id')
                                rev = rgithashes[spatch_hash]
                                revs[spatch_id] = rev
                                # same committer
                                committers[spatch_id] = committers[patch_id]

                        if rev is None:
                            logger.debug('Could not produce precise match for %s', spatch_id)
                            logger.debug('Will not update series: %s', sdata.get('name'))
                            update_queue = list()
                            break

                        update_queue.append((spatch.get('name'), spatch_id, to_state, rev))

                    if update_queue:
                        logger.info('Marking series "%s": %s', to_state, sdata.get('name'))
                        updated_series.append(sdata)
                        for sname, spatch_id, to_state, rev in update_queue:
                            count += 1
                            done_patches.update([spatch_id])
                            if not DRYRUN:
                                logger.info('  Updating: %s', sname)
                                rm.update_patch(spatch_id, state=to_state, commit_ref=rev)
                            else:
                                logger.info('  Updating (DRYRUN): %s', sname)

            if len(updated_series) and hsettings.get('send_summary', False):
                send_summary(updated_series, committers, to_state, refname, pname, rsettings, hsettings)
            if len(updated_series) and hsettings.get('notify_submitter', False):
                notify_submitters(updated_series, committers, refname, revs, pname, rsettings, hsettings)

        if count:
            logger.info('Updated %d patches on %s', count, rm.server)
        else:
            logger.info('No patches updated on %s', rm.server)

    rcdbconn.commit()
    rcdbconn.close()
    if not DRYRUN:
        db_save_repo_heads(c, git_heads)
        dbconn.commit()


def check_repos():
    # Use a global lock to make sure only a single process is running
    try:
        lockfh = open(os.path.join(CACHEDIR, 'patchwork-bot.global.lock'), 'w')
        lockf(lockfh, LOCK_EX | LOCK_NB)
    except IOError:
        logger.info('Could not obtain an exclusive lock, assuming another process is running.')
        sys.exit(0)

    for repo in CONFIG['repos']:
        fullpath = os.path.join(cmdargs.reposdir.rstrip('/'), repo.lstrip('/'))
        if not os.path.isdir(fullpath):
            logger.info('Repository not found: %s', repo)
            continue
        settings = CONFIG['repos'][repo]
        logger.info('Processing: %s', repo)
        pwrun(fullpath, settings)


if __name__ == '__main__':
    # noinspection PyTypeChecker
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('-c', '--cfgfile', required=True,
                        help='Config file with repository and project data.')
    parser.add_argument('-r', '--reposdir', default=None,
                        help='Directory with repositories to process')
    parser.add_argument('-l', '--logfile', default=None,
                        help='Log file for messages during quiet operation')
    parser.add_argument('-m', '--mailhost', default='localhost',
                        help='Mailhost to use when sending mail')
    parser.add_argument('-d', '--dry-run', dest='dryrun', action='store_true', default=False,
                        help='Do not mail or store anything, just do a dry run.')
    parser.add_argument('-n', '--no-mail', dest='nomail', action='store_true', default=False,
                        help='Do not mail anything, but store database entries.')
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Only output errors to the stdout')
    parser.add_argument('-v', '--verbose', action='store_true', default=False,
                        help='Be more verbose in logging output')
    parser.add_argument('-k', '--housekeeping', action='store_true', default=False,
                        help='Perform a housekeeping run (supersede, archive)')
    parser.add_argument('--cachedir', default=None,
                        help='Cache directory to use instead of ~/.cache/git-patchwork-bot')
    parser.add_argument('--domain', default=None,
                        help='Domain to use when creating message-ids')

    cmdargs = parser.parse_args()

    logger.setLevel(logging.DEBUG)

    if cmdargs.logfile:
        ch = logging.FileHandler(cmdargs.logfile)
        formatter = logging.Formatter(
            '[%(asctime)s] %(message)s')
        ch.setFormatter(formatter)

        if cmdargs.verbose:
            ch.setLevel(logging.DEBUG)
        else:
            ch.setLevel(logging.INFO)
        logger.addHandler(ch)

    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    ch.setFormatter(formatter)

    if cmdargs.quiet:
        ch.setLevel(logging.CRITICAL)
    elif cmdargs.verbose:
        ch.setLevel(logging.DEBUG)
    else:
        ch.setLevel(logging.INFO)

    logger.addHandler(ch)
    if cmdargs.nomail or cmdargs.dryrun:
        logger.info('NOMAIL: ON')
        NOMAIL = True
    if cmdargs.dryrun:
        logger.info('DRYRUN: ON')
        DRYRUN = True
    if cmdargs.cachedir:
        CACHEDIR = cmdargs.cachedir
    if cmdargs.domain:
        DOMAIN = cmdargs.domain
    MAILHOST = cmdargs.mailhost

    with open(cmdargs.cfgfile, 'r') as fh:
        cfgyaml = fh.read()
    CONFIG = ruamel.yaml.safe_load(cfgyaml)

    if cmdargs.housekeeping:
        for _pserver, _sconfig in CONFIG['patchworks'].items():
            for _pname in _sconfig['projects']:
                housekeeping(_pname)
    else:
        if not cmdargs.reposdir:
            logger.critical('-r is required for this mode')
            sys.exit(1)
        check_repos()
