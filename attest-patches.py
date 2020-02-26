#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# !EXPERIMENTAL!
# Proof of concept for patch attestation using signatures@kernel.org
# pseudo-list. Do not use for anything useful, as in its current form
# it doesn't cover a bunch of malicious use-cases.
#
# -*- coding: utf-8 -*-
#
__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import os
import sys
import argparse
import logging
import hashlib
import subprocess
import re
import email.message
import email.utils
import mailbox
import urllib
import requests

from tempfile import mkstemp

HUNK_RE = re.compile(r'^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@')
FILENAME_RE = re.compile(r'^(---|\+\+\+) (\S+)')

# Used for caching attestation data lookups
ATTESTATION_DATA = dict()
# Used for keeping a mapping of subkeys to UIDs
SUBKEY_DATA = dict()
# Used for keeping a list of validation errors
VALIDATION_ERRORS = set()

logger = logging.getLogger('attest-patches')

VERSION = '0.1'
ATTESTATION_FORMAT = '0.1'

GPGBIN = 'gpg2'
GPGTRUSTMODEL = 'pgp'


def get_config_from_git(regexp, defaults=None):
    args = ['config', '-z', '--get-regexp', regexp]
    ecode, out = git_run_command(None, args)
    gitconfig = defaults
    if not gitconfig:
        gitconfig = dict()
    if not out:
        return gitconfig

    for line in out.split('\x00'):
        if not line:
            continue
        key, value = line.split('\n', 1)
        try:
            chunks = key.split('.')
            cfgkey = chunks[-1]
            gitconfig[cfgkey.lower()] = value
        except ValueError:
            logger.debug('Ignoring git config entry %s', line)

    return gitconfig


def _run_command(cmdargs, stdin=None, logstderr=False):
    logger.debug('Running %s' % ' '.join(cmdargs))

    sp = subprocess.Popen(cmdargs,
                          stdout=subprocess.PIPE,
                          stdin=subprocess.PIPE,
                          stderr=subprocess.PIPE)

    (output, error) = sp.communicate(input=stdin)

    output = output.decode('utf-8', errors='replace')

    if logstderr and len(error.strip()):
        logger.debug('Stderr: %s', error.decode('utf-8', errors='replace'))

    return sp.returncode, output


def gpg_run_command(cmdargs, stdin=None, logstderr=False):
    logger.debug('Running %s' % ' '.join(cmdargs))

    return _run_command(cmdargs, stdin=stdin, logstderr=logstderr)


def git_run_command(gitdir, args, stdin=None, logstderr=False):
    cmdargs = ['git', '--no-pager']
    if gitdir:
        cmdargs += ['--git-dir', gitdir]
    cmdargs += args

    return _run_command(cmdargs, stdin=stdin, logstderr=logstderr)


def get_mailinfo_hashes(content):
    msg_out = mkstemp()
    patch_out = mkstemp()
    cmdargs = ['mailinfo', '--encoding=UTF-8', msg_out[1], patch_out[1]]
    ecode, info = git_run_command(None, cmdargs, content)
    if ecode > 0:
        logger.critical('ERROR: Could not get mailinfo')
        return None, None, None
    logger.debug(info)
    ihasher = hashlib.sha256()
    for line in info.split('\n'):
        # We don't use the "Date:" field because it is likely to be
        # mangled between when git-format-patch generates it and
        # when it is sent out by git-send-email (or other tools).
        # TODO: We can do some basic date sanity checking by
        #       looking at the PGP signature date and making sure
        #       that it is within a sane limit compared to the
        #       commit, though it is unlikely to matter for attestation
        if re.search(r'^(Author|Email|Subject):', line):
            ihasher.update((line + '\n').encode('utf-8'))
    ihash = ihasher.hexdigest()

    with open(msg_out[1], 'r') as mfh:
        msg = mfh.read()
        mhasher = hashlib.sha256()
        mhasher.update(msg.encode('utf-8'))
        mhash = mhasher.hexdigest()
    os.unlink(msg_out[1])

    with open(patch_out[1], 'r') as pfh:
        patch = pfh.read()
        if len(patch.strip()):
            phash = get_patch_hash(patch)
        else:
            phash = None
    os.unlink(patch_out[1])

    return ihash, mhash, phash


def get_patch_hash(diff):
    # The aim is to represent the patch as if you did the following:
    # git diff HEAD~.. | dos2unix | sha256sum
    #
    # This subroutine removes anything at the beginning of diff data, like
    # diffstat or any other auxiliary data, and anything trailing at the end
    # XXX: This currently doesn't work for git binary patches
    #
    diff = diff.replace('\r', '')
    diff = diff.strip() + '\n'

    # For keeping a buffer of lines preceding @@ ... @@
    buflines = list()

    phasher = hashlib.sha256()

    # Used for counting where we are in the patch
    pp = 0
    for line in diff.split('\n'):
        hunk_match = HUNK_RE.match(line)
        if hunk_match:
            # logger.debug('Crunching %s', line)
            mlines, plines = hunk_match.groups()
            pp = int(plines)
            addlines = list()
            for bline in reversed(buflines):
                # Go backward and add lines until we get to the start
                # or encounter a blank line
                if len(bline.strip()) == 0:
                    break
                addlines.append(bline)
            if addlines:
                phasher.update(('\n'.join(reversed(addlines))+'\n').encode('utf-8'))
            buflines = list()
            # Feed this line to the hasher
            phasher.update((line+'\n').encode('utf-8'))
            continue
        if pp > 0:
            # Inside the patch
            phasher.update((line+'\n').encode('utf-8'))
            if line[0] != '-':
                pp -= 1
            continue
        # Not anything we recognize, so stick into buflines
        buflines.append(line)

    return phasher.hexdigest()


def create_attestation(cmdargs):
    attlines = list()
    for patchfile in cmdargs.attest:
        with open(patchfile, 'rb') as fh:
            ihash, mhash, phash = get_mailinfo_hashes(fh.read())
            if not phash:
                logger.info('SKP | %s', os.path.basename(patchfile))
                continue
            logger.info('ADD | %s', os.path.basename(patchfile))
            attid = '%s-%s-%s' % (ihash[:8], mhash[:8], phash[:8])
            attlines.append('%s:' % attid)
            attlines.append('  i: %s' % ihash)
            attlines.append('  m: %s' % mhash)
            attlines.append('  p: %s' % phash)

    payload = '\n'.join(attlines)

    usercfg = get_config_from_git(r'user\..*')

    gpgargs = [GPGBIN, '--batch']
    if 'signingkey' in usercfg:
        gpgargs += ['-u', usercfg['signingkey']]
    gpgargs += ['--clearsign',
                '--comment', 'att-fmt-ver: %s' % ATTESTATION_FORMAT,
                '--comment', 'att-hash: sha256',
                ]

    ecode, signed = gpg_run_command(gpgargs, stdin=payload.encode('utf-8'))
    if ecode > 0:
        logger.critical('ERROR: Unable to sign using %s', GPGBIN)
        sys.exit(1)

    att_msg = email.message.EmailMessage()
    att_msg.set_payload(signed.encode('utf-8'))
    # GDPR-proofing: we don't care about the envelope.
    # All we need is in the hashes and in the PGP payload
    att_msg['From'] = '<devnull@kernel.org>'
    att_msg['To'] = '<signatures@kernel.org>'
    att_msg['Message-Id'] = email.utils.make_msgid(domain='kernel.org')
    att_msg['Subject'] = 'Patch attestation'

    # Future iterations will be able to submit this to a RESTful URL at git.kernel.org,
    # in order not to depend on avaialbility of SMTP gateways
    with open(cmdargs.output, 'wb') as fh:
        fh.write(att_msg.as_bytes())

    logger.info('---')
    logger.info('Wrote %s', cmdargs.output)
    logger.info('You can send it using:')
    logger.info('  sendmail -oi signatures@kernel.org < %s', cmdargs.output)
    logger.info('  mutt -H %s', cmdargs.output)


def load_attestation_data(link, content):
    global ATTESTATION_DATA
    gpgargs = [GPGBIN, '--batch', '--verify', '--status-fd=1']
    if GPGTRUSTMODEL == 'tofu':
        gpgargs += ['--trust-model', 'tofu', '--tofu-default-policy', 'good']

    ecode, output = gpg_run_command(gpgargs, stdin=content.encode('utf-8'))
    good = False
    valid = False
    trusted = False
    sigkey = None
    siguid = None
    if ecode == 0:
        # We're looking for both GOODSIG and VALIDSIG
        gs_matches = re.search(r'^\[GNUPG:\] GOODSIG ([0-9A-F]+)\s+(.*)$', output, re.M)
        if gs_matches:
            logger.debug('  GOODSIG')
            good = True
            sigkey, siguid = gs_matches.groups()
            if re.search(r'^\[GNUPG:\] VALIDSIG', output, re.M):
                logger.debug('  VALIDSIG')
                valid = True
                # Do we have a TRUST_(FULLY|ULTIMATE)?
                matches = re.search(r'^\[GNUPG:\] TRUST_(FULLY|ULTIMATE)', output, re.M)
                if matches:
                    logger.debug('  TRUST_%s', matches.groups()[0])
                    trusted = True
    else:
        # Are we missing a key?
        matches = re.search(r'^\[GNUPG:\] NO_PUBKEY ([0-9A-F]+)$', output, re.M)
        if matches:
            VALIDATION_ERRORS.update(('Missing public key: %s' % matches.groups()[0],))
        else:
            VALIDATION_ERRORS.update(('PGP Validation failed for: %s' % link,))

    siginfo = (good, valid, trusted, sigkey, siguid)

    # No need to go on if it's no good
    if not good:
        return

    ihash = mhash = phash = None
    for line in content.split('\n'):
        # It's a yaml, but we don't parse it as yaml for safety reasons
        line = line.rstrip()
        if re.search(r'^([0-9a-f-]{26}:|-----BEGIN.*)$', line):
            if ihash and mhash and phash:
                if (ihash, mhash, phash) not in ATTESTATION_DATA:
                    ATTESTATION_DATA[(ihash, mhash, phash)] = list()
                ATTESTATION_DATA[(ihash, mhash, phash)].append(siginfo)
                ihash = mhash = phash = None
            continue
        matches = re.search(r'^\s+([imp]):\s*([0-9a-f]{64})$', line)
        if matches:
            t = matches.groups()[0]
            if t == 'i':
                ihash = matches.groups()[1]
            elif t == 'm':
                mhash = matches.groups()[1]
            elif t == 'p':
                phash = matches.groups()[1]


def query_lore_signatures(attid, session):
    global ATTESTATION_DATA
    global VALIDATION_ERRORS
    # XXX: Querying this via the Atom feed is a temporary kludge until we have
    #      proper search API on lore.kernel.org
    queryurl = '%s?%s' % ('https://lore.kernel.org/signatures/', urllib.parse.urlencode({'q': attid, 'x': 'A'}))
    logger.debug('Query URL: %s', queryurl)
    resp = session.get(queryurl)
    content = resp.content.decode('utf-8')
    matches = re.findall(r'link\s+href="([^"]+)".*?(-----BEGIN PGP SIGNED MESSAGE-----.*?-----END PGP SIGNATURE-----)',
                         content, flags=re.DOTALL)

    if not matches:
        VALIDATION_ERRORS.update(('No matches found in the signatures archive on lore.',))
        return

    for link, sigdata in matches:
        load_attestation_data(link, sigdata)


def load_attestation_file(afile):
    with open(afile, 'r') as fh:
        sigdata = fh.read()
        load_attestation_data(afile, sigdata)


def get_lore_attestation(c_ihash, c_mhash, c_phash, session):
    global ATTESTATION_DATA
    if (c_ihash, c_mhash, c_phash) not in ATTESTATION_DATA:
        attid = '%s-%s-%s' % (c_ihash[:8], c_mhash[:8], c_phash[:8])
        query_lore_signatures(attid, session)

    # This will throw a KeyError on non-match, which we bubble up
    return ATTESTATION_DATA[(c_ihash, c_mhash, c_phash)]


def get_subkey_uids(keyid):
    global SUBKEY_DATA

    if keyid in SUBKEY_DATA:
        return SUBKEY_DATA[keyid]

    gpgargs = [GPGBIN, '--batch', '--with-colons', '--list-keys', keyid]
    ecode, keyinfo = gpg_run_command(gpgargs)
    if ecode > 0:
        logger.critical('ERROR: Unable to get UIDs list matching key %s', keyid)
        return None
    uids = list()
    for line in keyinfo.split('\n'):
        if line[:4] != 'uid:':
            continue
        chunks = line.split(':')
        if chunks[1] in ('r',):
            # Revoked UID, ignore
            continue
        uids.append(chunks[9])

    SUBKEY_DATA[keyid] = email.utils.getaddresses(uids)
    return SUBKEY_DATA[keyid]


def check_if_from_matches_uids(keyid, msg):
    uids = get_subkey_uids(keyid)
    fromaddr = email.utils.getaddresses(msg.get_all('from', []))[0]
    for uid in uids:
        if fromaddr[1] == uid[1]:
            return True

    return False


def verify_attestation(cmdargs):
    mbx = mailbox.mbox(cmdargs.check)
    if cmdargs.attfile:
        load_attestation_file(cmdargs.attfile)
    session = requests.session()
    session.headers.update({'User-Agent': 'attest-patches/%s' % VERSION})
    ecode = 1
    attestors = set()
    for msg in mbx:
        content = msg.as_bytes()
        ihash, mhash, phash = get_mailinfo_hashes(content)
        if not phash:
            logger.debug('SKIP | %s', msg['Subject'])
            continue
        logger.debug('Verifying: %s', msg['Subject'])
        logger.debug(' i: %s', ihash)
        logger.debug(' m: %s', mhash)
        logger.debug(' p: %s', phash)
        try:
            adata = get_lore_attestation(ihash, mhash, phash, session)
        except KeyError:
            # No attestations found
            logger.critical('FAIL | %s', msg['Subject'])
            if not cmdargs.nofast:
                logger.critical('Aborting due to failure.')
                ecode = 1
                break
            else:
                ecode = 128
                continue

        for good, valid, trusted, sigkey, siguid in adata:
            if cmdargs.ignorefrom or check_if_from_matches_uids(sigkey, msg):
                if not trusted:
                    VALIDATION_ERRORS.update(('Insufficient owner trust (model=%s): %s (key=%s)'
                                              % (GPGTRUSTMODEL, siguid, sigkey),))
                    ecode = 128
                else:
                    if ecode != 128:
                        attestors.update(('%s (%s)' % (siguid, sigkey),))
                        ecode = 0
                    break
            else:
                VALIDATION_ERRORS.update(('Attestation ignored due to From/UID mismatch: %s' % siguid,))
                ecode = 1

        if ecode > 0:
            logger.critical('FAIL | %s', msg['Subject'])
            if not cmdargs.nofast:
                logger.critical('Aborting due to failure.')
                break
        else:
            logger.critical('PASS | %s', msg['Subject'])

    logger.critical('---')
    if ecode > 0:
        logger.critical('Attestation verification failed.')
        if len(VALIDATION_ERRORS):
            logger.critical('---')
            logger.critical('The validation process reported the following errors:')
            for error in VALIDATION_ERRORS:
                logger.critical('  %s', error)
    else:
        logger.critical('All patches passed attestation:')
        for attestor in attestors:
            logger.critical('  Attested-by: %s', attestor)

    sys.exit(ecode)


def main(cmdargs):
    global GPGBIN
    global GPGTRUSTMODEL
    logger.setLevel(logging.DEBUG)

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
    gpgcfg = get_config_from_git(r'gpg\..*', {'program': GPGBIN})
    GPGBIN = gpgcfg['program']
    if cmdargs.tofu:
        GPGTRUSTMODEL = 'tofu'

    if cmdargs.attest and cmdargs.check:
        logger.critical('You cannot both --attest and --check. Pick one.')
        sys.exit(1)
    if cmdargs.attest:
        create_attestation(cmdargs)
    elif cmdargs.check:
        verify_attestation(cmdargs)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('-a', '--attest', nargs='+',
                        help='Create attestation for patches')
    parser.add_argument('-o', '--output', default='attestation.eml',
                        help='Save attestation message in this file (use with -a)')
    parser.add_argument('-c', '--check',
                        help='Check attestation for patches in an mbox file')
    parser.add_argument('-i', '--attestation-file', dest='attfile',
                        help='Use this file for attestation data instead of querying lore.kernel.org')
    parser.add_argument('-t', '--tofu', action='store_true', default=False,
                        help='Force TOFU trust model (otherwise uses your global GnuPG setting)')
    parser.add_argument('-X', '--no-fast-exit', dest='nofast', action='store_true', default=False,
                        help='Do not exit after first failure')
    parser.add_argument('-F', '--ignore-from-mismatch', dest='ignorefrom', action='store_true',
                        default=False, help='Ignore mismatches between From: and PGP uid data')
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Only output errors to the stdout')
    parser.add_argument('-v', '--verbose', action='store_true', default=False,
                        help='Be more verbose in logging output')

    main(parser.parse_args())
