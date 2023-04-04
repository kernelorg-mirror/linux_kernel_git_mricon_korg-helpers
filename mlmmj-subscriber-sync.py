#!/usr/bin/env python3
# Grabs a file full of subscribers and makes sure that
# all new ones are subscribed and all the ones that are
# gone are unsubscribed
#
# The URL of the file to download should be in
# /var/spool/mlmmj/list.name.foo/.external-subscribers-url

__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import argparse
import logging
import requests
import os
import sys
import subprocess

from typing import Set, List, Optional, Tuple
from fcntl import lockf, LOCK_EX, LOCK_NB, LOCK_UN


__APPNAME__ = 'mlmmj-subscriber-sync'
__VERSION__ = '0.1'

MLMMJ_SUB = '/usr/bin/mlmmj-sub'
MLMMJ_UNSUB = '/usr/bin/mlmmj-unsub'
logger = logging.getLogger(__APPNAME__)


def _run_command(args: List[str], stdin: Optional[bytes] = None) -> Tuple[int, bytes, bytes]:
    logger.info('Running %s' % ' '.join(args))

    sp = subprocess.Popen(args, stdout=subprocess.PIPE,
                          stdin=subprocess.PIPE,
                          stderr=subprocess.PIPE)

    (output, error) = sp.communicate(input=stdin)

    return sp.returncode, output, error


def get_remote_subscribers(mldir: str) -> Set[str]:
    urlfile = os.path.join(mldir, '.external-subscribers-url')
    if not os.path.exists(urlfile):
        raise FileNotFoundError('No .external-subscribers-url defined for %s', mldir)
    with open(urlfile, 'r') as fh:
        url = fh.read().strip()
    rses = requests.session()
    headers = {'User-Agent': f'{__APPNAME__}/{__VERSION__}'}
    rses.headers.update(headers)
    resp = rses.get(url)
    if resp.status_code != 200:
        logger.info('Unable to retrieve %s: %s', url, resp.text)
        raise FileNotFoundError
    resp.raise_for_status()
    data = resp.text.strip()
    subs = set(data.splitlines())
    logger.info('Loaded %s remote subscribers from %s', len(subs), url)
    return subs


def get_last_subscribers(mldir: str) -> Set[str]:
    lastfile = os.path.join(mldir, '.external-subscribers-last')
    if not os.path.exists(lastfile):
        logger.info('No lastfile in %s', mldir)
        return set()

    with open(lastfile, 'r') as fh:
        subs = set(fh.read().strip().splitlines())

    logger.info('Loaded %s last subscribers from %s', len(subs), lastfile)
    return subs


def store_last(subs: Set[str], mldir: str):
    lastfile = os.path.join(mldir, '.external-subscribers-last')
    logger.info('Storing %s with %s entries', lastfile, len(subs))
    with open(lastfile, 'w') as fh:
        fh.write('\n'.join(sorted(list(subs))) + '\n')


def mlmmj_sub(tosub: Set[str], mldir: str) -> None:
    for addr in tosub:
        logger.info('Subscribing %s', addr)
        args = [MLMMJ_SUB, '-L', mldir, '-f', '-q', '-s', '-a', addr]
        ecode, out, err = _run_command(args)
        if ecode > 0:
            logger.critical('Error: %s, %s', out.decode(), err.decode())
            raise RuntimeError('Unable to run mlmmj_sub')


def mlmmj_unsub(tounsub: Set[str], mldir: str) -> None:
    for addr in tounsub:
        logger.info('Unsubscribing %s', addr)
        args = [MLMMJ_UNSUB, '-L', mldir, '-q', '-s', '-a', addr]
        ecode, out, err = _run_command(args)
        if ecode > 0:
            logger.critical('Error: %s, %s', out.decode(), err.decode())
            raise RuntimeError('Unable to run mlmmj_unsub')


def subscriber_sync(cmdargs: argparse.Namespace) -> None:
    # List the spool dir
    for entry in os.listdir(cmdargs.mlmmj_spool_dir):
        mldir = os.path.join(cmdargs.mlmmj_spool_dir, entry)
        try:
            remote = get_remote_subscribers(mldir)
        except FileNotFoundError:
            continue
        ml = entry
        logger.info('Processing %s', ml)
        # if remote is empty, this is sus -- something probably went wrong
        if not len(remote):
            logger.info('Remote is empty, this is sus')
            continue
        # Lock it, so there are no clashes
        lf = os.path.join(mldir, '.mlmmj-subscriber-sync.lock')
        lfh = open(lf, 'w')
        try:
            lockf(lfh, LOCK_EX | LOCK_NB)
        except IOError:
            logger.info('Unable to lock %s, assuming it is busy', ml)
            continue
        local = get_last_subscribers(mldir)
        if local == remote:
            logger.info('No change for %s', ml)
            continue
        try:
            mlmmj_sub(remote.difference(local), mldir)
            mlmmj_unsub(local.difference(remote), mldir)
            store_last(remote, mldir)
        except RuntimeError:
            logger.critical('Unable to run mlmmj commands, exiting in panic')
            sys.exit(1)
        lockf(lfh, LOCK_UN)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-q', '--quiet', action='store_true',
                        default=False, help='Print critical output only')
    parser.add_argument('--mlmmj-spool-dir',
                        default='/var/spool/mlmmj',
                        help='Where mlmmj lists are, if not in /var/spool/mlmmj')
    _args = parser.parse_args()

    logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    ch.setFormatter(formatter)
    if _args.quiet:
        ch.setLevel(logging.CRITICAL)
    else:
        ch.setLevel(logging.INFO)
    logger.addHandler(ch)

    subscriber_sync(_args)
