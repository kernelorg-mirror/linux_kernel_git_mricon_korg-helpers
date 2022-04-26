#!/usr/bin/env python3
#
# This script will check random content published on www.kernel.org/pub against
# authorized signatures to identify when corruption or substitution happens. The name
# comes from the Russian word /proveryat/, meaning "to verify".
#
# The script it supposed to be fire-and-forget, running in a screen session, as
# a background task, or as a systemd service, with reports sent to admin@kernel.org.
#
# E.g. (after you play with it to verify that it's doing the right thing):
# ./sig-prover -c sig-prover.conf -q &
#
# CAUTION:
#    This script is not a guaranteed mechanism to detect intrusion -- an
#    attacker can defeat it by analyzing access patterns/IPs and serving
#    different content when it suspects that someone is running an automated
#    signature verification check. The script can probably be improved by
#    adding random delays between retrieving the tarball and the detached
#    signature, setting a referrer value, etc. However, even with added
#    measures, it will always act fairly predictably, so there will always
#    remain a way to detect and defeat it.
#
#    If you download tarballs from kernel.org for any purpose, you should
#    always run your own verification on each downloaded file.
#    https://www.kernel.org/signature.html
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# -*- coding: utf-8 -*-
#
__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import sys
import os
import logging
import argparse
import requests
import random
import subprocess
import tempfile
import re
import time
import json

import email
import email.message
import email.utils
import smtplib

from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

logger = logging.getLogger(__name__)
REQSESSION = None
GPGBIN = '/usr/bin/gpg'
SEEN = dict()

__VERSION__ = '0.1'


def get_requests_session(useragent=None):
    global REQSESSION
    if REQSESSION is None:
        REQSESSION = requests.session()
        retry = Retry(connect=3, backoff_factor=1)
        adapter = HTTPAdapter(max_retries=retry)
        REQSESSION.mount('http://', adapter)
        REQSESSION.mount('https://', adapter)
        if useragent is None:
            useragent = f'Sig-Prover/{__VERSION__}'

        headers = {
            'User-Agent': useragent,
        }
        REQSESSION.headers.update(headers)

    return REQSESSION


def get_random_target(config, rsect):
    global SEEN
    if rsect not in SEEN:
        SEEN[rsect] = set()

    ua = config[rsect].get('useragent')
    if ua:
        ua = random.choice(ua.split('\n'))
    rses = get_requests_session(useragent=ua)
    candidates = list()

    # Is it a releases.json, or a collection of hosts and paths?
    jurl = config[rsect].get('json')
    if jurl:
        logger.info('  retrieving %s', jurl)
        resp = rses.get(jurl)
        resp.raise_for_status()
        rels = json.loads(resp.content)
        for release in rels['releases']:
            if not release['pgp']:
                continue
            candidate = release['source']
            # Do we define hosts?
            hosts = config[rsect].get('hosts')
            if hosts and candidate.find('https://cdn') == 0:
                # Swap in the CDN URL with an actual host URL, as it doesn't
                # really make sense to check things over cdn cache which we don't
                # control and can't do anything about.
                for rhost in config[rsect].get('hosts').split('\n'):
                    hostcand = candidate.replace('https://cdn.kernel.org', rhost)
                    if hostcand not in SEEN[rsect]:
                        candidate = hostcand
                        break

            if candidate in SEEN[rsect]:
                logger.debug('Already checked %s in this session', candidate)
                continue
            candidates.append(candidate)

    else:
        # Grab a random host
        rhost = random.choice(config[rsect].get('hosts').split('\n'))
        # Grab a random path
        rpath = random.choice(config[rsect].get('paths').split('\n'))
        rurl = rhost + rpath
        # Now we grab the sha256sums.txt file from there
        shapath = rurl + 'sha256sums.asc'
        logger.info('  retrieving %s', shapath)
        resp = rses.get(shapath)
        resp.raise_for_status()

        keyring = os.path.join(config[rsect].get('keyringdir'), config[rsect].get('dirsigner_keyring'))
        logger.info('    verifying with %s', keyring)
        gpgargs = ['--verify', '--status-fd=2', '-']
        ecode, out, err = gpg_run_command(gpgargs, keyring, stdin=resp.content)
        if ecode == 0:
            good, valid, created, errors = validate_gpg_signature(err.decode())
            if good and valid:
                logger.info('    checksums signature is good and valid (created: %s)', created)
        else:
            errors = err.decode().split('\n')

        if errors:
            report_badness(config[rsect], shapath, errors)

        rmask = random.choice(config[rsect].get('masks').split('\n'))
        for line in resp.content.split(b'\n'):
            if re.search(rmask.encode(), line):
                filen = line.split()[1].decode()
                candidate = rurl + filen
                if candidate in SEEN[rsect]:
                    logger.debug('Already checked %s in this session', candidate)
                    continue
                candidates.append(rurl + filen)

    if not candidates:
        logger.debug('Already tried all possible choices for %s', rsect)
        candidates = list(SEEN[rsect])
        SEEN[rsect] = set()

    if not candidates:
        logger.info('No suitable candidates found for %s', rsect)
        return None

    candidate = random.choice(candidates)
    SEEN[rsect].add(candidate)
    return candidate


def _run_command(cmdargs, stdin=None):
    logger.debug('Running %s' % ' '.join(cmdargs))

    sp = subprocess.Popen(cmdargs,
                          stdout=subprocess.PIPE,
                          stdin=subprocess.PIPE,
                          stderr=subprocess.PIPE)

    (output, error) = sp.communicate(input=stdin)

    return sp.returncode, output, error


def gpg_run_command(args, keyring, stdin=None):
    cmdargs = [GPGBIN, '--batch', '--no-auto-key-retrieve', '--no-auto-check-trustdb', '--no-default-keyring',
               '--keyring', keyring]
    cmdargs += args

    return _run_command(cmdargs, stdin=stdin)


def validate_gpg_signature(output):
    good = False
    valid = False
    created = None
    errors = set()
    gs_matches = re.search(r'^\[GNUPG:] GOODSIG ([0-9A-F]+)\s+.*$', output, re.M)
    if gs_matches:
        logger.debug('  GOODSIG')
        good = True
        keyid = gs_matches.groups()[0]
        vs_matches = re.search(r'^\[GNUPG:] VALIDSIG ([0-9A-F]+) (\d{4}-\d{2}-\d{2}) (\d+)', output, re.M)
        if vs_matches:
            logger.debug('  VALIDSIG')
            valid = True
            created = vs_matches.groups()[1]
        else:
            errors.add('Signature not valid from key: %s' % keyid)
    else:
        # Are we missing a key?
        matches = re.search(r'^\[GNUPG:] NO_PUBKEY ([0-9A-F]+)$', output, re.M)
        if matches:
            errors.add('Missing public key: %s' % matches.groups()[0])
        # Is the key expired?
        matches = re.search(r'^\[GNUPG:] EXPKEYSIG (.*)$', output, re.M)
        if matches:
            errors.add('Expired key: %s' % matches.groups()[0])

    return good, valid, created, errors


def report_badness(config, furl, errors):
    if not config.get('notify'):
        logger.critical('ERROR: failed verifying: %s', furl)
        for entry in errors:
            logger.critical('  %s', entry)
        logger.debug('WARNING: notify not set, not sending a mail report')
        sys.exit(1)

    logger.info('ERROR: failed verifying: %s', furl)
    msg = email.message.Message()

    # Set to and cc
    msg['To'] = config.get('notify')
    targets = [msg['To']]

    ccs = config.get('notify_cc', '')
    if ccs:
        msg['Cc'] = ccs
        targets += [x.strip() for x in ccs.split(',')]

    msg['Subject'] = f'SIGFAIL: {furl}'
    msg['From'] = config.get('mailfrom', 'devnull@kernel.org')

    msg['Message-Id'] = email.utils.make_msgid('sig-prover')
    msg['Date'] = email.utils.formatdate(localtime=True)

    body = list()
    body.append('Hello:')
    body.append('')
    body.append('The following URL failed signature verification:')
    body.append(f'  {furl}')
    body.append('')
    body.append('Errors:')
    for error in errors:
        body.append(f'  {error}')

    msg.set_payload('\r\n'.join(body))

    logger.debug('Message follows')
    logger.debug(msg.as_string())

    mailhost = config.get('mailhost', 'localhost')

    try:
        server = smtplib.SMTP(mailhost)
        if config.getboolean('mailtls'):
            server.starttls()

        muser = config.get('mailuser')
        mpass = config.get('mailpass')
        if muser and mpass:
            server.login(muser, mpass)

        logger.info('Sending mail to %s', ', '.join(targets))
        server.sendmail(msg['From'], targets, msg.as_string())
        server.close()
    except Exception as ex: # noqa
        logger.critical('Unable to send mail to %s', ', '.join(targets))
        logger.critical('Attempting to use %s returned:', mailhost)
        logger.critical(ex)


def verify_tarball(config, turl):
    # Try the exact filename + .sign first
    signurl = turl + '.sign'
    rses = get_requests_session()
    resp = rses.get(signurl)
    zext = None
    zbin = None
    if resp.status_code > 200:
        # Try dropping the last .foo and trying again
        parts = turl.rsplit('.', 1)
        signurl = parts[0] + '.sign'
        zext = parts[1]
        # Are we capable of dealing with zext?
        zbin = config.get(f'un{zext}')
        if not zbin:
            logger.critical('Not aware of how to deal with %s compression', zext)
            sys.exit(1)
        logger.debug('Will use %s for uncompression', zbin)
        resp = rses.get(signurl)
    resp.raise_for_status()
    logger.info('  retrieving %s', signurl)
    with tempfile.TemporaryDirectory(suffix='.sig-prover', dir=config.get('tempdir', '/tmp')) as td:
        signfile = os.path.join(td, 'content.sig')
        with open(signfile, 'wb') as sfh:
            sfh.write(resp.content)
        resp.close()
        logger.info('  retrieving %s', turl)
        resp = rses.get(turl, stream=True)
        resp.raise_for_status()
        contentfile = os.path.join(td, 'content')
        if zext:
            contentfile = f'{contentfile}.{zext}'
        with open(contentfile, 'wb') as cfh:
            for chunk in resp.iter_content(chunk_size=8192):
                cfh.write(chunk)
        resp.close()
        if zext:
            logger.info('    uncompressing %s', zext)
            cmdargs = [zbin, contentfile]
            ecode, out, err = _run_command(cmdargs)
            if ecode > 0:
                # Failure to uncompress is not a critical failure, because
                # this could be the result of any number of things: bad cache,
                # errors during transmission, etc. We don't care for such
                # situations -- we are looking specifically at bad signatures.
                logger.info('Failed uncompressing %s')
                return
            contentfile = os.path.join(td, 'content')
        gpgargs = ['--verify', '--status-fd=2', signfile, contentfile]
        keyring = os.path.join(config.get('keyringdir'), config.get('keyring'))
        logger.info('    verifying with %s', keyring)
        ecode, out, err = gpg_run_command(gpgargs, keyring=keyring)
        if ecode == 0:
            good, valid, created, errors = validate_gpg_signature(err.decode())
            if good and valid:
                logger.info('  signature is good and valid (created: %s)', created)
                return
        else:
            errors = err.decode().split('\n')

        report_badness(config, turl, errors)


def get_random_sect(config):
    global GPGBIN
    sects = list(config.sections())
    weights = list()
    for sect in sects:
        weights.append(config[sect].getint('weight', 10))

    rsect = random.choices(sects, weights=weights, k=1)[0]
    if config[rsect].get('gpgbin'):
        GPGBIN = config[rsect].get('gpgbin')

    return rsect


def sig_verify(config):
    rsect = get_random_sect(config)
    logger.info('[%s]', rsect)
    try:
        target = get_random_target(config, rsect)
        if target:
            verify_tarball(config[rsect], target)
    except requests.exceptions.RequestException as ex:
        # Treat failures as non-critical, because hosts can be intermittently
        # unreachable for various reasons.
        logger.info('Failed getting remote content:')
        logger.info(ex)

    return config[rsect].getint('sleep', 0)


def read_config(cfgfile):
    from configparser import ConfigParser, ExtendedInterpolation
    if not os.path.exists(cfgfile):
        sys.stderr.write('ERROR: config file %s does not exist' % cfgfile)
        sys.exit(1)
    fconfig = ConfigParser(interpolation=ExtendedInterpolation())
    fconfig.read(cfgfile)

    return fconfig


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config-file', dest='cfgfile', required=True,
                        help='Config file to use')
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

    while True:
        sleep = sig_verify(_config)
        if not sleep:
            break
        logger.info('--- sleeping %s seconds ---', sleep)
        try:
            time.sleep(sleep)
        except KeyboardInterrupt:
            logger.info('Bye')
            sys.exit(0)
