#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# This webhook receives events from groups_io and injects them into the
# local mail queue to be processed as if it came in via the SMTP gateway.
#
__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import falcon
import json
import socket
import email
import email.utils
import email.header
import hmac
import smtplib
import os
import sys
import logging
import logging.handlers

from configparser import ConfigParser, ExtendedInterpolation

logger = logging.getLogger('groupsio-webhook')
logger.setLevel(logging.DEBUG)


# noinspection PyBroadException, PyMethodMayBeStatic
class GroupsioListener(object):
    _config: ConfigParser
    psk: str

    def __init__(self, _config: ConfigParser) -> None:
        self._config = _config
        self.psk = _config['main'].get('groupsio_psk')

        logfile = _config['main'].get('logfile')
        loglevel = _config['main'].get('loglevel', 'info')
        if logfile:
            self._init_logger(logfile, loglevel)

    def _init_logger(self, logfile: str, loglevel: str) -> None:
        global logger
        lch = logging.handlers.WatchedFileHandler(os.path.expanduser(logfile))
        lfmt = logging.Formatter('[%(process)d] %(asctime)s - %(levelname)s - %(message)s')
        lch.setFormatter(lfmt)
        if loglevel == 'critical':
            lch.setLevel(logging.CRITICAL)
        elif loglevel == 'debug':
            lch.setLevel(logging.DEBUG)
        else:
            lch.setLevel(logging.INFO)
        logger.addHandler(lch)

    def on_get(self, req, resp): # noqa
        resp.status = falcon.HTTP_200
        resp.body = "We don't serve GETs here\n"

    def _inject_message(self, doc: dict, req) -> bool:
        success = False
        msgnum = doc['msg_num']
        groupaddr = doc['group']['email_address']
        groupurl = doc['group']['group_url']
        logger.info('Received %s msg %s', groupaddr, msgnum)
        logger.debug('---%s START---', msgnum)
        logger.debug(doc['message'])
        logger.debug('---%s END---', msgnum)

        try:
            msg = email.message_from_string(doc['message'])
            try:
                theirname = socket.gethostbyaddr(req.remote_addr)[0]
                them = f'{theirname} [{req.remote_addr}]'
            except:
                them = f'[{req.remote_addr}]'
            try:
                us = socket.gethostname()
            except:
                us = 'localhost'
            logger.debug('us=%s', us)
            logger.debug('them=%s', them)

            listid = groupaddr.replace('@', '.')
            scheme = req.scheme.upper()
            rdate = email.utils.formatdate()
            rline = f'from {them} by {us} with {scheme} for <{groupaddr}>; {rdate}'
            rhdr = email.header.make_header([(rline.encode(), 'us-ascii')], maxlinelen=78)
            if msg.get('list-id'):
                msg.replace_header('List-Id', f'<{listid}>')
            else:
                msg.add_header('List-Id', f'<{listid}>')
            msg.add_header('X-Webhook-Received', rhdr.encode())
            msg.add_header('X-Groupsio-URL', f'{groupurl}/message/{msgnum}')

            # do we have a repodir for this list?
            if groupaddr in self._config.sections():
                pirepo = self._config[groupaddr].get('pirepo')
                success = self._write_pi(pirepo, msg)

            if not success:
                try:
                    mailfrom = doc['member_info']['email']
                except:
                    mailfrom = 'webhook@localhost'
                success = self._send_smtp(msg, mailfrom)

        except Exception as ex:
            logger.critical('Failed processing message %s:', msgnum)
            logger.critical(str(ex))
            emerg_dir = self._config['main'].get('emerg_dir')
            if emerg_dir and os.path.isdir(emerg_dir):
                efile = os.path.join(emerg_dir, f'{groupaddr}-{msgnum}.txt')
                with open(efile, 'w') as fh:
                    fh.write(doc['message'])
                    logger.critical('Saved message as %s', efile)

        return success

    def _write_pi(self, pirepo: str, msg: email.message.Message) -> bool:
        success = False
        try:
            import ezpi
            ezpi.add_rfc822(pirepo, msg)
            logger.info('Wrote to public-inbox at %s', pirepo)
            success = True
        except Exception as ex:
            logger.critical('Could not write to pirepo')
            logger.critical(str(ex))

        return success

    def _send_smtp(self, msg: email.message.Message, mailfrom: str) -> bool:
        mailhost = self._config['main'].get('mailhost', 'localhost')
        mailto = self._config['main'].get('mailto', 'webhook@archiver.kernel.org')
        try:
            smtp = smtplib.SMTP(mailhost)
            smtp.sendmail(mailfrom, [mailto], msg.as_bytes())
            smtp.close()
            logger.info('Successfully sent via SMTP to <%s>', mailto)
        except Exception as ex:
            logger.critical('Could not send SMTP')
            logger.critical(str(ex))
            return False

        return True

    def _verify_psk(self, psk, raw, vdigest):
        hm = hmac.new(psk.encode(), digestmod='sha256')
        hm.update(raw)
        return hmac.compare_digest(vdigest, hm.hexdigest())

    def on_post(self, req, resp):
        if not req.content_length:
            resp.status = falcon.HTTP_500
            resp.body = 'Payload required\n'
            return

        logger.info('Received POST from %s', req.remote_addr)
        raw = req.stream.read()
        if self.psk:
            vdigest = req.get_header('X-Groupsio-Signature')
            if not vdigest:
                resp.status = falcon.HTTP_401
                resp.body = 'HMAC signature header required\n'
                return
            if not self._verify_psk(self.psk, raw, vdigest):
                resp.status = falcon.HTTP_401
                resp.body = 'HMAC signature verification failed\n'
                return

        try:
            doc = json.loads(raw)
        except Exception as ex:
            resp.status = falcon.HTTP_500
            resp.body = 'Failed to parse payload as json\n'
            logger.critical('Failed to parse incoming json:')
            logger.critical(str(ex))
            return

        success = True
        if doc.get('action', '') == 'sent_message_accepted' and 'message' in doc:
            success = self._inject_message(doc, req)

        if success:
            resp.status = falcon.HTTP_200
            resp.body = 'OK thanks\n'
        else:
            resp.status = falcon.HTTP_500
            resp.body = 'Something went wrong, sorry.\n'


parser = ConfigParser(interpolation=ExtendedInterpolation())
cfgfile = os.getenv('CONFIG')
if not cfgfile or not os.path.exists(cfgfile):
    sys.stderr.write('CONFIG env var is not set or is not valid')
    sys.exit(1)

parser.read(cfgfile)

gl = GroupsioListener(parser)
app = falcon.App()
mp = os.getenv('MOUNTPOINT', '/groupsio_webhook')
app.add_route(mp, gl)
