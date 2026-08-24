#!/usr/bin/env python3
# Copyright (C) 2014 by The Linux Foundation and contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

__author__ = 'Konstantin Ryabitsev'

import argparse
import fnmatch
import functools
import hashlib
import json
import logging
import os
import re
import sys
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, lockf
from io import BytesIO
from typing import TypedDict

import gpg
import gpg.constants.sig
from packaging.version import InvalidVersion, Version

VERSION = '0.1'
CHUNK_SIZE = 8192
HASH_ALGORITHMS = ('md5', 'sha1', 'sha256', 'sha512')

logger = logging.getLogger('dirsigner')


class FileStatus(TypedDict):
    """What we remember about a file between runs.

    If any of ctime, mtime, size or inode changed, the file is rehashed.
    """

    ctime: int
    mtime: int
    size: int
    inode: int
    hash: str


def _safe_version(s: str) -> Version:
    try:
        return Version(s)
    except InvalidVersion:
        return Version('0')


def parse_args() -> argparse.Namespace:
    global CHUNK_SIZE

    parser = argparse.ArgumentParser(
        description='Iterate recursively through a tree and create signed checksum files',
    )
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('-v', '--verbose', dest='verbose', action='store_true',
                        default=False,
                        help='Be verbose and tell us what you are doing')
    parser.add_argument('-t', '--tree', dest='tree', required=True,
                        help='The tree to recursively scan')
    parser.add_argument('-a', '--hash-algorithm', dest='hash_algorithm',
                        default='sha256',
                        help=('Hashing algorithm to use (sha256 default, '
                              'but can be %s)' % ', '.join(HASH_ALGORITHMS)))
    parser.add_argument('-g', '--gnupghome', dest='gnupghome',
                        default=None,
                        help='Set GNUPGHOME to this path (default: use ~/.gnupg)')
    parser.add_argument('-x', '--exclude', dest='excludes', action='append',
                        default=[],
                        help='Exclude files matching this glob (can be used multiple times)')
    parser.add_argument('-l', '--log-file', dest='logfile',
                        default=None,
                        help='Path to the log file (default: %(default)s)')
    parser.add_argument('-d', '--debug', dest='debug', action='store_true',
                        default=False,
                        help='Add debugging information to the log (default: %(default)s)')
    parser.add_argument('-s', '--version-sort', dest='vsort', action='store_true',
                        default=False,
                        help='Sort entries in the sums file by version (default: %(default)s)')
    parser.add_argument('-k', '--lock-file', dest='lockfile',
                        default='.dirsigner.lock',
                        help='Path to the lockfile (default: %(default)s)')
    parser.add_argument('-b', '--chunk-bytes', dest='chunksize', type=int,
                        default=CHUNK_SIZE,
                        help='Calculate checksum using chunks of this size (default: %(default)s)')

    opts = parser.parse_args()

    CHUNK_SIZE = opts.chunksize

    if opts.hash_algorithm not in HASH_ALGORITHMS:
        parser.error('Unsupported algorithm: %s' % opts.hash_algorithm)

    return opts


def get_version_info(filen: str) -> tuple[str, Version, str]:
    basen = os.path.basename(filen)
    # split by last dash
    nv = basen.rsplit('-', 1)
    if len(nv) != 2:
        return basen, _safe_version('0'), ''

    name, end = nv

    chunks = end.split('.')
    tail: list[str] = list()
    while len(chunks):
        if re.search(r'\D', chunks[-1]):
            tail.append(chunks.pop(-1))
            continue
        break

    if chunks:
        ver = _safe_version('.'.join(chunks))
    else:
        ver = _safe_version('0')

    return name, ver, '.'.join(tail)


def version_sort(x: str, y: str) -> int:
    namex, verx, tailx = get_version_info(x)
    namey, very, taily = get_version_info(y)
    if namex > namey:
        return 1
    if namex < namey:
        return -1
    if verx > very:
        return 1
    if verx < very:
        return -1
    if tailx > taily:
        return 1
    if tailx < taily:
        return -1
    return 0


def load_status(statusfile: str) -> dict[str, FileStatus]:
    if os.path.exists(statusfile):
        logger.debug('Reading status file from %s' % statusfile)
        fh = open(statusfile, 'r')
        contents = fh.read()
        fh.close()
        status: dict[str, FileStatus] = json.loads(contents)
        logger.debug('Status file contains %d entries' % len(status.keys()))
    else:
        logger.debug('No status file found, assuming initial run')
        status = {}

    return status


def save_status(statusfile: str, status: dict[str, FileStatus]) -> None:
    if not len(status):
        # Nothing left to remember, so don't leave an empty stub behind
        if os.path.exists(statusfile):
            logger.debug('Removing obsolete %s' % statusfile)
            os.unlink(statusfile)
        return

    logger.debug('Saving status into %s' % statusfile)
    logger.debug('Status contains %d entries' % len(status.keys()))
    contents = json.dumps(status)
    fh = open(statusfile, 'w')
    fh.write(contents)
    fh.close()


def get_file_hash(full_path: str, hash_algorithm: str) -> str:
    if hash_algorithm == 'md5':
        m = hashlib.md5()
    elif hash_algorithm == 'sha1':
        m = hashlib.sha1()
    elif hash_algorithm == 'sha512':
        m = hashlib.sha512()
    else:
        m = hashlib.sha256()

    logger.debug('Opening %s for reading using %d-byte chunks' % (full_path, CHUNK_SIZE))
    with open(full_path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b''):
            m.update(chunk)

    return m.hexdigest()


def write_sums_file(root: str, found_files: list[str], status: dict[str, FileStatus],
                    hash_algorithm: str, vsort: bool = False) -> None:
    # Clean out any stale sums files
    for supported_algorithm in HASH_ALGORITHMS:
        sums_file = os.path.join(root, supported_algorithm + 'sums.asc')
        if os.path.exists(sums_file):
            os.unlink(sums_file)

    if not len(found_files):
        return

    tfh = BytesIO()
    if vsort:
        logger.info('Version-sorting %s', root)
        found_files.sort(key=functools.cmp_to_key(version_sort))
    for filename in found_files:
        checksum = status[filename]['hash']
        tfh.write(('%s  %s\n' % (checksum, filename)).encode('utf-8'))

    tfh.seek(0)

    sums_file = os.path.join(root, hash_algorithm + 'sums.asc')
    cfh = open(sums_file, 'wb')

    ctx = gpg.Context(armor=True)
    ctx.sign(tfh, cfh, mode=gpg.constants.sig.mode.CLEAR)

    tfh.close()
    cfh.close()
    logger.info('Wrote %s' % sums_file)


def sign_tree(tree: str, excludes: list[str], hash_algorithm: str, vsort: bool = False) -> None:
    logger.info('Recursively scanning %s' % tree)

    # Make sure '*sums.asc' and .dirsigner.* are always in excludes
    if '*sums.asc' not in excludes:
        excludes.append('*sums.asc')
    if '.dirsigner.*' not in excludes:
        excludes.append('.dirsigner.*')

    logger.debug('Exclude list: %s' % ' '.join(excludes))

    for root, dirs, files in os.walk(tree, topdown=True):
        logger.debug('Entered %s' % root)

        statusfile = os.path.join(root, '.dirsigner.status.js')
        status = load_status(statusfile)

        found_files: list[str] = []
        dir_changed = False

        for name in files:
            full_path = os.path.join(root, name)
            if os.path.islink(full_path):
                logger.debug('Ignoring symlink %s' % name)
                continue

            # Does it match excludes?
            excluded = False
            for exclude in excludes:
                if fnmatch.fnmatch(name, exclude) or fnmatch.fnmatch(full_path, exclude):
                    logger.debug('Excluding %s because it matches %s' % (full_path, exclude))
                    excluded = True
                    break
            if excluded:
                continue

            # Get stats on the file and compare to what we have in the status
            try:
                fstat = os.lstat(full_path)
            except OSError:
                logger.debug('Was not able to stat %s, ignoring' % full_path)
                continue

            found_files.append(name)
            # Whole seconds only, which is what the tuple form of stat used to give us
            ctime = int(fstat.st_ctime)
            mtime = int(fstat.st_mtime)
            size = fstat.st_size
            inode = fstat.st_ino

            # Is this path in status?
            changed = False
            if name in status:
                # Do ctime, mtime and size match?
                # A bit verbose, but lets us log the exact reason
                if status[name]['ctime'] != ctime:
                    logger.info('File %s changed ctime' % name)
                    changed = True
                elif status[name]['mtime'] != mtime:
                    logger.info('File %s changed mtime' % name)
                    changed = True
                elif status[name]['size'] != size:
                    logger.info('File %s changed size' % name)
                    changed = True
                elif status[name]['inode'] != inode:
                    logger.info('File %s changed inode' % name)
                    changed = True
            else:
                logger.debug('No previous record of %s' % name)
                changed = True

            if changed:
                file_hash = get_file_hash(full_path, hash_algorithm)

                dir_changed = True
                logger.info('%s hash of %s is: %s' % (hash_algorithm, full_path, file_hash))

                status[name] = {
                    'ctime': ctime,
                    'mtime': mtime,
                    'size': size,
                    'inode': inode,
                    'hash': file_hash,
                    }

        # Weed out status entries for files that are no longer there
        for entry in list(status.keys()):
            if entry not in found_files:
                logger.info('File %s from status is no longer there' % entry)
                del status[entry]
                dir_changed = True

        sums_file = os.path.join(root, hash_algorithm + 'sums.asc')
        if len(found_files) and not os.path.exists(sums_file):
            # Always generate one if we don't have it
            dir_changed = True

        if dir_changed:
            write_sums_file(root, found_files, status, hash_algorithm, vsort=vsort)
            # Save status after each changed dir
            save_status(statusfile, status)

        # Look at dirs and see if we need to exclude any of them
        to_rm: list[str] = []
        for name in dirs:
            full_path = os.path.join(root, name)

            if os.path.islink(full_path):
                logger.debug('Ignoring symlink %s' % name)
                continue

            excluded = False
            for exclude in excludes:
                if fnmatch.fnmatch(name, exclude) or fnmatch.fnmatch(full_path, exclude):
                    logger.debug('Excluding %s because it matches %s' % (full_path, exclude))
                    excluded = True
                    break
            if excluded:
                to_rm.append(name)
                continue

        for name in to_rm:
            dirs.remove(name)


def main() -> None:
    opts = parse_args()

    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    ch.setFormatter(formatter)

    if opts.verbose:
        ch.setLevel(logging.INFO)
    else:
        ch.setLevel(logging.CRITICAL)

    logger.addHandler(ch)

    if opts.logfile is not None:
        fh = logging.FileHandler(opts.logfile)
        formatter = logging.Formatter("[%(process)d] %(asctime)s - %(levelname)s - %(message)s")
        fh.setFormatter(formatter)

        if opts.debug:
            fh.setLevel(logging.DEBUG)
        else:
            fh.setLevel(logging.INFO)

        logger.addHandler(fh)

    if opts.gnupghome is not None:
        logger.info('Setting GNUPGHOME to %s' % opts.gnupghome)
        os.environ['GNUPGHOME'] = opts.gnupghome

    lock_fh = open(opts.lockfile, 'w')
    logger.debug('Obtaining exclusive lock for %s' % opts.lockfile)

    try:
        lockf(lock_fh, LOCK_EX | LOCK_NB)
    except IOError:
        logger.info('Could not obtain exclusive lock, assuming another process is running.')
        sys.exit(128)

    logger.debug('Lock obtained')

    sign_tree(opts.tree, opts.excludes, opts.hash_algorithm, vsort=opts.vsort)
    lockf(lock_fh, LOCK_UN)
    logger.debug('Lock released')


if __name__ == '__main__':
    main()
