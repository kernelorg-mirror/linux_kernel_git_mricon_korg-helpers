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
import hashlib

from typing import Set, List, Optional, Tuple
from fcntl import lockf, LOCK_EX, LOCK_NB, LOCK_UN


__APPNAME__ = 'mlmmj-subscriber-sync'
__VERSION__ = '0.1'

MLMMJ_SUB = '/usr/bin/mlmmj-sub'
MLMMJ_UNSUB = '/usr/bin/mlmmj-unsub'
logger = logging.getLogger(__APPNAME__)

CSUMS = dict()


def _run_command(args: List[str], stdin: Optional[bytes] = None) -> Tuple[int, bytes, bytes]:
    logger.info('Running %s' % ' '.join(args))

    sp = subprocess.Popen(args, stdout=subprocess.PIPE,
                          stdin=subprocess.PIPE,
                          stderr=subprocess.PIPE)

    (output, error) = sp.communicate(input=stdin)

    return sp.returncode, output, error


def get_git_subscribers(gitdir: str) -> Tuple[Optional[str], Set[str]]:
    # currently, we only support origin/master
    curdir = os.getcwd()
    os.chdir(gitdir)
    args = ['git', 'pull', 'origin', 'master', '-q']
    ecode, out, err = _run_command(args)
    os.chdir(curdir)
    if ecode > 0:
        logger.info('Was not able to pull %s', gitdir)
        logger.info(err.decode())
    subsfile = os.path.join(gitdir, 'subscribers')
    subs = set()
    with open(subsfile, 'r') as fh:
        while True:
            line = fh.readline()
            line = line.strip()
            if not line:
                break
            if line.startswith('#'):
                continue
            subs.add(line)

    logger.info('Loaded %s remote subscribers from %s', len(subs), subsfile)
    return None, subs


def get_remote_subscribers(mldir: str) -> Tuple[Optional[str], Set[str]]:
    global CSUMS
    # This overrides the presence of .external-subscribers-url
    gitdir = os.path.join(mldir, '.external-subscribers.git')
    if os.path.isdir(gitdir):
        return get_git_subscribers(gitdir)

    urlfile = os.path.join(mldir, '.external-subscribers-url')
    if not os.path.exists(urlfile):
        raise FileNotFoundError('No .external-subscribers-url defined for %s', mldir)
    with open(urlfile, 'r') as fh:
        url = fh.read().strip()
    rses = requests.session()
    headers = {'User-Agent': f'{__APPNAME__}/{__VERSION__}'}
    rses.headers.update(headers)
    # Grab the checksums file from that dir first
    rdir, lname = url.rsplit('/', maxsplit=1)
    ckpath = rdir + '/checksum.txt'
    if ckpath not in CSUMS:
        CSUMS[ckpath] = dict()
        resp = rses.get(ckpath)
        if resp.status_code == 200:
            # Oh, good, we have a checksum.txt
            data = resp.text.strip()
            for line in data.splitlines():
                csum, clname = line.split(maxsplit=1)
                CSUMS[ckpath][clname] = csum

    csum = None
    if lname in CSUMS[ckpath]:
        csum = CSUMS[ckpath][lname]
        lastcsfile = os.path.join(mldir, '.external-subscribers-last-csum')
        try:
            with open(lastcsfile, 'r') as fh:
                lastcsum = fh.read().strip()
            if lastcsum == csum:
                logger.debug('Remote checksum for %s is the same', lname)
                raise FileExistsError
        except FileNotFoundError:
            pass

    resp = rses.get(url)
    if resp.status_code != 200:
        logger.info('Unable to retrieve %s: %s', url, resp.text)
        raise FileNotFoundError
    resp.raise_for_status()
    bdata = resp.content
    if csum:
        # Hardcode checksum to sha256 for now
        mysum = hashlib.sha256(bdata).hexdigest()
        if mysum != csum:
            logger.debug('Checksum for %s did NOT match, ignoring', url)
            raise FileNotFoundError

    data = bdata.strip().decode()
    subs = set(data.splitlines())
    logger.info('Loaded %s remote subscribers from %s', len(subs), url)
    return csum, subs


def get_last_subscribers(mldir: str) -> Set[str]:
    lastfile = os.path.join(mldir, '.external-subscribers-last')
    if not os.path.exists(lastfile):
        logger.info('No lastfile in %s', mldir)
        return set()

    with open(lastfile, 'r') as fh:
        subs = set(fh.read().strip().splitlines())

    logger.info('Loaded %s last subscribers from %s', len(subs), lastfile)
    return subs


def store_last(subs: Set[str], csum: str, mldir: str):
    lastfile = os.path.join(mldir, '.external-subscribers-last')
    logger.info('Storing %s with %s entries', lastfile, len(subs))
    with open(lastfile, 'w') as fh:
        fh.write('\n'.join(sorted(list(subs))) + '\n')
    if not csum:
        return
    lastcsfile = os.path.join(mldir, '.external-subscribers-last-csum')
    logger.info('Storing %s with checksum=%s', lastcsfile, csum)
    with open(lastcsfile, 'w') as fh:
        fh.write(csum + '\n')


def mlmmj_subunsub(remote: Set[str], local: Set[str], mldir: str) -> None:
    # Make a local log
    ll = logging.getLogger(mldir)
    ll.setLevel(logging.DEBUG)
    lch = logging.FileHandler(os.path.join(mldir, '.external-subscribers.log'))
    lfmt = logging.Formatter('[%(asctime)s] %(message)s')
    lch.setFormatter(lfmt)
    lch.setLevel(logging.INFO)
    ll.addHandler(lch)
    for addr in remote.difference(local):
        logger.info('Subscribing %s', addr)
        args = [MLMMJ_SUB, '-L', mldir, '-f', '-q', '-s', '-a', addr]
        ecode, out, err = _run_command(args)
        if ecode > 0:
            logger.critical('Error: %s, %s', out.decode(), err.decode())
            raise RuntimeError('Unable to run mlmmj_sub')
        ll.info('subscribed %s', addr)
    for addr in local.difference(remote):
        logger.info('Unsubscribing %s', addr)
        args = [MLMMJ_UNSUB, '-L', mldir, '-q', '-s', '-a', addr]
        ecode, out, err = _run_command(args)
        if ecode > 0:
            logger.critical('Error: %s, %s', out.decode(), err.decode())
            raise RuntimeError('Unable to run mlmmj_unsub')
        ll.info('unsubscribed %s', addr)


def subscriber_sync(cmdargs: argparse.Namespace) -> None:
    # List the spool dir
    for entry in os.listdir(cmdargs.mlmmj_spool_dir):
        mldir = os.path.join(cmdargs.mlmmj_spool_dir, entry)
        try:
            csum, remote = get_remote_subscribers(mldir)
        except (FileNotFoundError, FileExistsError, TimeoutError):
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
            mlmmj_subunsub(remote, local, mldir)
            store_last(remote, csum, mldir)
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
    parser.add_argument('--sleep-upper', type=int, default=60,
                        help='Upper range for sleep, use 0 to disable')
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
    if _args.sleep_upper:
        import random
        import time
        sn = random.randrange(10, _args.sleep_upper, 5)
        logger.info('Sleeping %s seconds', sn)
        time.sleep(sn)

    subscriber_sync(_args)
