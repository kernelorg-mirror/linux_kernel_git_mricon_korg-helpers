"""Tests for pr-tracker-bot.py."""

import email
import sqlite3

import pytest
from conftest import load_script

prbot = load_script('pr-tracker-bot.py')

# A full-length commit-id, as emitted by request-pull on kernel.org.
IOMMUFD = 'e771677c937da5808f7b6c1f0e4a97ec1a84f8a8'
MM_TIP = '2f9f5887b42711595e768b9dc0582dccfdf60c3b'

REMOTE = ('git://git.kernel.org/pub/scm/linux/kernel/git/akpm/mm '
          'tags/mm-stable-2026-06-17-15-16')


def make_pull_request(since, upto):
    """Build a request-pull style email body with given since/up-to commits."""
    body = (
        'Linus, please merge these MM updates for the 7.2-rc1 tree, thanks.\n'
        '\n'
        'The following changes since commit {since}:\n'
        '\n'
        '  Merge tag \'for-linus-iommufd\' (2026-06-17 12:33:23 -0700)\n'
        '\n'
        'are available in the Git repository at:\n'
        '\n'
        '  {remote}\n'
        '\n'
        'for you to fetch changes up to {upto}:\n'
        '\n'
        '  Some final commit (2026-06-17 15:16:00 -0700)\n'
    ).format(since=since, upto=upto, remote=REMOTE)

    raw = (
        'From: Andrew Morton <akpm@linux-foundation.org>\n'
        'To: Linus Torvalds <torvalds@linux-foundation.org>\n'
        'Subject: [GIT PULL] MM updates for 7.2-rc1\n'
        'Message-Id: <test-pull@example.com>\n'
        'Date: Wed, 17 Jun 2026 15:31:44 -0700\n'
        'Content-Type: text/plain; charset="utf-8"\n'
        '\n'
    ) + body
    return email.message_from_string(raw)


class TestCommitIdsMatch:
    """Tests for the abbreviation-tolerant commit-id comparison."""

    def test_identical_full(self):
        assert prbot.commit_ids_match(IOMMUFD, IOMMUFD)

    def test_different_full(self):
        assert not prbot.commit_ids_match(IOMMUFD, MM_TIP)

    def test_abbrev_prefix_matches_full(self):
        assert prbot.commit_ids_match(IOMMUFD[:12], IOMMUFD)

    def test_case_insensitive(self):
        assert prbot.commit_ids_match(IOMMUFD.upper(), IOMMUFD)

    def test_empty_or_none(self):
        assert not prbot.commit_ids_match('', IOMMUFD)
        assert not prbot.commit_ids_match(IOMMUFD, None)
        assert not prbot.commit_ids_match(None, None)


class TestRecordPrData:
    """Tests for record_pr_data(), including the empty-PR guard."""

    @pytest.fixture
    def db(self):
        conn = sqlite3.connect(':memory:')
        c = conn.cursor()
        prbot.db_init_pr_sqlite_db(c)
        conn.commit()
        yield c
        conn.close()

    def test_normal_pr_is_tracked(self, db):
        """A well-formed PR (since != up-to) gets recorded with the tip."""
        msg = make_pull_request(since=IOMMUFD, upto=MM_TIP)
        assert prbot.record_pr_data(0, 'msgcommit1', msg, db) is True
        rows = db.execute('SELECT pr_commit_id FROM prs').fetchall()
        assert rows == [(MM_TIP,)]

    def test_empty_pr_is_ignored(self, db):
        """Andrew's bug: since == up-to means there is nothing to pull."""
        msg = make_pull_request(since=IOMMUFD, upto=IOMMUFD)
        assert prbot.record_pr_data(0, 'msgcommit2', msg, db) is False
        rows = db.execute('SELECT pr_commit_id FROM prs').fetchall()
        assert rows == []

    def test_empty_pr_abbrev_is_ignored(self, db):
        """Empty-PR detection still works if one side is abbreviated."""
        msg = make_pull_request(since=IOMMUFD[:12], upto=IOMMUFD)
        assert prbot.record_pr_data(0, 'msgcommit3', msg, db) is False
        rows = db.execute('SELECT pr_commit_id FROM prs').fetchall()
        assert rows == []
