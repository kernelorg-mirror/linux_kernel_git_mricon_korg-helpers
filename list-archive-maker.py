#!/usr/bin/env python3
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

import os
import sys
import mailbox
import email.utils
import email.policy
import fnmatch
import argparse

from typing import Tuple, List, Set

from email import charset
charset.add_charset('utf-8', None)

# Set our own policy
EMLPOLICY = email.policy.EmailPolicy(utf8=True, cte_type='8bit', max_line_length=None)

# Only retain the headers that are important to us
# must be lowercase for matching purposes.
# We treat "Received" headers with extra care for privacy, but if you
# want to exclude them entirely, you can remove them from this list.
# We also consider shell-globbing style wildcards.
WANTHDRS = {'return-path',
            'received',
            'sender',
            'from',
            'to',
            'cc',
            'subject',
            'date',
            'message-id',
            'resent-message-id',
            'reply-to',
            'in-reply-to',
            'references',
            'mime-*',
            'list-*',
            'content-*',
            'errors-to',
            'x-mailing-list',
            'resent-to',
            }

__VERSION__ = '2.0'


def formataddr(pair: Tuple[str, str]) -> str:
    try:
        return email.utils.formataddr(pair)
    except UnicodeEncodeError:
        # This might happen if the realname is encoded in a broken way; just
        # drop the real name then.
        return email.utils.formataddr((None, pair[1]))


def process_archives(sources: List[str], outdir: str, knownset: Set[str], listids: List[str],
                     rejectsfile: str, asmaildir: bool, extrahdrs: List[Tuple[str, str]]) -> Set[str]:
    outboxes = dict()
    writecount = dict()
    seenids = set()

    if asmaildir:
        outbox = mailbox.Maildir(outdir)
        outboxes[outdir] = outbox
        writecount[outdir] = 1

    # convert listids into email addresses by replacing the first '.' to '@'.
    # if you're working with a mailing list that has a non-standard list-id, you
    # can specify the list email address as part of the listids to satisfy this check.
    eaddrs = list()
    for listid in listids:
        if listid.find('@') < 0:
            eaddrs.append(listid.replace('.', '@', 1))
        else:
            eaddrs.append(listid)

    rejectsbox = None
    if rejectsfile:
        rejectsbox = mailbox.mbox(rejectsfile)

    for sourcefile in sources:
        sys.stdout.write('Opening %s...' % os.path.basename(sourcefile))
        sys.stdout.flush()
        # If the filename ends with /, we treat as maildir
        if sourcefile[-1] == '/':
            inbox = mailbox.Maildir(sourcefile)
        else:
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
            if msgid is None and msg.get('resent-message-id', ''):
                msgid = msg['resent-message-id']

            if msgid is None:
                # Huh, no message-id? Most likely, FOLDER-INTERNAL DATA marker or some other
                # system message.
                if rejectsfile:
                    msg._headers.append(('X-Import-Rejected-Reason', 'No Message-ID')) # noqa
                    rejectsbox.add(msg)
                skipped += 1
                nomsgid += 1
                continue

            msgid = msgid.strip()
            if msgid in knownset:
                # Duplicate Message-ID, either because we already have it in the known-ids,
                # or because the inbox has messages with same IDs. There is no fix for the
                # latter condition, so we just assume they got delivered multiple times and
                # use the first one found.
                if rejectsfile:
                    msg._headers.append(('X-Import-Rejected-Reason', 'Duplicate Message-ID')) # noqa
                    rejectsbox.add(msg)
                skipped += 1
                dupmsgid += 1
                continue

            if extrahdrs:
                msg._headers += extrahdrs  # noqa

            # Remove headers not in WANTHDRS list and any Received:
            # lines that do not mention the list email address
            newhdrs = list()
            to = list()
            cc = list()
            recvtime = None
            is_our_list = False
            for hdrname, hdrval in list(msg._headers):  # noqa
                lhdrname = hdrname.lower()
                lhdrval = hdrval.lower()
                wanthdr = False
                for hdrmatch in WANTHDRS:
                    if fnmatch.fnmatch(lhdrname, hdrmatch):
                        wanthdr = True
                        break

                if not wanthdr:
                    continue

                if lhdrname == 'received':
                    # noinspection PyBroadException
                    try:
                        if recvtime is None:
                            # Use the first Received header we find for the message date
                            # (for the purposes of knowing which mbox file to put it in)
                            recvtime = email.utils.parsedate_tz(hdrval.split(';')[-1].strip())
                        # does hdrval contain one of our email addresses?
                        for eaddr in eaddrs:
                            if lhdrval.find(eaddr) >= 0:
                                newhdrs.append((hdrname, hdrval))
                                break
                    except:
                        # Something went horribly wrong, eh?
                        pass

                elif lhdrname == 'list-id':
                    for listid in listids:
                        if lhdrval.find(listid) >= 0 or fnmatch.fnmatch(lhdrval, listid):
                            newhdrs.append((hdrname, hdrval))
                            is_our_list = True
                            break

                elif lhdrname == 'x-mailing-list':
                    for listid in listids:
                        if lhdrval.find(listid) >= 0:
                            # Stick the list-id that's first in our collection,
                            # since we assume that it's the canonical one
                            newhdrs.append(('List-Id', listids[0]))
                            is_our_list = True
                            break

                # Malformed emails can have multiple to: and cc: fields.  Merge
                # so there's one field for each header type.
                #
                # Save the place in newhdrs where the first to or cc list would
                # have appeared, so we can insert the merged list there rather
                # than strangely at the end.

                elif lhdrname == 'to':
                    for pair in email.utils.getaddresses([hdrval]):
                        if pair[1] in cc:
                            # already in Cc, so no need to add it to "To"
                            continue
                        to.append(formataddr(pair))

                elif lhdrname == 'cc':
                    for pair in email.utils.getaddresses([hdrval]):
                        if pair[1] in to:
                            # already in To, so no need to add it to CCs
                            continue
                        cc.append(formataddr(pair))

                else:
                    newhdrs.append((hdrname, hdrval))

            if len(to):
                newhdrs.append(('To', ', '.join(to)))

            if len(cc):
                newhdrs.append(('Cc', ', '.join(cc)))

            if not is_our_list:
                # Sometimes a message is cc'd to multiple mailing lists and the
                # archives only contain a copy of the message that was delivered to a
                # different list. E.g. something can be To: linux-mm@vger.kernel.org
                # and also Cc: linux-kernel@vger.kernel.org and we're looking for the
                # LKML list-id, the archive may only contain the copy that arrived to
                # linux-mm. We try to hedge for this by looking in the "To" and "Cc"
                # fields for any indication that this was intended for our mailing list.
                for eaddr in eaddrs:
                    if (str(msg.get('to', '')).lower().find(eaddr) >= 0 or
                            str(msg.get('cc', '')).lower().find(eaddr) >= 0 or
                            str(msg.get('resent-to', '')).lower().find(eaddr) >= 0):
                        # insert the list-id header
                        # (assuming the first one in the list to be the canonical one)
                        newhdrs.append(('List-ID', '<%s>' % listids[0]))
                        is_our_list = True
                        break

                if not is_our_list:
                    # Well, we tried everything
                    if rejectsfile:
                        msg._headers.append(('X-Import-Rejected-Reason', 'No matching List-ID')) # noqa
                        rejectsbox.add(msg)
                    skipped += 1
                    notourlist += 1
                    continue

            msg._headers = newhdrs

            msgdate = recvtime
            if msgdate is None:
                # fine, use the date in the message, even if it's bogus
                msgdate = email.utils.parsedate_tz(str(msg['Date']))

            if asmaildir:
                mboxname = outdir
            else:
                mboxname = '%04d-%02d.mbx' % (msgdate[0], msgdate[1])

            # do we have this mbox open already?
            if mboxname in outboxes:
                outbox = outboxes[mboxname]
            else:
                outbox = mailbox.mbox('%s/%s' % (outdir, mboxname))
                outboxes[mboxname] = outbox
                writecount[mboxname] = 1

            try:
                outbox.add(msg.as_string(policy=EMLPOLICY).encode())
                seenids.add(msgid)
                knownset.add(msgid)
                writecount[mboxname] += 1
            except:  # noqa
                # Oh well, toss it
                pass

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
    else:
        print('No new messages found.')

    return seenids


def main(args: argparse.Namespace):
    if not args.as_maildir and not os.path.isdir(args.exportdir):
        os.mkdir(args.exportdir)

    if args.known_ids and os.path.exists(args.known_ids):
        if args.known_ids.endswith('.sqlite3'):
            import sqlite3
            dbconn = sqlite3.connect(args.known_ids, sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
            cur = dbconn.cursor()
            rows = cur.execute('SELECT mid FROM msgmap').fetchall()
            knownids = {f'<{x[0]}>' for x in rows}
        else:
            with open(args.known_ids, 'r') as fh:
                knownids = set(fh.read().splitlines())
                fh.close()
        print('Loaded %s message-ids from "%s"' % (len(knownids), args.known_ids))
    else:
        # should we load message-ids from existing mailboxes found in the export dir?
        # right now we're just appending to them, which is probably not expected behaviour.
        knownids = set()

    if not args.source:
        print('You have to specify at least one source')
        sys.exit(1)

    # Make list ID matching case-insensitive to match more mail
    if args.list_ids:
        listids = [listid.lower() for listid in args.list_ids]
    else:
        listids = ['*']

    extrahdrs = list()
    if args.extrahdrs:
        for hdr in args.extrahdrs:
            name, val = hdr.split(':', maxsplit=1)
            if val.strip():
                extrahdrs.append((name.strip(), val.strip()))

    newids = process_archives(args.source, args.exportdir, knownids, listids, args.rejected, args.as_maildir,
                              extrahdrs)

    if newids is None or not args.known_ids or args.known_ids.endswith('.sqlite3'):
        sys.exit(0)

    knownids.update(newids)
    print('Wrote %s msgids into %s (%s new)' % (len(knownids), args.known_ids, len(newids)))
    with open(args.known_ids, 'w') as fh:
        fh.write('\n'.join(knownids))
        fh.close()


if __name__ == '__main__':
    # noinspection PyTypeChecker
    parser = argparse.ArgumentParser(
        description="Make a mbox of LKML messages we haven't yet archived",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('-s', '--source', nargs='+',
                        help=('Mbox file with archives, can be multiple. '
                              'Paths with trailing "/" will be treated as maildirs.'))
    parser.add_argument('-e', '--exportdir', required=True, default='list-archives',
                        help='Export dir where to put sanitized archives')
    parser.add_argument('-m', '--as-maildir', action='store_true', default=False,
                        help='Export as maildir instead of mailboxes')
    parser.add_argument('-k', '--known-ids',
                        help='File with known Message-IDs (one per line, or msgmap.sqlite3)')
    parser.add_argument('-l', '--list-ids', nargs='+',
                        help='Limit to just these list-ids (can be multiple)')
    parser.add_argument('-r', '--rejected',
                        help='Mailbox file where to save messages that were rejected '
                             '(adds X-Import-Rejected-Reason header)')
    parser.add_argument('-x', '--extrahdrs', nargs='+', metavar='FULLHDR',
                        help='Extra headers to inject into each message')

    main(parser.parse_args())
