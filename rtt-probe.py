#!/usr/bin/env python3

import os
import sys
import logging
import argparse
import hmac
import email.utils
import datetime
import subprocess
import json

from configparser import ConfigParser, ExtendedInterpolation, SectionProxy
from typing import Optional, Dict, List

logger = logging.getLogger('rtt-probe')


def parse_fuzzydate(fuzzydate):
    from dateutil import tz, parser
    try:
        r = parser.parse(fuzzydate, fuzzy=True)
        r = r.astimezone(tz.tzutc())
    # if the fuzzy parser failed to parse the line due to
    # incorrect timezone information issue 5 GitHub
    except ValueError:
        r = re.findall('^(.*?)\s*\(', line)
        if r:
            r = dateutil.parser.parse(r[0])
    return r


def get_rtt(bmsg: bytes, checkname: str, psk: str, use_rcvd: bool = False) -> Optional[int]:
    # Find the check line
    cline = ckhash = None
    in_recvd = False
    rline = list()
    for line in bmsg.split(b'\n'):
        if in_recvd and (line.startswith(b' ') or line.startswith(b'\t')):
            rline.append(line.strip().decode())
            continue
        in_recvd = False
        if line.startswith(b'rtt-probe:'):
            cline = line.strip()
        elif line.startswith(b'X-Rtt-Probe-Hmac:'):
            ckhash = line.strip().split(b':')[1]
            ckhash = ckhash.strip().decode()
        elif line.startswith(b'Received:') and not rline:
            in_recvd = True
            rline += line.strip().decode()

    if cline is None:
        return None

    hm = hmac.new(psk.encode(), digestmod='sha256')
    hm.update(cline)
    if not hmac.compare_digest(ckhash, hm.hexdigest()):
        logger.info('Hash did not verify, ignoring message')

    parts = cline.decode().split(':', maxsplit=2)
    if parts[1] != checkname:
        logger.info('Check name %s did not match %s, skipping', parts[1], checkname)
        return None
    try:
        emitted = int(parts[2])
    except ValueError:
        logger.info('Invalid timestamp: %s', parts[2])
        return None

    if use_rcvd and rline:
        rcvd = ' '.join(rline)
        if rcvd.find(';') > 0:
            junk, fuzzydate = rcvd.rsplit(';', maxsplit=1)
            rtime = parse_fuzzydate(fuzzydate.strip())
            if rtime:
                return int(rtime.timestamp()) - emitted

    return int(datetime.datetime.utcnow().timestamp()) - emitted


def check_imap(checkname: str, config: SectionProxy, expunge: bool = True) -> Optional[int]:
    import imaplib
    logger.info('Checking: %s', config['imaphost'])
    try:
        imap = imaplib.IMAP4_SSL(host=config['imaphost'])
        imap.login(config['imapuser'], config['imappass'])
        status, messages = imap.select(config.get('imapfolder', 'INBOX'))
    except:  # noqa
        logger.info('Unable to connect/check imap %s', config['imaphost'])
        return None

    # We don't exit 1 in case remote IMAP server is having problems. This lets us
    # avoid cronspam and simply log things in a logfile.
    if status != 'OK':
        logger.info('Incorrect imap settings')
        return None

    # Collect and delete messages matching our expected parameters
    # This assumes there aren't tons of messages in that folder,
    # so don't run it on anything huge
    logger.debug('Messages: %s', int(messages[0]))
    status, data = imap.search(None, f'SUBJECT "RTT-PROBE: {checkname}"')
    if status != 'OK':
        logger.info('Could not list messages in %s', config['imaphost'])
        return None
    if not int(messages[0]):
        logger.info('No matching messages on %s', config['imaphost'])
        return None

    least_rtt = None
    for num in data[0].split():
        typ, msgdata = imap.fetch(num, '(RFC822)')
        if typ == 'OK':
            bmsg = msgdata[0][1]  # noqa
            if not isinstance(bmsg, bytes) or bmsg.find(b'From') < 0:
                continue
            rtt = get_rtt(bmsg, checkname, config['psk'], use_rcvd=True)
            if expunge:
                imap.store(num, '+FLAGS', r'\Deleted')
            if rtt is not None and (least_rtt is None or rtt < least_rtt):
                least_rtt = rtt

    if expunge:
        imap.expunge()
    imap.close()
    imap.logout()
    return least_rtt


def git_run_command(gitdir: str, args: List[str]) -> bytes:
    args = ['git', '--no-pager', '--git-dir', gitdir] + args
    logger.debug('Running %s' % ' '.join(args))
    (output, error) = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()

    return output.strip()


def git_get_message_from_pi(fullpath: str, commit_id: str) -> bytes:
    logger.debug('Getting %s:m from %s', commit_id, fullpath)
    args = ['show', f'{commit_id}:m']
    return git_run_command(fullpath, args)


def git_get_new_revs(fullpath: str, latest: Optional[str]) -> Optional[list]:
    if latest:
        rev_range = f'{latest}..'
    else:
        rev_range = 'HEAD'

    args = ['rev-list', '--pretty=oneline', '--reverse', rev_range, 'master']
    output = git_run_command(fullpath, args)
    if not output:
        return None

    newrevs = list()
    for line in output.decode().split('\n'):
        (commit_id, logmsg) = line.split(' ', 1)
        logger.debug('commit_id=%s, subject=%s', commit_id, logmsg)
        newrevs.append((commit_id, logmsg))

    return newrevs


def check_public_inbox(checkname: str, config: SectionProxy, state: Dict) -> Optional[int]:
    pidir = config['pidir']
    if not os.path.isdir(pidir):
        logger.info('Not a directory: %s', pidir)
        return None
    # Find the maximum epoch and only bother about that one
    epoch = 0
    gitdir = None
    while True:
        checkdir = os.path.join(pidir, 'git', f'{str(epoch)}.git')
        if not os.path.isdir(checkdir):
            break
        gitdir = checkdir
        epoch += 1
    if gitdir is None:
        logger.info('Could not find a workable git directory in %s', pidir)
        return None

    logger.info('Checking: %s', gitdir)
    latest = None
    if 'public-inbox' in state:
        if checkname in state['public-inbox'] and gitdir in state['public-inbox'][checkname]:
            latest = state['public-inbox'][checkname][gitdir]
        else:
            state['public-inbox'][checkname] = dict()
    else:
        state['public-inbox'] = {checkname: dict()}

    revs = git_get_new_revs(gitdir, latest)
    if revs is None:
        return None

    least_rtt = None
    for (commit_id, logline) in revs:
        if not logline.startswith(f'RTT-PROBE: {checkname}'):
            logger.debug('Ignoring %s, not matched', logline)
            continue
        bmsg = git_get_message_from_pi(gitdir, commit_id)
        rtt = get_rtt(bmsg, checkname, config['psk'])
        if rtt is not None and (least_rtt is None or rtt < least_rtt):
            least_rtt = rtt
        latest = commit_id

    state['public-inbox'][checkname][gitdir] = latest

    return least_rtt


def log_report(checkname: str, rtt: int, use_syslog: bool, print_out: bool) -> None:
    if use_syslog:
        import syslog
        syslog.openlog('rtt-probe', facility=syslog.LOG_MAIL)
        syslog.syslog(syslog.LOG_INFO, f'{checkname}={rtt}')
    if print_out:
        sys.stdout.write(f'{checkname}={rtt}\n')
    logger.info('Success : checkname=%s; rtt=%s', checkname, rtt)


def send_smtp(checkname: str, config: SectionProxy) -> Optional[int]:
    import smtplib
    import email.message
    logger.debug('Emitting a message')
    # Build a message first
    myts = int(datetime.datetime.utcnow().timestamp())
    body = f'rtt-probe:{checkname}:{str(myts)}'
    hm = hmac.new(config.get('psk').encode(), digestmod='sha256')
    hm.update(body.encode())

    msg = email.message.Message()

    msg['To'] = config.get('mailto')
    targets = (msg['To'],)
    msg['Subject'] = f'RTT-PROBE: {checkname}'
    msg['From'] = config.get('mailfrom')

    msg['Message-Id'] = email.utils.make_msgid('rtt-probe')
    msg['X-Rtt-Probe-Hmac'] = hm.hexdigest()
    msg['Date'] = email.utils.formatdate(localtime=True)

    msg.set_payload(body + '\r\n')

    logger.debug('Message follows')
    logger.debug(msg.as_string())

    mailhost = config.get('mailhost', 'localhost')

    try:
        server = smtplib.SMTP(mailhost)
        logger.info('Sending mail to %s', ', '.join(targets))
        server.sendmail(msg['From'], targets, msg.as_string())
        server.close()
    except Exception as ex: # noqa
        logger.info('Unable to send mail to %s', ', '.join(targets))
        logger.info('Attempting to use %s returned:', mailhost)
        logger.info(ex)
        return None

    return myts


def main(cmdargs: argparse.Namespace) -> None:
    savestate = not cmdargs.debug
    statechanged = False
    if not os.path.exists(cmdargs.cfgfile):
        sys.stderr.write('ERROR: config file %s does not exist' % cmdargs.cfgfile)
        sys.exit(1)
    config = ConfigParser(interpolation=ExtendedInterpolation())
    config.read(cmdargs.cfgfile)
    if os.path.exists(cmdargs.statefile):
        with open(cmdargs.statefile, 'r') as fh:
            try:
                state = json.load(fh)
            except:  # noqa
                logger.info('Could not parse %s, making new', cmdargs.statefile)
    else:
        state = {}

    for section in config.sections():
        rtt = None
        subconf = config[section]
        action, checkname = section.split(maxsplit=1)
        if action == 'smtp':
            interval = subconf.getint('interval', 300)
            if section in state:
                # how long has it been since we sent the last one?
                lastsend = state[section].get('when', 0)
                now = datetime.datetime.utcnow().timestamp()
                elapsed = int(now - lastsend)
            else:
                elapsed = interval
            if elapsed >= interval:
                sentts = send_smtp(checkname, subconf)
                if sentts:
                    statechanged = True
                    state[section] = {
                        'when': sentts,
                    }
            else:
                logger.debug('elapsed < interval (%s < %s), not emitting new mail', elapsed, interval)

            # No rtt to report on send actions, so skip the rest
            continue

        elif action == 'imap':
            rtt = check_imap(checkname, subconf, expunge=savestate)
        elif action == 'public-inbox':
            rtt = check_public_inbox(checkname, subconf, state)
            if rtt:
                statechanged = True
        else:
            logger.critical('Unknown action in section: %s', section)

        if rtt is not None:
            printname = subconf.get('printname', checkname)
            log_report(printname, rtt, use_syslog=cmdargs.syslog, print_out=cmdargs.print)

    if statechanged and savestate:
        with open(cmdargs.statefile, 'w') as fh:
            logger.debug('Writing %s', cmdargs.statefile)
            json.dump(state, fh, indent=2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Perform a roundtrip email probe",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('-c', '--cfgfile', required=True,
                        help='Configuration file to use')
    parser.add_argument('-y', '--syslog', action='store_true', default=False,
                        help='Record results in syslog (forces -q)')
    parser.add_argument('-p', '--print', action='store_true', default=False,
                        help='Print check results to stdout (forces -q)')
    parser.add_argument('-d', '--debug', action='store_true', default=False,
                        help='Print out debug messages and do not change any state')
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Print only critical messages')
    parser.add_argument('-l', '--logfile',
                        help='Record activity in this log file')
    parser.add_argument('-s', '--statefile', default='/tmp/rtt-probe.state',
                        help='Keep state in this file')
    parser.add_argument('-r', '--randsleep', type=int, default=0,
                        help='Randomly sleep up to this many seconds')
    parser.add_argument('-t', '--socket-timeout', type=int, default=60,
                        help='Socket timeout for remote connections')
    _cmdargs = parser.parse_args()

    logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    ch.setFormatter(formatter)
    if _cmdargs.quiet or _cmdargs.syslog or _cmdargs.print:
        ch.setLevel(logging.CRITICAL)
    elif _cmdargs.debug:
        ch.setLevel(logging.DEBUG)
    else:
        ch.setLevel(logging.INFO)

    logger.addHandler(ch)
    if _cmdargs.logfile:
        ch = logging.FileHandler(_cmdargs.logfile)
        formatter = logging.Formatter(f'[%(asctime)s] %(message)s')
        ch.setFormatter(formatter)
        ch.setLevel(logging.INFO)
        logger.addHandler(ch)

    if _cmdargs.randsleep:
        import time
        import random
        snooze = random.randint(0, _cmdargs.randsleep)
        logger.info('Sleeping %s seconds', snooze)
        time.sleep(snooze)

    if _cmdargs.socket_timeout:
        import socket
        socket.setdefaulttimeout(_cmdargs.socket_timeout)

    main(_cmdargs)
