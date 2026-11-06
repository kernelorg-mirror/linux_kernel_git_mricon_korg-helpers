#!/usr/bin/env python3
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

import argparse
import email.message
import email.policy
import email.utils
import fnmatch
import logging
import mailbox
import os
import sqlite3
import sys
from dataclasses import dataclass

__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'
__VERSION__ = '2.1'

logger = logging.getLogger(__name__)

# Write the archive in 8bit, without folding, so headers and bodies stay as
# close to what the list sent as possible (and DKIM signatures stay valid).
EMLPOLICY = email.policy.EmailPolicy(utf8=True, cte_type='8bit', max_line_length=None)

# Only retain the headers that are important to us; must be lowercase for
# matching purposes, and may use shell-globbing style wildcards. Received
# headers are filtered further for privacy, see sanitize_message().
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
            'dkim-*',
            'x-developer-*',
            }

# Recipient headers that tell us a message was sent to our list, even when
# the subscriber only kept the copy that arrived via a different list.
ADDRHDRS = ('to', 'cc', 'resent-to', 'x-mailing-list')

Mailbox = mailbox.mbox | mailbox.Maildir


class MessageRejected(Exception):
    """The message does not belong in the archive; args[0] is the reason."""


@dataclass
class ListFilter:
    # Matched against the id inside the List-Id header, may be globs
    patterns: list[str]
    # Posting addresses of the list, for messages without a matching List-Id
    addresses: set[str]
    # The List-Id we add to messages that we matched by address
    canonical: str | None

    @classmethod
    def from_listids(cls, listids: list[str]) -> 'ListFilter':
        listids = [listid.lower() for listid in listids] or ['*']
        patterns = [listid for listid in listids if '@' not in listid]
        # Globs can only stand for a List-Id; there is no way to tell which
        # list a glob means when looking at a recipient address.
        plain = [listid for listid in listids if not any(char in listid for char in '*?[')]
        addresses = {listid if '@' in listid else listid.replace('.', '@', 1) for listid in plain}
        canonical = None
        if plain:
            # The first list-id is the canonical one, and someone who passes only a
            # list address for a non-standard list still gets a sane List-Id.
            canonical = plain[0].replace('@', '.', 1)
        return cls(patterns, addresses, canonical)

    def matches_list_id(self, hdrval: str) -> bool:
        # List-Id is "Optional Description <the.list.id>" (RFC 2919)
        listid = hdrval.lower().strip()
        if '<' in listid:
            listid = listid.split('<', 1)[1].split('>', 1)[0]
        return any(fnmatch.fnmatchcase(listid, pattern) for pattern in self.patterns)

    def mentions_address(self, hdrval: str) -> bool:
        hdrval = hdrval.lower()
        return any(addr in hdrval for addr in self.addresses)


def formataddr(pair: tuple[str, str]) -> str:
    try:
        return email.utils.formataddr(pair)
    except UnicodeEncodeError:
        # The real name is encoded in a broken way, but the address is
        # what matters, so keep just that.
        return pair[1]


def get_msgid(msg: email.message.Message) -> str:
    msgid = msg.get('Message-Id') or msg.get('Resent-Message-Id')
    if msgid is None:
        # Most likely a FOLDER-INTERNAL DATA marker or some other system message
        raise MessageRejected('No Message-ID')
    return str(msgid).strip()


def get_archive_month(msg: email.message.Message) -> tuple[int, int]:
    # The Date header is set by the sender and is often bogus, so prefer the
    # delivery time from the topmost Received header that has a usable one.
    candidates = [str(hdrval).split(';')[-1].strip() for hdrval in msg.get_all('Received', [])]
    candidates.append(str(msg.get('Date', '')))
    for candidate in candidates:
        parsed = email.utils.parsedate_tz(candidate)
        if parsed is not None:
            return parsed[0], parsed[1]
    raise MessageRejected('No usable date')


def sanitize_message(msg: email.message.Message, listfilter: ListFilter,
                     extrahdrs: list[tuple[str, str]]) -> None:
    """Replace the headers of msg with the ones fit for a public archive.

    The message is only modified when it is accepted, so rejected messages
    can be saved exactly as we found them.
    """
    newhdrs: list[tuple[str, str]] = list()
    # Malformed messages can have multiple To and Cc headers that repeat
    # addresses; merge them into one header each, listing each address once.
    recipients: dict[str, list[str]] = {'to': list(), 'cc': list()}
    seenaddrs: set[str] = set()
    is_our_list = False
    is_addressed_to_list = False

    # raw_items keeps undecodable 8bit headers as they are instead of
    # turning them into Header objects.
    for hdrname, hdrval in msg.raw_items():
        lhdrname = hdrname.lower()
        if not any(fnmatch.fnmatchcase(lhdrname, pattern) for pattern in WANTHDRS):
            continue

        if lhdrname in ADDRHDRS:
            for _, addr in email.utils.getaddresses([hdrval]):
                if addr.lower() in listfilter.addresses:
                    is_addressed_to_list = True

        if lhdrname == 'received':
            # Hops after the list server only describe the subscriber's mail setup
            if listfilter.mentions_address(hdrval):
                newhdrs.append((hdrname, hdrval))
        elif lhdrname == 'list-id':
            # Drop the List-Id of other lists the message was cross-posted to
            if listfilter.matches_list_id(hdrval):
                newhdrs.append((hdrname, hdrval))
                is_our_list = True
        elif lhdrname in recipients:
            for pair in email.utils.getaddresses([hdrval]):
                if pair[1].lower() not in seenaddrs:
                    seenaddrs.add(pair[1].lower())
                    recipients[lhdrname].append(formataddr(pair))
        else:
            newhdrs.append((hdrname, hdrval))

    if not is_our_list:
        if not (is_addressed_to_list and listfilter.canonical):
            raise MessageRejected('No matching List-ID')
        newhdrs.append(('List-Id', f'<{listfilter.canonical}>'))

    for hdrname, addrs in (('To', recipients['to']), ('Cc', recipients['cc'])):
        if addrs:
            newhdrs.append((hdrname, ', '.join(addrs)))

    newhdrs += extrahdrs

    for hdrname in set(msg.keys()):
        del msg[hdrname]
    for hdrname, hdrval in newhdrs:
        msg[hdrname] = hdrval


def open_mailbox(path: str) -> Mailbox:
    if os.path.isdir(path):
        return mailbox.Maildir(path)
    return mailbox.mbox(path)


def show_progress(text: str, final: bool = False) -> None:
    # Large archives take a while; only redraw a progress line for humans.
    if sys.stderr.isatty() and logger.isEnabledFor(logging.INFO):
        sys.stderr.write(f'\r{text}' + ('\n' if final else ''))
        sys.stderr.flush()


def process_archives(sources: list[str], outdir: str, knownids: set[str], listids: list[str],
                     rejectsfile: str | None, asmaildir: bool, extrahdrs: list[tuple[str, str]]) -> dict[str, int]:
    """Archive all new list messages from sources into outdir.

    Archived Message-IDs are added to knownids. Returns how many messages
    were written to each output mailbox.
    """
    listfilter = ListFilter.from_listids(listids)
    outboxes: dict[str, Mailbox] = dict()
    writecount: dict[str, int] = dict()
    rejectsbox = mailbox.mbox(rejectsfile) if rejectsfile else None

    for sourcefile in sources:
        inbox = open_mailbox(sourcefile)
        total = len(inbox)
        logger.info('%s: %s messages', sourcefile, total)
        rejects: dict[str, int] = dict()

        for counter, msg in enumerate(inbox, start=1):
            skipped = ', '.join(f'{count} {reason}' for reason, count in rejects.items())
            show_progress(f'  {counter}/{total} (skipped: {skipped or "none"})')
            try:
                msgid = get_msgid(msg)
                if msgid in knownids:
                    # We already have it, or the sources contain multiple
                    # deliveries of it; the first one we found wins.
                    raise MessageRejected('Duplicate Message-ID')
                year, month = get_archive_month(msg)
                sanitize_message(msg, listfilter, extrahdrs)
            except MessageRejected as ex:
                reason = str(ex)
                rejects[reason] = rejects.get(reason, 0) + 1
                logger.debug('Rejected %s: %s', msg.get('Message-Id'), reason)
                if rejectsbox is not None:
                    msg['X-Import-Rejected-Reason'] = reason
                    rejectsbox.add(msg)
                continue

            mboxname = outdir if asmaildir else os.path.join(outdir, f'{year:04d}-{month:02d}.mbx')
            if mboxname not in outboxes:
                outboxes[mboxname] = mailbox.Maildir(mboxname) if asmaildir else mailbox.mbox(mboxname)
                writecount[mboxname] = 0

            # as_bytes keeps undecodable 8bit content as it was received
            outboxes[mboxname].add(msg.as_bytes(policy=EMLPOLICY))
            knownids.add(msgid)
            writecount[mboxname] += 1

        inbox.close()
        skipped = ', '.join(f'{count} {reason}' for reason, count in rejects.items())
        show_progress(f'  {total}/{total} (skipped: {skipped or "none"})', final=True)

    if rejectsbox is not None:
        rejectsbox.close()

    if not outboxes:
        logger.info('No new messages found.')
    for mboxname in sorted(outboxes):
        logger.info('%s: %s new (%s total)', mboxname, writecount[mboxname], len(outboxes[mboxname]))
        outboxes[mboxname].close()

    return writecount


def load_known_ids(path: str) -> set[str]:
    if path.endswith('.sqlite3'):
        # public-inbox msgmap, which stores Message-IDs without brackets
        dbconn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            return {f'<{row[0]}>' for row in dbconn.execute('SELECT mid FROM msgmap')}
        finally:
            dbconn.close()
    with open(path, encoding='utf-8', errors='surrogateescape') as fh:
        return set(fh.read().splitlines())


def save_known_ids(path: str, knownids: set[str]) -> None:
    # Sorted, so the file is easy to diff between runs. Message-IDs with
    # undecodable 8bit bytes must survive the round trip unchanged.
    with open(path, 'w', encoding='utf-8', errors='surrogateescape') as fh:
        fh.writelines(f'{msgid}\n' for msgid in sorted(knownids))


def parse_extrahdrs(extrahdrs: list[str]) -> list[tuple[str, str]]:
    parsed = list()
    for hdr in extrahdrs:
        name, sep, val = hdr.partition(':')
        if not sep or not name.strip():
            raise argparse.ArgumentTypeError(f'Not a valid header: {hdr}')
        if val.strip():
            parsed.append((name.strip(), val.strip()))
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect mailing list messages we haven't yet archived into sanitized mailboxes",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('-s', '--source', nargs='+', required=True,
                        help='Mbox files or maildir directories with archives, can be multiple')
    parser.add_argument('-e', '--exportdir', required=True,
                        help='Export dir where to put sanitized archives')
    parser.add_argument('-m', '--as-maildir', action='store_true', default=False,
                        help='Export as maildir instead of mailboxes')
    parser.add_argument('-k', '--known-ids',
                        help='File with known Message-IDs (one per line, or public-inbox msgmap.sqlite3)')
    parser.add_argument('-l', '--list-ids', nargs='+', default=list(),
                        help=('Limit to just these list-ids (can be multiple, and can be globs). '
                              'The first non-glob list-id is added to messages sent to the list '
                              'that have no matching List-Id header. Add the list address for '
                              'lists whose address is not the list-id with the first "." replaced by "@".'))
    parser.add_argument('-r', '--rejected',
                        help=('Mailbox file where to save messages that were rejected '
                              '(adds X-Import-Rejected-Reason header)'))
    parser.add_argument('-x', '--extrahdrs', nargs='+', metavar='FULLHDR', default=list(),
                        help='Extra headers to inject into each message, e.g. "X-Archive-Source: alice"')
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Only output errors')
    parser.add_argument('-v', '--verbose', action='store_true', default=False,
                        help='Show why each message was rejected')
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(format='%(message)s',
                        level=logging.ERROR if args.quiet else logging.DEBUG if args.verbose else logging.INFO)

    try:
        extrahdrs = parse_extrahdrs(args.extrahdrs)
    except argparse.ArgumentTypeError as ex:
        parser.error(str(ex))

    if not args.as_maildir:
        os.makedirs(args.exportdir, exist_ok=True)

    knownids: set[str] = set()
    if args.known_ids and os.path.exists(args.known_ids):
        knownids = load_known_ids(args.known_ids)
        logger.info('Loaded %s message-ids from %s', len(knownids), args.known_ids)

    before = len(knownids)
    process_archives(args.source, args.exportdir, knownids, args.list_ids, args.rejected, args.as_maildir,
                     extrahdrs)

    # We never write into a public-inbox msgmap; public-inbox owns it.
    if args.known_ids and not args.known_ids.endswith('.sqlite3'):
        save_known_ids(args.known_ids, knownids)
        logger.info('Wrote %s message-ids into %s (%s new)', len(knownids), args.known_ids, len(knownids) - before)


if __name__ == '__main__':
    main()
