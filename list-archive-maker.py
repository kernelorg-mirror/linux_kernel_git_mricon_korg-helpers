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
import time
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
            'content-*',
            'errors-to',
            'x-mailing-list',
            'resent-to',
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

    rejectsbox = None
    if rejectsfile:
        rejectsbox = mailbox.mbox(rejectsfile)

    for sourcefile in sources:
        is_pipermail = False
        is_nntp = False

        # do you have a '://' in you?
        if sourcefile.find('://') > 0:
            if sourcefile.find('nntp://') == 0:
                is_nntp = True
            else:
                is_pipermail = True

        if is_nntp:
            # Expect in format nntp://news.gmane.org/gmane.linux.network
            sys.stdout.write('Connecting to %s...' % sourcefile)
            chunks = sourcefile.split('/')
            server, group = chunks[-2:]
            import nntplib
            nntplib._MAXLINE = 1 << 20
            server = nntplib.NNTP(server)
            resp, count, first, last, name = server.group(group)
            total = int(last)

            def nntp_msg_gen(last):
                aid = 1
                while aid <= last:
                    try:
                        resp, ainfo = server.article(aid)
                        message = email.message_from_bytes(b'\n'.join(ainfo[2]))
                        yield message
                    except nntplib.NNTPTemporaryError:
                        # Ignore one-off article failures -- probably deletes
                        pass
                    finally:
                        aid += 1

            inbox = nntp_msg_gen(total)

        else:
            if is_pipermail:
                sourcefile = grab_pipermail_archive(sourcefile, outdir)
                sys.stdout.write('parsing...')
                sys.stdout.flush()
                inbox = mailbox.mbox(sourcefile)
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

            msgid = msgid.strip()
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
            to = ''
            cc = ''
            recvtime = None
            is_our_list = False
            for hdrname, hdrval in list(msg._headers):
                lhdrname = hdrname.lower()
                if is_nntp and lhdrname.find('original-') == 0:
                    lhdrname = lhdrname.replace('original-', '')
                    hdrname = hdrname.replace('Original-', '')

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
                            if lhdrval.find(listid) >= 0:
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
                # have appeared so we can insert the merged list there rather
                # than strangely at the end.

                elif lhdrname == 'to':
                    for pair in email.utils.getaddresses([hdrval]):
                        if cc.find(pair[1]) >= 0:
                            # already in Cc, so no need to add it to To
                            continue
                        if len(to) and to.find(pair[1]) < 0:
                            to += ', %s' % email.utils.formataddr(pair)
                        else:
                            to += email.utils.formataddr(pair)

                elif lhdrname == 'cc':
                    for pair in email.utils.getaddresses([hdrval]):
                        if to.find(pair[1]) >= 0:
                            # already in To, so no need to add it to CCs
                            continue
                        if len(cc) and cc.find(pair[1]) < 0:
                            cc += ', %s' % email.utils.formataddr(pair)
                        else:
                            cc += email.utils.formataddr(pair)

                else:
                    newhdrs.append((hdrname, hdrval))

            if len(to):
                newhdrs.append(('To', to))

            if len(cc):
                newhdrs.append(('Cc', cc))

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
                elif is_nntp:
                    # We assume everything in the newsgroup matches our first list-id
                    newhdrs.append(('List-Id', listids[0]))
                    is_our_list = True
                else:
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
                        msg._headers.append(('X-Import-Rejected-Reason', 'No matching List-ID'))
                        rejectsbox.add(msg)
                    skipped += 1
                    notourlist += 1
                    continue

            msg._headers = newhdrs

            msgdate = recvtime
            if msgdate is None:
                # fine, use the date in the message, even if it's bogus
                msgdate = email.utils.parsedate_tz(str(msg['Date']))

            mboxname = '%04d-%02d.mbx' % (msgdate[0], msgdate[1])
            if is_nntp:
                msg.set_unixfrom('From nntp@import %s' % time.strftime('%c', msgdate[:9]))

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
                        help=('Mbox file with archives, can be multiple. '
                              'Paths with trailing "/" will be treated as maildirs.'))
    parser.add_argument('-pipermail',
                        help='Download mailman pipermail archives from this URL')
    parser.add_argument('-nntp',
                        help=('Download full archives from a NNTP server, '
                              'e.g. -n nntp://news.gmane.com/gmane.linux.kernel'))
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

    if args.nntp:
        mboxes.append(args.nntp)

    if args.source:
        mboxes += args.source

    if not mboxes:
        print('You have to specify at least one source (-s, -p, or -n)')
        sys.exit(1)

    # Make list ID matching case insensitive to match more mail
    listids = [listid.lower() for listid in args.listids]

    newids = main(mboxes, args.exportdir, knownids, listids, args.rejected)

    if newids is None or not args.knownids:
        sys.exit(0)

    new_idlist = knownids + newids
    print('Wrote %s msgids into %s (%s new)' % (len(new_idlist), args.knownids, len(newids)))
    with open(args.knownids, 'w') as fh:
        fh.write('\n'.join(new_idlist))
        fh.close()
