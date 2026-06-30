"""Tests for git-patchwork-bot.py."""

import datetime
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from conftest import load_script

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


class TestGetPatchworkPullRequests:
    """Tests for get_patchwork_pull_requests_by_project() with mocked API."""

    def _mock_rm(self, api_results):
        rm = MagicMock()
        rm.get_patches_list.return_value = api_results
        return rm

    def test_basic_pull_request(self):
        rm = self._mock_rm([
            {'id': 42, 'pull_url': f'{BT_URL_DOTGIT} tags/{BT_TAG}'},
        ])
        prs = pwbot.get_patchwork_pull_requests_by_project(rm, 1, ['new'])
        assert (BT_URL_DOTGIT, f'tags/{BT_TAG}', 42) in prs

    def test_pull_url_without_refname(self):
        """When pull_url has no branch/tag, refname defaults to master."""
        rm = self._mock_rm([
            {'id': 10, 'pull_url': 'https://github.com/user/repo.git'},
        ])
        prs = pwbot.get_patchwork_pull_requests_by_project(rm, 1, ['new'])
        assert ('https://github.com/user/repo.git', 'master', 10) in prs

    def test_entry_without_pull_url(self):
        """Patches without pull_url should be skipped."""
        rm = self._mock_rm([
            {'id': 10, 'pull_url': None},
            {'id': 11},
        ])
        prs = pwbot.get_patchwork_pull_requests_by_project(rm, 1, ['new'])
        assert len(prs) == 0

    def test_mixed_entries(self):
        """Mix of pull requests and regular patches."""
        rm = self._mock_rm([
            {'id': 10, 'pull_url': f'{BT_URL_DOTGIT} tags/{BT_TAG}'},
            {'id': 11, 'pull_url': None},
            {'id': 12, 'pull_url': 'https://github.com/user/repo.git fixes-branch'},
        ])
        prs = pwbot.get_patchwork_pull_requests_by_project(rm, 1, ['new'])
        assert len(prs) == 2
        ids = {p[2] for p in prs}
        assert ids == {10, 12}

    def test_empty_results(self):
        rm = self._mock_rm([])
        prs = pwbot.get_patchwork_pull_requests_by_project(rm, 1, ['new'])
        assert len(prs) == 0

    def test_query_params(self):
        """Verify correct API query parameters are sent."""
        rm = self._mock_rm([])
        pwbot.get_patchwork_pull_requests_by_project(rm, 99, ['new', 'under-review'])
        params = rm.get_patches_list.call_args[0][0]
        param_dict = dict(params)
        assert param_dict['project'] == 99
        assert param_dict['archived'] == 'false'
        assert param_dict['q'] == 'PULL'
        assert param_dict['order'] == '-date'


class TestGetPatchworkPatchesByHash:
    """Tests for get_patchwork_patches_by_project_hash() with mocked API."""

    def _mock_rm(self, api_results):
        rm = MagicMock()
        rm.get_patches_list.return_value = api_results
        return rm

    def test_single_match(self):
        rm = self._mock_rm([{'id': 100}])
        ids = pwbot.get_patchwork_patches_by_project_hash(rm, 1, 'abc123')
        assert ids == [100]

    def test_multiple_matches(self):
        rm = self._mock_rm([{'id': 100}, {'id': 101}, {'id': 102}])
        ids = pwbot.get_patchwork_patches_by_project_hash(rm, 1, 'abc123')
        assert ids == [100, 101, 102]

    def test_no_match(self):
        rm = self._mock_rm([])
        ids = pwbot.get_patchwork_patches_by_project_hash(rm, 1, 'abc123')
        assert ids == []

    def test_query_params(self):
        rm = self._mock_rm([])
        pwbot.get_patchwork_patches_by_project_hash(rm, 5, 'deadbeef')
        params = rm.get_patches_list.call_args[0][0]
        param_dict = dict(params)
        assert param_dict['project'] == 5
        assert param_dict['hash'] == 'deadbeef'
        assert param_dict['archived'] == 'false'


class TestGetPatchworkPatchesByMsgid:
    """Tests for get_patchwork_patches_by_project_msgid() with mocked API."""

    def _mock_rm(self, api_results):
        rm = MagicMock()
        rm.get_patches_list.return_value = api_results
        return rm

    def test_single_match(self):
        rm = self._mock_rm([{'id': 200}])
        ids = pwbot.get_patchwork_patches_by_project_msgid(rm, 1, 'msg@example.com')
        assert ids == [200]

    def test_no_match(self):
        rm = self._mock_rm([])
        ids = pwbot.get_patchwork_patches_by_project_msgid(rm, 1, 'msg@example.com')
        assert ids == []

    def test_query_params(self):
        rm = self._mock_rm([])
        pwbot.get_patchwork_patches_by_project_msgid(rm, 3, 'test@kernel.org')
        params = rm.get_patches_list.call_args[0][0]
        param_dict = dict(params)
        assert param_dict['project'] == 3
        assert param_dict['msgid'] == 'test@kernel.org'


class TestGetPatchworkHash:
    """Tests for get_patchwork_hash() diff hashing."""

    def test_simple_diff(self):
        diff = (
            'diff --git a/foo.c b/foo.c\n'
            '--- a/foo.c\n'
            '+++ b/foo.c\n'
            '@@ -1,3 +1,3 @@\n'
            ' context\n'
            '-old line\n'
            '+new line\n'
            ' context\n'
        )
        h = pwbot.get_patchwork_hash(diff)
        assert len(h) == 40  # sha1 hex digest

    def test_same_diff_same_hash(self):
        diff = (
            'diff --git a/foo.c b/foo.c\n'
            '--- a/foo.c\n'
            '+++ b/foo.c\n'
            '@@ -10,3 +10,3 @@\n'
            ' context\n'
            '-old\n'
            '+new\n'
        )
        assert pwbot.get_patchwork_hash(diff) == pwbot.get_patchwork_hash(diff)

    def test_line_numbers_ignored(self):
        """Patchwork hash ignores hunk line numbers, only keeps counts."""
        diff1 = (
            'diff --git a/foo.c b/foo.c\n'
            '--- a/foo.c\n'
            '+++ b/foo.c\n'
            '@@ -1,3 +1,3 @@\n'
            ' ctx\n'
            '-old\n'
            '+new\n'
        )
        diff2 = (
            'diff --git a/foo.c b/foo.c\n'
            '--- a/foo.c\n'
            '+++ b/foo.c\n'
            '@@ -99,3 +200,3 @@\n'
            ' ctx\n'
            '-old\n'
            '+new\n'
        )
        assert pwbot.get_patchwork_hash(diff1) == pwbot.get_patchwork_hash(diff2)

    def test_path_prefix_normalized(self):
        """Patchwork hash normalizes the -p1 top-directory prefix.

        Git always uses a/ and b/, but other tools might use different
        prefixes. The hash normalizes the first path component so that
        --- x/foo.c and --- a/foo.c produce the same hash.
        """
        diff1 = (
            'diff --git a/foo.c b/foo.c\n'
            '--- a/foo.c\n'
            '+++ b/foo.c\n'
            '@@ -1,1 +1,1 @@\n'
            '-old\n'
            '+new\n'
        )
        diff2 = (
            'diff --git x/foo.c y/foo.c\n'
            '--- x/foo.c\n'
            '+++ y/foo.c\n'
            '@@ -1,1 +1,1 @@\n'
            '-old\n'
            '+new\n'
        )
        assert pwbot.get_patchwork_hash(diff1) == pwbot.get_patchwork_hash(diff2)

    def test_content_before_diff_ignored(self):
        """Commit message text before the diff line should be ignored."""
        diff_only = (
            'diff --git a/foo.c b/foo.c\n'
            '--- a/foo.c\n'
            '+++ b/foo.c\n'
            '@@ -1,1 +1,1 @@\n'
            '-old\n'
            '+new\n'
        )
        with_preamble = (
            'Subject: Fix the thing\n'
            '\n'
            'This fixes a bug.\n'
            '\n'
            'diff --git a/foo.c b/foo.c\n'
            '--- a/foo.c\n'
            '+++ b/foo.c\n'
            '@@ -1,1 +1,1 @@\n'
            '-old\n'
            '+new\n'
        )
        assert pwbot.get_patchwork_hash(diff_only) == pwbot.get_patchwork_hash(with_preamble)

    def test_carriage_returns_normalized(self):
        diff_unix = 'diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-a\n+b\n'
        diff_dos = diff_unix.replace('\n', '\r\n')
        assert pwbot.get_patchwork_hash(diff_unix) == pwbot.get_patchwork_hash(diff_dos)


class TestListify:
    """Tests for listify()."""

    def test_list_input(self):
        assert pwbot.listify(['a', 'b']) == ['a', 'b']

    def test_list_input_returns_copy(self):
        original = ['a', 'b']
        result = pwbot.listify(original)
        assert result == original
        assert result is not original

    def test_string_input(self):
        assert pwbot.listify('hello') == ['hello']

    def test_none_input(self):
        assert pwbot.listify(None) == [None]

    def test_empty_list(self):
        assert pwbot.listify([]) == []


class TestGetTweaks:
    """Tests for get_tweaks() config merging."""

    def test_hconfig_overrides_pconfig(self):
        pconfig = {'from': 'project@example.com', 'summaryto': 'list@example.com'}
        hconfig = {'from': 'head@example.com'}
        result = pwbot.get_tweaks(pconfig, hconfig)
        assert result['from'] == 'head@example.com'
        assert result['summaryto'] == 'list@example.com'

    def test_pconfig_used_as_fallback(self):
        pconfig = {'from': 'project@example.com', 'alwayscc': 'cc@example.com'}
        hconfig = {}
        result = pwbot.get_tweaks(pconfig, hconfig)
        assert result['from'] == 'project@example.com'
        assert result['alwayscc'] == 'cc@example.com'

    def test_unknown_fields_ignored(self):
        pconfig = {'from': 'a@b.com', 'bogus': 'ignored'}
        hconfig = {'also_bogus': 'nope'}
        result = pwbot.get_tweaks(pconfig, hconfig)
        assert 'bogus' not in result
        assert 'also_bogus' not in result
        assert result['from'] == 'a@b.com'

    def test_empty_configs(self):
        assert pwbot.get_tweaks({}, {}) == {}

    def test_all_known_fields(self):
        fields = ['from', 'summaryto', 'onlyto', 'neverto', 'onlyifcc',
                   'neverifcc', 'alwayscc', 'alwaysbcc', 'cclist', 'ccall']
        pconfig = {f: f'p-{f}' for f in fields}
        result = pwbot.get_tweaks(pconfig, {})
        for f in fields:
            assert result[f] == f'p-{f}'


class TestGitGetNewRevs:
    """Tests for git_get_new_revs() with mocked git output."""

    MODULE = 'git_patchwork_bot'

    def test_basic_new_revs(self):
        db_heads = [('refs/heads/main', 'aaa')]
        git_heads = [('refs/heads/main', 'bbb')]
        git_output = [
            'deadbeef:Alice:alice@example.com:Fix null pointer bug',
            'cafebabe:Bob:bob@example.com:Add feature X',
        ]
        with patch.object(pwbot, 'git_get_command_lines', return_value=git_output):
            result = pwbot.git_get_new_revs('/fake/repo', db_heads, git_heads, [])
        assert 'refs/heads/main' in result
        revs = result['refs/heads/main']
        assert len(revs) == 2
        assert revs[0] == ('deadbeef', 'Fix null pointer bug', 'Alice <alice@example.com>')
        assert revs[1] == ('cafebabe', 'Add feature X', 'Bob <bob@example.com>')

    def test_no_changes(self):
        """Same commit in db and git means no new revs."""
        heads = [('refs/heads/main', 'aaa')]
        result = pwbot.git_get_new_revs('/fake/repo', list(heads), list(heads), [])
        assert result == {}

    def test_committer_filter(self):
        db_heads = [('refs/heads/main', 'aaa')]
        git_heads = [('refs/heads/main', 'bbb')]
        git_output = [
            'deadbeef:Alice:alice@example.com:Fix bug',
            'cafebabe:Bob:bob@example.com:Add feature',
        ]
        with patch.object(pwbot, 'git_get_command_lines', return_value=git_output):
            result = pwbot.git_get_new_revs('/fake/repo', db_heads, git_heads,
                                            ['alice@example.com'])
        revs = result['refs/heads/main']
        assert len(revs) == 1
        assert revs[0][0] == 'deadbeef'

    def test_empty_committer_name(self):
        """When committer name is empty, just use the email."""
        db_heads = [('refs/heads/main', 'aaa')]
        git_heads = [('refs/heads/main', 'bbb')]
        git_output = ['deadbeef::alice@example.com:Fix bug']
        with patch.object(pwbot, 'git_get_command_lines', return_value=git_output):
            result = pwbot.git_get_new_revs('/fake/repo', db_heads, git_heads, [])
        assert result['refs/heads/main'][0][2] == 'alice@example.com'

    def test_deleted_ref(self):
        """Ref present in db but gone from git should be removed from db_heads."""
        db_heads = [('refs/heads/gone', 'aaa'), ('refs/heads/main', 'bbb')]
        git_heads = [('refs/heads/main', 'ccc')]
        git_output = ['deadbeef:A:a@b.com:Fix']
        with patch.object(pwbot, 'git_get_command_lines', return_value=git_output):
            result = pwbot.git_get_new_revs('/fake/repo', db_heads, git_heads, [])
        # The deleted ref should have been removed from db_heads
        assert ('refs/heads/gone', 'aaa') not in db_heads
        assert 'refs/heads/main' in result

    def test_merges_flag(self):
        """When merges=False, --no-merges should be in the git args."""
        db_heads = [('refs/heads/main', 'aaa')]
        git_heads = [('refs/heads/main', 'bbb')]
        with patch.object(pwbot, 'git_get_command_lines', return_value=[]) as mock_git:
            pwbot.git_get_new_revs('/fake/repo', db_heads, git_heads, [], merges=False)
            args = mock_git.call_args[0][1]
            assert '--no-merges' in args

    def test_merges_flag_true(self):
        """When merges=True, --no-merges should NOT be in the git args."""
        db_heads = [('refs/heads/main', 'aaa')]
        git_heads = [('refs/heads/main', 'bbb')]
        with patch.object(pwbot, 'git_get_command_lines', return_value=[]) as mock_git:
            pwbot.git_get_new_revs('/fake/repo', db_heads, git_heads, [], merges=True)
            args = mock_git.call_args[0][1]
            assert '--no-merges' not in args

    def test_subject_with_colons(self):
        """Colons in the commit subject should not break parsing."""
        db_heads = [('refs/heads/main', 'aaa')]
        git_heads = [('refs/heads/main', 'bbb')]
        git_output = ['deadbeef:Alice:alice@example.com:net: fix: handle edge case']
        with patch.object(pwbot, 'git_get_command_lines', return_value=git_output):
            result = pwbot.git_get_new_revs('/fake/repo', db_heads, git_heads, [])
        assert result['refs/heads/main'][0][1] == 'net: fix: handle edge case'


class TestSendSummary:
    """Tests for send_summary() with mocked globals and SMTP."""

    FAKE_CONFIG = {
        'templates': {
            'summary': (
                'The following patches have been marked $newstate in $treename ($refname):\n'
                '\n'
                '$summary\n'
                'Total: $total\n'
                '$signature'
            ),
            'signature': '-- \npwbot',
        },
    }

    def _make_series(self, name='Test series', patch_ids=(100,),
                     submitter_name='Alice', submitter_email='alice@example.com',
                     cover_letter=None, web_url='https://pw.example.com/series/1/'):
        patches = [
            {'id': pid, 'name': f'[PATCH {i+1}/{len(patch_ids)}] patch {i+1}', 'msgid': f'<msg{pid}@example.com>'}
            for i, pid in enumerate(patch_ids)
        ]
        sdata = {
            'name': name,
            'patches': patches,
            'submitter': {'name': submitter_name, 'email': submitter_email},
            'web_url': web_url,
        }
        if cover_letter:
            sdata['cover_letter'] = cover_letter
        return sdata

    @pytest.fixture(autouse=True)
    def _setup_globals(self):
        """Set up module globals for send_summary tests."""
        orig_config = getattr(pwbot, 'CONFIG', None)
        orig_nomail = pwbot.NOMAIL
        orig_domain = pwbot.DOMAIN
        pwbot.CONFIG = self.FAKE_CONFIG
        pwbot.NOMAIL = True
        pwbot.DOMAIN = 'test.example.com'
        yield
        if orig_config is not None:
            pwbot.CONFIG = orig_config
        pwbot.NOMAIL = orig_nomail
        pwbot.DOMAIN = orig_domain

    @patch.object(pwbot, 'project_by_name')
    def test_single_patch_summary(self, mock_pbn):
        mock_pbn.return_value = ({}, MagicMock(), {
            'from': 'bot@example.com',
            'summaryto': 'maintainer@example.com',
        })
        serieslist = [self._make_series(name='Fix null pointer', patch_ids=(100,))]
        revs = {100: 'deadbeef'}
        committers = {100: 'Committer <c@example.com>'}

        msgid = pwbot.send_summary(
            serieslist, committers, 'accepted', 'refs/heads/main',
            revs, 'test-project',
            {'treename': 'linux-test'},
            {'from': 'bot@example.com', 'summaryto': 'maintainer@example.com'},
        )
        assert msgid  # returns a Message-Id
        assert 'git-patchwork-summary' in msgid

    @patch.object(pwbot, 'project_by_name')
    def test_series_vs_patch_label(self, mock_pbn):
        mock_pbn.return_value = ({}, MagicMock(), {
            'from': 'bot@example.com',
            'summaryto': 'maintainer@example.com',
        })
        # Single patch should say "Patch:"
        single = [self._make_series(name='Fix bug', patch_ids=(100,))]
        # Multi-patch should say "Series:"
        multi = [self._make_series(name='Big refactor', patch_ids=(100, 101, 102))]

        # We can't easily inspect the body with NOMAIL=True (it just logs),
        # but we can at least verify both cases don't crash
        pwbot.send_summary(
            single, {100: 'C <c@x>'}, 'accepted', 'refs/heads/main',
            {100: 'aaa'}, 'test-project',
            {'treename': 'linux-test'}, {},
        )
        pwbot.send_summary(
            multi, {100: 'C <c@x>'}, 'accepted', 'refs/heads/main',
            {100: 'aaa', 101: 'bbb', 102: 'ccc'}, 'test-project',
            {'treename': 'linux-test'}, {},
        )

    @patch.object(pwbot, 'project_by_name')
    def test_cover_letter_msgid_used(self, mock_pbn):
        mock_pbn.return_value = ({}, MagicMock(), {
            'from': 'bot@example.com',
            'summaryto': 'maintainer@example.com',
        })
        cover = {'msgid': '<cover@example.com>', 'id': 50}
        serieslist = [self._make_series(patch_ids=(100, 101), cover_letter=cover)]

        msgid = pwbot.send_summary(
            serieslist, {100: 'C <c@x>'}, 'accepted', 'refs/heads/main',
            {100: 'aaa', 101: 'bbb'}, 'test-project',
            {'treename': 'linux-test'}, {},
        )
        assert msgid


class TestGitGetRepoHeads:
    """Tests for git_get_repo_heads() with mocked git output."""

    def test_basic_heads(self):
        lines = [
            'aaa111 refs/heads/main',
            'bbb222 refs/heads/next',
        ]
        with patch.object(pwbot, 'git_get_command_lines', return_value=lines):
            result = pwbot.git_get_repo_heads('/fake/repo', 'refs/heads/*')
        assert result == [
            ('refs/heads/main', 'aaa111'),
            ('refs/heads/next', 'bbb222'),
        ]

    def test_with_ancestry(self):
        lines = ['aaa111 refs/heads/main']
        with patch.object(pwbot, 'git_get_command_lines', return_value=lines):
            result = pwbot.git_get_repo_heads('/fake/repo', 'refs/heads/*', ancestry='5')
        assert result == [('refs/heads/main', 'aaa111~5')]

    def test_no_ancestry_default(self):
        lines = ['aaa111 refs/heads/main']
        with patch.object(pwbot, 'git_get_command_lines', return_value=lines):
            result = pwbot.git_get_repo_heads('/fake/repo', 'refs/heads/*')
        assert result == [('refs/heads/main', 'aaa111')]

    def test_empty_output(self):
        with patch.object(pwbot, 'git_get_command_lines', return_value=[]):
            result = pwbot.git_get_repo_heads('/fake/repo', 'refs/heads/*')
        assert result == []

    def test_show_ref_args(self):
        with patch.object(pwbot, 'git_get_command_lines', return_value=[]) as mock_git:
            pwbot.git_get_repo_heads('/fake/repo', 'refs/heads/main')
            mock_git.assert_called_once_with('/fake/repo', ['show-ref', 'refs/heads/main'])


class TestGitGetCommandLines:
    """Tests for git_get_command_lines() output splitting."""

    def test_splits_lines(self):
        with patch.object(pwbot, 'git_run_command', return_value='line1\nline2\nline3'):
            result = pwbot.git_get_command_lines('/fake', ['log'])
        assert result == ['line1', 'line2', 'line3']

    def test_skips_empty_lines(self):
        with patch.object(pwbot, 'git_run_command', return_value='line1\n\nline2\n\n'):
            result = pwbot.git_get_command_lines('/fake', ['log'])
        assert result == ['line1', 'line2']

    def test_empty_output(self):
        with patch.object(pwbot, 'git_run_command', return_value=''):
            result = pwbot.git_get_command_lines('/fake', ['log'])
        assert result == []


class TestGitGetPatchId:
    """Tests for git_get_patch_id() parsing."""

    def test_parses_patch_id(self):
        with patch.object(pwbot, 'git_run_command', return_value='abc123def456 deadbeefcafe'):
            result = pwbot.git_get_patch_id('some diff')
        assert result == 'abc123def456'

    def test_empty_output(self):
        with patch.object(pwbot, 'git_run_command', return_value=''):
            result = pwbot.git_get_patch_id('some diff')
        assert result is None

    def test_passes_stdin(self):
        with patch.object(pwbot, 'git_run_command', return_value='abc 123') as mock_git:
            pwbot.git_get_patch_id('my diff content')
            mock_git.assert_called_once_with('', ['patch-id', '--stable'], stdin='my diff content')


class TestGitGetRevInfo:
    """Tests for git_get_rev_info() argument construction."""

    def test_default_algorithm(self):
        with patch.object(pwbot, 'git_run_command', return_value='') as mock_git:
            pwbot.git_get_rev_info('/fake/repo', 'abc123')
            mock_git.assert_called_once_with('/fake/repo', ['show', '--diff-algorithm=myers', 'abc123'])

    def test_histogram_algorithm(self):
        with patch.object(pwbot, 'git_run_command', return_value='') as mock_git:
            pwbot.git_get_rev_info('/fake/repo', 'abc123', algorithm='histogram')
            mock_git.assert_called_once_with('/fake/repo', ['show', '--diff-algorithm=histogram', 'abc123'])


class TestDatabase:
    """Tests for sqlite database functions using in-memory databases."""

    @pytest.fixture
    def db(self):
        conn = sqlite3.connect(':memory:')
        c = conn.cursor()
        pwbot.db_init_pw_sqlite_db(c)
        conn.commit()
        yield c
        conn.close()

    def test_init_creates_tables(self, db):
        """db_init_pw_sqlite_db should create meta and heads tables."""
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [t[0] for t in tables]
        assert 'meta' in table_names
        assert 'heads' in table_names

    def test_init_sets_db_version(self, db):
        version = db.execute('SELECT version FROM meta').fetchone()[0]
        assert str(version) == pwbot.DB_VERSION

    def test_save_and_get_heads_roundtrip(self, db):
        heads = [
            ('refs/heads/main', 'aaa111'),
            ('refs/heads/next', 'bbb222'),
            ('refs/heads/stable', 'ccc333'),
        ]
        pwbot.db_save_repo_heads(db, heads)
        result = pwbot.db_get_repo_heads(db)
        assert result == heads

    def test_save_heads_replaces_existing(self, db):
        pwbot.db_save_repo_heads(db, [('refs/heads/main', 'old111')])
        pwbot.db_save_repo_heads(db, [('refs/heads/main', 'new222')])
        result = pwbot.db_get_repo_heads(db)
        assert result == [('refs/heads/main', 'new222')]

    def test_save_empty_heads(self, db):
        pwbot.db_save_repo_heads(db, [('refs/heads/main', 'aaa')])
        pwbot.db_save_repo_heads(db, [])
        result = pwbot.db_get_repo_heads(db)
        assert result == []

    def test_save_meta_resets_version(self, db):
        """db_save_meta should always write the current DB_VERSION."""
        db.execute('DELETE FROM meta')
        db.execute('INSERT INTO meta VALUES(999)')
        pwbot.db_save_meta(db)
        version = db.execute('SELECT version FROM meta').fetchone()[0]
        assert str(version) == pwbot.DB_VERSION


class TestAutoArchive:
    """Tests for the autoarchive branch of housekeeping().

    These exercise the path that now relies on patchwork's server-side
    ``before`` filter instead of parsing patch dates locally. A pconfig
    with only ``autoarchive`` set skips the autosupersede block and
    returns before any mail is sent (the report is empty), so we can
    drive the archive loop in isolation.
    """

    @pytest.fixture(autouse=True)
    def _setup_globals(self):
        orig_dryrun = pwbot.DRYRUN
        orig_config = getattr(pwbot, 'CONFIG', None)
        pwbot.DRYRUN = False
        pwbot.CONFIG = {'templates': {'signature': 'sig'}}
        yield
        pwbot.DRYRUN = orig_dryrun
        if orig_config is not None:
            pwbot.CONFIG = orig_config

    def _mock_pbn(self, rm, hconfig=None):
        """Build a project_by_name() return value: (project, rm, pconfig)."""
        if hconfig is None:
            hconfig = {'autoarchive': 180}
        pconfig = {'housekeeping': hconfig}
        project = {'id': 7}
        return (project, rm, pconfig)

    def test_before_filter_is_sent(self):
        """The query must offload date filtering to patchwork's 'before'."""
        rm = MagicMock()
        rm.get_patches_list.return_value = []
        with patch.object(pwbot, 'project_by_name', return_value=self._mock_pbn(rm)):
            pwbot.housekeeping('test-project')
        params = dict(rm.get_patches_list.call_args[0][0])
        assert 'before' in params
        # The value must be a valid ISO timestamp patchwork can parse.
        datetime.datetime.fromisoformat(params['before'])
        assert params['state'] == 'new'
        assert params['archived'] == 'false'
        assert params['project'] == 7

    def test_before_reflects_cutoff_days(self):
        """The 'before' cutoff should track the configured autoarchive days."""
        rm = MagicMock()
        rm.get_patches_list.return_value = []
        with patch.object(pwbot, 'project_by_name',
                          return_value=self._mock_pbn(rm, {'autoarchive': 30})):
            pwbot.housekeeping('test-project')
        params = dict(rm.get_patches_list.call_args[0][0])
        before = datetime.datetime.fromisoformat(params['before'])
        delta_days = (datetime.datetime.now() - before).total_seconds() / 86400
        assert abs(delta_days - 30) < 1

    def test_microsecond_timestamp_does_not_crash(self):
        """Regression: ISO dates with microseconds used to crash strptime.

        The old code parsed entry['date'] with "%Y-%m-%dT%H:%M:%S", which
        raised ValueError on timestamps like '...:00.123456'. We no longer
        parse dates on our side at all, so such a patch archives cleanly.
        """
        rm = MagicMock()
        rm.get_patches_list.side_effect = [
            [{'id': 100, 'date': '2020-01-15T10:30:00.123456'}],
            [],
        ]
        with patch.object(pwbot, 'project_by_name', return_value=self._mock_pbn(rm)):
            pwbot.housekeeping('test-project')
        rm.update_patch.assert_called_once_with(100, archived=True)

    def test_archives_across_refetches(self):
        """In a real run, archived patches drop out, so we refetch until empty."""
        rm = MagicMock()
        rm.get_patches_list.side_effect = [
            [{'id': 1, 'date': '2020-01-01T00:00:00'},
             {'id': 2, 'date': '2020-01-02T00:00:00'}],
            [{'id': 3, 'date': '2020-01-03T00:00:00'}],
            [],
        ]
        with patch.object(pwbot, 'project_by_name', return_value=self._mock_pbn(rm)):
            pwbot.housekeeping('test-project')
        archived_ids = sorted(c.args[0] for c in rm.update_patch.call_args_list)
        assert archived_ids == [1, 2, 3]
        for c in rm.update_patch.call_args_list:
            assert c.kwargs == {'archived': True}

    def test_seen_guard_breaks_infinite_loop(self):
        """If the archived flag never sticks, the seen-set must break the loop."""
        rm = MagicMock()
        # Server keeps handing back the same patch no matter what.
        rm.get_patches_list.return_value = [{'id': 99, 'date': '2020-01-01T00:00:00'}]
        with patch.object(pwbot, 'project_by_name', return_value=self._mock_pbn(rm)):
            pwbot.housekeeping('test-project')
        rm.update_patch.assert_called_once_with(99, archived=True)

    def test_dryrun_paginates_and_does_not_archive(self):
        """Dryrun never archives, so it must paginate with incrementing pages."""
        pwbot.DRYRUN = True
        rm = MagicMock()
        rm.get_patches_list.side_effect = [
            [{'id': 1, 'date': '2020-01-01T00:00:00'}],
            [{'id': 2, 'date': '2020-01-02T00:00:00'}],
            [],
        ]
        with patch.object(pwbot, 'project_by_name', return_value=self._mock_pbn(rm)):
            pwbot.housekeeping('test-project')
        rm.update_patch.assert_not_called()
        pages = [dict(c.args[0]).get('page') for c in rm.get_patches_list.call_args_list]
        assert pages == [1, 2, 3]

    def test_no_patches_means_no_updates(self):
        """An empty project should archive nothing and make a single query."""
        rm = MagicMock()
        rm.get_patches_list.return_value = []
        with patch.object(pwbot, 'project_by_name', return_value=self._mock_pbn(rm)):
            pwbot.housekeeping('test-project')
        rm.update_patch.assert_not_called()
        assert rm.get_patches_list.call_count == 1


class TestProjectByName:
    """Tests for project_by_name() link_name matching.

    The server caches are pre-seeded so these never touch the network.
    """

    SERVER = 'https://pw.example.com'

    @pytest.fixture(autouse=True)
    def _setup(self):
        orig_config = getattr(pwbot, 'CONFIG', None)
        orig_pcache = dict(pwbot._project_cache)
        orig_scache = dict(pwbot._server_cache)
        pwbot._project_cache.clear()
        pwbot._server_cache.clear()
        yield
        pwbot._project_cache.clear()
        pwbot._project_cache.update(orig_pcache)
        pwbot._server_cache.clear()
        pwbot._server_cache.update(orig_scache)
        if orig_config is not None:
            pwbot.CONFIG = orig_config

    def _seed(self, config_projects, server_projects):
        """Set up CONFIG and pre-seed the server cache.

        config_projects: iterable of project names to put in the config.
        server_projects: list of (id, link_name) as returned by the API.
        """
        pwbot.CONFIG = {'patchworks': {self.SERVER: {
            'projects': {name: {'from': 'bot@example.com'} for name in config_projects}
        }}}
        plist = [{'id': pid, 'link_name': ln} for pid, ln in server_projects]
        rm = MagicMock()
        pwbot._server_cache[self.SERVER] = (rm, plist)
        return rm

    def test_exact_match_wins_over_prefix(self):
        """The linux-mm/linux-mmc bug: exact match must beat an earlier prefix.

        linux-mmc (id 32) is returned before linux-mm (id 365) by the real
        API, and 'linux-mmc'.startswith('linux-mm') is True, so the old
        startswith-only logic resolved linux-mm to linux-mmc.
        """
        self._seed(['linux-mm', 'linux-mmc'],
                   [(32, 'linux-mmc'), (365, 'linux-mm')])
        project, _, _ = pwbot.project_by_name('linux-mm')
        assert project['id'] == 365

    def test_prefix_project_resolves_to_itself(self):
        """The longer name must still resolve to its own project."""
        self._seed(['linux-mm', 'linux-mmc'],
                   [(32, 'linux-mmc'), (365, 'linux-mm')])
        project, _, _ = pwbot.project_by_name('linux-mmc')
        assert project['id'] == 32

    def test_exact_match_is_case_insensitive(self):
        self._seed(['Linux-MM'], [(365, 'linux-mm')])
        project, _, _ = pwbot.project_by_name('Linux-MM')
        assert project['id'] == 365

    def test_prefix_fallback_when_no_exact_match(self):
        """With no exact match, the legacy startswith behaviour still applies."""
        self._seed(['foo'], [(9, 'foobar')])
        project, _, _ = pwbot.project_by_name('foo')
        assert project['id'] == 9


class TestGetListPage:
    """Tests for Restmaker.get_list_page() status exposure."""

    def _rm(self):
        # Bypass __init__, which needs a full CONFIG; we only exercise session.
        rm = pwbot.Restmaker.__new__(pwbot.Restmaker)
        rm.session = MagicMock()
        return rm

    def test_success_returns_items_and_status(self):
        rm = self._rm()
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{'id': 1}, {'id': 2}]
        rm.session.get.return_value = resp
        items, status = rm.get_list_page('http://x/series/', [('page', 1)])
        assert items == [{'id': 1}, {'id': 2}]
        assert status == 200

    def test_http_error_exposes_status_code(self):
        rm = self._rm()
        resp = MagicMock(status_code=500)
        resp.raise_for_status.side_effect = pwbot.requests.exceptions.HTTPError(response=resp)
        rm.session.get.return_value = resp
        items, status = rm.get_list_page('http://x/series/', [])
        assert items == []
        assert status == 500

    def test_connection_error_returns_none_status(self):
        rm = self._rm()
        rm.session.get.side_effect = pwbot.requests.exceptions.ConnectionError()
        items, status = rm.get_list_page('http://x/series/', [])
        assert items == []
        assert status is None


class TestGetProjectSeriesSince:
    """Tests for get_project_series_since() resilient pagination.

    REST_PER_PAGE is shrunk to 3 so the bisect path is small enough to
    enumerate. rm.get_list_page is mocked to return canned (items, status)
    tuples in call order.
    """

    @pytest.fixture(autouse=True)
    def _small_pages(self):
        orig = pwbot.REST_PER_PAGE
        pwbot.REST_PER_PAGE = 3
        yield
        pwbot.REST_PER_PAGE = orig

    def _rm(self, pages):
        rm = MagicMock()
        rm.get_list_page.side_effect = pages
        return rm

    def test_clean_pagination(self):
        page1 = [{'id': 0}, {'id': 1}, {'id': 2}]  # full page -> keep going
        page2 = [{'id': 3}]                          # short page -> stop
        rm = self._rm([(page1, 200), (page2, 200)])
        out = pwbot.get_project_series_since(rm, 1, 'since')
        assert [s['id'] for s in out] == [0, 1, 2, 3]

    def test_skips_unserializable_series_and_continues(self):
        """A 500 page is re-fetched one series at a time; the bad one is skipped."""
        rm = self._rm([
            ([{'id': 0}, {'id': 1}, {'id': 2}], 200),  # page 1 (full)
            ([], 500),                                 # page 2 -> bisect indices 3,4,5
            ([{'id': 3}], 200),                        # index 3 ok
            ([], 500),                                 # index 4 is the poison -> skip
            ([{'id': 5}], 200),                        # index 5 ok
            ([], 404),                                 # page 3 past the end -> stop
        ])
        out = pwbot.get_project_series_since(rm, 1, 'since')
        assert [s['id'] for s in out] == [0, 1, 2, 3, 5]

    def test_bisect_reaching_end_stops(self):
        rm = self._rm([
            ([{'id': 0}, {'id': 1}, {'id': 2}], 200),  # page 1 (full)
            ([], 500),                                 # page 2 -> bisect
            ([{'id': 3}], 200),                        # index 3 ok
            ([], 404),                                 # index 4 past the end
        ])
        out = pwbot.get_project_series_since(rm, 1, 'since')
        assert [s['id'] for s in out] == [0, 1, 2, 3]

    def test_end_of_results_404_does_not_bisect(self):
        """Patchwork answers a page past the last one with 404, not an empty
        200. That is the end of the results, not an unserializable series, so it
        must stop instead of running away bisecting a page that does not exist.
        """
        rm = self._rm([
            ([{'id': 0}, {'id': 1}, {'id': 2}], 200),  # exactly one full page
            ([], 404),                                 # page 2 past the end
        ])
        out = pwbot.get_project_series_since(rm, 1, 'since')
        assert [s['id'] for s in out] == [0, 1, 2]
        # Must not have tried to isolate the (nonexistent) page 2 one item at a
        # time; that is the infinite-loop regression.
        assert rm.get_list_page.call_count == 2

    def test_unexpected_status_stops(self):
        """A non-5xx error (e.g. throttling) is not something we can step over
        one series at a time, so stop rather than hammer the API."""
        rm = self._rm([
            ([{'id': 0}, {'id': 1}, {'id': 2}], 200),  # page 1 (full)
            ([], 429),                                 # page 2 throttled
        ])
        out = pwbot.get_project_series_since(rm, 1, 'since')
        assert [s['id'] for s in out] == [0, 1, 2]
        assert rm.get_list_page.call_count == 2

    def test_connection_error_stops_gracefully(self):
        rm = self._rm([([], None)])
        out = pwbot.get_project_series_since(rm, 1, 'since')
        assert out == []
