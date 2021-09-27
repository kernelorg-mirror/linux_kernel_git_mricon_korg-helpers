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


# noinspection PyBroadException, PyMethodMayBeStatic
class GroupsioListener(object):

    def on_get(self, req, resp): # noqa
        resp.status = falcon.HTTP_200
        resp.body = "We don't serve GETs here\n"

    def _inject_message(self, doc, req):
        success = True
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

            groupaddr = doc['group']['email_address']
            groupurl = doc['group']['group_url']
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
            msgnum = doc['msg_num']
            msg.add_header('X-Groupsio-URL', f'{groupurl}/message/{msgnum}')

            try:
                mailfrom = doc['member_info']['email']
            except:
                mailfrom = 'webhook@localhost'

            mailhost = os.getenv('MAILHOST', 'localhost')
            mailto = os.getenv('MAILTO', 'webhook@archiver.kernel.org')

            smtp = smtplib.SMTP(mailhost)
            smtp.sendmail(mailfrom, [mailto], msg.as_bytes())
            smtp.close()

        except:
            return False

        return success

    def _verify_psk(self, psk, raw, vdigest):
        hm = hmac.new(psk.encode(), digestmod='sha256')
        hm.update(raw)
        return hmac.compare_digest(vdigest, hm.hexdigest())

    def on_post(self, req, resp):
        if not req.content_length:
            resp.status = falcon.HTTP_500
            resp.body = 'Payload required\n'
            return

        raw = req.stream.read()
        psk = os.getenv('GROUPSIO_PSK')
        if psk:
            vdigest = req.get_header('X-Groupsio-Signature')
            if not vdigest:
                resp.status = falcon.HTTP_401
                resp.body = 'HMAC signature header required\n'
                return
            if not self._verify_psk(psk, raw, vdigest):
                resp.status = falcon.HTTP_401
                resp.body = 'HMAC signature verification failed\n'
                return

        try:
            doc = json.loads(raw)
        except:
            resp.status = falcon.HTTP_500
            resp.body = 'Failed to parse payload as json\n'
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


app = falcon.API()
gl = GroupsioListener()
mp = os.getenv('MOUNTPOINT', '/groupsio_webhook')
app.add_route(mp, gl)
