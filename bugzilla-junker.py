#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# A helper script to go through the latest comments added to a bugzilla
# to see if any of them link to external sites. If the reviewer deems them
# spammy, the script will tag them as such.
#
# Caution: work in progress
#
__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import sys
import requests
import argparse
import logging
import re
import shelve
import datetime
import notify2
import time

from urllib.parse import urlparse
from configparser import ConfigParser

logger = logging.getLogger('default')

APIKEY = None
BZURL = None

REQSESSION = None

CACHEDATA = None


def notify_desktop(message):
    notify2.init('bugjunker')
    n = notify2.Notification('bugjunker', message)
    n.set_timeout(notify2.EXPIRES_NEVER)
    n.show()


def get_session():
    global REQSESSION
    if REQSESSION is None:
        REQSESSION = requests.session()
        REQSESSION.headers.update({'User-Agent': 'bugjunker'})
    return REQSESSION


def ban_hammer(spammers):
    params = {}
    for spammer in set(spammers):
        path = 'user/{spammer}'.format(spammer=spammer)
        logger.info('Banning %s', spammer)
        payload = {
            'email_enabled': False,
            'login_denied_text': 'Spammer',
        }
        bz_put(path, params, payload)


def tag_hammer(spamcids, spamtag):
    params = {}
    for cid in set(spamcids):
        logger.info('Tagging comment %s', cid)
        path = 'bug/comment/{cid}/tags'.format(cid=cid)
        payload = {
            'comment_id': cid,
            'add': [spamtag],
        }
        bz_put(path, params, payload)


def bug_hammer(spambugs, args):
    params = {}
    for bugid in set(spambugs):
        logger.info('Junking bug %s', bugid)
        path = 'bug/{bugid}'.format(bugid=bugid)
        payload = {
            'groups': {'add': [args.group]},
            'status': args.status,
            'resolution': args.resolution,
        }
        bz_put(path, params, payload)


def bz_get(path, params):
    url = '{BZURL}/rest/{path}'.format(BZURL=BZURL, path=path)
    params['api_key'] = APIKEY
    ses = get_session()
    res = ses.get(url, params=params)
    return res.json()


def bz_put(path, params, payload):
    url = '{BZURL}/rest/{path}'.format(BZURL=BZURL, path=path)
    params['api_key'] = APIKEY
    ses = get_session()
    res = ses.put(url, params=params, json=payload)
    return res.json()


def load_cache(cachefile):
    global CACHEDATA
    if CACHEDATA is not None:
        return CACHEDATA

    # noinspection PyBroadException
    try:
        with shelve.open(cachefile, 'r') as wc:
            logger.info('Loading cache from %s', cachefile)
            CACHEDATA = dict(wc)
    except:
        CACHEDATA = {
            'seencids': list(),
            'seenaids': list(),
            'okdomains': list(),
            'okfolks': list(),
        }
        pass

    if 'lastrun' in CACHEDATA:
        lastrun = CACHEDATA['lastrun']
    else:
        lastrun = '24h'

    return lastrun, CACHEDATA


def save_cache(cachefile, cachedata):
    with shelve.open(cachefile, 'c') as wc:
        for key, val in cachedata.items():
            wc[key] = val
        wc.sync()


def check_bad_urls(urls, okdomains):
    for url in urls:
        try:
            up = urlparse(url)
        except ValueError:
            return url, None
        isok = False
        for okd in okdomains:
            if okd == up.netloc:
                isok = True
                break
        if not isok:
            return url, up.netloc

    return None, None


def is_junk_attachment(attid):
    attid = str(attid)
    logger.info('    checking attachment %s', attid)
    path = 'bug/attachment/{attid}'.format(attid=attid)
    attinfo = bz_get(path, {})
    if attid not in attinfo['attachments']:
        return False
    attdata = attinfo['attachments'][attid]
    if attdata['is_patch'] or attdata['content_type'] in ('text/plain',):
        return False
    if attdata['content_type'] == 'text/html':
        # Almost certainly junk
        logger.info('    junking attachment %s', attid)
        payload = {
            'content_type': 'text/plain',
            'filename': 'caution.txt',
            'is_private': True,
        }
        bz_put(path, {}, payload)
        return True
    return False


def process_bugs(cmdargs, cachefile, c, bugs, spamtag):
    spammers = list()
    spamcids = list()
    spambugs = list()

    for bug in bugs:
        logger.info('Analyzing [%s]: %s', bug['id'], bug['summary'])
        params = {}
        bugid = bug['id']
        path = 'bug/{bugid}/comment'.format(bugid=bugid)
        comments = bz_get(path, params)
        for f1, f2 in comments['bugs'].items():
            c_count = -1
            for comment in f2['comments']:
                c_count += 1
                cid = comment['id']
                if cid in c['seencids']:
                    # already seen, skip
                    continue

                c['seencids'].append(cid)

                creator = comment['creator']
                if creator in c['okfolks']:
                    # Known good person
                    continue

                tags = comment['tags']
                if spamtag in tags:
                    # already marked as spammy
                    continue

                if creator in spammers:
                    # Made by a known spammer, banit
                    spamcids.append(cid)
                    logger.info('    auto-tagging comment by %s: %s', creator, cid)
                    continue

                if cmdargs.checkatt:
                    attid = comment['attachment_id']
                    if attid is not None and attid not in c['seenaids']:
                        c['seenaids'].append(attid)
                        if is_junk_attachment(attid):
                            logger.info('    check if spammer: %s', creator)

                # Look for remote URLs in the comment
                if bug['url'].find('http') > -1 or comment['text'].find('http') > -1:
                    urls = re.findall(r'(https?://\S+)', comment['text'])
                    if len(bug['url']):
                        urls.append(bug['url'])
                    badurl, baddomain = check_bad_urls(urls, c['okdomains'])

                    if badurl is not None:
                        if cmdargs.noninteractive:
                            notify_desktop('Spam in %s: %s' % (bug['id'], baddomain))
                            when = datetime.datetime.strptime(bug['last_change_time'], '%Y-%m-%dT%H:%M:%SZ')
                            c['seencids'].remove(cid)
                            c['lastrun'] = when.strftime('%Y-%m-%d %H:%M:%S')
                            c['needsinput'] = True
                            return spammers, spamcids, spambugs

                        logger.info('    ---')
                        logger.info('    suspish URL: %s', badurl)
                        logger.info('    checkit out: %s/show_bug.cgi?id=%s#c%s',
                                    BZURL, bugid, c_count)
                        baw = input('    (b)an, (a)llow, (w)hitelist %s: ' % baddomain)

                        if baw == 'a':
                            c['okfolks'].append(creator)
                            save_cache(cachefile, c)
                            continue

                        if baw == 'w':
                            logger.info('    whitelisted %s', baddomain)
                            c['okdomains'].append(baddomain)
                            c['okfolks'].append(creator)
                            save_cache(cachefile, c)
                            continue

                        logger.info('    spamcid: %s', cid)
                        spamcids.append(cid)

                        # If it's a comment #0, then the whole bug needs junking
                        if c_count == 0:
                            spambugs.append(bug['id'])

                        if creator not in spammers:
                            logger.info('    spammer: %s', creator)
                            spammers.append(creator)

    return spammers, spamcids, spambugs


def main(args):
    global BZURL
    global APIKEY

    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    ch.setFormatter(formatter)

    if args.quiet:
        ch.setLevel(logging.CRITICAL)
    elif args.debug:
        ch.setLevel(logging.DEBUG)
    else:
        ch.setLevel(logging.INFO)

    logger.addHandler(ch)

    logger.info('Loading configuration file %s', args.config)
    config = ConfigParser()
    config.read(args.config)
    BZURL = config.get('main', 'url')
    APIKEY = config.get('main', 'apikey')

    spamtag = config.get('main', 'spamtag')

    if config.get('main', 'logfile'):
        ch = logging.FileHandler(config.get('main', 'logfile'))
        fmt = '[%(process)d] %(asctime)s - %(message)s'
        ch.setFormatter(logging.Formatter(fmt))
        ch.setLevel(logging.INFO)
        logger.addHandler(ch)

    cachefile = config.get('main', 'cache')
    lastrun, c = load_cache(cachefile)


    if args.lookback is not None:
        lastrun = args.lookback

    while True:
        if args.noninteractive and 'needsinput' in c and c['needsinput']:
            logger.info('Need to run interactively to make some decisions')
            sys.exit(0)

        params = {
            'chfieldfrom': lastrun,
            'include_fields': 'id,summary,last_change_time,url',
        }
        logger.info('Querying %s for changes since %s', BZURL, lastrun)

        unow = datetime.datetime.utcnow()
        json = bz_get('bug', params)
        c['lastrun'] = unow.strftime('%Y-%m-%d %H:%M:%S')
        c['needsinput'] = False
        if len(json['bugs']):
            spammers, spamcids, spambugs = process_bugs(args, cachefile, c, json['bugs'], spamtag)

            if len(spammers) or len(spamcids) or len(spambugs):
                ban_hammer(spammers)
                tag_hammer(spamcids, spamtag)
                bug_hammer(spambugs, args)
            else:
                logger.info('No new spam found')
        else:
            logger.info('No changes since %s', lastrun)

        save_cache(cachefile, c)

        if not args.sleep:
            sys.exit(0)

        logger.info('Sleeping %d seconds', args.sleep)
        time.sleep(args.sleep)


def cmd():
    description = 'Junk spammy bugzilla comments and ban their authors'
    parser = argparse.ArgumentParser(description=description, prog='bz-comment-junker.py')
    parser.add_argument('-c', '--config', required=True,
                        help='Configuration file')
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Output only errors')
    parser.add_argument('-d', '--debug', action='store_true', default=False,
                        help='Add debugging info')
    parser.add_argument('-l', '--lookback', default=None,
                        help='How far back to look (default: since last run, or 24h if no cached data)')
    parser.add_argument('-n', '--noninteractive', action='store_true', default=False,
                        help='Run non-interactively and send an alert when potential spam is found')
    parser.add_argument('-a', '--check-attachments', action='store_true', dest='checkatt', default=False,
                        help='Check attachments for junkiness')
    parser.add_argument('--sleep', type=int, default=0,
                        help='After the run, sleep N seconds and then run again')
    parser.add_argument('--status', default='RESOLVED',
                        help='Status value for junked bugs')
    parser.add_argument('--resolution', default='INVALID',
                        help='Resolution value for junked bugs')
    parser.add_argument('--product', default='Other',
                        help='Product value for junked bugs')
    parser.add_argument('--component', default='Spam',
                        help='Component value for junked bugs')
    parser.add_argument('--group', default='Junk',
                        help='Private group name for junked bugs')

    args = parser.parse_args()
    main(args)


if __name__ == '__main__':
    cmd()
