#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# This webhook receives message events from groups.io and writes them
# straight into local public-inbox v2 repositories using ezpi.
#
__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import datetime
import email
import email.message
import email.utils
import hmac
import json
import logging
import logging.handlers
import os
import re
import socket
from configparser import ConfigParser, ExtendedInterpolation
from pathlib import Path
from typing import Any

import ezpi
import falcon

logger = logging.getLogger('groupsio-webhook')
logger.setLevel(logging.DEBUG)

# The group address becomes a directory name, so only allow characters that
# real list addresses use; this rules out path separators and traversal.
GROUPADDR_RE = re.compile(r'^[a-z0-9][a-z0-9._+-]*@[a-z0-9-]+(\.[a-z0-9-]+)+$')

AUTO_EPOCH_MODES = ('size', 'annual')


class GroupsioListener:
    def __init__(self, config: ConfigParser) -> None:
        self.config = config
        main = config['main']
        psk = main.get('groupsio_psk')
        # Inboxes get created on demand, so unsigned requests must never be
        # accepted, or anyone could create inboxes and inject messages.
        if not psk:
            raise ValueError('groupsio_psk must be set in [main]')
        self.psk = psk
        self.inboxdir = main.get('inboxdir')
        self.emerg_dir = main.get('emerg_dir')
        self.notify_file = main.get('notify_file')
        self.debug = main.get('loglevel', 'info') == 'debug'
        self.auto_epoch = main.get('auto_epoch', 'size')
        if self.auto_epoch not in AUTO_EPOCH_MODES:
            raise ValueError(f'auto_epoch must be one of {", ".join(AUTO_EPOCH_MODES)}')

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        resp.status = falcon.HTTP_200
        resp.text = "We don't serve GETs here\n"

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        logger.info('Received POST from %s', req.remote_addr)
        raw = req.bounded_stream.read()
        if not raw:
            resp.status = falcon.HTTP_400
            resp.text = 'Payload required\n'
            return

        vdigest = req.get_header('X-Groupsio-Signature')
        if not vdigest:
            resp.status = falcon.HTTP_401
            resp.text = 'HMAC signature header required\n'
            return
        if not self._verify_psk(raw, vdigest):
            resp.status = falcon.HTTP_401
            resp.text = 'HMAC signature verification failed\n'
            return

        if self.debug:
            self._save_emerg(raw, 'debug')

        try:
            doc = json.loads(raw)
        except ValueError:
            logger.exception('Failed to parse incoming json')
            resp.status = falcon.HTTP_400
            resp.text = 'Failed to parse payload as json\n'
            return

        if not isinstance(doc, dict) or not doc.get('message'):
            logger.debug('No message in payload, skipping')
            resp.status = falcon.HTTP_200
            resp.text = 'OK thanks\n'
            return

        resp.status = self._handle_message(doc, raw, req)
        if resp.status == falcon.HTTP_200:
            resp.text = 'OK thanks\n'
        else:
            resp.text = 'Something went wrong, sorry.\n'

    def _handle_message(self, doc: dict[str, Any], raw: bytes, req: falcon.Request) -> str:
        try:
            msgnum = doc['msg_num']
            groupaddr = doc['group']['email_address'].lower()
            groupurl = doc['group']['group_url']
            msg = email.message_from_string(doc['message'])
        except (KeyError, TypeError, AttributeError):
            logger.critical('Payload is malformed')
            self._save_emerg(raw, 'malformed')
            return falcon.HTTP_400

        logger.info('Received %s msg %s', groupaddr, msgnum)
        inbox = self._get_inbox(groupaddr)
        if inbox is None:
            # A signed payload for a list we don't archive is not an error on
            # groups.io's side, so keep it for a human and stop the retries.
            logger.critical('No inbox configured for %s, saving msg %s', groupaddr, msgnum)
            self._save_emerg(raw, 'noinbox')
            return falcon.HTTP_200

        self._add_headers(msg, groupaddr, f'{groupurl}/message/{msgnum}', req)
        try:
            ezpi.add_rfc822_v2(inbox, msg, auto_epoch=self.auto_epoch)
        except ValueError:
            # Missing From or Subject: retrying will never fix this.
            logger.exception('Message %s for %s is not archivable', msgnum, groupaddr)
            self._save_emerg(raw, 'invalid')
            return falcon.HTTP_200
        except Exception:
            # Lock or git failures may be transient, so ask groups.io to retry;
            # anything else unexpected still must not lose the payload.
            logger.exception('Could not write msg %s to %s', msgnum, inbox)
            self._save_emerg(raw, 'failed')
            return falcon.HTTP_500

        logger.info('Wrote %s msg %s to %s', groupaddr, msgnum, inbox)
        self._notify()
        return falcon.HTTP_200

    def _notify(self) -> None:
        # Indexing and anything else that follows a write is not ours to run:
        # it would hold up the response, and its failures would make groups.io
        # retry an already archived message. Touching a file lets something
        # like a systemd path unit pick it up instead.
        if not self.notify_file:
            return
        try:
            Path(self.notify_file).touch()
        except OSError:
            # The message is archived, so this is not the sender's problem.
            logger.exception('Could not touch notify_file %s', self.notify_file)

    def _get_inbox(self, groupaddr: str) -> str | None:
        if groupaddr in self.config:
            inbox = self.config[groupaddr].get('inbox')
            if inbox:
                return os.path.expanduser(inbox)
        if not self.inboxdir or not GROUPADDR_RE.match(groupaddr):
            return None
        return os.path.join(os.path.expanduser(self.inboxdir), groupaddr.replace('@', '.'))

    @staticmethod
    def _add_headers(msg: email.message.Message, groupaddr: str, msgurl: str, req: falcon.Request) -> None:
        try:
            them = f'{socket.gethostbyaddr(req.remote_addr)[0]} [{req.remote_addr}]'
        except OSError:
            them = f'[{req.remote_addr}]'
        us = socket.gethostname() or 'localhost'
        rdate = email.utils.formatdate()
        rline = f'from {them} by {us} with {req.scheme.upper()} for <{groupaddr}>; {rdate}'

        listid = f'<{groupaddr.replace("@", ".")}>'
        if msg.get('List-Id'):
            msg.replace_header('List-Id', listid)
        else:
            msg.add_header('List-Id', listid)
        msg.add_header('X-Webhook-Received', rline)
        msg.add_header('X-Groupsio-URL', msgurl)

    def _verify_psk(self, raw: bytes, vdigest: str) -> bool:
        hm = hmac.new(self.psk.encode(), raw, digestmod='sha256')
        return hmac.compare_digest(vdigest, hm.hexdigest())

    def _save_emerg(self, raw: bytes, reason: str) -> None:
        # Keep the whole signed payload rather than just the message, so it
        # can be replayed against the webhook once the problem is fixed.
        if not self.emerg_dir or not os.path.isdir(self.emerg_dir):
            logger.critical('No usable emerg_dir, payload was not saved')
            return
        ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        efile = os.path.join(self.emerg_dir, f'{ts}-{os.getpid()}-{reason}.json')
        with open(efile, 'wb') as fh:
            fh.write(raw)
        logger.info('Saved payload into %s', efile)


def _init_logger(logfile: str, loglevel: str) -> None:
    lch = logging.handlers.WatchedFileHandler(os.path.expanduser(logfile))
    lch.setFormatter(logging.Formatter('[%(process)d] %(asctime)s - %(levelname)s - %(message)s'))
    lch.setLevel({'critical': logging.CRITICAL, 'debug': logging.DEBUG}.get(loglevel, logging.INFO))
    logger.addHandler(lch)


def create_app(config: ConfigParser, mountpoint: str = '/groupsio_webhook') -> falcon.App:
    logfile = config['main'].get('logfile')
    if logfile:
        _init_logger(logfile, config['main'].get('loglevel', 'info'))
    app = falcon.App()
    app.add_route(mountpoint, GroupsioListener(config))
    return app


# uwsgi takes "app" straight from the module's namespace and never asks a
# module __getattr__, so the app has to exist once the module is loaded.
# Without CONFIG, as in the tests, the module only provides create_app().
_cfgfile = os.getenv('CONFIG')
if _cfgfile is not None:
    if not os.path.exists(_cfgfile):
        raise RuntimeError(f'CONFIG file {_cfgfile} does not exist')
    _config = ConfigParser(interpolation=ExtendedInterpolation())
    _config.read(_cfgfile)
    app = create_app(_config, os.getenv('MOUNTPOINT', '/groupsio_webhook'))
