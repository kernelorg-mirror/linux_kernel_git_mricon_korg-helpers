#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
from __future__ import (absolute_import,
                        division,
                        print_function,
                        unicode_literals)

__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import os
import sys
import argparse
import email
import email.message
import email.utils
import smtplib
import time
import subprocess
import sqlite3
import logging
import hashlib
import re
import glob

try:
    import xmlrpclib
except ImportError:
    # Python 3 has merged/renamed things.
    import xmlrpc.client as xmlrpclib

from fcntl import lockf, LOCK_EX, LOCK_NB
from string import Template

DB_VERSION = 1
HUNK_RE = re.compile(r'^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@')
FILENAME_RE = re.compile(r'^(---|\+\+\+) (\S+)')

_state_cache = None

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


# Lifted from pwclient with minor changes
def get_patchwork_patch_id_from_hash(rpc, project, hash):
    logger.debug('Looking up %s/%s', project, hash)
    try:
        patch = rpc.patch_get_by_project_hash(project, hash)
    except xmlrpclib.Fault as ex:
        logger.debug('Got a Fault: %s', ex.faultString)
        return None

    if patch == {}:
        logger.debug('No match for %s/%s', project, hash)
        return None

    patch_id = patch['id']
    # be super paranoid
    try:
        patch_id = int(patch_id)
    except ValueError:
        logger.debug('Invalid patch ID obtained from server')
        return None
    return patch_id


# Lifted from patchwork with minor changes
def state_id_by_name(rpc, name):
    if not name:
        return 0

    global _state_cache

    if _state_cache is None:
        _state_cache = rpc.state_list(name, 0)

    for state in _state_cache:
        if state['name'].lower().startswith(name.lower()):
            return state['id']

    return 0


# Lifted from pwclient with changes
def patchwork_update_patch(rpc, patch_id, state=None, archived=None, commit=None, dryrun=False):
    params = dict()

    if state:
        state_id = state_id_by_name(rpc, state)
        if not state_id:
            logger.debug('Error: No State found matching %s*', state)
            return False

        params['state'] = state_id

        patch = rpc.patch_get(patch_id)
        if not patch or 'state_id' not in patch:
            logger.info('  Could not get patch data')
            return False

        if patch['state_id'] == state_id:
            logger.info('  This patch is already marked as %s', state)
            return False

    if commit:
        params['commit_ref'] = commit

    if archived:
        params['archived'] = True

    success = False
    if not dryrun:
        try:
            success = rpc.patch_set(patch_id, params)
        except xmlrpclib.Fault as ex:
            logger.info('Error updating patch: %s', ex.faultString)
    else:
        logger.info('Dry run, not updating patch state')
        success = True

    return success


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


def to_or_cc_contains(tocc, addrs):
    for addr in addrs.split(','):
        if addr.strip() in tocc:
            return True

    return False


def mail_thanks_for_pr(refname, commit_id, merge_id, pirepo, msg_commit_id, config, nomail):
    orig = git_get_message_from_pi(pirepo, msg_commit_id)
    origbody = orig.get_payload(decode=True)
    if origbody is None:
        return None

    origbody = origbody.decode('utf-8', errors='replace')

    if 'notifinbody' in config:
        # Don't send anything if we find a certain string in the body
        # e.g. 'pr-tracker-no-ack'
        if origbody.find(config['notifinbody']) >= 0:
            logger.debug('Body contains notifinbody string')
            logger.debug('Skipping this Pull Request')
            return None

    if 'onlyifinbody' in config:
        # Only send follow-up if we find a specific string in the body
        # e.g. 'pr-tracker-ack'
        if origbody.find(config['onlyifinbody']) < 0:
            logger.debug('Body does not contain onlyifinbody string')
            logger.debug('Skipping this Pull Request')
            return None

    dest = email.utils.getaddresses(orig.get_all('from', []))
    dest += email.utils.getaddresses(orig.get_all('to', []))
    dest += email.utils.getaddresses(orig.get_all('cc', []))
    targets = [chunk[1] for chunk in dest]

    if 'onlyifto' in config:
        # Don't send anything unless the required email is in to or cc
        if not to_or_cc_contains(targets, config['onlyifto']):
            logger.debug('To or CC do not contain: %s', config['onlyifto'])
            logger.debug('Skipping this Pull Request')
            return None

    if 'notifto' in config:
        # Don't send anything if we find a specific address in to or cc
        if to_or_cc_contains(targets, config['notifto']):
            logger.debug('To or CC contains: %s', config['notifto'])
            logger.debug('Skipping this Pull Request')
            return None

    reqdate = orig.get('Date')
    if not reqdate:
        rechdr = orig.get('Received')
        reqdate = time.strftime('%Y-%m-%d at %H:%M:%S',
                                email.utils.parsedate(rechdr.split(';')[-1].strip()))

    msgvalues = dict()

    msgvalues['refname'] = refname
    msgvalues['shortrefname'] = refname.rsplit('/', 1)[-1]
    msgvalues['commit_id'] = merge_id
    msgvalues['merge_id'] = merge_id
    msgvalues['origsubject'] = orig['Subject']

    remoterepo, remoteref = get_remote_ref_from_body(origbody)
    msgvalues['remoteref'] = '(unable to parse the git remote)'
    if remoterepo is not None:
        msgvalues['remoteref'] = '%s %s' % (remoterepo, remoteref)

    msgvalues['reqdate'] = reqdate
    msgvalues['treename'] = ''
    if 'treename' in config:
        msgvalues['treename'] = config['treename']
    msgvalues['br'] = '\n'
    msgvalues['brbr'] = '\n\n'

    msg = email.message.Message()
    msg['Subject'] = Template(config['msgsubject']).safe_substitute(msgvalues)
    msg['From'] = config['from']
    # Set threading bits
    msg['In-Reply-To'] = orig['Message-Id']
    if orig.get('References', ''):
        msg['References'] = orig['References'] + ' ' + orig['Message-Id']
    else:
        msg['References'] = orig['Message-Id']

    msg['X-PR-Tracked-List-Id'] = orig.get('List-Id', '')
    msg['X-PR-Tracked-Message-Id'] = orig['Message-Id']
    msg['X-PR-Tracked-Remote'] = msgvalues['remoteref']
    msg['X-PR-Tracked-Commit-Id'] = commit_id
    msg['X-PR-Merge-Tree'] = msgvalues['treename']
    msg['X-PR-Merge-Refname'] = refname
    msg['X-PR-Merge-Commit-Id'] = merge_id

    msg['Message-Id'] = email.utils.make_msgid('pr-tracker-bot')
    msg['Date'] = email.utils.formatdate(localtime=True)

    # Set to and cc
    if 'onlyto' in config:
        msg['To'] = config['onlyto']
        # Override dest/targets
        targets = [config['onlyto']]
    else:
        msg['To'] = orig['From']
        origcc = orig.get('cc', '')
        if origcc:
            msg['Cc'] = orig['To'] + ', ' + orig['Cc']
        else:
            msg['Cc'] = orig['To']

    if 'alwayscc' in config:
        if msg.get('cc', ''):
            msg['Cc'] = msg.get('cc', '') + ', %s' % config['alwayscc']
        else:
            msg['Cc'] = config['alwayscc']

        if config['alwayscc'] not in targets:
            targets.append(config['alwayscc'])

    if 'alwaysbcc' in config:
        if config['alwaysbcc'] not in targets:
            targets.append(config['alwaysbcc'])

    msg.set_payload(Template(config['msgbody']).safe_substitute(msgvalues))

    logger.debug('Message follows')
    logger.debug(msg.as_string())
    if not nomail:
        logger.info('Mailing thanks for: %s', orig['Subject'])
        smtp = smtplib.SMTP(config['mailhost'])
        smtp.sendmail(msg['From'], targets, msg.as_string())
        smtp.close()
    else:
        logger.info('Would have sent the following:')
        logger.info('------------------------------')
        logger.info(msg.as_string())
        logger.info('------------------------------')

    return msg['Message-Id']


def pwrun(repo, cmdconfig, nomail, dryrun):
    if dryrun:
        nomail = True

    git_heads = git_get_repo_heads(repo)
    if not git_heads:
        logger.critical('Could not get the latest ref in %s', repo)
        sys.exit(1)

    try:
        with open(os.path.join(repo, '.pwrun.lock'), 'w') as lockfh:
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
    if not newrevs:
        logger.info('No new revs since last run.')
        return

    rpwhashes = {}
    for refname, revlines in newrevs.items():
        rpwhashes[refname] = list()
        logger.debug('Looking at %s', refname)
        for rev, logline in revlines:
            diff = git_get_rev_diff(repo, rev)
            pwhash = get_patchwork_hash(diff)
            if pwhash:
                rpwhashes[refname].append((rev, logline, pwhash))

    config = get_config_from_repo(repo, 'patchwork\..*', cmdconfig)

    for server, settings in config.items():
        logger.debug('Working on server %s', server)
        logger.debug('Settings follow')
        logger.debug(settings)
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
                refname, state = pair.split(':')
                if refname in rpwhashes.keys():
                    statemap[refname] = state

            except ValueError:
                logger.info('Invalid statemap entry: %s', pair)

        if not len(statemap):
            logger.info('No matching refnames in statemap for %s', server)
            continue

        for project in settings['projects'].split(','):
            count = 0
            project = project.strip()
            logger.info('Processing "%s/%s"', server, project)
            for refname, hashpairs in rpwhashes.items():
                state = statemap[refname]
                for rev, logline, hash in hashpairs:
                    # Do we have a matching hash on the server?
                    logger.debug('Querying %s (%s)', rev, logline)
                    patch_id = get_patchwork_patch_id_from_hash(rpc, project, hash)
                    if patch_id is None:
                        continue

                    logger.info('  %s: updating "%s" to "%s"', project, logline, state)
                    logger.debug('%s/patch/%d', server, patch_id)
                    success = patchwork_update_patch(rpc, patch_id, state=state,
                                                     archived=False, commit=rev, dryrun=dryrun)
                    if success:
                        logger.debug('Successfully updated patch_id=%s', patch_id)
                        count += 1

            if count:
                logger.info('Updated %d patches on %s', count, server)
            else:
                logger.info('No patches matched on %s', server)

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
                        help='Use these config values instead of looking in the repo (used with -m)')
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

