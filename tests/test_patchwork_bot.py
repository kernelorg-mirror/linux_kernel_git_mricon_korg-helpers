"""Tests for git-patchwork-bot.py."""

from conftest import load_script

import pytest

pwbot = load_script('git-patchwork-bot.py')

# Shorthand for the bluetooth PR that exposed the bug
BT_URL_GIT = 'git://git.kernel.org/pub/scm/linux/kernel/git/bluetooth/bluetooth'
BT_URL_DOTGIT = BT_URL_GIT + '.git'
BT_TAG = 'for-net-2026-03-12'


class TestGetUrlPath:
    """Tests for the get_url_path() URL normalization function."""

    @pytest.mark.parametrize("url,expected", [
        # Basic protocol stripping + hostname removal
        (
            'git://git.kernel.org/pub/scm/linux/kernel/git/bluetooth/bluetooth.git',
            'pub/scm/linux/kernel/git/bluetooth/bluetooth',
        ),
        (
            'https://git.kernel.org/pub/scm/linux/kernel/git/bluetooth/bluetooth',
            'pub/scm/linux/kernel/git/bluetooth/bluetooth',
        ),
        # Protocol differences should produce identical paths
        (
            'http://git.kernel.org/pub/scm/linux/kernel/git/tip/tip.git',
            'pub/scm/linux/kernel/git/tip/tip',
        ),
        # Subdomain variations (git vs gitolite)
        (
            'https://gitolite.kernel.org/pub/scm/linux/kernel/git/tip/tip.git',
            'pub/scm/linux/kernel/git/tip/tip',
        ),
        # Trailing slash should be stripped
        (
            'https://git.kernel.org/pub/scm/linux/kernel/git/user/repo.git/',
            'pub/scm/linux/kernel/git/user/repo',
        ),
        # .git suffix should be stripped
        (
            'git://git.kernel.org/pub/scm/linux/kernel/git/user/repo.git',
            'pub/scm/linux/kernel/git/user/repo',
        ),
        # No .git suffix should also work
        (
            'git://git.kernel.org/pub/scm/linux/kernel/git/user/repo',
            'pub/scm/linux/kernel/git/user/repo',
        ),
        # GitHub-style URLs
        (
            'https://github.com/user/repo.git',
            'user/repo',
        ),
        (
            'https://github.com/user/repo',
            'user/repo',
        ),
    ])
    def test_url_normalization(self, url, expected):
        assert pwbot.get_url_path(url) == expected

    def test_protocol_and_suffix_differences_match(self):
        """The exact scenario from the bluetooth PR bug."""
        # Patchwork stores the URL from the email (with .git)
        pull_host = 'git://git.kernel.org/pub/scm/linux/kernel/git/bluetooth/bluetooth.git'
        # Merge commit may drop the .git suffix
        m_host = 'git://git.kernel.org/pub/scm/linux/kernel/git/bluetooth/bluetooth'
        assert pwbot.get_url_path(pull_host) == pwbot.get_url_path(m_host)

    def test_protocol_mismatch_matches(self):
        """The original insteadOf bug that commit 44169ff was fixing."""
        pull_host = 'git://git.kernel.org/pub/scm/linux/kernel/git/user/repo.git'
        m_host = 'https://git.kernel.org/pub/scm/linux/kernel/git/user/repo.git'
        assert pwbot.get_url_path(pull_host) == pwbot.get_url_path(m_host)

    def test_subdomain_mismatch_matches(self):
        """gitolite vs git subdomain variation."""
        pull_host = 'https://git.kernel.org/pub/scm/linux/kernel/git/user/repo.git'
        m_host = 'https://gitolite.kernel.org/pub/scm/linux/kernel/git/user/repo.git'
        assert pwbot.get_url_path(pull_host) == pwbot.get_url_path(m_host)

    def test_all_three_differences_at_once(self):
        """Protocol + subdomain + .git suffix all differ."""
        pull_host = 'git://git.kernel.org/pub/scm/linux/kernel/git/user/repo.git'
        m_host = 'https://gitolite.kernel.org/pub/scm/linux/kernel/git/user/repo'
        assert pwbot.get_url_path(pull_host) == pwbot.get_url_path(m_host)


class TestParseMergeLogline:
    """Tests for parse_merge_logline()."""

    def test_merge_tag(self):
        logline = f"Merge tag '{BT_TAG}' of {BT_URL_GIT}"
        assert pwbot.parse_merge_logline(logline) == (BT_TAG, BT_URL_GIT)

    def test_merge_branch(self):
        logline = "Merge branch 'fixes' of https://git.kernel.org/pub/scm/linux/kernel/git/user/repo"
        ref, url = pwbot.parse_merge_logline(logline)
        assert ref == 'fixes'
        assert url == 'https://git.kernel.org/pub/scm/linux/kernel/git/user/repo'

    def test_merge_with_double_quotes(self):
        logline = f'Merge tag "{BT_TAG}" of {BT_URL_DOTGIT}'
        assert pwbot.parse_merge_logline(logline) == (BT_TAG, BT_URL_DOTGIT)

    def test_merge_bare_url(self):
        """Merge with just a URL and no ref defaults to master."""
        logline = 'Merge https://github.com/user/repo'
        ref, url = pwbot.parse_merge_logline(logline)
        assert ref == 'master'
        assert url == 'https://github.com/user/repo'

    def test_not_a_merge(self):
        assert pwbot.parse_merge_logline('Fix null pointer dereference in foo') is None

    def test_merge_without_url(self):
        assert pwbot.parse_merge_logline("Merge branch 'fixes' into main") is None

    def test_real_bluetooth_logline(self):
        """The exact logline from the merge commit that triggered the bug."""
        logline = (
            f"Merge tag '{BT_TAG}' of "
            "git://git.kernel.org/pub/scm/linux/kernel/git/bluetooth/bluetooth"
        )
        ref, url = pwbot.parse_merge_logline(logline)
        assert ref == BT_TAG
        assert 'bluetooth/bluetooth' in url
        assert '.git' not in url


class TestFindMatchingPr:
    """Tests for find_matching_pr()."""

    def _make_prs(self, *entries):
        """Build a PR set from (host, refname, patch_id) tuples."""
        return set(entries)

    def test_exact_match(self):
        prs = self._make_prs((BT_URL_DOTGIT, f'tags/{BT_TAG}', 42))
        assert pwbot.find_matching_pr(BT_URL_DOTGIT, BT_TAG, prs) == 42

    def test_dotgit_mismatch(self):
        """Patchwork has .git, merge commit doesn't — the bluetooth bug."""
        prs = self._make_prs((BT_URL_DOTGIT, f'tags/{BT_TAG}', 42))
        assert pwbot.find_matching_pr(BT_URL_GIT, BT_TAG, prs) == 42

    def test_protocol_mismatch(self):
        """git:// in patchwork, https:// in merge commit."""
        prs = self._make_prs((BT_URL_DOTGIT, f'tags/{BT_TAG}', 42))
        m_host = BT_URL_GIT.replace('git://', 'https://')
        assert pwbot.find_matching_pr(m_host, BT_TAG, prs) == 42

    def test_subdomain_mismatch(self):
        prs = self._make_prs((BT_URL_DOTGIT, f'tags/{BT_TAG}', 42))
        m_host = BT_URL_GIT.replace('git.kernel.org', 'gitolite.kernel.org')
        assert pwbot.find_matching_pr(m_host, BT_TAG, prs) == 42

    def test_all_differences(self):
        """Protocol + subdomain + .git suffix all differ at once."""
        prs = self._make_prs((BT_URL_DOTGIT, f'tags/{BT_TAG}', 42))
        m_host = 'https://gitolite.kernel.org/pub/scm/linux/kernel/git/bluetooth/bluetooth'
        assert pwbot.find_matching_pr(m_host, BT_TAG, prs) == 42

    def test_trailing_slash_on_pull_host(self):
        prs = self._make_prs((BT_URL_DOTGIT + '/', f'tags/{BT_TAG}', 42))
        assert pwbot.find_matching_pr(BT_URL_GIT, BT_TAG, prs) == 42

    def test_no_match_wrong_repo(self):
        prs = self._make_prs((
            'git://git.kernel.org/pub/scm/linux/kernel/git/netdev/net.git',
            f'tags/{BT_TAG}', 42,
        ))
        assert pwbot.find_matching_pr(BT_URL_GIT, BT_TAG, prs) is None

    def test_no_match_wrong_ref(self):
        prs = self._make_prs((BT_URL_DOTGIT, 'tags/wrong-tag', 42))
        assert pwbot.find_matching_pr(BT_URL_GIT, BT_TAG, prs) is None

    def test_no_match_empty_prs(self):
        assert pwbot.find_matching_pr(BT_URL_GIT, BT_TAG, set()) is None

    def test_skips_none_patch_id(self):
        prs = self._make_prs((BT_URL_DOTGIT, f'tags/{BT_TAG}', None))
        assert pwbot.find_matching_pr(BT_URL_GIT, BT_TAG, prs) is None

    def test_multiple_prs_returns_correct_one(self):
        prs = self._make_prs(
            ('git://git.kernel.org/pub/scm/linux/kernel/git/netdev/net.git', 'tags/net-v1', 10),
            (BT_URL_DOTGIT, f'tags/{BT_TAG}', 42),
            ('git://git.kernel.org/pub/scm/linux/kernel/git/hid/hid.git', 'tags/hid-v1', 20),
        )
        assert pwbot.find_matching_pr(BT_URL_GIT, BT_TAG, prs) == 42
