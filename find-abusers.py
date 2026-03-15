#!/usr/bin/env python3

import re
import os
import sys
import argparse
import datetime
import subprocess
import pyasn


# Adapted from:
# https://stackoverflow.com/questions/260273/most-efficient-way-to-search-the-last-x-lines-of-a-file/260433
def reversed_lines(_fh):
    part = ''
    for bblock in reversed_blocks(_fh):
        block = bblock.decode(errors='ignore')
        for c in reversed(block):
            if c == '\n' and part:
                yield part[::-1]
                part = ''
            part += c
    if part:
        yield part[::-1]


def reversed_blocks(_fh, blocksize=4096):
    _fh.seek(0, os.SEEK_END)
    here = _fh.tell()
    while 0 < here:
        delta = min(blocksize, here)
        here -= delta
        _fh.seek(here, os.SEEK_SET)
        yield _fh.read(delta)


users = dict()
sources = dict()

parser = argparse.ArgumentParser()
parser.add_argument('--last', help='For the last N minutes (default: 30)', type=int, default=30)
parser.add_argument('--asndb', help='Combine results by ASN, using this asn source file')
cmdargs = parser.parse_args()

cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=cmdargs.last)

chungus = 4096
for wsgifile in ('git-http-backend.log', 'cgit.log'):
    print('Reading %s' % wsgifile)
    with open(f'/var/log/uwsgi-apps/{wsgifile}', 'rb') as fh:
        for line in reversed_lines(fh):
            if not re.search(r'\(HTTP/\d\.\d \d+\)', line):
                continue
            # trim any junk at the start of the line
            line = line[line.find('[pid: '):]
            chunks = line.split()
            if len(chunks) < 5:
                continue

            when = ' '.join(chunks[11:16]).strip('[]')
            try:
                twhen = datetime.datetime.strptime(when, '%c').replace(tzinfo=datetime.timezone.utc)
            except ValueError:
                continue
            if twhen < cutoff:
                break
            ip = chunks[4]
            dur = None
            for chunk in chunks[4:]:
                if chunk == 'msecs':
                    dur = int(msecs)
                    break
                msecs = chunk

            if dur is None:
                continue

            if ip not in users:
                users[ip] = [0, 0]
            users[ip][0] += dur
            users[ip][1] += 1
            if ip not in sources:
                sources[ip] = set()
            sources[ip].add(wsgifile)

jcmd = ['/bin/journalctl', '--no-pager', '--since', f'{cmdargs.last} minutes ago', '-o', 'short-unix',
        '_COMM=systemd']
print('Reading journalctl/git-daemon')
sp = subprocess.Popen(jcmd, stdout=subprocess.PIPE)
procs = dict()
while True:
    line = sp.stdout.readline()
    if not line:
        break
    line = line.decode()
    if line.find('Started Git-daemon server ') > 0:
        matches = re.search(r'^(\S+).*\((.*):(\d+)\)', line)
        if not matches:
            continue
        uts = matches.group(1)
        ip = matches.group(2).strip('[]')
        port = matches.group(3)
        proc = f'{ip}:{port}'
        procs[proc] = uts
        continue
    elif line.find('Deactivated successfully') > 0:
        matches = re.search(r'^(\d+\.\d+).*gitd@\S+-(\S+):(\S+)\.service:', line)
        if not matches:
            continue
        uts = matches.group(1)
        ip = matches.group(2).strip('[]')
        port = matches.group(3)
        proc = f'{ip}:{port}'
        if proc not in procs:
            continue
        dur = int((float(uts) - float(procs[proc]))*1000)
        if ip not in users:
            users[ip] = [0, 0]
        users[ip][0] += dur
        users[ip][1] += 1
        if ip not in sources:
            sources[ip] = set()
        sources[ip].add('gitd')

if cmdargs.asndb:
    print('Collating by ASN')
    asndb = pyasn.pyasn(cmdargs.asndb)
    byasn = dict()
    for ip, acs in users.items():
        aspfx = asndb.lookup(ip)[1]
        if not aspfx:
            continue
        if aspfx not in byasn:
            byasn[aspfx] = acs
        else:
            byasn[aspfx][0] += acs[0]
            byasn[aspfx][1] += acs[1]
        if aspfx not in sources:
            sources[aspfx] = sources[ip]
        else:
            sources[aspfx].update(sources[ip])
    users = byasn

summary = list()
for user, acs in users.items():
    dur, times = acs
    if not dur or dur < 1000:
        continue
    summary.append((int(dur/1000), times, user, ', '.join(sources[user])))

print('---')
summary.sort()
print('secs\ttimes\tconsumer')
for entry in summary:
    print('%s\t%s\t%s (%s)' % entry)
    
# vim: ts=4 et sw=4 sts=4 ai si :
