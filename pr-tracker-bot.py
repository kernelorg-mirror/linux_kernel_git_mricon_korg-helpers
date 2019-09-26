#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# PR Tracker Bot tracks pull requests sent to a mailing list (via its
# public-inbox v2 git repository), and notifies senders when their pull
# requests get merged.
#
# It runs from a cronjob, but can be also run from post-update hooks with 
# extra wrappers. For more details, consult:
#
# https://korg.wiki.kernel.org/userdoc/prtracker
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
import random
import smtplib
import time
import subprocess
import sqlite3
import logging
import re
import glob

from fcntl import lockf, LOCK_EX, LOCK_NB
from string import Template

DB_VERSION = 2

# Case doesn't matter
PULL_SUBJECT_RE = [
    re.compile(r'^\S*:?\s*\[GIT', re.IGNORECASE),
    re.compile(r'^\S*:?\s*\[PULL', re.IGNORECASE),
    re.compile(r'^\S*:?\s*\[PLEASE PULL', re.IGNORECASE),
]

# I like these
PULL_BODY_WITH_COMMIT_ID_RE = [
    re.compile(r'^\s*for you to fetch changes up to ([0-9a-f]{5,40}):', re.MULTILINE | re.IGNORECASE),
]

# I don't like these
PULL_BODY_REMOTE_REF_RE = [
    re.compile(r'^\s*([\w+-]+(?:://|@)[\w/.@:~-]+)[\s\\]+([\w/._-]+)\s*$', re.MULTILINE | re.IGNORECASE),
    re.compile(r'^\s*([\w+-]+(?:://|@)[\w/.@~-]+)\s*$', re.MULTILINE | re.IGNORECASE),
]

logger = logging.getLogger('prtracker')


# Python-2.7 doesn't have a domain= keyword argument, so steal make_msgid from python-3.2+
def make_msgid(idstring=None, domain='kernel.org'):
    timeval = int(time.time()*100)
    pid = os.getpid()
    randint = random.getrandbits(64)
    if idstring is None:
        idstring = ''
    else:
        idstring = '.' + idstring

    return '<%d.%d.%d%s@%s>' % (timeval, pid, randint, idstring, domain)


def db_migrate_1_to_2(projpath):
    pirepo, maxshard = get_pirepo_dir(projpath, None)
    old_dbpath = os.path.join(projpath, '{0}.git'.format(maxshard), 'prs.db')
    dbconn_old = sqlite3.connect(old_dbpath, sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    c_old = dbconn_old.cursor()
    # Get all values from old db
    pr_rows = c_old.execute('SELECT * FROM prs').fetchall()
    h_rows = c_old.execute('SELECT * FROM heads').fetchall()
    dbconn_old.close()

    dbpath = os.path.join(projpath, 'prtracker.db')
    dbconn = sqlite3.connect(dbpath, sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    c = dbconn.cursor()
    db_init_pr_sqlite_db(c)

    for row in pr_rows:
        c.execute('INSERT INTO prs VALUES(?,?,?,?,?,?)', [maxshard]+list(row))

    for row in h_rows:
        c.execute('INSERT INTO heads VALUES(?,?,?)', [maxshard] + list(row))

    dbconn.commit()
    dbconn.close()


def db_save_meta(c):
    c.execute('DELETE FROM meta')
    c.execute('''INSERT INTO meta VALUES(?)''', (DB_VERSION,))


def db_save_repo_heads(c, heads, shard=None):
    if shard is None:
        c.execute('DELETE FROM heads')
        for refname, commit_id in heads:
            c.execute('''INSERT INTO heads VALUES(?,?)''', (refname, commit_id))
        return

    c.execute('DELETE FROM heads WHERE pi_shard=?', (shard,))
    for refname, commit_id in heads:
        c.execute('''INSERT INTO heads VALUES(?,?,?)''', (shard, refname, commit_id))


def db_get_repo_heads(c, shard=None):
    if shard is None:
        return c.execute('SELECT refname, commit_id FROM heads').fetchall()
    return c.execute('SELECT refname, commit_id FROM heads WHERE pi_shard=?',
                     (shard,)).fetchall()


def db_init_common_sqlite_db(c):
    c.execute('''
        CREATE TABLE meta (
            version INTEGER
        )''')
    db_save_meta(c)


def db_init_pr_sqlite_db(c):
    logger.info('Initializing new sqlite3 db with metadata version %s', DB_VERSION)
    db_init_common_sqlite_db(c)
    c.execute('''
        CREATE TABLE heads (
            pi_shard INTEGER,
            refname TEXT,
            commit_id TEXT
        )''')
    c.execute('''
        CREATE TABLE prs (
            pi_shard INTEGER,
            msg_commit_id TEXT UNIQUE,
            msg_id TEXT UNIQUE,
            subject TEXT,
            pr_commit_id TEXT,
            received DATE
        )''')


def db_init_thanks_sqlite_db(c):
    logger.info('Initializing new sqlite3 db with metadata version %s', DB_VERSION)
    db_init_common_sqlite_db(c)
    c.execute('''
        CREATE TABLE heads (
            refname TEXT,
            commit_id TEXT
        )''')
    c.execute('''
        CREATE TABLE thanks (
            pr_commit_id TEXT, --- pi.prs.pr_commit_id
            refname TEXT,
            sent_msgid TEXT,
            sent DATE
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


def git_run_command(gitdir, args, logstderr=False):
    cmdargs = ['git', '--no-pager']
    if gitdir:
        cmdargs += ['--git-dir', gitdir]
    cmdargs += args

    logger.debug('Running %s' % ' '.join(cmdargs))

    (output, error) = subprocess.Popen(cmdargs, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE).communicate()
    output = output.strip().decode('utf-8', errors='replace')

    if logstderr and len(error.strip()):
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


def git_get_message_from_pi(projpath, shard, commit_id):
    pirepo = os.path.join(projpath, '{0}.git'.format(shard))
    full_email = git_run_command(pirepo, ['show', '%s:m' % commit_id])
    if not len(full_email):
        return None

    msg = email.message_from_string(full_email.encode('utf-8'))
    return msg


def git_get_commit_id_from_repo_ref(repo, ref):
    # We only handle git and http/s URLs
    if not (repo.find('git://') == 0 or repo.find('http://') == 0 or repo.find('https://') == 0):
        logger.debug('%s uses unsupported protocol', repo)
        return None

    logger.debug('getting commit-id from: %s %s', repo, ref)
    # Drop the leading "refs/", if any
    ref = re.sub(r'^refs/', '', ref)
    # Is it a full ref name or a shortname?
    if ref.find('heads/') < 0 and ref.find('tags/') < 0:
        # Try grabbing it as a head first
        lines = git_get_command_lines(None, ['ls-remote', repo, 'refs/heads/%s' % ref])
        if not lines:
            # try it as a tag, then
            lines = git_get_command_lines(None, ['ls-remote', repo, 'refs/tags/%s^{}' % ref])

    elif ref.find('tags/') == 0:
        lines = git_get_command_lines(None, ['ls-remote', repo, 'refs/%s^{}' % ref])

    else:
        # Grab it as a head and hope for the best
        lines = git_get_command_lines(None, ['ls-remote', repo, 'refs/%s' % ref])

    if not lines:
        # Oh well, we tried
        logger.debug('did not find commit-id, ignoring pull request')
        return None

    commit_id = lines[0].split()[0]
    logger.debug('success, commit-id: %s', commit_id)
    return commit_id


def get_remote_ref_from_body(body):
    repo = None
    ref = None
    for reporef_re in PULL_BODY_REMOTE_REF_RE:
        matches = reporef_re.search(body)
        if matches:
            chunks = matches.groups(0)
            if len(chunks) > 1:
                (repo, ref) = chunks
            else:
                repo = chunks[0]
                ref = 'refs/heads/master'
            break
    return repo, ref


def record_pr_data(shard, msg_commit_id, msg, c):
    body = get_plain_part(msg)
    if body is None:
        return False

    pr_commit_id = None
    for cid_re in PULL_BODY_WITH_COMMIT_ID_RE:
        matches = cid_re.search(body)
        if matches:
            (pr_commit_id,) = matches.groups(0)
            break

    if pr_commit_id is None:
        repo, ref = get_remote_ref_from_body(body)
        if repo is not None:
            pr_commit_id = git_get_commit_id_from_repo_ref(repo, ref)

    if pr_commit_id is None:
        return False

    logger.debug('pr_commit_id=%s', pr_commit_id)
    msg_id = msg['Message-Id']
    received = time.strftime('%Y-%m-%dT%H:%M:%S%z')

    rechdr = msg.get('Received')
    if rechdr:
        received = time.strftime('%Y-%m-%dT%H:%M:%S%z',
                                 email.utils.parsedate(rechdr.split(';')[-1].strip()))
    else:
        # Do we have a date?
        datehdr = msg.get('Date')
        if datehdr:
            received = time.strftime('%Y-%m-%dT%H:%M:%S%z', email.utils.parsedate(datehdr))

    logger.debug('received=%s', received)
    subject = msg['Subject'].replace('\n', ' ')
    logger.debug('Found a new PR: %s', subject)
    logger.debug('     commit_id: %s', pr_commit_id)
    try:
        c.execute('INSERT INTO prs VALUES(?, ?, ?, ?, ?, ?)',
                  (shard, msg_commit_id, msg_id, subject, pr_commit_id, received))
        return True
    except sqlite3.IntegrityError:
        logger.debug('Got integrity-error for %s', msg_id)

    return False


def git_get_new_revs(gitdir, db_heads, git_heads):
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

        if len(db_commit_id):
            rev_range = '%s..%s' % (db_commit_id, git_commit_id)
        else:
            rev_range = git_commit_id

        lines = git_get_command_lines(gitdir, ['rev-list', '--pretty=oneline',
                                               '--reverse', rev_range, refname])
        if not lines:
            continue

        newrevs[refname] = list()

        for line in lines:
            (commit_id, logmsg) = line.split(' ', 1)
            logger.debug('commit_id=%s, subject=%s', commit_id, logmsg)
            newrevs[refname].append((commit_id, logmsg))

    return newrevs


def git_get_merge_id(repo, commit_id):
    # get merge commit id
    args = ['rev-list', '%s..' % commit_id, '--ancestry-path']
    lines = git_get_command_lines(repo, args)
    return lines[-1]


def get_pirepo_dir(projpath, topdir):
    if topdir:
        projpath = os.path.join(topdir, projpath)

    projpath = projpath.rstrip('/')

    # drop the #.git if we find it
    if projpath[-4:] == '.git':
        projpath = os.path.dirname(projpath)

    subs = os.listdir(projpath)
    at = 0
    while True:
        if '{0}.git'.format(at+1) not in subs:
            return projpath, at
        at += 1


def parse_pull_requests(pirepo, topdir, dryrun):
    projpath, maxshard = get_pirepo_dir(pirepo, topdir)
    pirepo = os.path.join(projpath, '{0}.git'.format(maxshard))
    logger.debug('pirepo=%s', pirepo)
    git_heads = git_get_repo_heads(pirepo)
    if not git_heads:
        logger.critical('Could not get the latest ref in %s', pirepo)
        sys.exit(1)

    try:
        lockfh = open(os.path.join(projpath, 'prtracker.lock'), 'w')
        lockf(lockfh, LOCK_EX | LOCK_NB)
    except IOError:
        logger.debug('Could not obtain an exclusive lock, assuming another process is running.')
        return

    # Do we have a prtracker.db there yet?
    dbpath = os.path.join(projpath, 'prtracker.db')
    db_exists = os.path.isfile(dbpath)
    if not db_exists:
        # Do we have a prs.db in the latest shard (metadata 1.0)
        old_dbpath = os.path.join(pirepo, 'prs.db')
        if os.path.exists(old_dbpath):
            db_migrate_1_to_2(projpath)
            db_exists = True

    dbconn = sqlite3.connect(dbpath, sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    c = dbconn.cursor()

    if not db_exists:
        # initialize it once we get the latest ref
        db_init_pr_sqlite_db(c)
        db_save_repo_heads(c, git_heads, maxshard)
        # Exit early
        dbconn.commit()
        return

    db_heads = db_get_repo_heads(c, maxshard)
    if not len(db_heads) and maxshard == 0:
        logger.info('Not sure what happened, but I did not find any db heads and maxshard=0')
        return

    if not len(db_heads):
        # This should only happen when we have rolled over
        # between shards, so we need to first finish the
        # old shard and then prep next run from starting from
        # the new shard
        # Prep the next run with the new shard. We use empty
        # space so that rev-list formulates a ..foo rev-list
        # that returns the revs from the beginning of history
        logger.info('Preparing for shard roll-over to %s.git', maxshard)
        db_save_repo_heads(c, (('refs/heads/master', ''),), maxshard)
        # Now let's finish with the previous shard
        maxshard -= 1
        # Refresh these
        pirepo = os.path.join(projpath, '{0}.git'.format(maxshard))
        logger.info('Finishing previous shard %s', pirepo)
        db_heads = db_get_repo_heads(c, maxshard)
        git_heads = git_get_repo_heads(pirepo)

    newrevs = git_get_new_revs(pirepo, db_heads, git_heads)
    if not newrevs:
        logger.info('No new revs since last run.')
        return

    # We are only interested in refs/heads/master for public-inbox
    msgrevs = newrevs['refs/heads/master']
    logger.info('Found %s new messages since last run', len(msgrevs))
    new_prs = 0
    for commit_id, subject in msgrevs:
        for subject_re in PULL_SUBJECT_RE:
            if subject_re.match(subject):
                logger.debug('potential match: "%s"', subject)
                msg = git_get_message_from_pi(projpath, maxshard, commit_id)
                if msg is not None and record_pr_data(maxshard, commit_id, msg, c):
                    new_prs += 1
                    logger.info('Started tracking: %s', subject)

    if not new_prs:
        logger.info('No new PRs found.')

    db_save_repo_heads(c, git_heads, maxshard)

    if not dryrun:
        dbconn.commit()


def get_config_from_repo(repo, pitopdir, cmdconfig):
    config = dict()
    args = ['config', '-z', '--local', '--get-regexp', r'prtracker\..*']
    out = git_run_command(repo, args)
    if not out:
        return config

    for line in out.split('\x00'):
        if not line:
            continue
        key, value = line.split('\n', 1)
        try:
            chunks = key.split('.')
            pirepo = '.'.join(chunks[1:-1])
            if not pirepo:
                pirepo = '*'
            if pirepo not in config:
                config[pirepo] = dict()
            cfgkey = chunks[-1]
            config[pirepo][cfgkey] = value
        except ValueError:
            logger.debug('Ignoring git config entry %s', line)

    if '*' in config and 'pirepos' in config['*']:
        subconfig = dict(config['*'])
        del(subconfig['pirepos'])
        for repoglob in config['*']['pirepos'].split(','):
            repoglob = repoglob.strip()
            if pitopdir:
                # It's hurky to add it here only to remove it there, but
                # the alternatives were hurkier.
                repoglob = os.path.join(pitopdir, repoglob)
            for pirepo in glob.glob(repoglob):
                if pitopdir:
                    pirepo = pirepo.replace(pitopdir, '').lstrip('/')
                config[pirepo] = subconfig
        del(config['*'])

    if cmdconfig:
        superconfig = dict()
        for entry in cmdconfig:
            key, value = entry.split('=', 1)
            superconfig[key] = value
        # add/override values with those passed from cmdline
        for pirepo in config.keys():
            config[pirepo].update(superconfig)

    return config


def get_all_thanked_prs(c, cutoffdays=30):
    rows = c.execute('''
                     SELECT pr_commit_id
                       FROM thanks
                      WHERE sent > datetime('now', ?)
                     ''', ('-%d day' % cutoffdays,)).fetchall()
    sent_prs = set()
    for row in rows:
        sent_prs.update((row[0],))

    return sent_prs


def get_all_prs(projpath, cutoffdays=30):
    dbpath = os.path.join(projpath, 'prtracker.db')
    if not os.path.isfile(dbpath):
        # The DB does not exist, and we don't create it
        # from this location.
        logger.info('No PRs database found, assuming it did not run yet.')
        return list()

    prdbconn = sqlite3.connect(dbpath, sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    prc = prdbconn.cursor()
    rows = prc.execute('''
                       SELECT pi_shard, pr_commit_id, msg_commit_id
                         FROM prs 
                        WHERE received >= datetime('now', ?)
                       ''', ('-%d day' % cutoffdays,)).fetchall()
    return rows


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


def get_unprocessed_prs(c, prs, cutoffdays=30):
    sent_prs = get_all_thanked_prs(c, cutoffdays)
    new_prs = list()
    for row in prs:
        if row[1] not in sent_prs:
            new_prs.append(row)
    return new_prs


def to_or_cc_contains(tocc, addrs):
    for addr in addrs.split(','):
        if addr.strip() in tocc:
            return True

    return False


def format_addrs(pairs):
    return ', '.join([email.utils.formataddr(pair) for pair in pairs])


def thank_for_pr(c, repo, refname, commit_id, projpath, pi_shard, msg_commit_id, config, dryrun, nomail):
    # Make sure we haven't thanked for it already
    c.execute('SELECT sent_msgid FROM thanks WHERE pr_commit_id=? AND refname=?',
              (commit_id, refname))
    rows = c.fetchall()
    if rows:
        logger.debug('Already thanked for this PR, skipping')
        return None

    orig = git_get_message_from_pi(projpath, pi_shard, msg_commit_id)
    if orig is None:
        return None

    origbody = get_plain_part(orig)
    if origbody is None:
        return None

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

    allfrom = email.utils.getaddresses(orig.get_all('from', []))
    allto = email.utils.getaddresses(orig.get_all('to', []))
    allcc = email.utils.getaddresses(orig.get_all('cc', []))

    targets = [chunk[1] for chunk in allfrom+allto+allcc]

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

    merge_id = git_get_merge_id(repo, commit_id)

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

    msg['Message-Id'] = make_msgid('pr-tracker-bot')
    msg['Date'] = email.utils.formatdate(localtime=True)

    # Set to and cc
    if 'onlyto' in config:
        msg['To'] = config['onlyto']
        # Override targets
        targets = [config['onlyto']]
    else:
        msg['To'] = format_addrs(allfrom)
        msg['Cc'] = format_addrs(allto+allcc)

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

    if not dryrun:
        try:
            c.execute('INSERT INTO thanks VALUES(?, ?, ?, ?)',
                      (commit_id, refname, msg['Message-Id'],
                       time.strftime('%Y-%m-%dT%H:%M:%S%z', time.gmtime())))
        except sqlite3.IntegrityError:
            logger.debug('Got integrity-error for %s/%s', refname, commit_id)

    return msg['Message-Id']


def send_thanks(repo, pitopdir, cmdconfig, nomail, dryrun):
    if dryrun:
        nomail = True

    git_heads = git_get_repo_heads(repo)
    if not git_heads:
        logger.critical('Could not get the latest ref in %s', repo)
        sys.exit(1)

    try:
        with open(os.path.join(repo, '.prtracker.lock'), 'w') as lockfh:
            lockf(lockfh, LOCK_EX | LOCK_NB)
    except IOError:
        logger.debug('Could not obtain an exclusive lock, assuming another process is running.')
        return

    # Do we have a thanks.db there yet?
    dbpath = os.path.join(repo, 'thanks.db')
    db_exists = os.path.isfile(dbpath)
    dbconn = sqlite3.connect(dbpath, sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    c = dbconn.cursor()

    if not db_exists:
        db_init_thanks_sqlite_db(c)
        db_save_repo_heads(c, git_heads)
        # Exit early
        dbconn.commit()
        return

    config = get_config_from_repo(repo, pitopdir, cmdconfig)

    tycount = 0
    for pirepo, settings in config.items():
        projpath, maxshard = get_pirepo_dir(pirepo, pitopdir)
        logger.info('Grabbing PR commits from %s', projpath)
        logger.debug('config follows')
        logger.debug(settings)

        cutoffdays = 30
        try:
            cutoffdays = int(settings['cutoffdays'])
        except (IndexError, ValueError):
            pass

        prs = get_all_prs(projpath, cutoffdays=cutoffdays)
        logger.debug('Found %s PRs in %s', len(prs), projpath)
        un_prs = get_unprocessed_prs(c, prs, cutoffdays=cutoffdays)
        logger.debug('Of them, %s not already thanked for', len(un_prs))
        for pi_shard, pr_commit_id, msg_commit_id in un_prs:
            logger.debug('Checking %s', pr_commit_id)
            # Is this pr_commit_id in the repo?
            lines = git_get_command_lines(repo, ['branch', '--contains', pr_commit_id])
            if len(lines):
                refname = 'refs/heads/%s' % lines[0].split()[-1]
                logger.debug('Found %s in %s', pr_commit_id, refname)
                sent_msgid = thank_for_pr(c, repo, refname, pr_commit_id, projpath, pi_shard,
                                          msg_commit_id, settings, dryrun, nomail)
                if sent_msgid and not dryrun:
                    tycount += 1
                    dbconn.commit()

    if not dryrun:
        db_save_repo_heads(c, git_heads)
        dbconn.commit()

    if tycount:
        logger.info('Sent %s thank-you notes.', tycount)
    else:
        logger.info('No new thank-yous to send.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('-p', '--parse-requests', dest='pirepo', default=None,
                        help='Check the Public Inbox ML repository for any new pull requests.')
    parser.add_argument('-m', '--mail-thankyous', dest='tyrepo', default=None,
                        help='Check the repository and thank for any matching pulled PRs.')
    parser.add_argument('-t', '--pirepos-topdir', dest='topdir', default=None,
                        help='Toplevel path where all public-inbox repos are (optional)')
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
    parser.add_argument('-v', '--verbose', action='store_true', default=False,
                        help='Output extra debugging information')

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

    if cmdargs.pirepo is not None:
        parse_pull_requests(cmdargs.pirepo, cmdargs.topdir, cmdargs.dryrun)

    if cmdargs.tyrepo is not None:
        send_thanks(cmdargs.tyrepo, cmdargs.topdir, cmdargs.config, cmdargs.nomail, cmdargs.dryrun)
