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
import re
import fnmatch

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
            'content-type',
            'errors-to',
            'x-mailing-list',
            }

def main(sources, outdir, msgids, listids, rejectsfile):
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

    if rejectsfile:
        rejectsbox = mailbox.mbox(rejectsfile)

    for sourcefile in sources:
        is_pipermail = False
        # do you have a '://' in you?
        if sourcefile.find('://') > 0:
            is_pipermail = True
            sourcefile = grab_pipermail_archive(sourcefile, outdir)
            sys.stdout.write('parsing...')
            sys.stdout.flush()
        else:
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
                    msg._headers.append(('X-Import-Rejected-Reason', 'No Message-ID'))
                    rejectsbox.add(msg)
                skipped += 1
                nomsgid += 1
                continue
            if msgid in knownset:
                # Duplicate Message-ID, either because we already have it in the known-ids,
                # or because the inbox has messages with same IDs. There is no fix for the
                # latter condition, so we just assume they got delivered multiple times and
                # use the first one found.
                if rejectsfile:
                    msg._headers.append(('X-Import-Rejected-Reason', 'Duplicate Message-ID'))
                    rejectsbox.add(msg)
                skipped += 1
                dupmsgid += 1
                continue

            # Remove headers not in WANTHDRS list and any Received:
            # lines that do not mention the list email address
            newhdrs = []
            recvtime = None
            is_our_list = False
            for hdrname, hdrval in msg._headers:
                lhdrname = hdrname.lower()
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
                            if hdrval.find(eaddr) >= 0:
                                newhdrs.append((hdrname, hdrval))
                                break
                    except:
                        # Something went horribly wrong, eh?
                        pass

                elif lhdrname == 'list-id':
                        for listid in listids:
                            if hdrval.lower().find(listid) >= 0:
                                newhdrs.append((hdrname, hdrval))
                                is_our_list = True
                                break

                elif lhdrname == 'x-mailing-list':
                        for listid in listids:
                            if hdrval.lower().find(listid) >= 0:
                                # Stick the list-id that's first in our collection,
                                # since we assume that it's the canonical one
                                newhdrs.append(('List-Id', listids[0]))
                                is_our_list = True
                                break

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
                if is_pipermail:
                    # Pipermail doesn't preserve the List-Id nor "To" headers,
                    # so put them back in place
                    newhdrs.append(('To', eaddrs[0]))
                    newhdrs.append(('List-Id', listids[0]))
                    is_our_list = True
                else:
                    for eaddr in eaddrs:
                        if (str(msg.get('to', '')).find(eaddr) >= 0 or
                                str(msg.get('cc', '')).find(eaddr) >= 0):
                            # insert the list-id header
                            # (assuming the first one in the list to be the canonical one)
                            newhdrs.append(('List-ID', '<%s>' % listids[0]))
                            is_our_list = True
                            break

                if not is_our_list:
                    # Well, we tried everything
                    if rejectsfile:
                        msg._headers.append(('X-Import-Rejected-Reason', 'No matching List-ID'))
                        rejectsbox.add(msg)
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
        if is_pipermail:
            os.unlink(sourcefile)

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


def parse_pipermail_index(pipermail_url):
    try:
        from bs4 import BeautifulSoup
    except ImportError as ex:
        print('You need to install python-beautifulsoup4 to parse pipermail URLs')
        print(ex)
        sys.exit(1)

    print('Grabbing the pipermail index from %s' % pipermail_url)
    with urllib_request.urlopen(pipermail_url) as response:
        index = response.read()
        response.close()

    soup = BeautifulSoup(index, features='lxml')

    mboxes = []
    for tag in soup.find_all('a'):
        # we are looking for a href that ends with .txt.gz
        if 'href' in tag.attrs and tag.attrs['href'][-7:] == '.txt.gz':
            mboxes.append(os.path.join(pipermail_url, tag.attrs['href']))

    return mboxes


def grab_pipermail_archive(pipermail_url, outdir):
    import gzip

    chunks = pipermail_url.split('/')

    sys.stdout.write('Grabbing %s...' % chunks[-1])
    sys.stdout.flush()
    # stick it into outdir/_tmp_pipermail_%last-chunk
    local_file = os.path.join(outdir, '_tmp_pipermail_%s' % chunks[-1])

    with urllib_request.urlopen(pipermail_url) as response:
        with gzip.GzipFile(fileobj=response) as uncompressed:
            # XXX: this can be horribly large
            mboxdata = uncompressed.read().decode('utf-8', errors='replace')
            uncompressed.close()
        response.close()

    # Pipermail does a nasty thing where it doesn't properly handle
    # lines in the body that start with "From ". First, we add ">" to
    # all lines starting with "From " and then fix some of them in the
    # next step.
    sys.stdout.write('demangling...')
    sys.stdout.flush()
    regex = r'^From '
    subst = '>From '
    mboxdata = re.sub(regex, subst, mboxdata, 0, re.MULTILINE)
    # Fix pipermail mangling where it changes some email addresses
    # to be ' at ' instead of '@'. This is easiest to do with a
    # handful of regexes than via actual message body manipulation
    # as parf of the python's email.message object
    regex = r'(<[^>]+) at ([^>]+>)'
    subst = '\\1@\\2'
    mboxdata = re.sub(regex, subst, mboxdata, 0, re.MULTILINE)
    regex = r'^>?(From:? \S+) at (\S+\..*)'
    mboxdata = re.sub(regex, subst, mboxdata, 0, re.MULTILINE)

    with open(local_file, 'wb') as out_fh:
        out_fh.write(mboxdata.encode('utf-8'))

    out_fh.close()
    return local_file


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(
        description="Make a mbox of LKML messages we haven't yet archived",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('-source', nargs='+',
                        help=('Source mailbox with your archives in it, can be multiple. '
                              'Paths with trailing / will be treated as maildirs'))
    parser.add_argument('-pipermail',
                        help='Alternatively, get mailman pipermail archives from this URL')
    parser.add_argument('-exportdir', required=True, default='list-archives',
                        help='Export dir where to put sanitized archives')
    parser.add_argument('-knownids',
                        help='File with known Message-IDs (one per line)')
    parser.add_argument('-listids', required=True, nargs='+',
                        help='List ID to match, can be multiple')
    parser.add_argument('-rejected',
                        help='Mailbox file where to save messages that were rejected '
                             '(adds X-Import-Rejected-Reason header)')

    args = parser.parse_args()

    if not os.path.isdir(args.exportdir):
        os.mkdir(args.exportdir)

    if args.knownids and os.path.exists(args.knownids):
        with open(args.knownids, 'r') as fh:
            knownids = fh.read().splitlines()
            fh.close()
        print('Loaded %s message-ids from "%s"' % (len(knownids), args.knownids))
    else:
        # should we load message-ids from existing mailboxes found in the export dir?
        # right now we're just appending to them, which is probably not expected behaviour.
        knownids = []

    # are you asking for a pipermail grab?
    mboxes = []
    if args.pipermail is not None:
        import urllib.request as urllib_request
        mboxes = parse_pipermail_index(args.pipermail)
        if not mboxes:
            print('Could not find any .txt.gz files listed at %s' % args.pipermail)
            sys.exit(1)

    if args.source:
        mboxes += args.source

    if not mboxes:
        print('You have to specify a pipermail URL or a list of mbox files (or both)')
        sys.exit(1)

    newids = main(mboxes, args.exportdir, knownids, args.listids, args.rejected)

    if newids is None or not args.knownids:
        sys.exit(0)

    new_idlist = knownids + newids
    print('Wrote %s msgids into %s (%s new)' % (len(new_idlist), args.knownids, len(newids)))
    with open(args.knownids, 'w') as fh:
        fh.write('\n'.join(new_idlist))
        fh.close()
