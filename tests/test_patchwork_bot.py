"""Tests for git-patchwork-bot.py."""

from conftest import load_script

import pytest

pwbot = load_script('git-patchwork-bot.py')


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
