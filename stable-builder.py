#!/usr/bin/env python3
# This is a very quick-and-dirty script to bring up a hardware builder in the
# Equinix Metal platform for the members of the LF Stable Kernel group.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# -*- coding: utf-8 -*-
#
__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import packet
import sys
import os
import logging
import argparse
import json
import datetime

from string import Template

logger = logging.getLogger('builder-ci')


def get_manager(config):
    eq_auth_token = config['core'].get('auth_token')
    if not eq_auth_token:
        logger.critical('core.auth_token not set')
        sys.exit(1)

    return packet.Manager(auth_token=eq_auth_token)


def print_ips(device, user):
    sshflags = '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
    for ipaddr in device.ip_addresses:
        if not ipaddr['public']:
            continue
        logger.info('    ssh %s %s@%s', sshflags, user, ipaddr['address'])


def get_builder(manager, project_id, hostname):
    for device in manager.list_all_devices(project_id=project_id):
        if device.hostname == hostname:
            return device

    return None


def create_builder(config, user):
    manager = get_manager(config)
    eq_project_id = config['core'].get('project_id')
    hostname = '%s-builder.ci.kernel.org' % user

    logger.info('Checking for existing %s', hostname)
    device = get_builder(manager, eq_project_id, hostname)
    if device:
        logger.critical('  device exists (%s)', device.id)
        print_ips(device, user)
        sys.exit(1)

    usec = 'user_%s' % user
    pkf = config[usec].get('pubkey')
    if not pkf:
        logger.critical('Need pubkey entry for %s', user)
        sys.exit(1)

    with open(pkf) as pkfh:
        pubkey = pkfh.read().strip()

    cif = config[usec].get('cloud_init')
    if cif:
        with open(cif) as cifh:
            citpt = cifh.read()

    tptdata = {
        'username': user,
        'pubkey': pubkey,
        'msmtp_user': config['msmtp'].get('user'),
        'msmtp_password': config['msmtp'].get('password'),
    }

    userdata = Template(citpt).safe_substitute(tptdata)
    eq_plan = config[usec].get('plan')
    eq_facility = config[usec].get('facility')
    eq_os = config[usec].get('os')
    if not eq_plan or not eq_facility or not eq_os:
        logger.critical('Need to set plan, facility, os')
        sys.exit(1)

    rip_after_hrs = config[usec].getint('rip_after_hrs', 2)
    rip_ts = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=rip_after_hrs)
    rip_after = rip_ts.isoformat(timespec='seconds')

    customdata = json.dumps({'rip_after': rip_after})

    logger.info('Creating %s (facility=%s, os=%s)', hostname, eq_facility, eq_os)
    manager.create_device(project_id=eq_project_id,
                          hostname=hostname,
                          plan=eq_plan,
                          facility=[eq_facility, 'any'],
                          operating_system=eq_os,
                          customdata=customdata,
                          userdata=userdata)
    logger.info('You will get an email to %s@kernel.org when it is ready.', user)
    logger.info('It will be auto-ripped in %sH (at %s).', rip_after_hrs, rip_after)


def destroy_builder(config, user):
    manager = get_manager(config)
    eq_project_id = config['core'].get('project_id')
    hostname = '%s-builder.ci.kernel.org' % user

    logger.info('Checking for existing %s', hostname)
    device = get_builder(manager, eq_project_id, hostname)
    if device:
        logger.info('Destroying %s', hostname)
        device.delete()
    else:
        logger.info('%s does not appear to be running', hostname)


def get_rip_after(device):
    now = datetime.datetime.now(datetime.timezone.utc)
    if 'rip_after' in device.customdata:
        # Trick for python 3.6 compatibility
        isodate = device.customdata['rip_after'].replace('+00:00', '+0000')
        rip_after = datetime.datetime.strptime(isodate, '%Y-%m-%dT%H:%M:%S%z')
    else:
        rip_after = None

    return rip_after, now


def check_builder(config, user):
    manager = get_manager(config)
    eq_project_id = config['core'].get('project_id')
    hostname = '%s-builder.ci.kernel.org' % user

    logger.info('Checking for existing %s', hostname)
    device = get_builder(manager, eq_project_id, hostname)
    if device:
        logger.info('Found active host:')
        print_ips(device, user)
        rip_after, now = get_rip_after(device)
        if rip_after is not None:
            if rip_after < now:
                logger.info('Will be ripped very soon (ish)')
            else:
                rip_when = rip_after - now
                mins = int(rip_when.seconds / 60)
                logger.info('Will be ripped in %s min (ish)', mins)
    else:
        logger.info('Not found: %s', hostname)


def rip_builder(config, user):
    manager = get_manager(config)
    eq_project_id = config['core'].get('project_id')
    hostname = '%s-builder.ci.kernel.org' % user

    device = get_builder(manager, eq_project_id, hostname)
    if device and 'rip_after' in device.customdata:
        rip_after, now = get_rip_after(device)
        if rip_after and rip_after < now:
            logger.info('Auto-ripping %s (rip_after: %s)', hostname, device.customdata['rip_after'])
            device.delete()
        else:
            logger.info('Not ripping %s (rip_after: %s)', hostname, device.customdata['rip_after'])


def extend_builder(config, user):
    manager = get_manager(config)
    eq_project_id = config['core'].get('project_id')
    hostname = '%s-builder.ci.kernel.org' % user

    device = get_builder(manager, eq_project_id, hostname)
    if device and 'rip_after' in device.customdata:
        rip_after, now = get_rip_after(device)
        if not rip_after:
            logger.info('Could not get auto-rip time for %s', hostname)
            return

        rip_when = rip_after - now
        if rip_after > now and rip_when.seconds > 3600:
            logger.info('%s already has more than 1H left', hostname)
            return

        rip_ts = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        new_rip_after = rip_ts.isoformat(timespec='seconds')

        customdata = json.dumps({'rip_after': new_rip_after})
        device.customdata = customdata
        device.update()
        logger.info('%s will be ripped in 1H (at %s).', hostname, new_rip_after)
    else:
        logger.info('Sorry, no such device: %s', hostname)


def read_config(cfgfile):
    from configparser import ConfigParser, ExtendedInterpolation
    if not os.path.exists(cfgfile):
        sys.stderr.write('ERROR: config file %s does not exist' % cfgfile)
        sys.exit(1)
    fconfig = ConfigParser(interpolation=ExtendedInterpolation())
    fconfig.read(cfgfile)

    if 'core' not in fconfig:
        sys.stderr.write('ERROR: missing [core] section in %s' % cfgfile)
        sys.exit(1)

    return fconfig


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config-file', dest='cfgfile', required=True,
                        help='Config file to use')
    parser.add_argument('-u', '--user', dest='user', required=True,
                        help='User section to parse')
    parser.add_argument('-q', '--quiet', dest='quiet', action='store_true', default=False,
                        help='Quiet operation (cron mode)')
    parser.add_argument('-d', '--debug', dest='debug', action='store_true', default=False,
                        help='Output debug information')
    parser.add_argument('action', help='Action to perform')

    cmdargs = parser.parse_args()
    _config = read_config(cmdargs.cfgfile)
    logger.setLevel(logging.DEBUG)

    logfile = _config['core'].get('logfile', '')
    if logfile:
        ch = logging.FileHandler(logfile)
        formatter = logging.Formatter(f'[%(asctime)s] {cmdargs.user}: %(message)s')
        ch.setFormatter(formatter)
        ch.setLevel(logging.INFO)
        logger.addHandler(ch)

    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    ch.setFormatter(formatter)
    if cmdargs.quiet:
        ch.setLevel(logging.CRITICAL)
    elif cmdargs.debug:
        ch.setLevel(logging.DEBUG)
    else:
        ch.setLevel(logging.INFO)
    logger.addHandler(ch)

    if 'user_%s' % cmdargs.user not in _config:
        logger.critical('Section [user_%s] not in %s', cmdargs.user, cmdargs.cfgfile)
        sys.exit(1)

    if not _config['core'].get('project_id'):
        logger.critical('core.project_id not set')
        sys.exit(1)

    if cmdargs.action == 'create':
        create_builder(_config, cmdargs.user)
    elif cmdargs.action == 'destroy':
        destroy_builder(_config, cmdargs.user)
    elif cmdargs.action == 'check':
        check_builder(_config, cmdargs.user)
    elif cmdargs.action == 'rip':
        rip_builder(_config, cmdargs.user)
    elif cmdargs.action == 'extend':
        extend_builder(_config, cmdargs.user)
    else:
        logger.critical('Unknown action: %s', cmdargs.action)
        sys.exit(1)
