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
    re.compile(r'^\[GIT PULL', re.IGNORECASE),
    re.compile(r'^\[PULL REQUEST', re.IGNORECASE),
    re.compile(r'^\[PLEASE PULL', re.IGNORECASE),
    re.compile(r'^\[PULL', re.IGNORECASE),
]

# I like these
PULL_BODIES_WITH_COMMIT_ID = [
    re.compile(r'^\s*for you to fetch changes up to ([0-9a-f]{5,40}):', re.MULTILINE | re.IGNORECASE),
]

# I don't like these
PULL_BODIES_WITHOUT_COMMIT_ID = [
    re.compile(r'^\s*(\S+://\S+)\s(tags/\S+)$', re.MULTILINE | re.IGNORECASE),
]

logger = logging.getLogger(__name__)


def set_db_meta(c, at_ref):
    c.execute('DELETE FROM meta')
    c.execute('''INSERT INTO meta VALUES(?, ?)''', (DB_VERSION, at_ref))


def get_db_meta(c):
    return c.execute('SELECT * FROM meta').fetchone()


def init_pr_sqlite_db(c, at_ref):
    logger.info('Initializing new sqlite3 db with metadata version %s', DB_VERSION)

    c.execute('''
        CREATE TABLE meta (
            version INTEGER,
            at_ref TEXT
        )''')
    set_db_meta(c, at_ref)
    c.execute('''
        CREATE TABLE prs (
            msg_commit_id TEXT UNIQUE,
            msg_id TEXT UNIQUE,
            subject TEXT,
            pr_commit_id TEXT,
            received TIMESTAMP
        )''')


def init_thanks_sqlite_db(c, at_ref):
    logger.info('Initializing new sqlite3 db with metadata version %s', DB_VERSION)
    c.execute('''
        CREATE TABLE meta (
            version INTEGER,
            at_ref TEXT
        )''')
    set_db_meta(c, at_ref)
    c.execute('''
        CREATE TABLE thanks (
            pr_commit_id TEXT, --- pi.prs.pr_commit_id
            refname TEXT,
            sent_msgid TEXT,
            sent TIMESTAMP
        )''')


def git_run_command(gitdir, args, splitlines=True):
    args = ['git', '--no-pager', '--git-dir', gitdir] + args

    logger.debug('Running %s' % ' '.join(args))

    (output, error) = subprocess.Popen(args, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE).communicate()
    output = output.strip()

    if len(error.strip()):
        logger.critical(error.decode('utf-8'))
        return None

    if not splitlines:
        return output

    lines = list()
    logger.debug('Processing the output...')
    for line in output.split(b'\n'):
        if line == b'':
            continue
        lines.append(line)

    return lines


def get_message(pirepo, commit_id):
    full_email = git_run_command(pirepo, ['show', '%s:m' % commit_id], splitlines=False)
    msg = email.message_from_string(full_email)
    return msg


def get_commit_id_from_repo_ref(repo, ref):
    logger.debug('Attempting to get commit_id from: %s, %s', repo, ref)
    lines = git_run_command('', ['ls-remote', repo, '%s^{}' % ref])
    if not lines:
        # Oh well, we tried
        return None

    commit_id = lines[0].split()[0]
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
                    pr_commit_id = get_commit_id_from_repo_ref(repo, ref)
                    break

    if pr_commit_id is None:
        return False

    logger.debug('pr_commit_id=%s', pr_commit_id)

    msg_id = msg['Message-Id']
    rechdr = msg.get('Received')
    received = time.mktime(email.utils.parsedate(rechdr.split(';')[-1].strip()))
    logger.debug('received=%s', received)
    subject = msg['Subject']
    logger.debug('Found a new PR: %s', subject)
    logger.debug('     commit_id: %s', pr_commit_id)
    try:
        c.execute('INSERT INTO prs VALUES(?, ?, ?, ?, ?)',
                  (msg_commit_id, msg_id, subject, pr_commit_id, received))
        return True
    except sqlite3.IntegrityError:
        logger.debug('Got integrity-error for %s', msg_id)

    return False


def parse_pull_requests(pirepo):
    # Is it a git repo? It should always have a
    # refs/heads/master file (all Public-Inbox repos have a master ref)
    if not os.path.isfile(os.path.join(pirepo, 'refs/heads/master')):
        logger.critical('Probably not a git repo: %s', pirepo)
        sys.exit(1)

    lines = git_run_command(pirepo, ['show-ref', 'refs/heads/master'])
    if not lines:
        logger.critical('Could not get the latest ref in %s', pirepo)
        sys.exit(1)
    repo_at_ref = None
    for line in lines:
        if line.find('refs/heads/master') > 0:
            repo_at_ref = line.split()[0]
    if repo_at_ref is None:
        logger.critical('Could not get the latest ref in %s', pirepo)

    # Do we have a prs.db there yet?
    dbpath = os.path.join(pirepo, 'prs.db')
    if not os.path.isfile(dbpath):
        # initialize it once we get the latest ref
        dbconn = sqlite3.connect(dbpath, sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
        c = dbconn.cursor()
        init_pr_sqlite_db(c, repo_at_ref)
    else:
        dbconn = sqlite3.connect(dbpath, sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
        c = dbconn.cursor()

    db_ver, db_at_ref = get_db_meta(c)
    if repo_at_ref == db_at_ref:
        logger.info('Nothing new to process, bye.')
        sys.exit(0)

    logger.info('Getting new refs since %s', db_at_ref)
    lines = git_run_command(pirepo, ['rev-list', '--pretty=oneline', '%s..HEAD' % db_at_ref])
    logger.info('Found %s new messages since last run', len(lines))
    new_prs = 0
    for line in lines:
        (commit_id, subject) = line.split(' ', 1)
        logger.debug('commit_id=%s, subject=%s', commit_id, subject)
        for subject_re in PULL_SUBJECTS:
            if subject_re.match(subject):
                logger.debug('Potential match: "%s"', subject)
                msg = get_message(pirepo, commit_id)
                if record_pr_data(commit_id, msg, c):
                    new_prs += 1
                    logger.info('Started tracking: %s', msg['subject'])

    if not new_prs:
        logger.info('No new PRs found.')

    set_db_meta(c, repo_at_ref)
    dbconn.commit()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('-p', '--parse-requests', dest='pr', default=None,
                        help='Check the Public Inbox ML repository for any new pull requests.')
    parser.add_argument('-l', '--logfile', default=None,
                        help='Log file for messages during quiet operation')
    parser.add_argument('-d', dest='dryrun', action='store_true', default=False,
                        help='Do not mail anything, just do a dry run.')
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Only output errors to the stdout')

    args = parser.parse_args()

    logger.setLevel(logging.DEBUG)

    if args.logfile:
        ch = logging.FileHandler(args.logfile)
        formatter = logging.Formatter(
            '{%(funcName)s:%(lineno)s} %(message)s')
        ch.setFormatter(formatter)

        ch.setLevel(logging.DEBUG)
        logger.addHandler(ch)

    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    ch.setFormatter(formatter)

    if args.quiet:
        ch.setLevel(logging.CRITICAL)
    else:
        ch.setLevel(logging.INFO)

    logger.addHandler(ch)

    if args.pr is not None:
        parse_pull_requests(args.pr)
