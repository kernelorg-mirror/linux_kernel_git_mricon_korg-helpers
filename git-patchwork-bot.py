#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# This bot automatically recognizes when patchwork-tracked patches
# are applied to git repositories and marks them as "Accepted." It can
# additionally send mail notifications to the maintainers and to the
# patch submitters.
#
# It runs from a cronjob, but can be also run from post-update hooks with
# extra wrappers. For more details, consult:
#
# https://korg.docs.kernel.org/patchwork/pwbot.html
#
#

__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import argparse
import datetime
import fnmatch
import hashlib
import logging
import os
import re
import smtplib
import sqlite3
import subprocess
import sys

# Send all email 8-bit, this is not 1999
from email import charset
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formatdate, getaddresses, make_msgid
from fcntl import LOCK_EX, LOCK_NB, lockf
from string import Template
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import requests
import yaml as pyyaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

charset.add_charset('utf-8', charset.SHORTEST)

__VERSION__: str = '2.0'
BOT_VERSION: str = '2026081801'
DB_VERSION: str = '2'
REST_API_VERSION: str = '1.2'
MSGID_LINK_RE: re.Pattern[str] = re.compile(
    r'^\s*Link:\s+\S+://patch\.msgid\.link/([^@]+@[^@\s/]+)$', flags=re.M | re.I
)
HUNK_RE: re.Pattern[str] = re.compile(r'^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@')
FILENAME_RE: re.Pattern[str] = re.compile(r'^(---|\+\+\+) (\S+)')
REST_PER_PAGE: int = 100

CONFIG: Dict[str, Any]

NOMAIL: bool = False
DRYRUN: bool = False
MAILHOST: str = 'localhost'
DOMAIN: str = ''
USER_AGENT: str = 'git-patchwork-helper'
CACHEDIR: str = os.path.expanduser('~/.cache/git-patchwork-bot')

_project_cache: Dict[str, Tuple[Dict[str, Any], 'Restmaker', Dict[str, Any]]] = dict()
_server_cache: Dict[str, Tuple['Restmaker', List[Dict[str, Any]]]] = dict()
_rev_cache: Dict[str, Tuple[str, str, str, Optional[str], Optional[str]]] = dict()
_upstream_commit_cache: Dict[Tuple[str, str], Optional[str]] = dict()

logger = logging.getLogger('gitpwcron')


class PatchworkFetchError(KeyError):
    """Raised when a single patchwork object cannot be retrieved.

    Subclasses KeyError so that callers written against the older getters,
    which raised a bare KeyError, keep working unchanged. Carries the HTTP
    status so callers can tell an error that is specific to *this* object from
    one that affects every request we are about to make.
    """

    def __init__(self, what: str, obj_id: Any, status: Optional[int] = None) -> None:
        self.what = what
        self.obj_id = obj_id
        self.status = status
        super().__init__('Not able to get %s %s (HTTP %s)' % (what, obj_id, status))

    @property
    def skippable(self) -> bool:
        """True when the failure is a property of this object, not of the API.

        Patchwork answers 404 for an object that is gone and 5xx when it cannot
        serialize one (e.g. a patch whose data breaks URL building). Neither
        resolves on a retry, so the only useful thing to do is step over this
        object and get on with the run. Every other status -- and a connection
        failure, which has no status at all -- says something about the API or
        the network rather than the object, so it must not be swallowed.
        """
        if self.status == 404:
            return True
        return self.status is not None and 500 <= self.status < 600

    def __str__(self) -> str:
        # KeyError.__str__ is repr(args[0]), which would wrap the message in
        # quotes; we want it to read plainly in the logs.
        return str(self.args[0])


def _status_of(ex: requests.exceptions.RequestException) -> Optional[int]:
    """HTTP status carried by a request exception, or None if it never got one."""
    if ex.response is None:
        return None
    return int(ex.response.status_code)


class CircuitOpenError(Exception):
    """Raised instead of making a request once a server has tripped its breaker.

    Patchwork answering 5xx (or not answering at all) for several requests in
    a row means the server itself is struggling, not that we hit a run of bad
    objects. Skipping each failure in turn at that point just adds more load
    to a server that is already not coping, and -- because pwrun() has no
    per-object memory of what it attempted -- would silently and permanently
    drop whatever it could not fetch. Once the breaker trips, every further
    call to that server is refused locally, with no network round trip, so
    the caller can stop cleanly instead of grinding through the rest of the
    batch one failure at a time.
    """


def reraise_unless_skippable(ex: PatchworkFetchError) -> None:
    """Return if the caller may skip this object, otherwise re-raise.

    Skipping an object patchwork cannot give us keeps the run going, which
    matters because pwrun() only saves the new repo heads once it reaches the
    end -- an escaping exception wedges the bot into re-walking, and re-failing
    on, the same revisions on every subsequent run.

    Blanket-swallowing is just as bad the other way: if the API starts refusing
    every request, skipping each object in turn would look like a clean run
    that simply found nothing to do. So anything that is not object-specific
    keeps propagating.
    """
    if not ex.skippable:
        raise ex


class Restmaker:
    server: str
    url: str
    series_url: str
    patches_url: str
    projects_url: str
    session: requests.Session
    _patches: Dict[int, Optional[Dict[str, Any]]]

    # Consecutive server-side failures (5xx or no response at all) before we
    # stop making requests to this server for the rest of the process. Kept
    # low: our own run is itself extra load on a server that is, by
    # definition, already struggling by the time this counts as "consecutive."
    CIRCUIT_BREAKER_THRESHOLD: int = 3

    def __init__(self, server: str) -> None:
        self.server = server
        self.url = '/'.join((server.rstrip('/'), 'api', REST_API_VERSION))

        self.series_url = '/'.join((self.url, 'series'))
        self.patches_url = '/'.join((self.url, 'patches'))
        self.covers_url = '/'.join((self.url, 'covers'))
        self.projects_url = '/'.join((self.url, 'projects'))

        # Simple local cache
        self._patches = dict()

        # Circuit breaker state. Shared across every project/repo that talks
        # to this server, because _server_cache hands out one Restmaker per
        # server URL for the life of the process.
        self.consecutive_failures = 0
        self.circuit_open = False

        self.session = requests.session()
        retry = Retry(connect=3, backoff_factor=0.5)
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        try:
            useragent = CONFIG['bot'].get('useragent', USER_AGENT)
        except KeyError:
            useragent = USER_AGENT

        headers = {
            'User-Agent': f'{useragent}/{__VERSION__}',
        }
        apitoken = CONFIG['patchworks'][server].get('apitoken', None)
        if not apitoken:
            logger.critical('We require an apitoken for anything to work')
            sys.exit(1)
        headers['Authorization'] = f'Token {apitoken}'
        self.session.headers.update(headers)

    def _check_circuit(self) -> None:
        if self.circuit_open:
            raise CircuitOpenError(f'Circuit open for {self.server}, refusing to make further requests')

    def _note_success(self) -> None:
        self.consecutive_failures = 0

    def _note_failure(self, status: Optional[int]) -> None:
        # A 404 (or any other non-5xx status) says something about the object
        # we asked for, not about the server, so it neither builds toward
        # tripping the breaker nor resets a streak that is already building.
        if status is not None and not (500 <= status < 600):
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.CIRCUIT_BREAKER_THRESHOLD and not self.circuit_open:
            self.circuit_open = True
            logger.critical('%d consecutive failures talking to %s, opening circuit breaker',
                            self.consecutive_failures, self.server)

    def get_unpaginated(self, url: str, params: List[Tuple[str, Any]]) -> List[Dict[str, Any]]:
        # Caller should catch RequestException
        page = 0
        results: List[Dict[str, Any]] = list()
        params.append(('per_page', REST_PER_PAGE))
        while True:
            page += 1
            logger.debug('Processing page %s', page)
            _params = list(params) + [('page', page)]
            logger.debug('Performing query: url=%s, params=%s', url, _params)
            rsp = self.session.get(url, params=_params, stream=False)
            if rsp.status_code == 404:
                logger.debug('No such page: %s', page)
                break
            rsp.raise_for_status()
            pagedata = rsp.json()
            if not pagedata:
                logger.debug('No pagedata returned, exiting fetches')
                break
            results.extend(pagedata)
            if len(pagedata) < REST_PER_PAGE:
                logger.debug('Fewer than %s returned, assuming last page', REST_PER_PAGE)
                break

        return results

    def get_cover(self, cover_id: int) -> Dict[str, Any]:
        self._check_circuit()
        try:
            logger.debug('Grabbing cover %d', cover_id)
            url = '/'.join((self.covers_url, str(cover_id), ''))
            logger.debug('url=%s', url)
            rsp = self.session.get(url, stream=False)
            rsp.raise_for_status()
            self._note_success()
            return dict(rsp.json())
        except requests.exceptions.RequestException as ex:
            logger.info('REST error: %s', ex)
            self._note_failure(_status_of(ex))
            raise PatchworkFetchError('cover', cover_id, _status_of(ex))

    def get_patch(self, patch_id: int) -> Optional[Dict[str, Any]]:
        if patch_id not in self._patches:
            self._check_circuit()
            try:
                logger.debug('Grabbing patch %d', patch_id)
                url = '/'.join((self.patches_url, str(patch_id), ''))
                logger.debug('url=%s', url)
                rsp = self.session.get(url, stream=False)
                rsp.raise_for_status()
                self._note_success()
                self._patches[patch_id] = rsp.json()
            except requests.exceptions.RequestException as ex:
                logger.info('REST error: %s', ex)
                self._note_failure(_status_of(ex))
                fetch_error = PatchworkFetchError('patch', patch_id, _status_of(ex))
                if fetch_error.skippable:
                    # Remember the miss so that a series with several patches
                    # does not re-request one patchwork has already refused.
                    # Only for skippable errors: a transient failure must not
                    # poison the cache for the rest of the run.
                    self._patches[patch_id] = None
                raise fetch_error

        return self._patches[patch_id]

    def get_series(self, series_id: int) -> Dict[str, Any]:
        self._check_circuit()
        try:
            logger.debug('Grabbing series %d', series_id)
            url = '/'.join((self.series_url, str(series_id), ''))
            logger.debug('url=%s', url)
            rsp = self.session.get(url, stream=False)
            rsp.raise_for_status()
            self._note_success()
            return dict(rsp.json())
        except requests.exceptions.RequestException as ex:
            logger.info('REST error: %s', ex)
            self._note_failure(_status_of(ex))
            raise PatchworkFetchError('series', series_id, _status_of(ex))

    def get_patches_list(self, params: List[Tuple[str, Any]],
                         unpaginated: bool = True) -> List[Dict[str, Any]]:
        try:
            if unpaginated:
                return self.get_unpaginated(self.patches_url, params)
            else:
                rsp = self.session.get(self.patches_url, params=params, stream=False)
                rsp.raise_for_status()
                return list(rsp.json())
        except requests.exceptions.RequestException as ex:
            logger.info('REST error: %s', ex)
            return list()

    def get_series_list(self, params: List[Tuple[str, Any]],
                        unpaginated: bool = True) -> List[Dict[str, Any]]:
        try:
            if unpaginated:
                return self.get_unpaginated(self.series_url, params)
            else:
                rsp = self.session.get(self.series_url, params=params, stream=False)
                rsp.raise_for_status()
                return list(rsp.json())
        except requests.exceptions.RequestException as ex:
            logger.info('REST error: %s', ex)
            return list()

    def get_list_page(self, url: str,
                      params: List[Tuple[str, Any]]) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        """Fetch a single result page, exposing the HTTP status to the caller.

        Returns an (items, status_code) tuple. On a request error, items is an
        empty list and status_code is the HTTP status if the server responded,
        or None for a connection-level failure. Unlike the get_*_list helpers,
        this does not hide the failure, so the caller can react to it (e.g.
        retry a smaller page to step over a single unserializable record).
        """
        try:
            rsp = self.session.get(url, params=params, stream=False)
            status = rsp.status_code
            rsp.raise_for_status()
            return list(rsp.json()), status
        except requests.exceptions.RequestException as ex:
            logger.debug('REST error: %s', ex)
            return list(), _status_of(ex)

    def get_projects_list(self, params: List[Tuple[str, Any]]) -> List[Dict[str, Any]]:
        try:
            return self.get_unpaginated(self.projects_url, params)
        except requests.exceptions.RequestException as ex:
            logger.info('REST error: %s', ex)
            return list()

    def update_patch(self, patch_id: int, state: Optional[str] = None, archived: bool = False,
                     commit_ref: Optional[str] = None) -> List[Tuple[str, Union[str, bool]]]:
        self._check_circuit()
        # Clear it out of the cache
        if patch_id in self._patches:
            del self._patches[patch_id]

        try:
            logger.debug('Updating patch %d:', patch_id)
            url = '/'.join((self.patches_url, str(patch_id), ''))
            logger.debug('url=%s', url)
            data: List[Tuple[str, str | bool]] = list()
            if state is not None:
                logger.debug('  state=%s', state)
                data.append(('state', state))
            if archived:
                logger.debug('  archived=True')
                data.append(('archived', True))
            if commit_ref is not None:
                logger.debug('  commit_ref=%s', commit_ref)
                data.append(('commit_ref', commit_ref))

            rsp = self.session.patch(url, data=data, stream=False)
            rsp.raise_for_status()
            self._note_success()
        except requests.exceptions.RequestException as ex:
            logger.info('REST error: %s', ex)
            self._note_failure(_status_of(ex))
            raise PatchworkFetchError('patch update', patch_id, _status_of(ex))

        return list(rsp.json())


def get_patchwork_patches_by_project_hash(rm: Restmaker, project: int, pwhash: str) -> List[int]:
    logger.debug('Looking up hash=%s', pwhash)
    params: List[Tuple[str, Any]] = [
        ('project', project),
        ('archived', 'false'),
        ('hash', pwhash),
    ]
    patches = rm.get_patches_list(params)
    if not patches:
        logger.debug('No match for hash=%s', pwhash)
        return list()

    return [patch['id'] for patch in patches]


def get_patchwork_patches_by_project_msgid(rm: Restmaker, project: int, msgid: str) -> List[int]:
    logger.debug('Looking up msgid=%s', msgid)
    params: List[Tuple[str, Union[str, int]]] = [
        ('project', project),
        ('archived', 'false'),
        ('msgid', msgid),
    ]
    patches = rm.get_patches_list(params)
    if not patches:
        logger.debug('No match for msgid=%s', msgid)
        return list()

    return [patch['id'] for patch in patches]


def get_patchwork_pull_requests_by_project(rm: Restmaker, project: int,
                                           fromstate: List[str]) -> Set[Tuple[str, str, Optional[int]]]:
    params: List[Tuple[str, Any]] = [
        ('project', project),
        ('archived', 'false'),
        ('state', fromstate),
        ('order', '-date'),
        ('q', 'PULL'),
    ]
    prs: Set[Tuple[str, str, Optional[int]]] = set()
    results = rm.get_patches_list(params)
    if not results:
        return prs
    for entry in results:
        pull_url = entry.get('pull_url')
        if pull_url:
            patch_id = entry.get('id')
            logger.info('Found pull request: %s (%s)', pull_url, patch_id)
            chunks = pull_url.split()
            pull_host = chunks[0]
            if len(chunks) > 1:
                pull_refname = chunks[1]
            else:
                pull_refname = 'master'

            prs.add((pull_host, pull_refname, patch_id))
    return prs


def project_by_name(pname: str) -> Tuple[Dict[str, Any], Restmaker, Dict[str, Any]]:
    global _project_cache
    global _server_cache

    if not pname:
        raise KeyError('Must specify project name')

    if pname not in _project_cache:
        # Find patchwork definition containing this project
        server = None
        pconfig: Optional[Any] = None
        for defurl in CONFIG['patchworks']:
            if pname in CONFIG['patchworks'][defurl]['projects']:
                server = defurl
                pconfig = CONFIG['patchworks'][defurl]['projects'][pname]
                break
        if not server or not pconfig:
            logger.critical('Could not find project matching %s in config', pname)
            sys.exit(1)

        if server not in _server_cache:
            rm = Restmaker(server)
            plist: List[Dict[str, Any]] = rm.get_projects_list(list())
            if not plist:
                logger.info('Unable to get project list on %s', server)
                sys.exit(1)

            _server_cache[server] = (rm, plist)
        else:
            rm, plist = _server_cache[server]

        found = False
        # Prefer an exact link_name match. We used to accept any link_name
        # that merely started with the configured name, but that silently
        # resolved to the wrong project whenever one name was a prefix of
        # another (e.g. "linux-mm" matching "linux-mmc", which sorts first),
        # so those patches were never processed. Fall back to the old
        # startswith behaviour only when nothing matches exactly, to stay
        # compatible with any configuration that relied on it.
        for project in plist:
            if project['link_name'].lower() == pname.lower():
                logger.debug('project lookup (exact): linkname=%s, server=%s, id=%d',
                             pname, server, project['id'])
                _project_cache[pname] = (project, rm, pconfig)
                found = True
                break
        if not found:
            for project in plist:
                if project['link_name'].lower().startswith(pname.lower()):
                    logger.debug('project lookup (prefix): linkname=%s, server=%s, id=%d',
                                 pname, server, project['id'])
                    _project_cache[pname] = (project, rm, pconfig)
                    found = True
                    break
        if not found:
            logger.info('Could not find project matching %s on server %s', pname, server)
            raise KeyError(f'No match for project {pname} on server {server}')

    return _project_cache[pname]


def db_save_meta(c: sqlite3.Cursor) -> None:
    c.execute('DELETE FROM meta')
    c.execute('''INSERT INTO meta VALUES(?)''', (DB_VERSION,))


def db_save_repo_heads(c: sqlite3.Cursor, heads: List[Tuple[str, str]]) -> None:
    c.execute('DELETE FROM heads')
    for refname, commit_id in heads:
        c.execute('''INSERT INTO heads VALUES(?,?)''', (refname, commit_id))


def db_get_repo_heads(c: sqlite3.Cursor) -> List[Tuple[str, str]]:
    return c.execute('SELECT refname, commit_id FROM heads').fetchall()


def db_ensure_pending_ops(c: sqlite3.Cursor) -> None:
    c.execute('''
        CREATE TABLE IF NOT EXISTS pending_ops (
            pname TEXT,
            refname TEXT,
            rev TEXT,
            logline TEXT,
            committer TEXT,
            pwhash TEXT,
            msgid TEXT,
            stage TEXT,
            note TEXT,
            first_failed TEXT,
            PRIMARY KEY (pname, refname, rev)
        )''')


def db_record_pending_op(c: sqlite3.Cursor, pname: str, refname: str,
                          hashpair: Tuple[str, str, str, Optional[str], Optional[str]],
                          stage: str, note: str) -> None:
    rev, logline, committer, pwhash, msgid = hashpair
    existing = c.execute(
        'SELECT first_failed FROM pending_ops WHERE pname=? AND refname=? AND rev=?',
        (pname, refname, rev)).fetchone()
    first_failed = existing[0] if existing else datetime.datetime.now(datetime.timezone.utc).isoformat()
    c.execute('''
        INSERT INTO pending_ops (pname, refname, rev, logline, committer, pwhash, msgid, stage, note, first_failed)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(pname, refname, rev) DO UPDATE SET
            logline=excluded.logline, committer=excluded.committer, pwhash=excluded.pwhash,
            msgid=excluded.msgid, stage=excluded.stage, note=excluded.note
        ''', (pname, refname, rev, logline, committer, pwhash, msgid, stage, note, first_failed))


def db_clear_pending_op(c: sqlite3.Cursor, pname: str, refname: str, rev: str) -> bool:
    c.execute('DELETE FROM pending_ops WHERE pname=? AND refname=? AND rev=?', (pname, refname, rev))
    return c.rowcount > 0


def db_get_pending_ops(c: sqlite3.Cursor,
                        pname: str) -> Dict[str, Set[Tuple[str, str, str, Optional[str], Optional[str]]]]:
    rows = c.execute(
        'SELECT refname, rev, logline, committer, pwhash, msgid FROM pending_ops WHERE pname=?',
        (pname,)).fetchall()
    by_ref: Dict[str, Set[Tuple[str, str, str, Optional[str], Optional[str]]]] = dict()
    for refname, rev, logline, committer, pwhash, msgid in rows:
        by_ref.setdefault(refname, set()).add((rev, logline, committer, pwhash, msgid))
    return by_ref


def db_init_common_sqlite_db(c: sqlite3.Cursor) -> None:
    c.execute('''
        CREATE TABLE meta (
            version INTEGER
        )''')
    db_save_meta(c)


def db_init_pw_sqlite_db(c: sqlite3.Cursor) -> None:
    logger.info('Initializing new sqlite3 db with metadata version %s', DB_VERSION)
    db_init_common_sqlite_db(c)
    c.execute('''
        CREATE TABLE heads (
            refname TEXT,
            commit_id TEXT
        )''')
    db_ensure_pending_ops(c)


def git_get_command_lines(gitdir: str, args: List[str]) -> List[str]:
    out = git_run_command(gitdir, args)
    lines: List[str] = list()
    if out:
        for line in out.split('\n'):
            if line == '':
                continue
            lines.append(line)

    return lines


def git_run_command(gitdir: str, args: List[str], stdin: Optional[str] = None) -> str:
    args = ['git', '--no-pager', '--git-dir', gitdir] + args

    logger.debug('Running %s' % ' '.join(args))

    if stdin is None:
        (output, error) = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()
    else:
        pp = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (output, error) = pp.communicate(input=stdin.encode('utf-8'))

    decoded = output.strip().decode('utf-8', errors='replace')
    if len(error.strip()):
        logger.debug('Stderr: %s', error.decode('utf-8', errors='replace'))

    return decoded


def git_get_repo_heads(gitdir: str, branch: str, ancestry: Optional[str] = None) -> List[Tuple[str, str]]:
    refs: List[Tuple[str, str]] = list()
    lines = git_get_command_lines(gitdir, ['show-ref', branch])
    if ancestry is None:
        ancestry = ''
    else:
        ancestry = f'~{ancestry}'
    if lines:
        for line in lines:
            (commit_id, refname) = line.split()
            refs.append((refname, commit_id + ancestry))
    return refs


def git_get_new_revs(gitdir: str,
                     db_heads: List[Tuple[str, str]],
                     git_heads: List[Tuple[str, str]],
                     committers: List[str],
                     merges: bool = False,
                     upstream_gitdir: Optional[str] = None,
                     upstream_ref: str = 'refs/heads/master') -> Dict[str, List[Tuple[str, str, str]]]:
    newrevs: Dict[str, List[Tuple[str, str, str]]] = dict()
    if committers:
        logger.debug('filtering by committers=%s', committers)

    upstream_commit: Optional[str] = None
    if upstream_gitdir:
        cache_key = (upstream_gitdir, upstream_ref)
        if cache_key in _upstream_commit_cache:
            upstream_commit = _upstream_commit_cache[cache_key]
        elif not os.path.isdir(upstream_gitdir):
            logger.info('Upstream repo not found: %s, proceeding without exclusion', upstream_gitdir)
            _upstream_commit_cache[cache_key] = None
        else:
            up_sha = git_run_command(upstream_gitdir, ['rev-parse', '--verify', upstream_ref])
            if not up_sha:
                logger.info('Could not resolve upstream ref %s in %s, proceeding without exclusion',
                            upstream_ref, upstream_gitdir)
                _upstream_commit_cache[cache_key] = None
            else:
                upstream_commit = up_sha
                _upstream_commit_cache[cache_key] = upstream_commit
                logger.debug('Excluding commits reachable from upstream %s (%s)',
                             upstream_ref, upstream_commit[:12])

    if upstream_commit:
        # The upstream SHA is resolved from the upstream repo, but git log runs
        # in *this* repo. When the two share an object store (e.g. grokmirror
        # alternates pointing both at one objstore), the SHA is present here and
        # exclusion works. When they do not, the SHA is absent, and
        # "git log ... ^<sha>" fails with "bad object", which
        # git_get_command_lines() returns as an empty result -- silently
        # skipping every ref. Guard against that: verify the object is present
        # and, if not, fall back to the unconstrained range (the old behavior).
        present = git_run_command(gitdir, ['rev-parse', '--verify', '--quiet',
                                           upstream_commit + '^{commit}'])
        if not present:
            logger.info('Upstream commit %s not present in %s, proceeding without exclusion',
                        upstream_commit[:12], gitdir)
            upstream_commit = None

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
        args = ['log', '--pretty=%H:%cn:%ce:%s', '--reverse']
        if not merges:
            args += ['--no-merges']

        args += [rev_range, refname]
        if upstream_commit:
            # Exclude commits already in the upstream (mainline) tree. This
            # matters after a post-merge-window rebase: the branch now contains
            # all the mainline commits as direct ancestors, so the naive
            # db_head..new_head range would include thousands of already-merged
            # commits. ^upstream_commit cuts them out, leaving only the patches
            # that are genuinely new to this branch.
            args.append('^' + upstream_commit)

        lines = git_get_command_lines(gitdir, args)
        if not lines:
            continue

        revs: List[Tuple[str, str, str]] = list()

        for line in lines:
            (commit_id, cn, ce, logmsg) = line.split(':', 3)
            if committers and ce not in committers:
                logger.debug('Skipping %s, ce=%s', commit_id, ce)
                continue
            if len(cn):
                committer = '%s <%s>' % (cn, ce)
            else:
                committer = ce
            logger.debug('commit_id=%s, committer=%s, subject=%s', commit_id, committer, logmsg)
            revs.append((commit_id, logmsg, committer))

        if revs:
            newrevs[refname] = revs

    return newrevs


def git_get_rev_info(gitdir: str, rev: str, algorithm: str = 'myers') -> str:
    args = ['show', f'--diff-algorithm={algorithm}', rev]
    return git_run_command(gitdir, args)


def git_get_patch_id(diff: str) -> Optional[str]:
    args = ['patch-id', '--stable']
    out = git_run_command('', args, stdin=diff)
    logger.debug('out=%s', out)
    if not out:
        return None
    return out.split()[0]


def get_patchwork_hash(diff: str) -> str:
    """Generate a hash from a diff. Lifted near verbatim from patchwork."""

    # normalise spaces
    diff = diff.replace('\r', '')
    diff = diff.strip() + '\n'

    prefixes = ['-', '+', ' ']
    hashed = hashlib.sha1()

    inpatch = False
    for line in diff.split('\n'):
        if len(line) <= 0:
            continue
        # Ignore any content before "^diff "
        if not inpatch and not line.startswith('diff '):
            continue

        inpatch = True

        hunk_match = HUNK_RE.match(line)
        filename_match = FILENAME_RE.match(line)

        if filename_match:
            # normalise -p1 top-directories
            if filename_match.group(1) == '---':
                filename = 'a/'
            else:
                filename = 'b/'
            filename += '/'.join(filename_match.group(2).split('/')[1:])

            line = filename_match.group(1) + ' ' + filename
        elif hunk_match:
            # remove line numbers, but leave line counts
            def fn(x: Union[str, int]) -> int:
                if not x:
                    return 1
                return int(x)
            line_nos = list(map(fn, hunk_match.groups()))
            line = '@@ -%d +%d @@' % tuple(line_nos)
        elif line[0] in prefixes:
            # if we have a +, - or context line, leave as-is
            pass
        else:
            # other lines are ignored
            continue

        hashed.update((line + '\n').encode('utf-8'))

    return hashed.hexdigest()


def listify(obj: Union[str, List[Any], None]) -> List[Any]:
    if isinstance(obj, list):
        return list(obj)
    return [obj]


def get_url_path(url: str) -> str:
    """Extract the path portion from a git URL for comparison.

    Handles protocol differences (git://, https://, http://) and
    subdomain variations (git.kernel.org vs gitolite.kernel.org)
    by returning just the path component. Strips trailing slashes
    and .git suffixes to normalize for comparison.
    """
    # Strip protocol prefix
    path = re.sub(r'^\w+://', '', url)
    # Strip hostname (everything up to first /)
    if '/' in path:
        path = path.split('/', 1)[1]
    path = path.rstrip('/')
    if path.endswith('.git'):
        path = path[:-4]
    return path


def parse_merge_logline(logline: str) -> Optional[Tuple[str, str]]:
    """Parse a merge commit subject line to extract the ref and URL.

    Returns a (refname, url) tuple, or None if the logline is not
    a recognizable merge commit.
    """
    if logline.find(' of ') > 0:
        matches = re.search(r'Merge\s\S+\s[\'\"](\S+)[\'\"]\sof\s(\w+://\S+)', logline)
        if not matches:
            return None
        return matches.group(1), matches.group(2)
    elif logline.find('://') > 0:
        matches = re.search(r'Merge\s(\w+://\S+)', logline)
        if not matches:
            return None
        return 'master', matches.group(1)
    return None


def find_matching_pr(m_host: str, m_refname: str,
                     prs: Set[Tuple[str, str, Optional[int]]]) -> Optional[int]:
    """Find a pull request matching a merge commit's URL and ref.

    Compares the path portion of URLs (ignoring protocol, hostname,
    trailing slashes, and .git suffixes) and checks that the ref name
    appears in the pull request ref. Returns the matching patch_id,
    or None if no match is found.
    """
    m_path = get_url_path(m_host)
    for pull_host, pull_refname, patch_id in prs:
        if get_url_path(pull_host) == m_path and pull_refname.find(m_refname) > -1 and patch_id is not None:
            return patch_id
    return None


def send_summary(serieslist: List[Dict[str, Any]],
                 committers: Dict[int, str],
                 to_state: str, refname: str,
                 revs: Dict[int, str],
                 pname: str,
                 rs: Dict[str, str],
                 hs: Dict[str, str]) -> str:
    logger.info('Preparing summary')
    # we send summaries by project, so the project name is going to be all the same

    count = 0
    summary: List[str] = list()
    for sdata in serieslist:
        count += 1
        logger.debug('Summarizing: %s', sdata.get('name'))

        # If we have a cover letter, then the reference is the msgid of the cover letter,
        # else the reference is the msgid of the first patch
        patches: List[Dict[str, Any]] = sdata.get('patches', list())

        submitter: Dict[str, Any] = sdata.get('submitter', list())

        if len(patches) == 1:
            summary.append('Patch: %s' % sdata.get('name'))
        else:
            summary.append('Series: %s' % sdata.get('name'))

        summary.append('  Submitter: %s <%s>' % (submitter.get('name'), submitter.get('email')))
        pid = patches[0].get('id')
        if pid in committers:
            summary.append('  Committer: %s' % committers[pid])
        summary.append('  Patchwork: %s' % sdata.get('web_url'))

        if sdata.get('cover_letter'):
            msgid = sdata.get('cover_letter', dict()).get('msgid').strip('<>')
        else:
            msgid = patches[0].get('msgid', '').strip('<>')

        link = 'https://lore.kernel.org/r/%s' % msgid
        summary.append('  Lore link: %s' % link)

        if len(patches) > 1:
            summary.append('    Patches: %s' % patches[0].get('name'))
            for patch in patches[1:]:
                pid = patch.get('id')
                if pid in revs:
                    count += 1
                    summary.append('             %s' % patch.get('name'))
        summary.append('')

    bodytpt = Template(CONFIG['templates']['summary'])
    params: Dict[str, str] = {
        'newstate': to_state,
        'treename': rs['treename'],
        'refname': refname.replace('refs/heads/', '', 1),
        'summary': '\n'.join(summary),
        'total': str(count),
        'signature': CONFIG['templates']['signature'],
    }
    body = bodytpt.safe_substitute(params)

    _, _, pconfig = project_by_name(pname)
    tweaks = get_tweaks(pconfig, hs)

    msg = MIMEText(body, _charset='utf-8')
    msg.replace_header('Content-Transfer-Encoding', '8bit')

    msg['Subject'] = Header('Patchwork summary for: %s' % pname).encode()
    msg['From'] = Header(tweaks['from']).encode()
    msg['Message-Id'] = make_msgid('git-patchwork-summary', domain=DOMAIN)
    msg['Date'] = formatdate(localtime=True)

    targets = listify(tweaks['summaryto'])
    msg['To'] = Header(', '.join(targets)).encode()
    if 'alwayscc' in tweaks:
        msg['Cc'] = Header(', '.join(listify(tweaks['alwayscc']))).encode()
        targets.append(listify(tweaks['alwayscc']))
    if 'alwaysbcc' in tweaks:
        targets.append(listify(tweaks['alwaysbcc']))

    if not NOMAIL:
        logger.debug('Message follows')
        logger.debug(msg.as_string())
        logger.info('Sending summary to: %s', msg['To'])

        smtp = smtplib.SMTP(MAILHOST)
        smtp.sendmail(tweaks['from'], targets, msg.as_bytes())
        smtp.close()
    else:
        logger.info('Would have sent the following:')
        logger.info('------------------------------')
        logger.info(msg.as_string())
        logger.info('------------------------------')

    return str(msg['Message-Id'])


def get_tweaks(pconfig: Dict[str, str], hconfig: Dict[str, Any]) -> Dict[str, str]:
    fields = ['from', 'summaryto', 'onlyto', 'neverto', 'onlyifcc', 'neverifcc',
              'alwayscc', 'alwaysbcc', 'cclist', 'ccall']
    bubbled: Dict[str, str] = dict()
    for field in fields:
        if field in hconfig:
            bubbled[field] = hconfig[field]
            continue
        if field in pconfig:
            bubbled[field] = pconfig[field]
    return bubbled


def notify_submitters(serieslist: List[Dict[str, Any]],
                      committers: Dict[int, str],
                      refname: str,
                      revs: Dict[int, str],
                      pname: str,
                      rs: Dict[str, Any],
                      hs: Dict[str, Any]) -> None:
    logger.info('Sending submitter notifications')
    project, rm, pconfig = project_by_name(pname)

    tweaks = get_tweaks(pconfig, hs)

    for sdata in serieslist:
        # If we have a cover letter, then the reference is the msgid of the cover letter,
        # else the reference is the msgid of the first patch
        patches = sdata.get('patches', list())
        is_pull_request = False
        content: str = ''
        headers: Dict[str, Any] = dict()
        reference: str = ''
        if sdata.get('cover_letter'):
            reference = sdata.get('cover_letter', dict()).get('msgid')
            try:
                fullcover = rm.get_cover(sdata.get('cover_letter', dict()).get('id'))
                headers = {k.lower(): v for k, v in fullcover.get('headers', dict()).items()}
                content = fullcover.get('content', '')
            except PatchworkFetchError as ex:
                reraise_unless_skippable(ex)
                logger.debug('Unable to get cover letter (%s), will try first patch', ex)
        if not reference:
            reference = patches[0].get('msgid')
            try:
                fullpatch = rm.get_patch(patches[0].get('id'))
                if fullpatch is not None:
                    headers = {k.lower(): v for k, v in fullpatch.get('headers', dict()).items()}
                    content = fullpatch.get('content', '')
                    if fullpatch.get('pull_url'):
                        is_pull_request = True
            except PatchworkFetchError as ex:
                reraise_unless_skippable(ex)
                logger.debug('Unable to get first patch reference (%s), bailing on %s', ex, sdata.get('id'))
                continue

        submitter = sdata.get('submitter', dict())
        project = sdata.get('project', dict())

        if 'neverto' in tweaks:
            neverto = listify(tweaks['neverto'])
            if submitter.get('email') in neverto:
                logger.debug('Skipping neverto address:%s', submitter.get('email'))
                continue

        ccs: List[str] = list()
        cchdr = headers.get('cc')
        if cchdr:
            ccs = [chunk[1] for chunk in getaddresses(listify(cchdr))]
        tos: List[str] = list()
        tohdr = headers.get('to')
        if tohdr:
            tos = [chunk[1] for chunk in getaddresses(listify(tohdr))]

        xpb = headers.get('x-patchwork-bot')
        logger.debug('X-Patchwork-Bot=%s', xpb)
        # If X-Patchwork-Bot header is set to "notify" we always notify
        if xpb != 'notify':
            # Use cc-based notification logic
            if 'onlyifcc' in tweaks:
                match = None
                for chunk in listify(tweaks['onlyifcc']):
                    if chunk in ccs:
                        match = chunk
                        break
                if match is None:
                    logger.debug('Skipping %s due to onlyifcc=%s', submitter.get('email'), tweaks['onlyifcc'])
                    continue

            if ccs and 'neverifcc' in tweaks:
                match = None
                for chunk in listify(tweaks['neverifcc']):
                    if chunk in ccs:
                        match = chunk
                        break
                if match is not None:
                    logger.debug('Skipping %s due to neverifcc=%s', submitter.get('email'), tweaks['neverifcc'])
                    continue

        logger.debug('Preparing a notification for %s', submitter.get('email'))
        if is_pull_request:
            reqtype = 'pull request'
        elif len(patches) > 1:
            reqtype = 'series'
        else:
            reqtype = 'patch'

        trimquote: List[str] = list()
        if content:
            qcount = 0
            for cline in content.split('\n'):
                # Quote the first paragraph only and then [snip] if we quoted more than 5 lines
                if qcount > 5 and (not len(cline.strip()) or cline.strip().find('---') == 0):
                    trimquote.append('> ')
                    trimquote.append('> [...]')
                    break
                trimquote.append('> %s' % cline.rstrip())
                qcount += 1

        summary: List[str] = list()
        committer = 'unknown committer'
        for patch in patches:
            summary.append('  - %s' % patch.get('name'))
            pid = patch.get('id')
            if pid in revs:
                committer = committers.get(pid, 'unknown committer')
                if 'commitlink' in rs:
                    summary.append('    %s' % (rs['commitlink'] % revs[pid]))
            else:
                summary.append('    (no matching commit)')

        bodytpt = Template(CONFIG['templates']['submitter'])
        params: Dict[str, str] = {
            'reqtype': reqtype,
            'treename': rs['treename'],
            'refname': refname.replace('refs/heads/', '', 1),
            'committer': committer,
            'sentdate': str(headers.get('date')),
            'trimquote': '\n'.join(trimquote),
            'summary': '\n'.join(summary),
            'signature': CONFIG['templates']['signature'],
        }

        body = bodytpt.safe_substitute(params)

        msg = MIMEText(body, _charset='utf-8')
        msg.replace_header('Content-Transfer-Encoding', '8bit')

        msg['Subject'] = Header('Re: %s' % headers.get('subject')).encode()
        msg['From'] = Header(tweaks['from']).encode()
        msg['Message-Id'] = make_msgid('git-patchwork-notify', domain=DOMAIN)
        msg['Date'] = formatdate(localtime=True)
        msg['References'] = Header(reference).encode()
        msg['In-Reply-To'] = Header(reference).encode()

        if 'onlyto' in tweaks:
            targets = listify(tweaks['onlyto'])
            msg['To'] = '%s <%s>' % (submitter.get('name'), targets[0])
        else:
            targets = [submitter.get('email')]
            msg['To'] = Header('%s <%s>' % (submitter.get('name'), submitter.get('email'))).encode()

            ccaddrs: List[str] = list()
            if tweaks.get('alwayscc'):
                ccaddrs += listify(tweaks['alwayscc'])
                targets += ccaddrs
            if tweaks.get('cclist'):
                ccaddrs.append(project.get('list_email'))
                targets.append(project.get('list_email'))
            if tweaks.get('ccall'):
                for addr in tos + ccs:
                    if addr not in targets:
                        targets.append(addr)
                        ccaddrs.append(addr)

            if 'alwaysbcc' in tweaks:
                targets += listify(tweaks['alwaysbcc'])

            if len(ccaddrs):
                msg['Cc'] = ', '.join(ccaddrs)

        if not NOMAIL:
            logger.debug('Message follows')
            logger.debug(msg.as_string())
            logger.info('Notifying %s', submitter.get('email'))

            smtp = smtplib.SMTP(MAILHOST)
            smtp.sendmail(tweaks['from'], targets, msg.as_bytes())
            smtp.close()
        else:
            logger.info('Would have sent the following:')
            logger.info('------------------------------')
            logger.info(msg.as_string())
            logger.info('------------------------------')


def get_project_series_since(rm: Restmaker, project_id: int, since_iso: str) -> List[Dict[str, Any]]:
    """Return all series for a project with a date at or after since_iso.

    Patchwork returns HTTP 500 for an *entire* page if it cannot serialize even
    a single series in it (e.g. a patch whose data breaks URL building). With a
    plain "newest first" query that bad series lands on the first page, so the
    whole autosupersede run for the project silently fetches nothing.

    To stay robust, we order by id (stable, unlike date which can tie) and page
    through ourselves. When a page fails, we re-fetch it one series at a time so
    we can skip just the series patchwork chokes on and still process the rest.
    """
    collected: List[Dict[str, Any]] = list()
    page = 1
    while True:
        params: List[Tuple[str, Any]] = [
            ('project', project_id),
            ('since', since_iso),
            ('order', 'id'),
            ('page', page),
            ('per_page', REST_PER_PAGE),
        ]
        items, status = rm.get_list_page(rm.series_url, params)
        if status == 200:
            collected.extend(items)
            if len(items) < REST_PER_PAGE:
                break
            page += 1
            continue
        if status == 404:
            # Patchwork returns 404 for a page past the last one. That is simply
            # the end of the results (the same signal get_unpaginated() relies
            # on), not an unserializable series, so stop rather than trying to
            # isolate a page that does not exist.
            break
        if status is None:
            logger.info('Could not fetch series page %d for project %d, stopping', page, project_id)
            break
        if not 500 <= status < 600:
            # Only a server error (5xx) means patchwork choked on serializing a
            # series. Anything else unexpected (auth, throttling, ...) is not
            # something we can step over one series at a time, so don't hammer
            # the API; just stop.
            logger.info('Series page %d for project %d returned HTTP %d, stopping',
                        page, project_id, status)
            break

        # A series on this page cannot be serialized by patchwork. Re-fetch the
        # page one series at a time so a single bad series does not block the
        # rest. per_page=1 page numbers index into the same id-ordered list.
        logger.info('Series page %d for project %d returned HTTP %d; '
                    'isolating the unserializable series', page, project_id, status)
        base = (page - 1) * REST_PER_PAGE
        reached_end = False
        for offset in range(REST_PER_PAGE):
            one_params: List[Tuple[str, Any]] = [
                ('project', project_id),
                ('since', since_iso),
                ('order', 'id'),
                ('page', base + offset + 1),
                ('per_page', 1),
            ]
            one, one_status = rm.get_list_page(rm.series_url, one_params)
            if one_status == 200:
                if not one:
                    reached_end = True
                    break
                collected.extend(one)
            elif one_status == 404:
                # Walked past the last series in the result set; we're done.
                reached_end = True
                break
            elif one_status is not None and 500 <= one_status < 600:
                logger.info('  Skipping unserializable series at index %d (project %d, HTTP %s)',
                            base + offset, project_id, one_status)
            else:
                # Connection error or some other unexpected status: stop rather
                # than spinning over the rest of the page.
                logger.info('  Could not fetch series at index %d (project %d, HTTP %s), stopping',
                            base + offset, project_id, one_status)
                reached_end = True
                break
        if reached_end:
            break
        page += 1

    return collected


def housekeeping(pname: str) -> Dict[str, int]:
    hk_stats = {'superseded': 0, 'archived': 0}

    project, rm, pconfig = project_by_name(pname)
    if 'housekeeping' not in pconfig:
        return hk_stats

    project_id = project['id']

    logger.info('Running housekeeping for %s', pname)
    hconfig = pconfig['housekeeping']
    cutoffdays = 90
    report = ''

    if 'autosupersede' in hconfig:
        logger.info('Getting series from %s/%s', rm.server, pname)
        try:
            cutoffdays = int(hconfig['autosupersede'])
        except ValueError:
            pass

        cutoffdate = datetime.datetime.now() - datetime.timedelta(days=cutoffdays)
        logger.debug('cutoffdate=%s', cutoffdate)
        series: Dict[Tuple[str, str], Dict[datetime.datetime, Any]] = dict()
        # Let patchwork filter by date for us with "since"; this keeps the query
        # small and lets get_project_series_since() page through resiliently, so
        # a single series patchwork cannot serialize does not stall the project.
        for entry in get_project_series_since(rm, project_id, cutoffdate.isoformat()):
            s_date = entry.get('date')
            if not s_date:
                continue
            # fromisoformat copes with optional microseconds; strptime with a
            # fixed format would raise on timestamps that carry them.
            series_date = datetime.datetime.fromisoformat(s_date)

            s_id = entry.get('id')
            s_name = entry.get('name')
            if s_name is None:
                # Ignoring this one, because we must have a name
                continue

            # Remove any [foo] from the front, for best matching.
            # Usually, patchwork strips these, but not always.
            s_name = re.sub(r'^\[.*?]\s*', '', s_name)

            ver = entry.get('version')
            submitter: Any = entry.get('submitter') or dict()
            subm_id = submitter.get('id')
            patches = [patch.get('id') for patch in entry.get('patches', list())]

            if not patches:
                # Not sure how we can have a series without patches, but ok
                continue

            received_all = entry.get('received_all')
            if (subm_id, s_name) not in series:
                series[(subm_id, s_name)] = dict()

            series[(subm_id, s_name)][series_date] = {
                'id': s_id,
                'patches': patches,
                'complete': received_all,
                'date': s_date,
                'rev': ver,
            }
            logger.debug('Processed id=%s (%s)', s_id, s_name)

        for key, items in series.items():
            if len(items) < 2:
                # Not a redundant series
                continue

            subm_id, subject = key
            versions = list(items.keys())
            versions.sort()
            latest_version = versions.pop()
            logger.debug('%s: latest_version: %s', subject, items[latest_version]['date'])
            if not items[latest_version]['complete']:
                logger.debug('Skipping this series, because it is not complete')
                continue

            sreport: List[str] = list()
            logger.info('Checking: [v%s] %s (%s)', items[latest_version]['rev'], subject,
                        items[latest_version]['date'])
            for v in versions:
                rev = items[v]['rev']
                s_date = items[v]['date']
                patch_id = items[v]['patches'][0]
                try:
                    patch = rm.get_patch(patch_id)
                except PatchworkFetchError as ex:
                    reraise_unless_skippable(ex)
                    logger.info('  Skipping unavailable patch %s: %s', patch_id, ex)
                    continue
                if not patch:
                    # Huh, what happened?
                    continue
                state = patch.get('state')
                if state != 'superseded':
                    logger.info('  Marking series as superseded: [v%s] %s (%s)', rev, subject, s_date)
                    sreport.append('  Superseding: [v%s] %s (%s):' % (rev, subject, s_date))
                    # Yes, we need to supersede these patches
                    for patch_id in items[v]['patches']:
                        logger.info('    Superseding patch: %d', patch_id)
                        try:
                            patch = rm.get_patch(patch_id)
                        except PatchworkFetchError as ex:
                            reraise_unless_skippable(ex)
                            logger.info('    Skipping unavailable patch %s: %s', patch_id, ex)
                            continue
                        if patch is None:
                            continue
                        patch_title = patch.get('name')
                        current_state = patch.get('state')
                        if current_state == 'superseded':
                            logger.info('    Patch already set to superseded, skipping')
                            continue
                        sreport.append('    %s' % patch_title)
                        hk_stats['superseded'] += 1
                        if not DRYRUN:
                            rm.update_patch(patch_id, state='superseded')
                        else:
                            logger.info('    Dryrun: Not actually setting state')

            if sreport:
                report += 'Latest series: [v%s] %s (%s)\n' % (items[latest_version]['rev'], subject,
                                                              items[latest_version]['date'])
                report += '\n'.join(sreport)
                report += '\n\n'

    if 'autoarchive' in hconfig:
        logger.info('Auto-archiving old patches in %s/%s', rm.server, pname)
        try:
            cutoffdays = int(hconfig['autoarchive'])
        except ValueError:
            pass

        cutoffdate = datetime.datetime.now() - datetime.timedelta(days=cutoffdays)
        logger.debug('cutoffdate=%s', cutoffdate)

        page = 0
        seen: Set[str] = set()
        pagedata: List[Any] = list()
        archived = 0
        while True:
            if not pagedata:
                if archived:
                    logger.info('Archived %d patches, grabbing next page', archived)
                params = [
                    ('project', project_id),
                    ('archived', 'false'),
                    ('state', 'new'),
                    # Let patchwork do the date filtering for us. The "before"
                    # filter (lookup_expr=lt on the patch date) has been part of
                    # the REST API since well before our pinned v1.2, so every
                    # patch the server hands back is already old enough to
                    # archive. This avoids parsing dates on our side, which used
                    # to choke on ISO timestamps that carried microseconds.
                    ('before', cutoffdate.isoformat()),
                    ('order', 'date'),
                    ('per_page', REST_PER_PAGE)
                ]

                if DRYRUN:
                    # We don't need pagination if we're not in dryrun, because
                    # once we archive the patches, they don't show up in this
                    # query any longer. In dryrun we never actually archive, so
                    # we have to walk the pages ourselves.
                    page += 1
                    params.append(('page', page))

                # we do our own pagination
                pagedata = rm.get_patches_list(params, unpaginated=False)

            if not pagedata:
                logger.debug('Finished processing all patches')
                break

            entry = pagedata.pop()
            patch_id = entry.get('id')
            if patch_id is None:
                logger.debug('No patch ID for entry %s, skipping', entry)
                continue
            if patch_id in seen:
                # If the archived setting isn't actually sticking on the server for
                # some reason, then we are in for an infinite loop. Recognize this
                # and quit when that happens.
                logger.info('Setting to archived is not working, exiting loop.')
                break

            seen.add(patch_id)
            archived += 1
            if not DRYRUN:
                rm.update_patch(patch_id, archived=True)
            else:
                logger.info('  Dryrun: Not actually archiving')

        hk_stats['archived'] = archived
        if archived:
            logger.info('Archived %d total patches', archived)

        if not report:
            return hk_stats

        if 'summaryto' not in pconfig:
            logger.info('Report follows')
            logger.info('------------------------------')
            logger.info(report)
            logger.info('------------------------------')
            logger.debug('summaryto not set, not sending report')
            return hk_stats

        report += '\n-- \n' + CONFIG['templates']['signature']

        msg = MIMEText(report, _charset='utf-8')
        msg.replace_header('Content-Transfer-Encoding', '8bit')

        msg['Subject'] = 'Patchwork housekeeping for: %s' % pname
        msg['From'] = pconfig['from']
        msg['Message-Id'] = make_msgid('git-patchwork-housekeeping', domain=DOMAIN)
        msg['Date'] = formatdate(localtime=True)

        targets = listify(pconfig['summaryto'])
        msg['To'] = ', '.join(targets)
        if 'alwayscc' in pconfig:
            msg['Cc'] = ', '.join(listify(pconfig['alwayscc']))
            targets += listify(pconfig['alwayscc'])
        if 'alwaysbcc' in pconfig:
            targets += listify(pconfig['alwaysbcc'])

        if not NOMAIL:
            logger.debug('Message follows')
            logger.debug(msg.as_string())
            logger.info('Sending housekeeping summary to: %s', msg['To'])

            smtp = smtplib.SMTP(MAILHOST)
            smtp.sendmail(pconfig['from'], targets, msg.as_bytes())
            smtp.close()
        else:
            logger.info('Would have sent the following:')
            logger.info('------------------------------')
            logger.info(msg.as_string())
            logger.info('------------------------------')

    return hk_stats


def pwrun(repo: str, rsettings: Dict[str, Any]) -> Dict[str, int]:
    global _rev_cache

    stats: Dict[str, int] = {
        'patches_updated': 0,
        'refs_held_back': 0,
        'pending_recorded': 0,
        'pending_cleared': 0,
    }

    branch = rsettings.get('branch', '--heads')
    if not isinstance(branch, str):
        logger.critical('Branch must be a string')
        sys.exit(1)

    git_heads = git_get_repo_heads(repo, branch=branch)
    if not git_heads:
        logger.info('Could not get the latest ref in %s', repo)
        sys.exit(1)

    dbpath = repo
    # If we're aimed at a worktree, move up from the ".git" file to
    # the worktree directory.
    if not os.path.isdir(dbpath):
        gitdir = open(dbpath).readline().strip()
        if not gitdir.startswith('gitdir: '):
            logger.info('Could not find git tree in %s', dbpath)
            sys.exit(1)
        gitdir = gitdir.split(' ', 1)[1]
        gitdir, _ = os.path.split(gitdir)
        gitdir, category = os.path.split(gitdir)
        if category != "worktrees":
            logger.info('Could not find git worktree in %s', dbpath)
            sys.exit(1)
        # To store multiple pw.db files in a single .git directory,
        # add a suffix based on the repo treename.
        treename = rsettings.get('treename')
        if not isinstance(treename, str):
            logger.critical('treename must be a string')
            sys.exit(1)
        treename = treename.replace('/', '_')
        dbpath = os.path.join(gitdir, f'pw-{treename}.db')
    else:
        dbpath = os.path.join(dbpath, 'pw.db')

    # Do we have a pw.db there yet?
    db_exists = os.path.isfile(dbpath)
    dbconn = sqlite3.connect(dbpath, sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    c = dbconn.cursor()
    db_ensure_pending_ops(c)

    if not db_exists:
        db_init_pw_sqlite_db(c)
        initial_git_heads = git_get_repo_heads(repo, branch=rsettings.get('branch', '--heads'),
                                               ancestry=cmdargs.ancestors)
        db_save_repo_heads(c, initial_git_heads)
        # Exit early
        dbconn.commit()
        return stats

    db_heads = db_get_repo_heads(c)
    # git_get_new_revs() mutates db_heads (drops rows for refs gone from
    # git), so snapshot the old commit_id per ref now -- it is what we fall
    # back to for any ref the circuit breaker interrupts below.
    db_heads_by_ref: Dict[str, str] = dict(db_heads)
    climit = rsettings.get('committers', list())

    upstream_gitdir: Optional[str] = None
    upstream_ref = 'refs/heads/master'
    upstream_str = rsettings.get('upstream')
    if upstream_str and isinstance(cmdargs.reposdir, str):
        parts = upstream_str.split(None, 1)
        upstream_path = parts[0]
        if len(parts) > 1:
            upstream_ref = parts[1]
        upstream_gitdir = os.path.join(cmdargs.reposdir.rstrip('/'), upstream_path.lstrip('/'))

    newrevs = git_get_new_revs(repo, db_heads, git_heads, committers=climit, merges=True,
                                upstream_gitdir=upstream_gitdir, upstream_ref=upstream_ref)
    have_pending_ops = c.execute('SELECT 1 FROM pending_ops LIMIT 1').fetchone() is not None
    if not newrevs and not have_pending_ops:
        # Save current heads if we limited by committers
        if climit and not DRYRUN:
            db_save_repo_heads(c, git_heads)
            dbconn.commit()
        logger.debug('No new revs in %s', repo)
        return stats

    logger.info('Processing: %s', repo)

    count = 0
    # Refs whose processing was interrupted by an open circuit breaker.
    # Their heads must not advance -- see the end-of-run head-save below.
    failed_refs: Set[str] = set()
    for pname, psettings in rsettings['projects'].items():
        rpwhashes: Dict[str, Set[Tuple[str, str, str, Optional[str], Optional[str]]]] = dict()
        wantstates: List[str] = list()
        have_prs = False
        for refname, revlines in newrevs.items():
            found = False
            for wanthead, hsettings in psettings.items():
                # Check endswith, but also do fnmatch matching for wildcards
                if refname.endswith(wanthead) or fnmatch.fnmatch(refname, wanthead):
                    found = True
                    if 'fromstate' in hsettings:
                        wantstates += hsettings['fromstate']
                    break
            if not found:
                logger.debug('Skipping ref %s (not wanted)')
                continue

            rpwhashes[refname] = set()
            for rev, logline, committer in revlines:
                if logline.find('Merge') == 0 and logline.find('://') > 0:
                    have_prs = True
                    rpwhashes[refname].add((rev, logline, committer, None, None))
                    continue

                if rev not in _rev_cache:
                    info = git_get_rev_info(repo, rev)
                    rpwhash = get_patchwork_hash(info)
                    if not rpwhash:
                        # Theoretically, should never happen?
                        logger.debug('Skipping %s (no pwhash)', rev)
                        continue
                    msgid = None
                    lore_match = MSGID_LINK_RE.search(info)
                    if lore_match:
                        msgid = lore_match.group(1)
                        logger.debug('Msgid for %s: %s', rev, msgid)
                    _rev_cache[rev] = (rev, logline, committer, rpwhash, msgid)

                rpwhashes[refname].add(_rev_cache[rev])

        # Re-inject revs that hit an isolated failure on a previous run
        # (breaker not tripped at the time) so they get retried alongside
        # anything freshly discovered this run.
        for p_refname, pending_hashpairs in db_get_pending_ops(c, pname).items():
            rpwhashes.setdefault(p_refname, set()).update(pending_hashpairs)

        if not wantstates:
            wantstates = ['new', 'under-review']

        logger.debug('wantstates=%s', wantstates)

        logger.info(' project : %s', pname)
        project, rm, _ = project_by_name(pname)
        project_id = project['id']

        prs: Set[Tuple[str, str, int | None]] = set()
        if have_prs:
            logger.info('  PR merge commit found, loading up pull requests')
            # Find all from states we're interested in
            prs = get_patchwork_pull_requests_by_project(rm, project_id, wantstates)
        else:
            prs = set()

        for refname, hashpairs in rpwhashes.items():
            if rm.circuit_open:
                logger.warning('  Circuit open for %s, holding back head for %s this run', rm.server, refname)
                failed_refs.add(refname)
                continue
            try:
                logger.info('  Analyzing %d revisions in %s', len(hashpairs), refname)
                # Per-rev ledger bookkeeping for isolated (non-systemic) failures --
                # a rev that raises here gets a pending_ops row so a later run
                # retries just that rev, instead of us re-walking the whole ref.
                rev_hashpair: Dict[str, Tuple[str, str, str, Optional[str], Optional[str]]] = {
                    hp[0]: hp for hp in hashpairs
                }
                revs_last_stage: Dict[str, Tuple[str, str]] = dict()
                # Get our settings
                hsettings = None
                for wanthead, hsettings in psettings.items():
                    if refname.endswith(wanthead) or fnmatch.fnmatch(refname, wanthead):
                        break
                if hsettings is None:
                    continue
                # Patchwork lowercases state name and replaces spaces with dashes
                to_state = hsettings['tostate'].lower().replace(' ', '-')
                fromstate: List[str] = list()
                for fs in hsettings.get('fromstate', list()):
                    fromstate.append(fs.lower().replace(' ', '-'))
                if not fromstate:
                    fromstate = list(wantstates)

                # We create patch_id->rev mapping first
                revs: Dict[int, str] = dict()
                committers: Dict[int, str] = dict()
                for rev, logline, committer, pwhash, msgid in hashpairs:
                    if have_prs and pwhash is None:
                        parsed = parse_merge_logline(logline)
                        if parsed is None:
                            continue
                        m_refname, m_host = parsed

                        logger.debug('Looking for ref %s host %s', m_refname, m_host)

                        patch_id = find_matching_pr(m_host, m_refname, prs)
                        if patch_id is not None:
                            logger.info('  Found matching pull request in %s (id: %s)', logline, patch_id)
                            revs[patch_id] = rev
                            committers[patch_id] = committer
                        continue

                    # Do we have a matching hash on the server?
                    if pwhash is None:
                        logger.info('  No pwhash for %s, skipping', rev)
                        continue
                    logger.info('  Matching by hash: %s (%s)', pwhash, logline)
                    patch_ids = get_patchwork_patches_by_project_hash(rm, project_id, pwhash)
                    if not patch_ids and msgid:
                        # Match by message-id, if we have it
                        logger.info('  Matching by msgid: %s (%s)', msgid, logline)
                        patch_ids = get_patchwork_patches_by_project_msgid(rm, project_id, msgid)
                    if not patch_ids:
                        logger.info('  Regenerating %s using --histogram diff algorithm', rev)
                        info = git_get_rev_info(repo, rev, algorithm='histogram')
                        hpwhash = get_patchwork_hash(info)
                        if hpwhash != pwhash:
                            logger.info('  Matching by --histogram hash: %s (%s)', hpwhash, logline)
                            patch_ids = get_patchwork_patches_by_project_hash(rm, project_id, hpwhash)
                        else:
                            logger.info('    diff --histogram resulted in the same hash, ignoring')
                    if not patch_ids:
                        logger.info('    No match for: %s', logline)
                        continue

                    for patch_id in patch_ids:
                        logger.info('   Matched: %s', patch_id)
                        try:
                            pdata = rm.get_patch(patch_id)
                        except PatchworkFetchError as ex:
                            reraise_unless_skippable(ex)
                            logger.info('   Ignoring unavailable patch: %s', ex)
                            revs_last_stage[rev] = ('match', str(ex))
                            continue
                        if not pdata:
                            logger.info('   Ignoring due to REST error')
                            continue
                        if pdata.get('state') not in fromstate:
                            logger.info('   Ignoring due to state=%s', pdata.get('state'))
                            continue
                        revs[patch_id] = rev
                        committers[patch_id] = committer

                # Now we iterate through it
                updated_series: List[Dict[str, Any]] = list()
                done_patches: Set[int] = set()
                for patch_id in list(revs.keys()):
                    logger.info('   Processing: %s', patch_id)
                    if patch_id in done_patches:
                        # we've already updated this series
                        logger.info('    Already applied as part of previous series')
                        continue
                    try:
                        pdata = rm.get_patch(patch_id)
                    except PatchworkFetchError as ex:
                        reraise_unless_skippable(ex)
                        logger.info('    Skipping unavailable patch: %s', ex)
                        fail_rev = revs.get(patch_id)
                        if fail_rev is not None:
                            revs_last_stage[fail_rev] = ('resolve-patch', str(ex))
                        continue
                    if pdata is None:
                        logger.info('    Patch not found on %s, skipping', rm.server)
                        continue
                    serieslist = pdata.get('series', None)
                    if not serieslist:
                        # This is probably from the time before patchwork-2 migration.
                        # We'll just ignore those.
                        logger.info('    A patch without an associated series? Woah.')
                        continue

                    for series in serieslist:
                        series_id = series.get('id')
                        try:
                            sdata = rm.get_series(series_id)
                        except PatchworkFetchError as ex:
                            # Patchwork 500s on a series it cannot serialize. Letting
                            # that escape aborts pwrun() before db_save_repo_heads(),
                            # so the next run re-walks the same revs and dies again.
                            reraise_unless_skippable(ex)
                            logger.info('    Skipping unavailable series: %s', ex)
                            fail_rev = revs.get(patch_id)
                            if fail_rev is not None:
                                revs_last_stage[fail_rev] = ('resolve-series', str(ex))
                            continue
                        update_queue: List[Tuple[str, int, str, str]] = list()
                        for spatch in sdata.get('patches', dict()):
                            spatch_id = spatch.get('id')

                            if spatch_id in revs:
                                rev = revs[spatch_id]
                                update_queue.append((spatch.get('name'), spatch_id, to_state, rev))

                        if update_queue:
                            logger.info('Marking series "%s": %s', to_state, sdata.get('name'))
                            updated_series.append(sdata)
                            for sname, spatch_id, to_state, rev in update_queue:
                                count += 1
                                done_patches.update([spatch_id])
                                if not DRYRUN:
                                    logger.info('  Updating: %s', sname)
                                    try:
                                        rm.update_patch(spatch_id, state=to_state, commit_ref=rev)
                                    except PatchworkFetchError as ex:
                                        reraise_unless_skippable(ex)
                                        logger.info('  Skipping update for %s: %s', sname, ex)
                                        revs_last_stage[rev] = ('apply', str(ex))
                                        continue
                                else:
                                    logger.info('  Updating (DRYRUN): %s', sname)

                if len(updated_series) and hsettings.get('send_summary', False):
                    send_summary(updated_series, committers, to_state, refname, revs, pname, rsettings, hsettings)
                if len(updated_series) and hsettings.get('notify_submitter', False):
                    notify_submitters(updated_series, committers, refname, revs, pname, rsettings, hsettings)

                if not DRYRUN:
                    for hp_rev, hp in rev_hashpair.items():
                        if hp_rev in revs_last_stage:
                            stage, note = revs_last_stage[hp_rev]
                            db_record_pending_op(c, pname, refname, hp, stage, note)
                            stats['pending_recorded'] += 1
                        elif db_clear_pending_op(c, pname, refname, hp_rev):
                            stats['pending_cleared'] += 1
            except CircuitOpenError as ex:
                logger.warning('  %s; holding back head for %s this run', ex, refname)
                failed_refs.add(refname)
                continue

        if count:
            logger.info('Updated %d patches on %s', count, rm.server)
        else:
            logger.info('No patches updated on %s', rm.server)

    stats['patches_updated'] = count
    stats['refs_held_back'] = len(failed_refs)

    if not DRYRUN:
        heads_to_save: List[Tuple[str, str]] = list()
        for refname, commit_id in git_heads:
            old_commit_id = db_heads_by_ref.get(refname) if refname in failed_refs else None
            if old_commit_id is not None:
                # The circuit breaker interrupted this ref before it finished,
                # so its head stays put -- the next run's plain git diff picks
                # up the whole gap (whatever we already applied here included,
                # since we have no per-commit record of that) rather than us
                # trying to enumerate what's left, which for a bulk push can
                # be hundreds of revisions.
                logger.info('Holding back head for %s at %s (would have advanced to %s)',
                            refname, old_commit_id, commit_id)
                heads_to_save.append((refname, old_commit_id))
            else:
                heads_to_save.append((refname, commit_id))
        db_save_repo_heads(c, heads_to_save)
        dbconn.commit()

    return stats


def check_repos() -> None:
    logger.info('=== starting regular run (bot version %s) ===', BOT_VERSION)

    # Use a global lock to make sure only a single process is running
    try:
        lockfh = open(os.path.join(CACHEDIR, 'patchwork-bot.global.lock'), 'w')
        lockf(lockfh, LOCK_EX | LOCK_NB)
    except IOError:
        logger.info('Could not obtain an exclusive lock, assuming another process is running.')
        sys.exit(0)

    if not isinstance(cmdargs.reposdir, str):
        logger.critical('reposdir must be a string')
        sys.exit(1)

    repos_processed = 0
    totals: Dict[str, int] = {
        'patches_updated': 0,
        'refs_held_back': 0,
        'pending_recorded': 0,
        'pending_cleared': 0,
    }
    for repo in CONFIG.get('repos', list()):
        fullpath = os.path.join(cmdargs.reposdir.rstrip('/'), repo.lstrip('/'))
        if not os.path.exists(fullpath):
            logger.info('Repository not found: %s', repo)
            continue
        settings = CONFIG['repos'][repo]
        if not os.path.isdir(fullpath) and not settings.get('branch'):
            logger.info('Worktree must specify "branch" setting: %s', repo)
            continue
        repo_stats = pwrun(fullpath, settings)
        repos_processed += 1
        for key in totals:
            totals[key] += repo_stats[key]

    logger.info('=== finished regular run: %d repos processed, %d patches updated, '
                '%d refs held back, %d pending ops recorded, %d pending ops cleared ===',
                repos_processed, totals['patches_updated'], totals['refs_held_back'],
                totals['pending_recorded'], totals['pending_cleared'])


def pwhash_differ() -> None:
    diff = sys.stdin.read()
    inhash = get_patchwork_hash(diff)
    logger.info('stdin hash: %s', inhash)
    check_patch_id = cmdargs.pwhash
    for pw in CONFIG['patchworks']:
        logger.info('Patchwork: %s', pw)
        for pname, _ in CONFIG['patchworks'][pw]['projects'].items():
            _, rm, _ = project_by_name(pname)
            try:
                patch = rm.get_patch(check_patch_id)
            except PatchworkFetchError as ex:
                # Nothing to skip past here -- fetching this one patch is the
                # whole job -- so report it cleanly instead of dumping a
                # traceback at whoever ran the command.
                logger.info('Could not get patch %d from %s: %s', check_patch_id, rm.server, ex)
                sys.exit(1)
            if not patch:
                logger.info('Patch %d not found on %s', check_patch_id, rm.server)
                sys.exit(1)
            if patch.get('hash') != inhash:
                logger.info('--- patchwork diff ---')
                logger.info(patch.get('diff'))
                logger.info('--- hash: %s ---', patch.get('hash'))
                sys.exit(1)


if __name__ == '__main__':
    # noinspection PyTypeChecker
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('-c', '--cfgfile', required=True,
                        help='Config file with repository and project data.')
    parser.add_argument('-r', '--reposdir', default=None,
                        help='Directory with repositories to process')
    parser.add_argument('-l', '--logfile', default=None,
                        help='Log file for messages during quiet operation')
    parser.add_argument('-m', '--mailhost', default='localhost',
                        help='Mailhost to use when sending mail')
    parser.add_argument('-d', '--dry-run', dest='dryrun', action='store_true', default=False,
                        help='Do not mail or store anything, just do a dry run.')
    parser.add_argument('-n', '--no-mail', dest='nomail', action='store_true', default=False,
                        help='Do not mail anything, but store database entries.')
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Only output errors to the stdout')
    parser.add_argument('-v', '--verbose', action='store_true', default=False,
                        help='Be more verbose in logging output')
    parser.add_argument('-k', '--housekeeping', action='store_true', default=False,
                        help='Perform a housekeeping run (supersede, archive)')
    parser.add_argument('--cachedir', default=None,
                        help='Cache directory to use instead of ~/.cache/git-patchwork-bot')
    parser.add_argument('--domain', default=None,
                        help='Domain to use when creating message-ids')
    parser.add_argument('--ancestors', default=None,
                        help='During initial database creation, consider this many ancestor commits as fresh')
    parser.add_argument('--pwhash', default=None, type=int, metavar='PATCH-ID',
                        help='Debug pwhash mismatches. Compare patchwork hash of diff from stdin to patch id')
    parser.add_argument('--tokens-file', default=None,
                        help='Separate configuration file containing just API tokens')

    cmdargs = parser.parse_args()

    logger.setLevel(logging.DEBUG)

    if cmdargs.logfile:
        fch = logging.FileHandler(cmdargs.logfile)
        formatter = logging.Formatter(
            '[%(asctime)s] %(message)s')
        fch.setFormatter(formatter)

        if cmdargs.verbose:
            fch.setLevel(logging.DEBUG)
        else:
            fch.setLevel(logging.INFO)
        logger.addHandler(fch)

    sch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    sch.setFormatter(formatter)

    if cmdargs.quiet:
        sch.setLevel(logging.CRITICAL)
    elif cmdargs.verbose:
        sch.setLevel(logging.DEBUG)
    else:
        sch.setLevel(logging.INFO)

    logger.addHandler(sch)
    if cmdargs.nomail or cmdargs.dryrun:
        logger.info('NOMAIL: ON')
        NOMAIL = True
    if cmdargs.dryrun:
        logger.info('DRYRUN: ON')
        DRYRUN = True
    if cmdargs.cachedir:
        CACHEDIR = cmdargs.cachedir
    if cmdargs.domain:
        DOMAIN = str(cmdargs.domain)
    MAILHOST = cmdargs.mailhost

    with open(cmdargs.cfgfile, 'r') as fh:
        cfgyaml = fh.read()

    CONFIG = pyyaml.safe_load(cfgyaml)

    if cmdargs.tokens_file:
        with open(cmdargs.tokens_file, 'r') as fh:
            tkyaml = fh.read()
        tks = pyyaml.safe_load(tkyaml)
        for _pserver, _sconfig in tks['patchworks'].items():
            if _pserver in CONFIG['patchworks']:
                logger.debug('Taking apitoken info for %s from %s', _pserver, cmdargs.tokens_file)
                CONFIG['patchworks'][_pserver]['apitoken'] = _sconfig.get('apitoken')

    if not os.path.isdir(CACHEDIR):
        os.makedirs(CACHEDIR, exist_ok=True)

    if cmdargs.pwhash:
        pwhash_differ()
        sys.exit(0)

    if cmdargs.housekeeping or cmdargs.pwhash:
        logger.info('=== starting housekeeping run (bot version %s) ===', BOT_VERSION)
        _hk_totals = {'superseded': 0, 'archived': 0}
        _hk_projects = 0
        for _pserver, _sconfig in CONFIG['patchworks'].items():
            for _pname in _sconfig['projects']:
                _hk_stats = housekeeping(_pname)
                _hk_projects += 1
                for _key in _hk_totals:
                    _hk_totals[_key] += _hk_stats[_key]
        logger.info('=== finished housekeeping run: %d projects processed, %d series superseded, '
                    '%d patches archived ===',
                    _hk_projects, _hk_totals['superseded'], _hk_totals['archived'])
    else:
        if not cmdargs.reposdir:
            logger.critical('-r is required for this mode')
            sys.exit(1)
        check_repos()
