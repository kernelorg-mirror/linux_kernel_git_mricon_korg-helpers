#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Send gitmail from a cron process instead of a git hook
#
# Large merge pushes to large repositories like linux.git can contain
# thousands of commits. Generating git-commit mail for each of them in
# the post-receive hook takes a long time, which greatly annoys Linux
# devs, who are subtle and quick to anger.
#
# This wrapper to git_multimail can run from cron instead. It keeps track
# of the previously processed commit per each of the heads in a repo. This
# script is what generates mail sent to git-commits-head@vger.kernel.org,
# and you should adapt it to your needs if you're going to use it.
#
# You should additionally redirect stdout to some logfile.
#
# Author:  Konstantin Ryabitsev <konstantin@linuxfoundation.org>
#

import os
import re
import sys
import argparse
import json

from fcntl import lockf, LOCK_EX, LOCK_NB

# You need the latest dev version that supports excludeMergeRevisions
import git_multimail as gm

gm.REVISION_HEADER_TEMPLATE = """\
Date: %(send_date)s
To: %(recipients)s
Cc: %(cc_recipients)s
Subject: %(oneline)s
MIME-Version: 1.0
Content-Type: text/%(contenttype)s; charset=%(charset)s
Content-Transfer-Encoding: 8bit
From: %(fromaddr)s
Reply-To: %(reply_to)s
Message-Id: <git-mailbomb-%(repo_shortname)s-%(short_refname)s-%(rev)s@kernel.org>
X-Git-Refname: %(refname)s
X-Git-Rev: %(rev)s
X-Git-Parent: %(parents)s
X-Git-Multimail-Version: %(multimail_version)s
"""

gm.REVISION_INTRO_TEMPLATE = """\
Commit:     %(rev)s
Parent:     %(parents)s
Refname:    %(refname)s
"""

gm.LINK_TEXT_TEMPLATE="""\
Web:        %(browse_url)s
"""

gm.REVISION_FOOTER_TEMPLATE=''

def legacy_filter(lines):
    # This is done to match with old legacy mailer format. I'm not sure
    # what is so special about that format, but I know for certain that
    # if I change it, there will be no end to complaints about it,
    # because it broke someone's automation.
    for line in lines:
        if re.match('^commit [A-Fa-f0-9]{40}$', line):
            continue
        elif re.match('^Commit:     ', line):
            yield line.replace('Commit:     ', 'Committer:  ', 1)
        elif re.match('^Merge: [A-Fa-f0-9]+ [A-Fa-f0-9]+', line):
            yield line.replace('Merge: ', 'Merge:      ', 1)
        else:
            yield line

def main(args):
    os.environ['GIT_DIR'] = args.gitdir

    head_lines = gm.read_git_lines(['show-ref', '--heads'])
    if not len(head_lines):
        print('Was not able to read refs in %s' % args.gitdir)
        sys.exit(1)

    try:
        lockfh = open('%s.lock' % args.statefile, 'w')
        lockf(lockfh, LOCK_EX | LOCK_NB)
    except IOError:
        print('Could not obtain an exclusive lock, assuming another process is running.')
        sys.exit(0)

    initial_run = False
    try:
        with open(args.statefile, 'r') as sfh:
            known = json.load(sfh)
    except IOError as ex:
        known = {}
        initial_run = True
    except ValueError as ex:
        print('Corrupted state file?')
        known = {}
        initial_run = True

    needs_doing = []
    for line in head_lines:
        sha, refname = line.split()
        if refname in known and sha != known[refname]:
            needs_doing.append((refname, known[refname], sha))
        known[refname] = sha

    if initial_run:
        with open(args.statefile, 'w') as sfh:
            json.dump(known, sfh, indent=4)
        print('Initial run, not sending any mails.')
        sys.exit(0)

    if not len(needs_doing):
        # nothing to do
        print('No changes in any heads, exiting early.')
        sys.exit(0)

    config = gm.Config('multimailhook')

    # These can be set in the repository, but since the script
    # runs from a mirrored clone of the master repo, it's easier
    # to set all configs in this section instead.
    gm.Config.add_config_parameters((
        'multimailhook.commitList=%s' % args.recipient,
        'multimailhook.commitEmailFormat=text',
        'multimailhook.commitBrowseURL=https://git.kernel.org/torvalds/c/%(id)s',
        'multimailhook.mailer=smtp',
        'multimailhook.smtpServer=%s' % args.smtpserv,
        'multimailhook.from=Linux Kernel Mailing List <linux-kernel@vger.kernel.org>',
        'multimailhook.envelopeSender=devnull@kernel.org',
        'multimailhook.combineWhenSingleCommit=False',
        'multimailhook.maxCommitEmails=100000',
        'multimailhook.excludeMergeRevisions=True',
        'multimailhook.commitLogOpts=-C --stat -p --cc --pretty=fuller',
        ))

    environment = gm.GenericEnvironment(config=config)
    environment.check()

    if args.dryrun:
        mailer = gm.OutputMailer(sys.stdout)
    else:
        mailer = gm.choose_mailer(config, environment)

    for refname, oldrev, newrev in needs_doing:
        changes = [
                gm.ReferenceChange.create(
                    environment,
                    gm.read_git_output(['rev-parse', '--verify', oldrev]),
                    gm.read_git_output(['rev-parse', '--verify', newrev]),
                    refname,
                    ),
                ]

        push = gm.Push(environment, changes)
        push.send_emails(mailer, body_filter=legacy_filter)

    if not args.dryrun:
        with open(args.statefile, 'w') as sfh:
            json.dump(known, sfh, indent=4)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('-s', dest='statefile', action='store', required=True,
                        help='State file to use')
    parser.add_argument('-g', dest='gitdir', action='store', required=True,
                        help='Git repository to use')
    parser.add_argument('-r', dest='recipient', action='store', required=True,
                        help='Recipient email address')
    parser.add_argument('-m', dest='smtpserv', action='store', required=True,
                        help='SMTP Server to use')
    parser.add_argument('-d', dest='dryrun', action='store_true',
                        help='Do not mail anything, just do a dry run')

    args = parser.parse_args()

    main(args)
