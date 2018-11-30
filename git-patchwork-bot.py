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
from email.utils import make_msgid, formatdate, getaddresses

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

_state_cache = None
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

    return patches


# Lifted from patchwork with minor changes
def state_id_by_name(rpc, name):
    if not name:
        return 0

    global _state_cache

    if _state_cache is None:
        _state_cache = rpc.state_list('', 0)
        logger.debug('_state_cache=%s', _state_cache)

    for state in _state_cache:
        if state['name'].lower().startswith(name.lower()):
            return state['id']

    return 0


def project_id_by_name(rpc, name):
    if not name:
        return 0

    global _project_cache

    if _project_cache is None:
        _project_cache = rpc.project_list('', 0)
        logger.debug('_project_cache=%s', _project_cache)

    for project in _project_cache:
        if project['linkname'].lower().startswith(name.lower()):
            logger.debug('project lookup: linkname=%s, id=%d', name, project['id'])
            return project['id']

    return 0


# Lifted from pwclient with changes
def patchwork_update_patch(rpc, patch, state=None, archived=None, commit=None, dryrun=False):
    ret = dict()

    ret['patch_id'] = patch['id']
    ret['msgid'] = patch['msgid'].decode('utf-8', errors='ignore')
    ret['project'] = patch['project'].data.decode('utf-8', errors='ignore')
    ret['submitter'] = patch['submitter'].data.decode('utf-8', errors='ignore')
    ret['date'] = patch['date'].data
    ret['name'] = patch['name'].decode('utf-8', errors='ignore')
    ret['state'] = state
    ret['commit'] = commit

    params = dict()
    if state:
        state_id = state_id_by_name(rpc, state)
        if not state_id:
            logger.debug('Error: No State found matching %s*', state)
            return None

        params['state'] = state_id

        if patch['state_id'] == state_id:
            logger.info('  This patch is already marked as %s', state)
            return None

    if commit:
        params['commit_ref'] = commit

    if archived:
        params['archived'] = True

    success = False
    if not dryrun:
        try:
            success = rpc.patch_set(patch['id'], params)
        except xmlrpclib.Fault as ex:
            logger.info('Error updating patch: %s', ex.faultString)
    else:
        logger.info('Dry run, not updating patch state')
        success = True

    if success:
        return ret

    return None


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


def git_run_command(gitdir, args):
    args = ['git', '--no-pager', '--git-dir', gitdir] + args

    logger.debug('Running %s' % ' '.join(args))

    (output, error) = subprocess.Popen(args, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE).communicate()
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


def summarize_patches(summarydata, server, config, submitter=None):
    references = list()
    body = ''
    count = 0
    for pdata in sorted(summarydata, key=lambda i: (i['submitter'], i['patch_id'])):
        if submitter is not None and submitter != pdata['submitter']:
            # skip this submitter
            continue
        count += 1

        references.append(pdata['msgid'])
        body += '\n'
        body += '%s\n' % pdata['name']
        body += '    Status   : %s\n' % pdata['state']
        if submitter is None:
            body += '    Submitter: %s\n' % pdata['submitter']
        body += '    Patchwork: %s/patch/%s\n' % (server, pdata['patch_id'])

        if 'commitlink' in config:
            body += '    Webview  : %s/%s\n' % (config['commitlink'], pdata['commit'])

    body += '\nTotal patches: %d\n' % count

    return body, references


def send_summary(summarydata, server, refname, config, nomail):
    logger.info('Preparing summary')
    # we send summaries by project, so the project name is going to be all the same
    project = summarydata[0]['project']
    body = (
               'Hello:\n\n'
               'The following patches were automatically processed, because they were applied to\n'
               '%s:%s:\n'
           ) % (config['treename'], refname)

    pbody, references = summarize_patches(summarydata, server, config)

    body += pbody

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


def notify_submitters(summarydata, server, refname, config, nomail):
    logger.info('Sending submitter notifications')
    # we send a message per submitter, so first find all unique submitters
    submitters = list()
    for pdata in summarydata:
        if pdata['submitter'] not in submitters:
            submitters.append(pdata['submitter'])

    logger.debug('submitters=%s', submitters)
    for submitter in submitters:
        logger.debug('Preparing a notification for %s', submitter)
        body = (
                   'Hello:\n\n'
                   'The following patches you had submitted were recently pushed to\n'
                   '%s (%s):\n'
               ) % (config['treename'], refname)

        pbody, references = summarize_patches(summarydata, server, config, submitter=submitter)

        body += pbody

        body += ('\nYou are awesome, thank you!\n\n'
                 '-- \nDeet-doot-dot, I am a bot.\n'
                 'https://korg.wiki.kernel.org/userdoc/pwbot\n'
                 )

        msg = MIMEText(body, _charset='utf-8')
        msg.replace_header('Content-Transfer-Encoding', '8bit')

        msg['Subject'] = Header('Your patches were applied to %s' % config['treename'], 'utf-8')
        msg['From'] = Header(config['from'], 'utf-8')
        msg['Message-Id'] = make_msgid('git-patchwork-notify')
        msg['Date'] = formatdate(localtime=True)
        msg['References'] = Header(', '.join(references), 'utf-8')

        if 'onlyto' in config:
            targets = config['onlyto']
            msg['To'] = config['onlyto']
        else:
            dest = getaddresses(submitter)
            targets = [chunk[1] for chunk in dest]

            msg['To'] = Header(submitter, 'utf-8')

            if 'alwayscc' in config:
                msg['Cc'] = config['alwayscc']
                targets.append(config['alwayscc'])
            if 'alwaysbcc' in config:
                targets.append(config['alwaysbcc'])

        if not nomail:
            logger.debug('Message follows')
            logger.debug(msg.as_string().decode('utf-8'))
            logger.info('Notifying %s', submitter)

            smtp = smtplib.SMTP(config['mailhost'])
            smtp.sendmail(msg['From'], targets, msg.as_string())
            smtp.close()
        else:
            logger.info('Would have sent the following:')
            logger.info('------------------------------')
            logger.info(msg.as_string().decode('utf-8'))
            logger.info('------------------------------')


def get_rest_patch_detail(patches_url, patch_id, auth, headers):
    try:
        logger.debug('Grabbing patch %s', patch_id)
        url = '/'.join((patches_url, str(patch_id), ''))
        logger.debug('url=%s', url)
        rsp = requests.get(url, auth=auth, headers=headers,
                           params=list(), stream=False)
        rsp.raise_for_status()
    except requests.exceptions.RequestException as ex:
        logger.info('REST error: %s', ex)
        return None

    return rsp.json()


def set_rest_patch_state(patches_url, patch_id, state, auth, headers, archive=False):
    try:
        logger.debug('Setting state for patch %s to %s', patch_id, state)
        url = '/'.join((patches_url, str(patch_id), ''))
        logger.debug('url=%s', url)
        data = list()
        if state is not None:
            data.append(('state', state))
        if archive:
            data.append(('archived', True))

        rsp = requests.patch(url, auth=auth, headers=headers,
                             data=data, stream=False)
        rsp.raise_for_status()
    except requests.exceptions.RequestException as ex:
        logger.info('REST error: %s', ex)
        return None

    return rsp.json()


def housekeeping(server, settings, nomail, dryrun):
    logger.info('Running housekeeping in %s', server)
    # We use the REST API for this, because we need series support
    url = '/'.join((server.rstrip('/'), 'api', REST_API_VERSION))
    headers = {
        'User-Agent': 'git-patchwork-bot',
    }
    auth = requests.auth.HTTPBasicAuth(settings['user'], settings['pass'])

    series_url = '/'.join((url, 'series'))
    patches_url = '/'.join((url, 'patches'))

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
            logger.info('Getting series from %s/%s', server, project)
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
                    try:
                        logger.info('  grabbing page %d', page)
                        params = [
                            ('project', project),
                            ('order', '-date'),
                            ('page', page),
                        ]
                        rsp = requests.get(series_url,  auth=auth, headers=headers,
                                           params=params, stream=False)
                        rsp.raise_for_status()
                        pagedata = rsp.json()
                    except requests.exceptions.RequestException as ex:
                        logger.info('REST error: %s', ex)
                        break

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

                id = entry.get('id')
                name = entry.get('name')
                if name is None:
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
                if (subm_id, name) not in series:
                    series[(subm_id, name)] = dict()
                series[(subm_id, name)][ver] = {
                    'id': id,
                    'patches': patches,
                    'complete': received_all,
                    'date': series_date,
                }
                logger.debug('Processed id=%s (%s)', id, name)

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
                    patch_detail = get_rest_patch_detail(patches_url, patch_id, auth, headers)
                    state = patch_detail.get('state')
                    if state != 'superseded':
                        logger.info('Marking series as superseded: [v%s] %s', i, name)
                        # Yes, we need to supersede these patches
                        for patch_id in items[i]['patches']:
                            logger.info('  Superseding patch: %d', patch_id)
                            patch_detail = get_rest_patch_detail(patches_url, patch_id, auth, headers)
                            patch_title = patch_detail.get('name')
                            current_state = patch_detail.get('state')
                            if current_state == 'superseded':
                                logger.info('  Patch already set to superseded, skipping')
                                continue
                            sreport.append('    %s' % patch_title)
                            if not dryrun:
                                set_rest_patch_state(patches_url, patch_id, 'superseded', auth, headers)
                            else:
                                logger.info('  Dryrun: Not actually setting state')

                if sreport:
                    report += 'Latest series: [v%d] %s\n' % (max_version, name)
                    report += '  Marked the following patches as superseded:\n'
                    report += '\n'.join(sreport)
                    report += '\n\n'

        if 'autoarchive' in hconfig:
            logger.info('Auto-archiving old patches in %s/%s', server, project)
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

                    try:
                        rsp = requests.get(patches_url,  auth=auth, headers=headers,
                                           params=params, stream=False)
                        rsp.raise_for_status()
                        pagedata = rsp.json()
                    except requests.exceptions.RequestException as ex:
                        logger.info('REST error: %s', ex)
                        break

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
                patch_state = entry.get('state')
                patch_title = entry.get('name')
                logger.info('Archiving: %s', patch_title)
                if not dryrun:
                    set_rest_patch_state(patches_url, patch_id, None, auth, headers, archive=True)
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

    global _state_cache
    global _project_cache

    for server, settings in config.items():
        # state is server-specific, so blank out _state_cache
        _state_cache = None
        _project_cache = None
        logger.debug('Working on server %s', server)
        logger.debug('Settings follow')
        logger.debug(settings)
        if not newrevs and 'housekeeping' in settings:
            housekeeping(server, settings, nomail, dryrun)
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
        for refname, revlines in newrevs.items():
            if refname not in statemap:
                # We don't care about this ref
                continue

            rpwhashes[refname] = list()
            logger.debug('Looking at %s', refname)
            for rev, logline in revlines:
                diff = git_get_rev_diff(repo, rev)
                pwhash = get_patchwork_hash(diff)
                if pwhash:
                    rpwhashes[refname].append((rev, logline, pwhash))

        for project in settings['projects'].split(','):
            count = 0
            project = project.strip()
            logger.info('Processing "%s/%s"', server, project)
            project_id = project_id_by_name(rpc, project)
            for refname, hashpairs in rpwhashes.items():
                logger.info('Analyzing %d revisions', len(hashpairs))
                summary = list()
                state = statemap[refname][0]
                archive = False
                if 'archive' in statemap[refname]:
                    archive = True
                for rev, logline, pwhash in hashpairs:
                    # Do we have a matching hash on the server?
                    logger.debug('Querying %s (%s)', rev, logline)
                    patches = get_patchwork_patches_by_project_id_hash(rpc, project_id, pwhash)
                    if not patches:
                        continue

                    for patch in patches:
                        logger.info('  %s: updating "%s" to "%s"', project, logline, state)
                        logger.debug('%s/patch/%d', server, patch['id'])
                        pdata = patchwork_update_patch(rpc, patch, state=state,
                                                       archived=archive, commit=rev, dryrun=dryrun)
                        if pdata is not None:
                            summary.append(pdata)
                            logger.debug('Successfully updated patch_id=%s', patch['id'])
                            count += 1

                if len(summary) and 'send_summary' in statemap[refname]:
                    send_summary(summary, server, refname, settings, nomail)
                if len(summary) and 'notify_submitter' in statemap[refname]:
                    notify_submitters(summary, server, refname, settings, nomail)

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
