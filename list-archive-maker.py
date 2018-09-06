#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# List archive collator and sanitizer
#
# The purpose of this script is to make a complete mailing list archive by
# collecting individual archives from individual list subscribers. It uses a
# list of known IDs to locate messages we don't already have in the
# archive, and sanitizes the headers to remove as much private
# information as possible. It also makes sure to consider messages
# that have the proper mailing list header, so you can aim it at any
# inbox to find relevant messages.
#
# Example usage:
#   list-archive-maker.py -s mail/lists/* -k known-msgids.list \
#                         -l linux-kernel.vger.kernel.org -e collected
#
# The results will be written out into a "collected" dir in the YYYY-MM.mbx format.
# You can review these files to make sure the script did the right thing.
#
# Author:  Konstantin Ryabitsev <konstantin@linuxfoundation.org>
#

from __future__ import (absolute_import,
                        division,
                        print_function,
                        unicode_literals)

import os
import sys
import mailbox
import email.utils

# Remove headers that are likely to be either of the following:
# - violating the privacy of the person donating the logs
# - useless for archiving
# - useless in general
#
# header names must be lowercase
TRIMHDRS = {'delivered-to', 'status', 'precedence', 'x-sender', 'x-status', 'x-uid', 'x-from_', 'x-keywords',
            'x-authentication-warning', 'x-received', 'x-spam-checker-version', 'x-spam-level', 'x-spam-status',
            'x-scanned-by', 'x-virus-scanned', 'x-virus-status', 'x-mimedefang-filter', 'dkim-signature',
            'x-google-dkim-signature', 'x-gm-message-state', 'received-spf', 'x-original-to', 'x-source-folder',
            'domainkey-signature', 'x-mimeole', 'x-spamprobe', 'x-evolution', 'x-loop', 'x-orcpt', 'x-antivirus',
            'x-antivirus-status', 'x-greylist', 'x-face', 'x-priority', 'importance', 'x-provags-id'}


def main(sources, outdir, msgids, listids):
    outboxes = {}
    writecount = {}
    seenids = []
    knownset = set(msgids)

    # convert listids into email addresses by replacing the first '.' to '@'.
    # if you're working with a mailing list that has a non-standard list-id, you
    # can specify the list email address as part of the listids to satisfy this check.
    eaddrs = []
    for listid in listids:
        if listid.find('@') < 0:
            eaddrs.append(listid.replace('.', '@', 1))
        else:
            eaddrs.append(listid)

    for sourcefile in sources:
        sys.stdout.write('Opening %s...' % sourcefile)
        sys.stdout.flush()
        inbox = mailbox.mbox(sourcefile)
        total = len(inbox)
        sys.stdout.write('%s messages\n' % total)
        sys.stdout.flush()

        counter = 0
        skipped = 0
        dupmsgid = 0
        nomsgid = 0
        notourlist = 0

        for msg in inbox:
            counter += 1
            sys.stdout.write('  %s/%s (%s skipped: %s dupmsgid, %s nomsgid, %s notourlist)\r' %
                             (counter, total, skipped, dupmsgid, nomsgid, notourlist))
            sys.stdout.flush()

            msgid = msg['message-id']
            if msgid is None:
                # Huh, no message-id? Most likely, FOLDER-INTERNAL DATA marker or some other
                # system message.
                skipped += 1
                nomsgid += 1
                continue
            if msgid in knownset:
                # Duplicate Message-ID, either because we already have it in the known-ids,
                # or because the inbox has messages with same IDs. There is no fix for the
                # latter condition, so we just assume they got delivered multiple times and
                # use the first one found.
                skipped += 1
                dupmsgid += 1
                continue

            # Remove headers in the TRIMHDRS list and any Received:
            # lines that do not mention the list email address
            newhdrs = []
            recvtime = None
            is_our_list = False
            for hdrname, hdrval in msg._headers:
                lhdrname = hdrname.lower()
                if lhdrname not in TRIMHDRS:
                    if lhdrname == 'received':
                        try:
                            if recvtime is None:
                                # Use the first Received header we find for the message date
                                # (for the purposes of knowing which mbox file to put it in)
                                recvtime = email.utils.parsedate_tz(hdrval.split(';')[-1].strip())
                            # does hdrval contain one of our email addresses?
                            for eaddr in eaddrs:
                                if hdrval.find(eaddr) >= 0:
                                    newhdrs.append((hdrname, hdrval))
                                    break
                        except:
                            # Something went horribly wrong, eh?
                            pass
                    elif lhdrname == 'list-id':
                            this_listid = hdrval.strip().lower().lstrip('<').rstrip('>')
                            if this_listid in listids:
                                newhdrs.append((hdrname, hdrval))
                                is_our_list = True
                    else:
                        newhdrs.append((hdrname, hdrval))

            if not is_our_list:
                # Sometimes a message is cc'd to multiple mailing lists and the
                # archives only contain a copy of the message that was delivered to a
                # different list. E.g. something can be To: linux-mm@vger.kernel.org
                # and also Cc: linux-kernel@vger.kernel.org and we're looking for the
                # LKML list-id, the archive may only contain the copy that arrived to
                # linux-mm. We try to hedge for this by looking in the "To" and "Cc"
                # fields for any indication that this was intended for our mailing list.
                for eaddr in eaddrs:
                    if ('to' in msg and msg['to'].find(eaddr) >= 0 or
                            'cc' in msg and msg['cc'].find(eaddr) >= 0):
                        # insert the list-id header
                        # (assuming the first one in the list to be the canonical one)
                        newhdrs.append(('List-ID', '<%s>' % listids[0]))
                    
                else:
                    skipped += 1
                    notourlist += 1
                    continue

            msg._headers = newhdrs

            msgdate = recvtime
            if msgdate is None:
                # fine, use the date in the message, even if it's bogus
                msgdate = email.utils.parsedate_tz(msg['Date'])

            mboxname = '%04d-%02d.mbx' % (msgdate[0], msgdate[1])

            # do we have this mbox open already?
            if mboxname in outboxes:
                outbox = outboxes[mboxname]
                writecount[mboxname] += 1
            else:
                outbox = mailbox.mbox('%s/%s' % (outdir, mboxname))
                outboxes[mboxname] = outbox
                writecount[mboxname] = 1

            outbox.add(msg)
            seenids.append(msgid)
            knownset.add(msgid)

        inbox.close()
        sys.stdout.write('  %s/%s (%s skipped: %s dupmsgid, %s nomsgid, %s notourlist)\n' %
                         (counter, total, skipped, dupmsgid, nomsgid, notourlist))

    allboxes = sorted(outboxes)

    if len(allboxes):
        print()
        print('Summary')
        for mboxname in allboxes:
            print('  %s: %s new (%s total)' %
                  (os.path.join(outdir, mboxname), writecount[mboxname], len(outboxes[mboxname])))
            outboxes[mboxname].close()
        return seenids
    else:
        print('No new messages found.')
        return None


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(
        description="Make a mbox of LKML messages we haven't yet archived",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('-sourcembox', required=True, nargs='+',
                        help='Source mbox with your archives in it, can be multiple')
    parser.add_argument('-exportdir', required=True,
                        help='Export dir where to put sanitized archives')
    parser.add_argument('-knownids', default=None,
                        help='File with known Message-IDs (one per line)')
    parser.add_argument('-listids', required=True, nargs='+',
                        help='List ID to match, can be multiple')

    args = parser.parse_args()

    if args.knownids:
        fh = open(args.knownids, 'r')
        msgids = fh.read().splitlines()
        fh.close()
        print('Loaded %s message-ids from "%s"' % (len(msgids), args.idlist))
    else:
        msgids = []

    if not os.path.isdir(args.exportdir):
        os.mkdir(args.exportdir)

    newids = main(args.sourcembox, args.exportdir, msgids, args.listids)

    if newids is None or not args.knownids:
        sys.exit(0)

    new_idlist = msgids + newids
    print('Wrote %s msgids into %s (%s new)' % (len(new_idlist), args.knownids, len(newids)))
    fh = open(args.knownids, 'w')
    fh.write('\n'.join(new_idlist))
    fh.close()
