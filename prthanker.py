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
import time
import subprocess
import sqlite3
import logging
import re

DB_VERSION = 1

# Case doesn't matter
PULL_SUBJECTS = [
    re.compile(r'^\[GIT', re.IGNORECASE),
    re.compile(r'^\[PULL', re.IGNORECASE),
    re.compile(r'^\[PLEASE PULL', re.IGNORECASE),
]

# I like these
PULL_BODIES_WITH_COMMIT_ID = [
    re.compile(r'^\s*for you to fetch changes up to ([0-9a-f]{5,40}):', re.MULTILINE | re.IGNORECASE),
]

# I don't like these
PULL_BODIES_WITHOUT_COMMIT_ID = [
    re.compile(r'^\s*((?:git|https?)://[\w/._-]+)\s([\w/._-]+)$', re.MULTILINE | re.IGNORECASE),
]

logger = logging.getLogger('prthanker')


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


def db_init_pr_sqlite_db(c):
    logger.info('Initializing new sqlite3 db with metadata version %s', DB_VERSION)
    db_init_common_sqlite_db(c)

    c.execute('''
        CREATE TABLE prs (
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
        CREATE TABLE thanks (
            pr_commit_id TEXT, --- pi.prs.pr_commit_id
            refname TEXT,
            sent_msgid TEXT,
            sent DATE
        )''')


def git_run_command(gitdir, args, splitlines=True):
    args = ['git', '--no-pager', '--git-dir', gitdir] + args

    logger.debug('Running %s' % ' '.join(args))

    (output, error) = subprocess.Popen(args, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE).communicate()
    output = output.strip().decode('utf-8', errors='replace')

    if len(error.strip()):
        logger.critical(error.decode('utf-8', errors='replace'))
        return None

    if not splitlines:
        return output

    lines = list()
    logger.debug('Processing the output...')
    for line in output.split('\n'):
        if line == '':
            continue
        lines.append(line)

    return lines


def git_get_repo_heads(gitdir):
    refs = list()
    lines = git_run_command(gitdir, ['show-ref', '--heads'])
    if lines is not None:
        for line in lines:
            (commit_id, refname) = line.split()
            refs.append((refname, commit_id))
    return refs


def git_get_message_from_pi(pirepo, commit_id):
    full_email = git_run_command(pirepo, ['show', '%s:m' % commit_id], splitlines=False)
    msg = email.message_from_string(full_email.encode('utf-8'))
    return msg


def git_get_commit_id_from_repo_ref(repo, ref):
    logger.debug('getting commit-id from: %s %s', repo, ref)
    lines = git_run_command('', ['ls-remote', repo, '%s^{}' % ref])
    if not lines:
        # Oh well, we tried
        logger.debug('failed, ignoring pull request')
        return None

    commit_id = lines[0].split()[0]
    logger.debug('success: %s', commit_id)
    return commit_id


def record_pr_data(msg_commit_id, msg, c):
    pr_commit_id = None
    for part in msg.walk():
        body = str(part.get_payload())
        for cid_re in PULL_BODIES_WITH_COMMIT_ID:
            matches = cid_re.search(body)
            if matches:
                (pr_commit_id,) = matches.groups(0)
                break

    if pr_commit_id is None:
        # Okay, try the one we like less :(
        for part in msg.walk():
            body = str(part.get_payload())
            for reporef_re in PULL_BODIES_WITHOUT_COMMIT_ID:
                matches = reporef_re.search(body)
                if matches:
                    (repo, ref) = matches.groups(0)
                    pr_commit_id = git_get_commit_id_from_repo_ref(repo, ref)
                    break

    if pr_commit_id is None:
        return False

    logger.debug('pr_commit_id=%s', pr_commit_id)

    msg_id = msg['Message-Id']
    rechdr = msg.get('Received')
    received = time.strftime('%Y-%m-%dT%H:%M:%S%z',
                             email.utils.parsedate(rechdr.split(';')[-1].strip()))
    logger.debug('received=%s', received)
    subject = msg['Subject'].replace('\n', ' ')
    logger.debug('Found a new PR: %s', subject)
    logger.debug('     commit_id: %s', pr_commit_id)
    try:
        c.execute('INSERT INTO prs VALUES(?, ?, ?, ?, ?)',
                  (msg_commit_id, msg_id, subject, pr_commit_id, received))
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

        rev_range = '%s..%s' % (db_commit_id, git_commit_id)
        lines = git_run_command(gitdir, ['rev-list', '--pretty=oneline',
                                         '--reverse', rev_range, refname])
        if lines:
            newrevs[refname] = lines

    return newrevs


def parse_pull_requests(pirepo, dryrun):
    # Is it a git repo? It should always have a
    # refs/heads/master file (all Public-Inbox repos have a master ref)
    if not os.path.isfile(os.path.join(pirepo, 'refs/heads/master')):
        logger.critical('Probably not a git repo: %s', pirepo)
        sys.exit(1)

    git_heads = git_get_repo_heads(pirepo)
    if not git_heads:
        logger.critical('Could not get the latest ref in %s', pirepo)
        sys.exit(1)

    # Do we have a prs.db there yet?
    dbpath = os.path.join(pirepo, 'prs.db')
    db_exists = os.path.isfile(dbpath)
    dbconn = sqlite3.connect(dbpath, sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    c = dbconn.cursor()

    if not db_exists:
        # initialize it once we get the latest ref
        db_init_pr_sqlite_db(c)
        db_save_repo_heads(c, git_heads)
        # Exit early
        dbconn.commit()
        sys.exit(0)

    db_heads = db_get_repo_heads(c)

    newrevs = git_get_new_revs(pirepo, db_heads, git_heads)
    if not newrevs:
        logger.info('No new revs since last run.')
        sys.exit(0)

    # We are only interested in refs/heads/master for public-inbox
    msgrevs = newrevs['refs/heads/master']
    logger.info('Found %s new messages since last run', len(msgrevs))
    new_prs = 0
    for revline in msgrevs:
        (commit_id, subject) = revline.split(' ', 1)
        logger.debug('commit_id=%s, subject=%s', commit_id, subject)
        for subject_re in PULL_SUBJECTS:
            if subject_re.match(subject):
                logger.debug('Potential match: "%s"', subject)
                msg = git_get_message_from_pi(pirepo, commit_id)
                if record_pr_data(commit_id, msg, c):
                    new_prs += 1
                    logger.info('Started tracking: %s', subject)

    if not new_prs:
        logger.info('No new PRs found.')

    db_save_repo_heads(c, git_heads)

    if not dryrun:
        dbconn.commit()


def get_config_from_repo(repo):
    config = dict()
    args = ['config', '-z', '--local', '--get-regexp', 'prthanker\..*']
    out = git_run_command(repo, args, splitlines=False)
    if not out:
        return config

    for line in out.split('\x00'):
        if not line:
            continue
        key, value = line.split('\n', 1)
        config[key] = value

    return config


def add_config_from_cmdargs(cmdconfig, config):
    for entry in cmdconfig:
        key, value = entry.split('=', 1)
        config[key] = value

    return config


def get_all_thanked_prs(c, refname, cutoffdays=30):
    rows = c.execute('''
                     SELECT pr_commit_id
                       FROM thanks
                      WHERE refname = ?
                        AND sent > datetime('now', ?)
                     ''', (refname, '-%d day' % cutoffdays,)).fetchall()
    sent_prs = set()
    for row in rows:
        sent_prs.update(row[0])

    return sent_prs


def get_all_prs(pirepo, cutoffdays=30):
    dbpath = os.path.join(pirepo, 'prs.db')
    if not os.path.isfile(dbpath):
        # The DB does not exist, and we don't create it
        # from this location.
        logger.info('No PRs database found, assuming it did not run yet.')
        return list()

    prdbconn = sqlite3.connect(dbpath, sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    prc = prdbconn.cursor()
    rows = prc.execute('''
                       SELECT pr_commit_id, msg_commit_id
                         FROM prs 
                        WHERE received >= datetime('now', ?)
                       ''', ('-%d day' % cutoffdays,)).fetchall()
    return rows


def get_all_new_prs(c, refname, prs, cutoffdays=30):
    sent_prs = get_all_thanked_prs(c, refname, cutoffdays)
    new_prs = list()
    for row in prs:
        if row[0] not in sent_prs:
            new_prs.append(row)
    return new_prs


def send_thanks(repo, cmdconfig, dryrun):
    git_heads = git_get_repo_heads(repo)
    if not git_heads:
        logger.critical('Could not get the latest ref in %s', repo)
        sys.exit(1)

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
        sys.exit(0)

    config = get_config_from_repo(repo)
    if len(cmdconfig):
        config = add_config_from_cmdargs(cmdconfig, config)

    logger.debug('config follows')
    logger.debug(config)
    db_heads = db_get_repo_heads(c)

    newrevs = git_get_new_revs(repo, db_heads, git_heads)
    if not newrevs:
        logger.info('No new revs since last run.')
        sys.exit(0)

    prs = get_all_prs(config['prthanker.pirepo'])
    for refname, revs in newrevs.items():
        srevs = set(revs)
        for pr_commit_id, msg_commit_id in get_all_new_prs(c, refname, prs):
            if pr_commit_id in srevs:
                logger.debug('Found a matching PR commit: %s', pr_commit_id)
                print('bazonga')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('-p', '--parse-requests', dest='pirepo', default=None,
                        help='Check the Public Inbox ML repository for any new pull requests.')
    parser.add_argument('-t', '--mail-thankyous', dest='tyrepo', default=None,
                        help='Check the repository and thank for any matching pulled PRs.')
    parser.add_argument('-c', '--config', dest='config', nargs='+', default=list(),
                        help='Use these config values instead of looking in the repo')
    parser.add_argument('-l', '--logfile', default=None,
                        help='Log file for messages during quiet operation')
    parser.add_argument('-d', dest='dryrun', action='store_true', default=False,
                        help='Do not mail or store anything, just do a dry run.')
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Only output errors to the stdout')

    cmdargs = parser.parse_args()

    logger.setLevel(logging.DEBUG)

    if cmdargs.pirepo and cmdargs.tyrepo:
        parser.error('Cannot use both -p and -t at the same time')

    if cmdargs.logfile:
        ch = logging.FileHandler(cmdargs.logfile)
        formatter = logging.Formatter(
            '{%(funcName)s:%(lineno)s} %(message)s')
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

    if cmdargs.pirepo is not None:
        parse_pull_requests(cmdargs.pirepo, cmdargs.dryrun)

    if cmdargs.tyrepo is not None:
        send_thanks(cmdargs.tyrepo, cmdargs.config, cmdargs.dryrun)
