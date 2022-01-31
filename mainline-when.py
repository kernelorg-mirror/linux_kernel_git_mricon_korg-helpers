#!/usr/bin/env python3
# Quickie script to estimate mainline release dates.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# -*- coding: utf-8 -*-
#
__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'


import argparse
import requests
import datetime
import json
import sys

from packaging import version  # noqa

RELEASES_JSON = 'https://www.kernel.org/releases.json'
WINDOW_DAYS = 14
RC_COUNT = 7
CYCLE_DAYS = WINDOW_DAYS + (RC_COUNT * 7)
VERSION = '1.0'


def main(estnext=3, forcever=None):
    rses = requests.session()
    headers = {'User-Agent': f'mainline-when/{VERSION}'}
    rses.headers.update(headers)
    resp = rses.get(RELEASES_JSON)
    resp.raise_for_status()
    rels = json.loads(resp.content)
    release = None
    for release in rels['releases']:
        if release['moniker'] != 'mainline':
            continue
        break
    if release is None:
        sys.stdout.write('Could not find mainline release info\n')
        sys.exit(1)

    if forcever:
        mver = version.parse(forcever)
    else:
        mver = version.parse(release.get('version'))
    majver, minver = mver.release
    crel = datetime.datetime.strptime(release['released']['isodate'], '%Y-%m-%d')
    rcn = None
    if mver.is_prerelease:
        rcn = mver.pre[1]
        print(f'current status: {majver}.{minver}-rc{rcn}')
        mrel = crel - datetime.timedelta(days=(7*rcn)+7)
        if rcn < 8:
            frel = mrel + datetime.timedelta(days=CYCLE_DAYS)
        else:
            # Add 7 days to the latest release and hope for the best
            frel = crel + datetime.timedelta(days=7)
    else:
        # We're currently in a merge window
        minver += 1
        print(f'current status:\t{majver}.{minver} merge window')
        mrel = crel
        frel = crel + datetime.timedelta(days=CYCLE_DAYS)
    print('---')
    rcrel = mrel + datetime.timedelta(days=WINDOW_DAYS)
    if rcn:
        print(f'{majver}.{minver} window open: {mrel.strftime("%Y-%m-%d")}')
        if rcn == 1:
            print(f'{majver}.{minver}-rc1 release: {rcrel.strftime("%Y-%m-%d")}  <-- you are here')
        else:
            print(f'{majver}.{minver}-rc1 release: {rcrel.strftime("%Y-%m-%d")}')
            print(f'{majver}.{minver}-rc{rcn} release: {crel.strftime("%Y-%m-%d")}  <-- you are here')
    else:
        print(f'{majver}.{minver} window open: {mrel.strftime("%Y-%m-%d")}  <-- you are here')
        print(f'{majver}.{minver}-rc1 release: {rcrel.strftime("%Y-%m-%d")}')

    print(f'{majver}.{minver} final      : {frel.strftime("%Y-%m-%d")}')

    # Estimate next versions
    for nextver in range(minver+1, minver+estnext+1):
        print('---')
        print(f'{majver}.{nextver} window open: {frel.strftime("%Y-%m-%d")}')
        rcrel = frel + datetime.timedelta(days=WINDOW_DAYS)
        print(f'{majver}.{nextver}-rc1 release: {rcrel.strftime("%Y-%m-%d")}')
        frel = frel + datetime.timedelta(days=CYCLE_DAYS)
        print(f'{majver}.{nextver} final      : {frel.strftime("%Y-%m-%d")}')
    print('---')
    print('NB: all dates are estimates')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-n', '--next', type=int, default=3, help='How many versions to estimate')
    parser.add_argument('--force-version', help='Force version to be this (testing only)')
    cmdargs = parser.parse_args()
    main(estnext=cmdargs.next, forcever=cmdargs.force_version)
