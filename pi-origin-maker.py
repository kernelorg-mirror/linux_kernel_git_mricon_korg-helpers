#!/usr/bin/env python3
# -*- coding: utf-8 -*-
__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import os
import sys
import argparse
import logging
import subprocess

from fcntl import lockf, LOCK_EX, LOCK_UN
from typing import Optional, Tuple, Dict
from email.utils import formatdate
from collections import OrderedDict

PI_HEAD = 'refs/meta/origins'
logger = logging.getLogger(__name__)

DEFAULT_NAME = 'PI Origin Maker'
DEFAULT_ADDR = 'devnull@kernel.org'
DEFAULT_SUBJ = 'Origin commit'


def git_run_command(gitdir: str, args: list, stdin: Optional[bytes] = None,
                    env: Optional[Dict] = None) -> Tuple[int, bytes, bytes]:
    if not env:
        env = dict()
    if gitdir:
        env['GIT_DIR'] = gitdir
    args = ['git', '--no-pager'] + args
    logger.debug('Running %s', ' '.join(args))
    pp = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    (output, error) = pp.communicate(input=stdin)

    return pp.returncode, output, error


def check_valid_repo(repo: str) -> None:
    # check that it exists and has 'objects' and 'refs'
    if not os.path.isdir(repo):
        raise FileNotFoundError(f'Path does not exist: {repo}')
    musts = {'objects', 'refs'}
    for must in musts:
        if not os.path.exists(os.path.join(repo, must)):
            raise FileNotFoundError(f'Path is not a valid bare git repository: {repo}')


def git_write_commit(repo: str, env: dict, c_msg: str, body: bytes, dest: str = 'i') -> None:
    # We use git porcelain commands here. We could use pygit2, but this would pull in a fairly
    # large external lib for what is effectively 4 commands that we need to run.
    # Lock the repository
    try:
        # The lock shouldn't be held open for very long, so try without a timeout
        lockfh = open(os.path.join(repo, 'ezpi.lock'), 'w')
        lockf(lockfh, LOCK_EX)
    except IOError:
        raise RuntimeError('Could not obtain an exclusive lock')

    # Create a blob first
    ee, out, err = git_run_command(repo, ['hash-object', '-w', '--stdin'], stdin=body)
    if ee > 0:
        raise RuntimeError(f'Could not create a blob in {repo}: {err.decode()}')
    blob = out.strip(b'\n')
    # Create a tree object now
    treeline = b'100644 blob ' + blob + b'\t' + dest.encode()
    # Now mktree
    ee, out, err = git_run_command(repo, ['mktree'], stdin=treeline)
    if ee > 0:
        raise RuntimeError(f'Could not mktree in {repo}: {err.decode()}')
    tree = out.decode().strip()
    # Find out if we are the first commit or not
    ee, out, err = git_run_command(repo, ['rev-parse', f'{PI_HEAD}^0'])
    if ee > 0:
        args = ['commit-tree', '-m', c_msg, tree]
    else:
        args = ['commit-tree', '-p', PI_HEAD, '-m', c_msg, tree]
    # Commit the tree
    ee, out, err = git_run_command(repo, args, env=env)
    if ee > 0:
        raise RuntimeError(f'Could not commit-tree in {repo}: {err.decode()}')
    # Finally, update the ref
    commit = out.decode().strip()
    ee, out, err = git_run_command(repo, ['update-ref', PI_HEAD, commit])
    if ee > 0:
        raise RuntimeError(f'Could not update-ref in {repo}: {err.decode()}')
    lockf(lockfh, LOCK_UN)


def read_config(cfgfile):
    from configparser import ConfigParser
    if not os.path.exists(cfgfile):
        sys.stderr.write('ERROR: config file %s does not exist' % cfgfile)
        sys.exit(1)
    # We don't support duplicates right now
    piconfig = ConfigParser(strict=False)
    piconfig.read(cfgfile)

    return piconfig


def get_pi_repos(mainrepo: str) -> Dict:
    res = dict()
    at = 0
    latest_origins = None
    latest_repo = None
    while True:
        repo = os.path.join(mainrepo, 'git', '%d.git' % at)
        try:
            check_valid_repo(repo)
            logger.debug('Checking current origins in %s', repo)
            ec, out, err = git_run_command(repo, ['show', f'{PI_HEAD}:i'])
            # If it's blank, then we force it to be written
            if not len(out):
                res[repo] = ''
            else:
                latest_repo = repo
                latest_origins = out
            at += 1
        except FileNotFoundError:
            break
    if latest_origins is None or latest_repo is None:
        logger.debug('Did not find any valid pi repos in %s', mainrepo)
        return res

    res[latest_repo] = latest_origins.decode()
    return res


def make_origins(config, cmdargs):
    for section in config.sections():
        # We want named sections
        if section.find(' ') < 0:
            continue
        origin = OrderedDict()
        origin['infourl'] = cmdargs.infourl
        origin['contact'] = cmdargs.contact
        mainrepo = config[section].get('mainrepo')
        if not mainrepo:
            mainrepo = config[section].get('inboxdir')
        if not mainrepo or not os.path.isdir(mainrepo):
            logger.info('%s: mainrepo=%s does not exist', section, mainrepo)
            continue
        mainrepo = mainrepo.rstrip('/')
        if cmdargs.repotop and os.path.dirname(mainrepo) != cmdargs.repotop:
            logger.info('Skipped %s: not directly in %s', mainrepo, cmdargs.repotop)
            continue

        pirepos = get_pi_repos(mainrepo)
        if not len(pirepos):
            logger.info('%s contains no public-inbox repos', mainrepo)
            continue
        origin['address'] = config[section].get('address')
        origin['listid'] = config[section].get('listid')
        if not origin['listid']:
            origin['listid'] = origin['address'].replace('@', '.')
        origin['newsgroup'] = config[section].get('newsgroup')
        if not origin['newsgroup']:
            origin['newsgroup'] = '.'.join(reversed(origin['listid'].split('.')))
        odata_new = '[publicinbox]\n'
        for opt, val in origin.items():
            odata_new += f'{opt}={val}\n'

        for pirepo, odata_old in pirepos.items():
            if odata_new != odata_old:
                logger.debug('Setting new origins for %s', pirepo)
                logger.debug(odata_new)
                env = {
                    'GIT_AUTHOR_NAME': DEFAULT_NAME,
                    'GIT_AUTHOR_EMAIL': DEFAULT_ADDR,
                    'GIT_AUTHOR_DATE': formatdate(),
                    'GIT_COMMITTER_NAME': DEFAULT_NAME,
                    'GIT_COMMITTER_EMAIL': DEFAULT_ADDR,
                    'GIT_COMMITTER_DATE': formatdate(),
                }
                try:
                    git_write_commit(pirepo, env, DEFAULT_SUBJ, odata_new.encode())
                    logger.info('Updated origins for %s', pirepo)
                except RuntimeError as ex:
                    logger.info('Could not update origins in %s: %s', pirepo, ex)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--pi-config-file', dest='cfgfile',
                        default='/etc/public-inbox/config',
                        help='Public-Inbox config file to use')
    parser.add_argument('-i', '--infourl', dest='infourl',
                        default='https://www.kernel.org/lore.html',
                        help='infourl value')
    parser.add_argument('-e', '--contact-email', dest='contact',
                        default='postmaster <postmaster@kernel.org>',
                        help='contact value')
    parser.add_argument('-t', '--repo-top', dest='repotop',
                        help='Only work on repos in this topdir')
    parser.add_argument('-q', '--quiet', dest='quiet', action='store_true', default=False,
                        help='Quiet operation (cron mode)')
    parser.add_argument('-d', '--debug', dest='debug', action='store_true', default=False,
                        help='Output debug information')
    parser.add_argument('-l', '--logfile', dest='logfile',
                        help='Record activity in this log file')

    _cmdargs = parser.parse_args()
    _config = read_config(_cmdargs.cfgfile)
    logger.setLevel(logging.DEBUG)

    if _cmdargs.logfile:
        ch = logging.FileHandler(_cmdargs.logfile)
        formatter = logging.Formatter(f'[%(asctime)s] %(message)s')
        ch.setFormatter(formatter)
        ch.setLevel(logging.INFO)
        logger.addHandler(ch)

    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    ch.setFormatter(formatter)
    if _cmdargs.quiet:
        ch.setLevel(logging.CRITICAL)
    elif _cmdargs.debug:
        ch.setLevel(logging.DEBUG)
    else:
        ch.setLevel(logging.INFO)
    logger.addHandler(ch)

    make_origins(_config, _cmdargs)
