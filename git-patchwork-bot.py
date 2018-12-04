#!/usr/bin/env python
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
from __future__ import (absolute_import,
                        division,
                        print_function,
                        unicode_literals)

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

from email.mime.text import MIMEText
from email.header import Header
from email.utils import make_msgid, formatdate

# Send all email 8-bit, this is not 1999
from email import charset
charset.add_charset('utf-8', charset.SHORTEST, '8bit')

try:
    import xmlrpclib
except ImportError:
    # Python 3 has merged/renamed things.
    import xmlrpc.client as xmlrpclib

from fcntl import lockf, LOCK_EX, LOCK_NB

DB_VERSION = 1
REST_API_VERSION = '1.1'
HUNK_RE = re.compile(r'^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@')
FILENAME_RE = re.compile(r'^(---|\+\+\+) (\S+)')

_project_cache = None

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

    def set_credentials(self, username=None, password=None):
        self.credentials = '%s:%s' % (username, password)

    def make_connection(self, host):
        self.host = host
        if self.proxy:
            host = self.proxy.split('://', 1)[-1].rstrip('/')
        if self.credentials:
            host = '@'.join([self.credentials, host])
        if self.https:
            return xmlrpclib.SafeTransport.make_connection(self, host)
        else:
            return xmlrpclib.Transport.make_connection(self, host)

    if sys.version_info[0] == 2:
        def send_request(self, connection, handler, request_body):
            handler = '%s://%s%s' % (self.scheme, self.host, handler)
            xmlrpclib.Transport.send_request(self, connection, handler,
                                             request_body)
    else:  # Python 3
        def send_request(self, host, handler, request_body, debug):
            handler = '%s://%s%s' % (self.scheme, host, handler)
            return xmlrpclib.Transport.send_request(self, host, handler,
                                                    request_body, debug)


class Restmaker:
    def __init__(self, server, settings):
        self.server = server
        self.url = '/'.join((server.rstrip('/'), 'api', REST_API_VERSION))
        self.headers = {
            'User-Agent': 'git-patchwork-bot',
        }
        # As long as the REST api does not expose filtering by hash, we have to use
        # user/pass authentication for xmlrpc purposes. We'll implement token
        # authentication when that stops being the case.
        self.auth = requests.auth.HTTPBasicAuth(settings['user'], settings['pass'])

        self.series_url = '/'.join((self.url, 'series'))
        self.patches_url = '/'.join((self.url, 'patches'))
        self.covers_url = '/'.join((self.url, 'covers'))

        # Simple local cache
        self._patches = dict()

    def get_cover(self, cover_id):
        try:
            logger.debug('Grabbing cover %d', cover_id)
            url = '/'.join((self.covers_url, str(cover_id), ''))
            logger.debug('url=%s', url)
            rsp = requests.get(url, auth=self.auth, headers=self.headers,
                               params=list(), stream=False)
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
                rsp = requests.get(url, auth=self.auth, headers=self.headers,
                                   params=list(), stream=False)
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
            rsp = requests.get(url, auth=self.auth, headers=self.headers,
                               params=list(), stream=False)
            rsp.raise_for_status()
        except requests.exceptions.RequestException as ex:
            logger.info('REST error: %s', ex)
            return None

        return rsp.json()

    def get_patch_list(self, params):
        try:
            logger.debug('Grabbing patch list with params=%s', params)
            rsp = requests.get(self.patches_url, auth=self.auth, headers=self.headers,
                               params=params, stream=False)
            rsp.raise_for_status()
        except requests.exceptions.RequestException as ex:
            logger.info('REST error: %s', ex)
            return None

        return rsp.json()

    def get_series_list(self, params):
        try:
            logger.debug('Grabbing series with params=%s', params)
            rsp = requests.get(self.series_url, auth=self.auth, headers=self.headers,
                               params=params, stream=False)
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

            rsp = requests.patch(url, auth=self.auth, headers=self.headers,
                                 data=data, stream=False)
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


def project_id_by_name(rpc, name):
    if not name:
        return 0

    global _project_cache

    if _project_cache is None:
        _project_cache = rpc.project_list('', 0)

    for project in _project_cache:
        if project['linkname'].lower().startswith(name.lower()):
            logger.debug('project lookup: linkname=%s, id=%d', name, project['id'])
            return project['id']

    return 0


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
    c.execute('''
        CREATE TABLE heads (
            refname TEXT,
            commit_id TEXT
        )''')


def db_init_pw_sqlite_db(c):
    logger.info('Initializing new sqlite3 db with metadata version %s', DB_VERSION)
    db_init_common_sqlite_db(c)


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
        (output, error) = subprocess.Popen(args, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE).communicate()
    else:
        pp = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
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


def git_get_new_revs(gitdir, db_heads, git_heads, merges=False):
    newrevs = dict()
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
        args = ['rev-list', '--pretty=oneline', '--reverse']
        if not merges:
            args += ['--no-merges']

        args += [rev_range, refname]

        lines = git_get_command_lines(gitdir, args)
        if not lines:
            continue

        newrevs[refname] = list()

        for line in lines:
            (commit_id, logmsg) = line.split(' ', 1)
            logger.debug('commit_id=%s, subject=%s', commit_id, logmsg)
            newrevs[refname].append((commit_id, logmsg))

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


def get_config_from_repo(repo, regexp, cmdconfig):
    config = dict()
    args = ['config', '-z', '--local', '--get-regexp', regexp]
    out = git_run_command(repo, args)
    if not out:
        return config

    for line in out.split('\x00'):
        if not line:
            continue
        key, value = line.split('\n', 1)
        try:
            chunks = key.split('.')
            ident = '.'.join(chunks[1:-1])
            if not ident:
                ident = '*'
            if ident not in config:
                config[ident] = dict()
            cfgkey = chunks[-1]
            config[ident][cfgkey] = value
        except ValueError:
            logger.debug('Ignoring git config entry %s', line)

    if cmdconfig:
        superconfig = dict()
        for entry in cmdconfig:
            key, value = entry.split('=', 1)
            superconfig[key] = value
        # add/override values with those passed from cmdline
        for ident in config.keys():
            config[ident].update(superconfig)

    return config


def send_summary(serieslist, to_state, refname, config, nomail):
    logger.info('Preparing summary')
    # we send summaries by project, so the project name is going to be all the same
    project = serieslist[0].get('project').get('link_name')
    body = (
               'Hello:\n\n'
               'The following patches were marked "%s", because they were applied to\n'
               '%s (%s):\n'
           ) % (to_state, config['treename'], refname)

    references = list()
    count = 0
    for sdata in serieslist:
        count += 1
        logger.debug('Summarizing: %s', sdata.get('name'))

        # If we have a cover letter, then the reference is the msgid of the cover letter,
        # else the reference is the msgid of the first patch
        patches = sdata.get('patches')
        if sdata.get('cover_letter'):
            references.append(sdata.get('cover_letter').get('msgid'))
        else:
            references.append(patches[0].get('msgid'))

        submitter = sdata.get('submitter')
        body += '\n'

        if len(patches) == 1:
            body += 'Patch: %s\n' % sdata.get('name')
        else:
            body += 'Series: %s\n' % sdata.get('name')

        body += '  Submitter: %s <%s>\n' % (submitter.get('name'), submitter.get('email'))
        body += '  Patchwork: %s\n' % sdata.get('web_url')

        if len(patches) > 1:
            body += '    Patches: %s\n' % patches[0].get('name')
            for patch in patches[1:]:
                count += 1
                body += '             %s\n' % patch.get('name')

    body += '\nTotal patches: %d\n' % count

    body += '\n-- \nDeet-doot-dot, I am a bot.\nhttps://korg.wiki.kernel.org/userdoc/pwbot\n'

    msg = MIMEText(body.encode('utf-8'), _charset='utf-8')
    msg.replace_header('Content-Transfer-Encoding', '8bit')

    msg['Subject'] = Header('Patchwork summary for: %s' % project, 'utf-8')
    msg['From'] = Header(config['from'], 'utf-8')
    msg['Message-Id'] = make_msgid('git-patchwork-summary')
    msg['Date'] = formatdate(localtime=True)
    msg['References'] = Header(', '.join(references), 'utf-8')

    targets = config['summaryto'].split(',')
    msg['To'] = Header(', '.join(targets), 'utf-8')
    if 'alwayscc' in config:
        msg['Cc'] = config['alwayscc']
        targets.append(config['alwayscc'])
    if 'alwaysbcc' in config:
        targets.append(config['alwaysbcc'])

    if not nomail:
        logger.debug('Message follows')
        logger.debug(msg.as_string().decode('utf-8'))
        logger.info('Sending summary to: %s', msg['To'])

        smtp = smtplib.SMTP(config['mailhost'])
        smtp.sendmail(msg['From'], targets, msg.as_string())
        smtp.close()
    else:
        logger.info('Would have sent the following:')
        logger.info('------------------------------')
        logger.info(msg.as_string().decode('utf-8'))
        logger.info('------------------------------')

    return msg['Message-Id']


def notify_submitters(rm, serieslist, refname, config, revs, nomail):
    logger.info('Sending submitter notifications')
    for sdata in serieslist:
        # If we have a cover letter, then the reference is the msgid of the cover letter,
        # else the reference is the msgid of the first patch
        patches = sdata.get('patches')
        if sdata.get('cover_letter'):
            reference = sdata.get('cover_letter').get('msgid')
            fullcover = rm.get_cover(sdata.get('cover_letter').get('id'))
            headers = fullcover.get('headers')
            content = fullcover.get('content')
        else:
            reference = patches[0].get('msgid')
            fullpatch = rm.get_patch(patches[0].get('id'))
            headers = fullpatch.get('headers')
            content = fullpatch.get('content')

        submitter = sdata.get('submitter')
        if 'neverto' in config:
            neverto = config['neverto'].split(',')
            if submitter.get('email') in neverto:
                logger.debug('Skipping neverto address:%s', submitter.get('email'))
                continue

        if 'onlyifcc' in config:
            onlyifcc = config['onlyifcc']
            cc = headers.get('Cc').lower()
            if not cc.find(onlyifcc) >= 0:
                logger.debug('Skipping %s due to onlyifcc=%s', submitter.get('email'), onlyifcc)
                continue

        if 'neverifcc' in config:
            neverifcc = config['neverifcc']
            cc = headers.get('Cc').lower()
            if cc.find(neverifcc) >= 0:
                logger.debug('Skipping %s due to neverifcc=%s', submitter.get('email'), neverifcc)
                continue

        logger.debug('Preparing a notification for %s', submitter.get('email'))
        body = (
                   'On %s you wrote:\n'
               ) % headers.get('Date')

        if content:
            for cline in content.split('\n'):
                body += '> %s\n' % cline.rstrip()
            body += '\n'

        body += '\nThese patches were applied to %s (%s):\n' % (config['treename'], refname)

        for patch in sdata.get('patches'):
            body += '  - %s\n' % patch.get('name')
            if 'commitlink' in config:
                body += '    %s/%s\n' % (config['commitlink'], revs[patch.get('id')])

        body += ('\nYou are awesome, thank you!\n\n'
                 '-- \nDeet-doot-dot, I am a bot.\n'
                 'https://korg.wiki.kernel.org/userdoc/pwbot\n')

        msg = MIMEText(body, _charset='utf-8')
        msg.replace_header('Content-Transfer-Encoding', '8bit')

        msg['Subject'] = Header('Re: %s' % headers.get('Subject'), 'utf-8')
        msg['From'] = Header(config['from'], 'utf-8')
        msg['Message-Id'] = make_msgid('git-patchwork-notify')
        msg['Date'] = formatdate(localtime=True)
        msg['References'] = Header(reference, 'utf-8')
        msg['In-Reply-To'] = Header(reference, 'utf-8')

        if 'onlyto' in config:
            targets = [config['onlyto']]
            msg['To'] = '%s <%s>' % (submitter.get('name'), config['onlyto'])
        else:
            targets = [submitter.get('email')]
            msg['To'] = Header('%s <%s>' % (submitter.get('name'), submitter.get('email')), 'utf-8')

            if 'alwayscc' in config:
                msg['Cc'] = config['alwayscc']
                targets.append(config['alwayscc'].split(','))
            if 'alwaysbcc' in config:
                targets.append(config['alwaysbcc'].split(','))

        if not nomail:
            logger.debug('Message follows')
            logger.debug(msg.as_string().decode('utf-8'))
            logger.info('Notifying %s', submitter.get('email'))

            smtp = smtplib.SMTP(config['mailhost'])
            smtp.sendmail(msg['From'], targets, msg.as_string())
            smtp.close()
        else:
            logger.info('Would have sent the following:')
            logger.info('------------------------------')
            logger.info(msg.as_string().decode('utf-8'))
            logger.info('------------------------------')


def housekeeping(rm, settings, nomail, dryrun):
    logger.info('Running housekeeping in %s', rm.server)
    hconfig = dict()
    cutoffdays = 90

    for chunk in settings['housekeeping'].split(','):
        try:
            key, val = chunk.split('=')
        except ValueError:
            logger.debug('Invalid housekeeping setting: %s', chunk)
            continue
        hconfig[key] = val

    for project in settings['projects'].split(','):
        report = ''
        project = project.strip()
        if 'autosupersede' in hconfig:
            logger.info('Getting series from %s/%s', rm.server, project)
            try:
                cutoffdays = int(hconfig['autosupersede'])
            except ValueError:
                pass

            cutoffdate = datetime.datetime.now() - datetime.timedelta(days=cutoffdays)
            logger.debug('cutoffdate=%s', cutoffdate)
            series = dict()
            page = 0
            pagedata = list()
            while True:
                if not pagedata:
                    page += 1
                    logger.info('  grabbing page %d', page)
                    params = [
                        ('project', project),
                        ('order', '-date'),
                        ('page', page),
                    ]
                    pagedata = rm.get_series_list(params)

                if not pagedata:
                    # Got them all?
                    logger.debug('Finished processing all series')
                    break

                entry = pagedata.pop()
                # Did we go too far back?
                series_date = datetime.datetime.strptime(entry.get('date'), "%Y-%m-%dT%H:%M:%S")
                if series_date < cutoffdate:
                    logger.debug('Went too far back, stopping at %s', series_date)
                    break

                s_id = entry.get('id')
                s_name = entry.get('name')
                if s_name is None:
                    # Ignoring this one, because we must have a name
                    continue
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
                series[(subm_id, s_name)][ver] = {
                    'id': id,
                    'patches': patches,
                    'complete': received_all,
                    'date': series_date,
                }
                logger.debug('Processed id=%s (%s)', s_id, s_name)

            for key, items in series.items():
                if len(items) < 2:
                    # Not a redundant series
                    continue
                subm_id, name = key
                max_version = max(items.keys())
                logger.debug('%s: max version: %d', name, max_version)
                if not items[max_version]['complete']:
                    logger.debug('Skipping this series, because it is not complete')
                    continue

                sreport = list()
                for i in range(1, max_version):
                    if i not in items:
                        continue
                    logger.info('Checking [v%d] %s', i, name)
                    patch_id = items[i]['patches'][0]
                    patch = rm.get_patch(patch_id)
                    state = patch.get('state')
                    if state != 'superseded':
                        logger.info('Marking series as superseded: [v%s] %s', i, name)
                        # Yes, we need to supersede these patches
                        for patch_id in items[i]['patches']:
                            logger.info('  Superseding patch: %d', patch_id)
                            patch = rm.get_patch(patch_id)
                            patch_title = patch.get('name')
                            current_state = patch.get('state')
                            if current_state == 'superseded':
                                logger.info('  Patch already set to superseded, skipping')
                                continue
                            sreport.append('    %s' % patch_title)
                            if not dryrun:
                                rm.update_patch(patch_id, state='superseded')
                            else:
                                logger.info('  Dryrun: Not actually setting state')

                if sreport:
                    report += 'Latest series: [v%d] %s\n' % (max_version, name)
                    report += '  Marked the following patches as superseded:\n'
                    report += '\n'.join(sreport)
                    report += '\n\n'

        if 'autoarchive' in hconfig:
            logger.info('Auto-archiving old patches in %s/%s', rm.server, project)
            try:
                cutoffdays = int(hconfig['autoarchive'])
            except ValueError:
                pass

            cutoffdate = datetime.datetime.now() - datetime.timedelta(days=cutoffdays)
            logger.debug('cutoffdate=%s', cutoffdate)

            page = 0
            seen = set()
            pagedata = list()
            while True:
                if not pagedata:
                    params = [
                        ('project', project),
                        ('archived', 'false'),
                        ('state', 'new'),
                        ('order', 'date'),
                    ]

                    if dryrun:
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
                    logger.debug('Reached the cutoff date, stopping at %s', patch_date)
                    break

                patch_id = entry.get('id')
                if patch_id in seen:
                    # If the archived setting isn't actually sticking on the server for
                    # some reason, then we are in for an infinite loop. Recognize this
                    # and quit when that happens.
                    logger.info('Setting to archived is not working, exiting loop.')
                    break

                seen.update([patch_id])
                patch_title = entry.get('name')
                logger.info('Archiving: %s', patch_title)
                if not dryrun:
                    rm.update_patch(patch_id, archived=True)
                else:
                    logger.info('  Dryrun: Not actually archiving')

        if not report:
            continue

        report += '\n-- \nDeet-doot-dot, I am a bot.\nhttps://korg.wiki.kernel.org/userdoc/pwbot\n'

        msg = MIMEText(report, _charset='utf-8')
        msg.replace_header('Content-Transfer-Encoding', '8bit')

        msg['Subject'] = 'Patchwork housekeeping for: %s' % project
        msg['From'] = settings['from']
        msg['Message-Id'] = make_msgid('git-patchwork-housekeeping')
        msg['Date'] = formatdate(localtime=True)

        targets = settings['summaryto'].split(',')
        msg['To'] = ', '.join(targets)
        if 'alwayscc' in settings:
            msg['Cc'] = settings['alwayscc']
            targets.append(settings['alwayscc'])
        if 'alwaysbcc' in settings:
            targets.append(settings['alwaysbcc'])

        if not nomail:
            logger.debug('Message follows')
            logger.debug(msg.as_string().decode('utf-8'))
            logger.info('Sending housekeeping summary to: %s', msg['To'])

            smtp = smtplib.SMTP(settings['mailhost'])
            smtp.sendmail(msg['From'], targets, msg.as_string())
            smtp.close()
        else:
            logger.info('Would have sent the following:')
            logger.info('------------------------------')
            logger.info(msg.as_string().decode('utf-8'))
            logger.info('------------------------------')


def pwrun(repo, cmdconfig, nomail, dryrun):
    if dryrun:
        nomail = True

    git_heads = git_get_repo_heads(repo)
    if not git_heads:
        logger.info('Could not get the latest ref in %s', repo)
        sys.exit(1)

    try:
        lockfh = open(os.path.join(repo, '.pwrun.lock'), 'w')
        lockf(lockfh, LOCK_EX | LOCK_NB)
    except IOError:
        logger.debug('Could not obtain an exclusive lock, assuming another process is running.')
        return

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

    newrevs = git_get_new_revs(repo, db_heads, git_heads)
    config = get_config_from_repo(repo, 'patchwork\..*', cmdconfig)

    global _project_cache

    for server, settings in config.items():
        _project_cache = None
        logger.debug('Working on server %s', server)
        logger.debug('Settings follow')
        logger.debug(settings)
        rm = Restmaker(server, settings)
        if not newrevs and 'housekeeping' in settings:
            housekeeping(rm, settings, nomail, dryrun)
            return

        url = '%s/xmlrpc/' % server

        transport = Transport(url)
        transport.set_credentials(settings['user'], settings['pass'])

        try:
            rpc = xmlrpclib.Server(url, transport=transport)
        except (IOError, OSError):
            logger.info('Unable to connect to %s', url)
            continue

        # Generate the state map
        statemap = dict()
        for pair in settings['statemap'].split(','):
            try:
                refname, params = pair.split(':')
                statemap[refname] = params.split('/')
            except ValueError:
                logger.info('Invalid statemap entry: %s', pair)

        logger.debug('statemap: %s', statemap)

        rpwhashes = dict()
        rgithashes = dict()
        for refname, revlines in newrevs.items():
            if refname not in statemap:
                # We don't care about this ref
                continue

            rpwhashes[refname] = list()
            logger.debug('Looking at %s', refname)
            for rev, logline in revlines:
                diff = git_get_rev_diff(repo, rev)
                pwhash = get_patchwork_hash(diff)
                git_patch_id = git_get_patch_id(diff)
                rgithashes[git_patch_id] = rev
                if pwhash:
                    rpwhashes[refname].append((rev, logline, pwhash))

        for project in settings['projects'].split(','):
            count = 0
            project = project.strip()
            logger.info('Processing "%s/%s"', server, project)
            project_id = project_id_by_name(rpc, project)

            for refname, hashpairs in rpwhashes.items():
                logger.info('Analyzing %d revisions', len(hashpairs))
                to_state = statemap[refname][0].lower()

                # We create patch_id->rev mapping first
                revs = dict()
                for rev, logline, pwhash in hashpairs:
                    # Do we have a matching hash on the server?
                    logger.info('Matching: %s', logline)
                    # Theoretically, should only return one, but we play it safe and
                    # handle for multiple matches.
                    patch_ids = get_patchwork_patches_by_project_id_hash(rpc, project_id, pwhash)
                    if not patch_ids:
                        continue

                    for patch_id in patch_ids:
                        pdata = rm.get_patch(patch_id)
                        if pdata.get('state') in ('superseded', to_state):
                            logger.debug('Ignoring patch_id=%d (%s)', patch_id, pdata.get('state'))
                            continue
                        revs[patch_id] = rev

                # Now we iterate through it
                updated_series = list()
                done_patches = set()
                for patch_id in revs.keys():
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

                            if rev is None:
                                logger.debug('Could not produce precise match for %s', spatch_id)
                                logger.debug('Will not update series: %s', sdata.get('name'))
                                update_queue = list()
                                break

                            update_queue.append((spatch.get('name'), spatch_id, to_state, rev))

                        if update_queue:
                            logger.info('Accepting series: %s', sdata.get('name'))
                            updated_series.append(sdata)
                            for name, spatch_id, to_state, rev in update_queue:
                                count += 1
                                done_patches.update([spatch_id])
                                if not dryrun:
                                    logger.info('  Updating: %s', name)
                                    rm.update_patch(spatch_id, state=to_state, commit_ref=rev)
                                else:
                                    logger.info('  Updating (DRYRUN): %s', name)

                if len(updated_series) and 'send_summary' in statemap[refname]:
                    send_summary(updated_series, to_state, refname, settings, nomail)
                if len(updated_series) and 'notify_submitter' in statemap[refname]:
                    notify_submitters(rm, updated_series, refname, settings, revs, nomail)

            if count:
                logger.info('Updated %d patches on %s', count, server)
            else:
                logger.info('No patches updated on %s', server)

    if not dryrun:
        db_save_repo_heads(c, git_heads)
        dbconn.commit()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('-r', '--repository', dest='repo', required=True,
                        help='Check the repository and auto-accept any applied patches.')
    parser.add_argument('-c', '--config', dest='config', nargs='+', default=list(),
                        help='Use these config values instead of those in the repo config')
    parser.add_argument('-l', '--logfile', default=None,
                        help='Log file for messages during quiet operation')
    parser.add_argument('-d', '--dry-run', dest='dryrun', action='store_true', default=False,
                        help='Do not mail or store anything, just do a dry run.')
    parser.add_argument('-n', '--no-mail', dest='nomail', action='store_true', default=False,
                        help='Do not mail anything, but store database entries.')
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Only output errors to the stdout')

    cmdargs = parser.parse_args()

    logger.setLevel(logging.DEBUG)

    if cmdargs.logfile:
        ch = logging.FileHandler(cmdargs.logfile)
        formatter = logging.Formatter(
            '[%(asctime)s] %(message)s')
        ch.setFormatter(formatter)

        ch.setLevel(logging.DEBUG)
        logger.addHandler(ch)

    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    ch.setFormatter(formatter)

    if cmdargs.quiet:
        ch.setLevel(logging.CRITICAL)
    else:
        ch.setLevel(logging.INFO)

    logger.addHandler(ch)

    pwrun(cmdargs.repo, cmdargs.config, cmdargs.nomail, cmdargs.dryrun)
