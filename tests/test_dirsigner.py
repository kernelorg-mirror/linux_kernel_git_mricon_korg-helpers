"""Tests for dirsigner.py."""

import builtins
import calendar
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
import types
from pathlib import Path
from typing import IO, NamedTuple

import pytest
from conftest import load_script


def _install_gpg_stub() -> None:
    """Make ``import gpg`` work without the gpgme bindings installed.

    dirsigner imports gpg at module level, but the fake_gpg fixture replaces
    the module before anything is signed, so this stub only has to satisfy
    the import itself. When the real bindings are present we leave them alone.
    """
    try:
        if importlib.util.find_spec('gpg') is not None:
            return
    except ModuleNotFoundError:
        pass

    gpg_mod = types.ModuleType('gpg')
    constants_mod = types.ModuleType('gpg.constants')
    sig_mod = types.ModuleType('gpg.constants.sig')

    class _Mode:
        CLEAR = 'stub-clear'

    # setattr(), because these modules are built at runtime and have no
    # declared attributes for a type checker to look at.
    setattr(sig_mod, 'mode', _Mode)
    setattr(constants_mod, 'sig', sig_mod)
    setattr(gpg_mod, 'constants', constants_mod)

    sys.modules.setdefault('gpg', gpg_mod)
    sys.modules.setdefault('gpg.constants', constants_mod)
    sys.modules.setdefault('gpg.constants.sig', sig_mod)


_install_gpg_stub()

dirsigner = load_script('dirsigner.py')

# What our fake signer wraps the payload in, so tests can unwrap it again.
ARMOR_HEAD = b'-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\n'
ARMOR_TAIL = b'-----BEGIN PGP SIGNATURE-----\nnot-a-real-signature\n-----END PGP SIGNATURE-----\n'

STATUS_FILE = '.dirsigner.status.js'


class SignCall(NamedTuple):
    """One recorded call into the fake signing context."""

    armor: bool
    mode: object
    payload: bytes


@pytest.fixture(autouse=True)
def fake_gpg(monkeypatch: pytest.MonkeyPatch) -> list[SignCall]:
    """Replace dirsigner's gpg module so no test touches a real keyring.

    Returns the list of signing calls made, in order.
    """
    calls: list[SignCall] = []

    class Context:
        def __init__(self, armor: bool = False) -> None:
            self.armor = armor

        def sign(self, infh: IO[bytes], outfh: IO[bytes], mode: object = None) -> None:
            payload = infh.read()
            calls.append(SignCall(self.armor, mode, payload))
            outfh.write(ARMOR_HEAD + payload + ARMOR_TAIL)

    mode = types.SimpleNamespace(CLEAR='fake-clear')
    fake = types.SimpleNamespace(
        Context=Context,
        constants=types.SimpleNamespace(sig=types.SimpleNamespace(mode=mode)),
    )
    monkeypatch.setattr(dirsigner, 'gpg', fake)
    return calls


@pytest.fixture(autouse=True)
def restore_chunk_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep parse_args() and friends from leaking CHUNK_SIZE across tests."""
    monkeypatch.setattr(dirsigner, 'CHUNK_SIZE', dirsigner.CHUNK_SIZE)


def read_sums(path: Path) -> list[str]:
    """Unwrap a sums file written by the fake signer and return its lines."""
    data = path.read_bytes()
    assert data.startswith(ARMOR_HEAD), 'sums file is not clearsigned'
    assert data.endswith(ARMOR_TAIL), 'sums file has no signature block'
    body = data[len(ARMOR_HEAD):-len(ARMOR_TAIL)]
    return body.decode('utf-8').splitlines()


def read_status(directory: Path) -> dict[str, dict[str, object]]:
    """Load the status file dirsigner left in a directory."""
    contents: dict[str, dict[str, object]] = json.loads((directory / STATUS_FILE).read_text())
    return contents


def make_tree(root: Path, files: dict[str, str]) -> None:
    """Create files (relative paths, with content) under root."""
    for relpath, content in files.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


class TestSafeVersion:
    """Tests for the packaging.version wrapper."""

    def test_valid_version(self) -> None:
        assert str(dirsigner._safe_version('6.1.2')) == '6.1.2'

    def test_invalid_version_becomes_zero(self) -> None:
        assert dirsigner._safe_version('not a version') == dirsigner._safe_version('0')

    def test_empty_string_becomes_zero(self) -> None:
        assert dirsigner._safe_version('') == dirsigner._safe_version('0')


class TestGetVersionInfo:
    """Tests for splitting a filename into name, version and tail."""

    def test_no_dash_at_all(self) -> None:
        name, ver, tail = dirsigner.get_version_info('README')
        assert name == 'README'
        assert ver == dirsigner._safe_version('0')
        assert tail == ''

    def test_name_version_and_extension(self) -> None:
        name, ver, tail = dirsigner.get_version_info('linux-6.1.2.tar.gz')
        assert name == 'linux'
        assert ver == dirsigner._safe_version('6.1.2')
        # The tail comes back reversed, because it is popped off the end. It is
        # only ever compared against another tail, so the order does not matter.
        assert tail == 'gz.tar'

    def test_only_the_last_dash_splits(self) -> None:
        name, ver, tail = dirsigner.get_version_info('linux-next-20260824.tar.xz')
        assert name == 'linux-next'
        assert ver == dirsigner._safe_version('20260824')
        assert tail == 'xz.tar'

    def test_full_path_uses_basename(self) -> None:
        name, ver, tail = dirsigner.get_version_info('/pub/linux/kernel/patch-6.12.1.xz')
        assert name == 'patch'
        assert ver == dirsigner._safe_version('6.12.1')
        assert tail == 'xz'

    def test_no_numeric_chunks_falls_back_to_zero(self) -> None:
        name, ver, tail = dirsigner.get_version_info('ChangeLog-old.txt')
        assert name == 'ChangeLog'
        assert ver == dirsigner._safe_version('0')
        assert tail == 'txt.old'

    def test_trailing_dash(self) -> None:
        name, ver, tail = dirsigner.get_version_info('sha256sums-')
        assert name == 'sha256sums'
        assert ver == dirsigner._safe_version('0')
        assert tail == ''


def vsorted(names: list[str]) -> list[str]:
    """Sort the way --version-sort does."""
    return sorted(names, key=dirsigner.get_version_info)


class TestVersionSort:
    """Tests for the ordering get_version_info gives us as a sort key.

    get_version_info returns (name, version, tail), so sorting on it compares
    those three in order. These tests pin that ordering down.
    """

    def test_identical_names_are_equal(self) -> None:
        a, b = 'linux-6.1.tar.gz', 'linux-6.1.tar.gz'
        assert dirsigner.get_version_info(a) == dirsigner.get_version_info(b)

    def test_name_wins_over_version(self) -> None:
        assert vsorted(['zzz-1.0.gz', 'aaa-9.0.gz']) == ['aaa-9.0.gz', 'zzz-1.0.gz']

    def test_version_is_numeric_not_lexical(self) -> None:
        # This is the whole point of --version-sort: 1.10 comes after 1.2.
        assert vsorted(['linux-1.10.tar.gz', 'linux-1.2.tar.gz']) == [
            'linux-1.2.tar.gz', 'linux-1.10.tar.gz']

    def test_tail_breaks_the_tie(self) -> None:
        assert vsorted(['linux-6.1.tar.sign', 'linux-6.1.tar.gz']) == [
            'linux-6.1.tar.gz', 'linux-6.1.tar.sign']

    def test_sorts_a_realistic_listing(self) -> None:
        names = [
            'linux-6.12.10.tar.xz',
            'linux-6.12.2.tar.xz',
            'patch-6.12.1.xz',
            'linux-6.12.2.tar.sign',
            'linux-6.9.tar.xz',
        ]
        assert vsorted(names) == [
            'linux-6.9.tar.xz',
            'linux-6.12.2.tar.sign',
            'linux-6.12.2.tar.xz',
            'linux-6.12.10.tar.xz',
            'patch-6.12.1.xz',
        ]

    def test_unparseable_names_still_sort_by_name(self) -> None:
        # Anything without a name-version shape lands at version 0, so the
        # name is all we have left to go on. It must not blow up.
        assert vsorted(['zebra', 'apple', 'a-', '-1']) == ['-1', 'a-', 'apple', 'zebra']


class TestStatusFile:
    """Tests for loading and saving the per-directory status file."""

    def test_missing_file_is_an_empty_status(self, tmp_path: Path) -> None:
        assert dirsigner.load_status(str(tmp_path / STATUS_FILE)) == {}

    def test_roundtrip(self, tmp_path: Path) -> None:
        statusfile = str(tmp_path / STATUS_FILE)
        status = {
            'linux-6.1.tar.xz': {
                'ctime': 1700000000,
                'mtime': 1700000000,
                'size': 42,
                'inode': 12345,
                'hash': 'abc123',
            },
        }
        dirsigner.save_status(statusfile, status)
        assert dirsigner.load_status(statusfile) == status

    def test_empty_status_removes_the_file(self, tmp_path: Path) -> None:
        statusfile = tmp_path / STATUS_FILE
        statusfile.write_text('{"gone": {}}')
        dirsigner.save_status(str(statusfile), {})
        assert not statusfile.exists()

    def test_empty_status_writes_no_stub_file(self, tmp_path: Path) -> None:
        statusfile = tmp_path / STATUS_FILE
        dirsigner.save_status(str(statusfile), {})
        assert not statusfile.exists()


class TestGetFileHash:
    """Tests for the chunked file hasher."""

    @pytest.mark.parametrize('algorithm', ['md5', 'sha1', 'sha256', 'sha512'])
    def test_matches_hashlib(self, tmp_path: Path, algorithm: str) -> None:
        target = tmp_path / 'blob'
        payload = b'kernel.org' * 1000
        target.write_bytes(payload)
        expected = hashlib.new(algorithm, payload).hexdigest()
        assert dirsigner.get_file_hash(str(target), algorithm) == expected

    def test_unknown_algorithm_falls_back_to_sha256(self, tmp_path: Path) -> None:
        target = tmp_path / 'blob'
        target.write_bytes(b'hello')
        assert dirsigner.get_file_hash(str(target), 'whirlpool') == hashlib.sha256(b'hello').hexdigest()

    def test_chunk_size_does_not_change_the_digest(self, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / 'blob'
        payload = b'a' * 10000
        target.write_bytes(payload)
        monkeypatch.setattr(dirsigner, 'CHUNK_SIZE', 7)
        assert dirsigner.get_file_hash(str(target), 'sha256') == hashlib.sha256(payload).hexdigest()

    def test_empty_file(self, tmp_path: Path) -> None:
        target = tmp_path / 'empty'
        target.write_bytes(b'')
        assert dirsigner.get_file_hash(str(target), 'sha256') == hashlib.sha256(b'').hexdigest()


class TestWriteSumsFile:
    """Tests for writing and signing the sums file."""

    @staticmethod
    def _status(*names: str) -> dict[str, dict[str, object]]:
        return {name: {'hash': 'hash-of-%s' % name} for name in names}

    def test_writes_hash_and_filename(self, tmp_path: Path) -> None:
        names = ['linux-6.1.tar.xz', 'linux-6.2.tar.xz']
        dirsigner.write_sums_file(str(tmp_path), names, self._status(*names), 'sha256')
        assert read_sums(tmp_path / 'sha256sums.asc') == [
            'hash-of-linux-6.1.tar.xz  linux-6.1.tar.xz',
            'hash-of-linux-6.2.tar.xz  linux-6.2.tar.xz',
        ]

    def test_preserves_given_order_without_vsort(self, tmp_path: Path) -> None:
        names = ['linux-6.10.tar.xz', 'linux-6.2.tar.xz']
        dirsigner.write_sums_file(str(tmp_path), names, self._status(*names), 'sha256')
        lines = read_sums(tmp_path / 'sha256sums.asc')
        assert [line.split('  ')[1] for line in lines] == names

    def test_writes_in_the_order_it_is_given(self, tmp_path: Path) -> None:
        # Ordering is sign_tree's job now, so that the sums file and
        # index.html always agree; write_sums_file must not reshuffle.
        names = ['linux-6.2.tar.xz', 'linux-6.10.tar.xz']
        dirsigner.write_sums_file(str(tmp_path), names, self._status(*names), 'sha256')
        lines = read_sums(tmp_path / 'sha256sums.asc')
        assert [line.split('  ')[1] for line in lines] == names

    def test_signs_clearsigned_and_armored(self, tmp_path: Path, fake_gpg: list[SignCall]) -> None:
        dirsigner.write_sums_file(str(tmp_path), ['a'], self._status('a'), 'sha256')
        assert len(fake_gpg) == 1
        assert fake_gpg[0].armor is True
        assert fake_gpg[0].mode == dirsigner.gpg.constants.sig.mode.CLEAR
        assert fake_gpg[0].payload == b'hash-of-a  a\n'

    def test_removes_sums_files_for_other_algorithms(self, tmp_path: Path) -> None:
        for stale in ('md5sums.asc', 'sha1sums.asc', 'sha512sums.asc'):
            (tmp_path / stale).write_text('stale')
        dirsigner.write_sums_file(str(tmp_path), ['a'], self._status('a'), 'sha256')
        assert sorted(p.name for p in tmp_path.iterdir()) == ['sha256sums.asc']

    def test_no_files_means_no_sums_file(self, tmp_path: Path, fake_gpg: list[SignCall]) -> None:
        (tmp_path / 'sha256sums.asc').write_text('stale')
        dirsigner.write_sums_file(str(tmp_path), [], {}, 'sha256')
        assert list(tmp_path.iterdir()) == []
        assert fake_gpg == []


class TestSignTree:
    """End-to-end tests for walking and signing a tree."""

    def test_signs_every_directory(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {
            'linux-6.1.tar.xz': 'six-one',
            'v6.x/linux-6.2.tar.xz': 'six-two',
        })
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')

        for directory, filename, content in (
            (tmp_path, 'linux-6.1.tar.xz', b'six-one'),
            (tmp_path / 'v6.x', 'linux-6.2.tar.xz', b'six-two'),
        ):
            expected = hashlib.sha256(content).hexdigest()
            assert read_sums(directory / 'sha256sums.asc') == ['%s  %s' % (expected, filename)]
            assert read_status(directory)[filename]['hash'] == expected

    def test_honours_the_hash_algorithm(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha512')
        expected = hashlib.sha512(b'content').hexdigest()
        assert read_sums(tmp_path / 'sha512sums.asc') == ['%s  blob' % expected]
        assert not (tmp_path / 'sha256sums.asc').exists()

    def test_excludes_its_own_bookkeeping_files(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        # Run again now that sha256sums.asc and the status file exist.
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        assert read_sums(tmp_path / 'sha256sums.asc') == [
            '%s  blob' % hashlib.sha256(b'content').hexdigest(),
        ]
        assert list(read_status(tmp_path).keys()) == ['blob']

    def test_caller_excludes_are_applied(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'keep.tar.xz': 'keep', 'skip.txt': 'skip'})
        dirsigner.sign_tree(str(tmp_path), ['*.txt'], 'sha256')
        assert [line.split('  ')[1] for line in read_sums(tmp_path / 'sha256sums.asc')] == ['keep.tar.xz']

    def test_excluded_directories_are_not_descended(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content', 'nope/inner': 'inner'})
        dirsigner.sign_tree(str(tmp_path), ['nope'], 'sha256')
        assert (tmp_path / 'sha256sums.asc').exists()
        assert not (tmp_path / 'nope' / 'sha256sums.asc').exists()
        assert not (tmp_path / 'nope' / STATUS_FILE).exists()

    def test_symlinks_are_ignored(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'real.tar.xz': 'content'})
        (tmp_path / 'link.tar.xz').symlink_to(tmp_path / 'real.tar.xz')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        assert [line.split('  ')[1] for line in read_sums(tmp_path / 'sha256sums.asc')] == ['real.tar.xz']

    def test_unchanged_tree_is_not_rehashed(self, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch,
                                            fake_gpg: list[SignCall]) -> None:
        make_tree(tmp_path, {'blob': 'content'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        assert len(fake_gpg) == 1

        hashed: list[str] = []
        real_hash = dirsigner.get_file_hash

        def counting_hash(full_path: str, hash_algorithm: str) -> str:
            hashed.append(full_path)
            return str(real_hash(full_path, hash_algorithm))

        monkeypatch.setattr(dirsigner, 'get_file_hash', counting_hash)
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        assert hashed == []
        assert len(fake_gpg) == 1, 'unchanged directory was signed again'

    def test_changed_file_is_rehashed(self, tmp_path: Path, fake_gpg: list[SignCall]) -> None:
        target = tmp_path / 'blob'
        make_tree(tmp_path, {'blob': 'content'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')

        target.write_text('completely different content')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        assert len(fake_gpg) == 2
        expected = hashlib.sha256(b'completely different content').hexdigest()
        assert read_sums(tmp_path / 'sha256sums.asc') == ['%s  blob' % expected]

    def test_removed_file_drops_out_of_the_sums_file(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'stays': 'a', 'goes': 'b'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        assert len(read_sums(tmp_path / 'sha256sums.asc')) == 2

        os.unlink(tmp_path / 'goes')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        assert [line.split('  ')[1] for line in read_sums(tmp_path / 'sha256sums.asc')] == ['stays']
        assert list(read_status(tmp_path).keys()) == ['stays']

    def test_emptied_directory_loses_sums_and_status(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        os.unlink(tmp_path / 'blob')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        assert list(tmp_path.iterdir()) == []

    def test_missing_sums_file_is_regenerated(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        os.unlink(tmp_path / 'sha256sums.asc')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        assert read_sums(tmp_path / 'sha256sums.asc') == [
            '%s  blob' % hashlib.sha256(b'content').hexdigest(),
        ]

    def test_empty_directory_is_left_alone(self, tmp_path: Path) -> None:
        (tmp_path / 'empty').mkdir()
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        assert list((tmp_path / 'empty').iterdir()) == []

    def test_version_sort_end_to_end(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {
            'linux-6.10.tar.xz': 'ten',
            'linux-6.2.tar.xz': 'two',
        })
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', vsort=True)
        assert [line.split('  ')[1] for line in read_sums(tmp_path / 'sha256sums.asc')] == [
            'linux-6.2.tar.xz',
            'linux-6.10.tar.xz',
        ]

    def test_caller_exclude_list_is_not_clobbered(self, tmp_path: Path) -> None:
        # sign_tree appends its own patterns; make sure that does not surprise
        # a caller that reuses the list.
        excludes = ['*.txt']
        make_tree(tmp_path, {'blob': 'content'})
        dirsigner.sign_tree(str(tmp_path), excludes, 'sha256')
        assert '*.txt' in excludes


class TestReadOurIndex:
    """Tests for telling our own listings apart from hand-written ones."""

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert dirsigner.read_our_index(str(tmp_path / 'index.html')) is None

    def test_our_own_page_comes_back(self, tmp_path: Path) -> None:
        target = tmp_path / 'index.html'
        page = dirsigner.render_index('/pub/', [])
        target.write_bytes(page)
        assert dirsigner.read_our_index(str(target)) == page

    def test_hand_written_page_is_not_ours(self, tmp_path: Path) -> None:
        target = tmp_path / 'index.html'
        target.write_text('<html><body>Please read the README first</body></html>')
        assert dirsigner.read_our_index(str(target)) is dirsigner.NOT_OURS

    def test_marker_must_be_on_the_first_line(self, tmp_path: Path) -> None:
        # Buried further down it does not count, so a page that merely quotes
        # the marker cannot trick us into overwriting it
        target = tmp_path / 'index.html'
        target.write_bytes(b'<html>\r\n' + dirsigner.INDEX_MARKER + b'\r\n')
        assert dirsigner.read_our_index(str(target)) is dirsigner.NOT_OURS

    def test_symlink_is_not_ours(self, tmp_path: Path) -> None:
        # Even one pointing at a page that does carry the marker: we never
        # create symlinks, so it is not our doing
        (tmp_path / 'real.html').write_bytes(dirsigner.render_index('/pub/', []))
        target = tmp_path / 'index.html'
        target.symlink_to('real.html')
        assert dirsigner.read_our_index(str(target)) is dirsigner.NOT_OURS

    def test_empty_file_is_not_ours(self, tmp_path: Path) -> None:
        target = tmp_path / 'index.html'
        target.write_bytes(b'')
        assert dirsigner.read_our_index(str(target)) is dirsigner.NOT_OURS

    def test_unreadable_file_is_not_ours(self, tmp_path: Path) -> None:
        target = tmp_path / 'index.html'
        target.write_bytes(dirsigner.render_index('/pub/', []))
        target.chmod(0o000)
        try:
            if os.access(str(target), os.R_OK):
                pytest.skip('running as root, so permissions do not bite')
            assert dirsigner.read_our_index(str(target)) is dirsigner.NOT_OURS
        finally:
            target.chmod(0o644)


class TestHandWrittenIndex:
    """A page we did not generate must survive untouched."""

    def test_hand_written_index_is_not_overwritten(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'linux-6.12.tar.xz': 'content'})
        page = '<html><body>See the README before downloading</body></html>'
        (tmp_path / 'index.html').write_text(page)
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        assert (tmp_path / 'index.html').read_text() == page

    def test_the_rest_of_the_directory_is_still_signed(self, tmp_path: Path) -> None:
        # Refusing to touch the listing must not stop us doing our real job
        make_tree(tmp_path, {'linux-6.12.tar.xz': 'content'})
        (tmp_path / 'index.html').write_text('<html>hands off</html>')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        signed = [line.split('  ')[1] for line in read_sums(tmp_path / 'sha256sums.asc')]
        assert signed == ['linux-6.12.tar.xz']

    def test_hand_written_index_survives_repeated_runs(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content'})
        page = '<html><body>mine</body></html>'
        (tmp_path / 'index.html').write_text(page)
        for _ in range(3):
            dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        assert (tmp_path / 'index.html').read_text() == page

    def test_a_subdirectory_with_a_hand_written_index_is_still_listed(self, tmp_path: Path) -> None:
        # The parent's listing does not care who wrote the child's page
        make_tree(tmp_path, {'sub/blob': 'content'})
        (tmp_path / 'sub' / 'index.html').write_text('<html>mine</html>')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        assert '<a href="sub/">' in (tmp_path / 'index.html').read_text()

    def test_our_own_page_is_replaced_when_it_changes(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'linux-6.12.tar.xz': 'content'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        assert (tmp_path / 'index.html').read_bytes().startswith(dirsigner.INDEX_MARKER)

        make_tree(tmp_path, {'linux-6.13.tar.xz': 'more'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        assert 'linux-6.13.tar.xz' in (tmp_path / 'index.html').read_text()


class TestFsChurn:
    """A run over an unchanged tree must not touch the filesystem."""

    def test_default_chunk_size_is_not_tiny(self) -> None:
        # 8K meant 24k read() calls to hash a 200MB tarball
        assert dirsigner.CHUNK_SIZE >= 65536

    def test_unchanged_run_opens_nothing_for_writing(self, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
        make_tree(tmp_path, {'a/linux-6.1.tar.xz': 'one', 'a/linux-6.2.tar.xz': 'two',
                             'b/patch-6.1.xz': 'three', 'b/sub/deep': 'four'})
        (tmp_path / 'a' / 'latest.tar.xz').symlink_to('linux-6.2.tar.xz')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True, vsort=True)

        opened: list[tuple[str, str]] = []
        real_open = builtins.open

        def spy(path: object, mode: str = 'r', *args: object, **kwargs: object) -> object:
            opened.append((str(path), mode))
            return real_open(path, mode, *args, **kwargs)  # type: ignore[call-overload]

        monkeypatch.setattr(builtins, 'open', spy)
        unlinked: list[str] = []
        monkeypatch.setattr(dirsigner.os, 'unlink', lambda p: unlinked.append(str(p)))

        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True, vsort=True)

        writes = [path for path, mode in opened if 'w' in mode or 'a' in mode]
        assert writes == [], 'nothing changed, so nothing should be rewritten'
        assert unlinked == [], 'nothing changed, so nothing should be removed'

    def test_unchanged_run_reads_each_file_at_most_once(self, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
        # Files must not be reopened for hashing when the status still matches
        make_tree(tmp_path, {'linux-6.1.tar.xz': 'one', 'linux-6.2.tar.xz': 'two'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)

        hashed: list[str] = []

        def spy_hash(full_path: str, hash_algorithm: str) -> str:
            hashed.append(full_path)
            return 'deadbeef'

        monkeypatch.setattr(dirsigner, 'get_file_hash', spy_hash)
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        assert hashed == [], 'unchanged files must not be rehashed'

    def test_stat_calls_stay_near_one_per_file(self, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
        make_tree(tmp_path, {'f-%d.txt' % i: 'x' for i in range(50)})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)

        counts = {'stat': 0, 'lstat': 0}
        for fn in ('stat', 'lstat'):
            real = getattr(os, fn)

            def wrap(path: object, *a: object, _r: object = real, _n: str = fn,
                     **kw: object) -> object:
                counts[_n] += 1
                return _r(path, *a, **kw)  # type: ignore[operator]

            monkeypatch.setattr(os, fn, wrap)

        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        total = counts['stat'] + counts['lstat']
        # 50 files plus a handful for the directory, its status, sums and index
        assert total <= 60, 'got %d stat calls for 50 files: %r' % (total, counts)


class TestParseArgs:
    """Tests for the command line."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, 'argv', ['dirsigner.py', '-t', '/pub'])
        opts = dirsigner.parse_args()
        assert opts.tree == '/pub'
        assert opts.hash_algorithm == 'sha256'
        assert opts.excludes == []
        assert opts.vsort is False
        assert opts.lockfile == '.dirsigner.lock'

    def test_tree_is_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, 'argv', ['dirsigner.py'])
        with pytest.raises(SystemExit):
            dirsigner.parse_args()

    def test_unsupported_algorithm_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, 'argv', ['dirsigner.py', '-t', '/pub', '-a', 'whirlpool'])
        with pytest.raises(SystemExit):
            dirsigner.parse_args()

    def test_repeated_excludes_accumulate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, 'argv',
                            ['dirsigner.py', '-t', '/pub', '-x', '*.txt', '-x', 'tmp*'])
        assert dirsigner.parse_args().excludes == ['*.txt', 'tmp*']

    def test_chunk_bytes_sets_the_global(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, 'argv', ['dirsigner.py', '-t', '/pub', '-b', '4096'])
        opts = dirsigner.parse_args()
        assert opts.chunksize == 4096
        assert dirsigner.CHUNK_SIZE == 4096


# A real listing served by kernel.org's nginx, used to check that our own
# rendering is byte-for-byte identical. Fetched from
# https://www.kernel.org/pub/software/devel/liblore/
NGINX_LIBLORE = (
    '<html>\r\n'
    '<head><title>Index of /pub/software/devel/liblore/</title></head>\r\n'
    '<body>\r\n'
    '<h1>Index of /pub/software/devel/liblore/</h1><hr><pre><a href="../">../</a>\r\n'
    '<a href="liblore-0.4.0.tar.gz">liblore-0.4.0.tar.gz</a>'
    '                               10-Aug-2026 19:00     40K\r\n'
    '<a href="liblore-0.4.0.tar.sign">liblore-0.4.0.tar.sign</a>'
    '                             10-Aug-2026 19:00     384\r\n'
    '<a href="liblore-0.9.0.tar.xz">liblore-0.9.0.tar.xz</a>'
    '                               13-Aug-2026 01:05    111K\r\n'
    '<a href="sha256sums.asc">sha256sums.asc</a>'
    '                                     13-Aug-2026 01:05    1578\r\n'
    '</pre><hr></body>\r\n'
    '</html>\r\n'
).encode('utf-8')

# The same, for a directory-only listing, from
# https://www.kernel.org/pub/software/devel/
NGINX_DEVEL = (
    '<html>\r\n'
    '<head><title>Index of /pub/software/devel/</title></head>\r\n'
    '<body>\r\n'
    '<h1>Index of /pub/software/devel/</h1><hr><pre><a href="../">../</a>\r\n'
    '<a href="b4/">b4/</a>'
    '                                                10-Aug-2026 19:05       -\r\n'
    '<a href="liblore/">liblore/</a>'
    '                                           13-Aug-2026 01:05       -\r\n'
    '</pre><hr></body>\r\n'
    '</html>\r\n'
).encode('utf-8')


def without_marker(page: bytes) -> bytes:
    """Strip our marker line, leaving what nginx itself would have served."""
    assert page.startswith(dirsigner.INDEX_MARKER + b'\r\n'), 'marker must be the first line'
    return page[len(dirsigner.INDEX_MARKER) + 2:]


def visible(line: str) -> str:
    """Strip anchor markup from an index line, leaving what a browser shows."""
    text = re.sub(r'</?a[^>]*>', '', line)
    return text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')


def utc(stamp: str) -> int:
    """Turn an nginx-style '10-Aug-2026 19:00' timestamp into a UTC epoch."""
    return calendar.timegm(time.strptime(stamp, '%d-%b-%Y %H:%M'))


class TestFormatSize:
    """Tests for nginx-compatible size rendering."""

    @pytest.mark.parametrize(('size', 'expected'), [
        (0, '0'),
        (384, '384'),
        (1578, '1578'),
        (9999, '9999'),          # the largest size shown in bytes
        (10000, '10K'),          # ...and the first one shown in K
        (40 * 1024, '40K'),
        (1024 * 1024 - 1, '1024K'),
        (1024 * 1024, '1M'),
        (1024 ** 3 - 1, '1024M'),
        (1024 ** 3, '1G'),
    ])
    def test_thresholds(self, size: int, expected: str) -> None:
        assert dirsigner.format_size(size) == expected

    def test_rounds_up_at_half_a_unit(self) -> None:
        assert dirsigner.format_size(40 * 1024 + 512) == '41K'

    def test_rounds_down_below_half_a_unit(self) -> None:
        assert dirsigner.format_size(40 * 1024 + 511) == '40K'


class TestFormatIndexEntry:
    """Tests for a single index line."""

    def test_file_entry(self) -> None:
        line = dirsigner.format_index_entry('liblore-0.4.0.tar.gz', utc('10-Aug-2026 19:00'), 40 * 1024)
        assert line == ('<a href="liblore-0.4.0.tar.gz">liblore-0.4.0.tar.gz</a>'
                        '                               10-Aug-2026 19:00     40K')

    def test_directory_entry_has_a_dash_for_size(self) -> None:
        line = dirsigner.format_index_entry('b4/', utc('10-Aug-2026 19:05'), None)
        assert line == ('<a href="b4/">b4/</a>'
                        '                                                10-Aug-2026 19:05       -')

    def test_columns_line_up(self) -> None:
        rendered = [visible(dirsigner.format_index_entry(name, 0, 1))
                    for name in ('a', 'a' * 49, 'a' * 50, 'a' * 80)]
        # The name column is 50 wide, then one space, then the date
        assert {line.index('01-Jan-1970') for line in rendered} == {51}
        assert len(set(len(line) for line in rendered)) == 1

    def test_timestamps_are_utc(self) -> None:
        assert '01-Jan-1970 00:00' in dirsigner.format_index_entry('a', 0, 1)

    def test_long_name_is_truncated_but_still_links_in_full(self) -> None:
        name = 'linux-' + 'x' * 60 + '.tar.xz'
        line = dirsigner.format_index_entry(name, 0, 1)
        assert '<a href="%s">' % name in line, 'the link must use the whole name'
        assert '%s..&gt;</a>' % name[:47] in line
        # ...and what the browser shows is exactly 50 columns wide
        assert visible(line).index('01-Jan-1970') == 51
        assert visible(line).startswith(name[:47] + '..>')

    def test_special_characters_are_escaped(self) -> None:
        line = dirsigner.format_index_entry('a&b <c>.txt', 0, 1)
        assert '>a&amp;b &lt;c&gt;.txt</a>' in line
        assert 'href="a%26b%20%3Cc%3E.txt"' in line

    def test_utf8_name(self) -> None:
        line = dirsigner.format_index_entry('café.tar.xz', 0, 1)
        assert '>café.tar.xz</a>' in line
        assert 'href="caf%C3%A9.tar.xz"' in line


class TestRenderIndex:
    """Tests that our page is what nginx's autoindex would have served."""

    def test_matches_a_real_nginx_file_listing(self) -> None:
        entries = [
            dirsigner.format_index_entry('liblore-0.4.0.tar.gz', utc('10-Aug-2026 19:00'), 40 * 1024),
            dirsigner.format_index_entry('liblore-0.4.0.tar.sign', utc('10-Aug-2026 19:00'), 384),
            dirsigner.format_index_entry('liblore-0.9.0.tar.xz', utc('13-Aug-2026 01:05'), 111 * 1024),
            dirsigner.format_index_entry('sha256sums.asc', utc('13-Aug-2026 01:05'), 1578),
        ]
        assert without_marker(dirsigner.render_index(
            '/pub/software/devel/liblore/', entries)) == NGINX_LIBLORE

    def test_matches_a_real_nginx_directory_listing(self) -> None:
        entries = [
            dirsigner.format_index_entry('b4/', utc('10-Aug-2026 19:05'), None),
            dirsigner.format_index_entry('liblore/', utc('13-Aug-2026 01:05'), None),
        ]
        assert without_marker(dirsigner.render_index(
            '/pub/software/devel/', entries)) == NGINX_DEVEL

    def test_uses_crlf_line_endings(self) -> None:
        page = dirsigner.render_index('/', [])
        assert page.endswith(b'</pre><hr></body>\r\n</html>\r\n')
        assert b'\n' not in page.replace(b'\r\n', b'')

    def test_always_links_to_the_parent(self) -> None:
        assert b'<a href="../">../</a>' in dirsigner.render_index('/', [])


class TestWriteIndexFile:
    """Tests for writing index.html in one directory."""

    @staticmethod
    def _status(**sizes: int) -> dict[str, dict[str, object]]:
        return {name: {'mtime': 0, 'size': size, 'hash': 'x'} for name, size in sizes.items()}

    def test_lists_directories_first_then_files(self, tmp_path: Path) -> None:
        (tmp_path / 'v6.x').mkdir()
        (tmp_path / 'README').write_text('hi')
        dirsigner.write_index_file(str(tmp_path), '/pub/', ['README'], ['v6.x'],
                                   self._status(README=2), 'sha256', [])
        names = [line.split('"')[1] for line in
                 (tmp_path / 'index.html').read_text().splitlines() if '<a href' in line]
        assert names == ['../', 'v6.x/', 'README']

    def test_keeps_the_order_it_is_given(self, tmp_path: Path) -> None:
        # sign_tree hands us files in sums-file order; we must not reshuffle.
        files = ['linux-6.2.tar.xz', 'linux-6.10.tar.xz']
        dirsigner.write_index_file(str(tmp_path), '/pub/', files, [],
                                   self._status(**{f: 1 for f in files}), 'sha256', [])
        names = [line.split('"')[1] for line in
                 (tmp_path / 'index.html').read_text().splitlines() if '<a href' in line]
        assert names == ['../'] + files

    def test_lists_the_sums_file_last(self, tmp_path: Path) -> None:
        (tmp_path / 'sha256sums.asc').write_text('signed stuff')
        dirsigner.write_index_file(str(tmp_path), '/pub/', ['README'], [],
                                   self._status(README=2), 'sha256', [])
        names = [line.split('"')[1] for line in
                 (tmp_path / 'index.html').read_text().splitlines() if '<a href' in line]
        assert names == ['../', 'README', 'sha256sums.asc']

    def test_does_not_list_a_sums_file_that_is_not_there(self, tmp_path: Path) -> None:
        dirsigner.write_index_file(str(tmp_path), '/pub/', ['README'], [],
                                   self._status(README=2), 'sha256', [])
        assert 'sums.asc' not in (tmp_path / 'index.html').read_text()

    def test_dotfiles_are_left_out(self, tmp_path: Path) -> None:
        dirsigner.write_index_file(str(tmp_path), '/pub/', ['.htaccess', 'README'], [],
                                   self._status(**{'.htaccess': 1, 'README': 2}), 'sha256', [])
        assert '.htaccess' not in (tmp_path / 'index.html').read_text()

    def test_nothing_to_list_still_writes_a_bare_index(self, tmp_path: Path) -> None:
        # autoindex served a page with just the parent link for an empty
        # directory, so leaving none behind would turn those into a 404
        dirsigner.write_index_file(str(tmp_path), '/pub/', [], [], {}, 'sha256', [])
        written = (tmp_path / 'index.html').read_bytes()
        assert without_marker(written) == (
            b'<html>\r\n'
            b'<head><title>Index of /pub/</title></head>\r\n'
            b'<body>\r\n'
            b'<h1>Index of /pub/</h1><hr><pre><a href="../">../</a>\r\n'
            b'</pre><hr></body>\r\n'
            b'</html>\r\n'
        )

    def test_nothing_to_list_replaces_our_own_stale_index(self, tmp_path: Path) -> None:
        (tmp_path / 'index.html').write_bytes(dirsigner.INDEX_MARKER + b'\r\nstale')
        dirsigner.write_index_file(str(tmp_path), '/pub/', [], [], {}, 'sha256', [])
        assert b'Index of /pub/' in (tmp_path / 'index.html').read_bytes()

    def test_unchanged_index_is_not_rewritten(self, tmp_path: Path) -> None:
        args: tuple[object, ...] = (str(tmp_path), '/pub/', ['README'], [],
                                    self._status(README=2), 'sha256', [])
        dirsigner.write_index_file(*args)
        index = tmp_path / 'index.html'
        before = index.stat().st_mtime_ns
        os.utime(index, ns=(before - 10_000_000_000, before - 10_000_000_000))
        stale = index.stat().st_mtime_ns
        dirsigner.write_index_file(*args)
        assert index.stat().st_mtime_ns == stale, 'identical index.html was rewritten'

    def test_changed_index_is_rewritten(self, tmp_path: Path) -> None:
        dirsigner.write_index_file(str(tmp_path), '/pub/', ['README'], [],
                                   self._status(README=2), 'sha256', [])
        dirsigner.write_index_file(str(tmp_path), '/pub/', ['README', 'NEWS'], [],
                                   self._status(README=2, NEWS=3), 'sha256', [])
        assert 'NEWS' in (tmp_path / 'index.html').read_text()

    def test_sizes_and_mtimes_come_from_the_status(self, tmp_path: Path) -> None:
        # The file on disk is empty, but the status says otherwise: proof that
        # we are not stat()ing files we already know about.
        (tmp_path / 'blob').write_text('')
        status = {'blob': {'mtime': utc('10-Aug-2026 19:00'), 'size': 40 * 1024, 'hash': 'x'}}
        dirsigner.write_index_file(str(tmp_path), '/pub/', ['blob'], [], status, 'sha256', [])
        assert '10-Aug-2026 19:00     40K' in (tmp_path / 'index.html').read_text()

    def test_unstatable_subdirectory_is_skipped(self, tmp_path: Path) -> None:
        dirsigner.write_index_file(str(tmp_path), '/pub/', ['README'], ['vanished'],
                                   self._status(README=2), 'sha256', [])
        assert 'vanished' not in (tmp_path / 'index.html').read_text()


class TestSignTreeIndex:
    """End-to-end tests for the index.html feature."""

    def test_off_by_default(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content', 'sub/inner': 'inner'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        assert not (tmp_path / 'index.html').exists()
        assert not (tmp_path / 'sub' / 'index.html').exists()

    def test_off_by_default_still_signs_a_stray_index_html(self, tmp_path: Path) -> None:
        # Turning the feature on is what starts excluding index.html; with it
        # off, a hand-written index.html is treated like any other file.
        make_tree(tmp_path, {'index.html': 'hand written'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        assert [line.split('  ')[1] for line in read_sums(tmp_path / 'sha256sums.asc')] == ['index.html']

    def test_writes_an_index_in_every_directory(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content', 'sub/inner': 'inner'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        assert (tmp_path / 'index.html').exists()
        assert (tmp_path / 'sub' / 'index.html').exists()

    def test_index_is_never_checksummed(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        assert [line.split('  ')[1] for line in read_sums(tmp_path / 'sha256sums.asc')] == ['blob']
        assert list(read_status(tmp_path).keys()) == ['blob']

    def test_a_previously_signed_index_drops_out(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'index.html': 'hand written', 'blob': 'content'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256')
        assert 'index.html' in read_status(tmp_path)

        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        assert list(read_status(tmp_path).keys()) == ['blob']
        assert [line.split('  ')[1] for line in read_sums(tmp_path / 'sha256sums.asc')] == ['blob']

    def test_index_matches_the_sums_file_order(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {
            'linux-6.10.tar.xz': 'ten',
            'linux-6.2.tar.xz': 'two',
            'linux-6.9.tar.xz': 'nine',
        })
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True, vsort=True)
        signed = [line.split('  ')[1] for line in read_sums(tmp_path / 'sha256sums.asc')]
        listed = [line.split('"')[1] for line in
                  (tmp_path / 'index.html').read_text().splitlines() if '<a href' in line]
        assert signed == ['linux-6.2.tar.xz', 'linux-6.9.tar.xz', 'linux-6.10.tar.xz']
        assert listed == ['../'] + signed + ['sha256sums.asc']

    def test_subdirectories_are_listed_sorted_by_name(self, tmp_path: Path) -> None:
        # By name even with vsort, same as the server's autoindex did.
        make_tree(tmp_path, {'v6.10/a': 'a', 'v6.2/b': 'b', 'v6.9/c': 'c'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True, vsort=True)
        listed = [line.split('"')[1] for line in
                  (tmp_path / 'index.html').read_text().splitlines() if '<a href' in line]
        assert listed == ['../', 'v6.10/', 'v6.2/', 'v6.9/']

    def test_excluded_directories_are_not_listed(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content', 'nope/inner': 'inner'})
        dirsigner.sign_tree(str(tmp_path), ['nope'], 'sha256', index=True)
        assert 'nope' not in (tmp_path / 'index.html').read_text()
        assert not (tmp_path / 'nope' / 'index.html').exists()

    def test_url_prefix_shows_up_in_the_title(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'v6.x/blob': 'content'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True, urlprefix='/pub')
        assert '<title>Index of /pub/</title>' in (tmp_path / 'index.html').read_text()
        assert '<h1>Index of /pub/v6.x/</h1>' in (tmp_path / 'v6.x' / 'index.html').read_text()

    @pytest.mark.parametrize('prefix', ['/', '', 'pub/', '/pub', '/pub/'])
    def test_url_prefix_is_normalised(self, tmp_path: Path, prefix: str) -> None:
        make_tree(tmp_path, {'blob': 'content'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True, urlprefix=prefix)
        expected = '/pub/' if 'pub' in prefix else '/'
        assert '<title>Index of %s</title>' % expected in (tmp_path / 'index.html').read_text()

    def test_second_run_changes_nothing(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content', 'sub/inner': 'inner'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        before = {p: p.read_bytes() for p in sorted(tmp_path.rglob('*')) if p.is_file()}
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        after = {p: p.read_bytes() for p in sorted(tmp_path.rglob('*')) if p.is_file()}
        assert before == after

    def test_index_follows_a_removed_file(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'stays': 'a', 'goes': 'b'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        assert 'goes' in (tmp_path / 'index.html').read_text()

        os.unlink(tmp_path / 'goes')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        assert 'goes' not in (tmp_path / 'index.html').read_text()

    def test_emptied_directory_keeps_a_bare_index(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        os.unlink(tmp_path / 'blob')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        # The sums file and the status file go, but the listing stays, now
        # holding nothing but the link to the parent
        assert [p.name for p in tmp_path.iterdir()] == ['index.html']
        listed = [line.split('"')[1] for line in
                  (tmp_path / 'index.html').read_text().splitlines() if '<a href' in line]
        assert listed == ['../']

    def test_directory_with_only_subdirectories_gets_an_index(self, tmp_path: Path) -> None:
        # There is no sums file here, so this only works because the index is
        # written outside the "did anything change" gate.
        make_tree(tmp_path, {'sub/blob': 'content'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        assert not (tmp_path / 'sha256sums.asc').exists()
        assert '<a href="sub/">' in (tmp_path / 'index.html').read_text()


class TestStatIndexEntry:
    """Tests for the on-disk lookup used for dirs, symlinks and the sums file."""

    def test_plain_file(self, tmp_path: Path) -> None:
        (tmp_path / 'blob').write_bytes(b'x' * 40960)
        os.utime(tmp_path / 'blob', (0, utc('10-Aug-2026 19:00')))
        entry = dirsigner.stat_index_entry(str(tmp_path), 'blob')
        assert entry is not None
        assert '10-Aug-2026 19:00     40K' in entry

    def test_directory_gets_a_slash_and_a_dash(self, tmp_path: Path) -> None:
        (tmp_path / 'v6.x').mkdir()
        entry = dirsigner.stat_index_entry(str(tmp_path), 'v6.x')
        assert entry is not None
        assert entry.startswith('<a href="v6.x/">v6.x/</a>')
        assert entry.endswith('       -')

    def test_symlink_shows_the_target(self, tmp_path: Path) -> None:
        (tmp_path / 'linux-6.12.tar.xz').write_bytes(b'x' * 40960)
        os.utime(tmp_path / 'linux-6.12.tar.xz', (0, utc('10-Aug-2026 19:00')))
        (tmp_path / 'linux-latest.tar.xz').symlink_to('linux-6.12.tar.xz')
        entry = dirsigner.stat_index_entry(str(tmp_path), 'linux-latest.tar.xz')
        assert entry is not None
        # The link's own name, but the target's size and mtime
        assert entry.startswith('<a href="linux-latest.tar.xz">linux-latest.tar.xz</a>')
        assert '10-Aug-2026 19:00     40K' in entry

    def test_symlink_to_a_directory_is_listed_as_one(self, tmp_path: Path) -> None:
        (tmp_path / 'v6.12').mkdir()
        (tmp_path / 'latest').symlink_to(tmp_path / 'v6.12')
        entry = dirsigner.stat_index_entry(str(tmp_path), 'latest')
        assert entry is not None
        assert entry.startswith('<a href="latest/">latest/</a>')
        assert entry.endswith('       -')

    def test_dangling_symlink_falls_back_to_the_link(self, tmp_path: Path) -> None:
        (tmp_path / 'broken').symlink_to('nowhere-at-all')
        entry = dirsigner.stat_index_entry(str(tmp_path), 'broken')
        assert entry is not None
        assert entry.startswith('<a href="broken">broken</a>')
        # The size is the length of the target path, which is what lstat says
        assert entry.endswith('%8d' % len('nowhere-at-all'))

    def test_missing_name_returns_none(self, tmp_path: Path) -> None:
        assert dirsigner.stat_index_entry(str(tmp_path), 'not-there') is None


class TestSymlinksInIndex:
    """Symlinks are listed, but never checksummed."""

    def test_symlink_is_listed_but_not_signed(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'linux-6.12.tar.xz': 'content'})
        (tmp_path / 'linux-latest.tar.xz').symlink_to('linux-6.12.tar.xz')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)

        signed = [line.split('  ')[1] for line in read_sums(tmp_path / 'sha256sums.asc')]
        assert signed == ['linux-6.12.tar.xz'], 'symlinks must stay out of the sums file'
        assert list(read_status(tmp_path).keys()) == ['linux-6.12.tar.xz']

        listed = [line.split('"')[1] for line in
                  (tmp_path / 'index.html').read_text().splitlines() if '<a href' in line]
        assert listed == ['../', 'linux-6.12.tar.xz', 'linux-latest.tar.xz', 'sha256sums.asc']

    def test_symlinks_come_after_the_signed_files(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'linux-6.2.tar.xz': 'a', 'linux-6.10.tar.xz': 'b'})
        (tmp_path / 'linux-6.20.tar.xz').symlink_to('linux-6.2.tar.xz')
        (tmp_path / 'linux-6.3.tar.xz').symlink_to('linux-6.2.tar.xz')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True, vsort=True)
        listed = [line.split('"')[1] for line in
                  (tmp_path / 'index.html').read_text().splitlines() if '<a href' in line]
        assert listed == ['../',
                          'linux-6.2.tar.xz', 'linux-6.10.tar.xz',      # signed, version sorted
                          'linux-6.3.tar.xz', 'linux-6.20.tar.xz',      # links, version sorted
                          'sha256sums.asc']

    def test_symlink_to_a_subdirectory_is_listed(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'v6.12/blob': 'content'})
        (tmp_path / 'latest').symlink_to(tmp_path / 'v6.12')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        listed = [line.split('"')[1] for line in
                  (tmp_path / 'index.html').read_text().splitlines() if '<a href' in line]
        assert listed == ['../', 'latest/', 'v6.12/']

    def test_symlinked_directory_is_not_descended_into(self, tmp_path: Path) -> None:
        # os.walk does not follow directory symlinks, so the target is visited
        # once, under its real name, and gets a single index.html.
        make_tree(tmp_path, {'v6.12/blob': 'content'})
        (tmp_path / 'latest').symlink_to(tmp_path / 'v6.12')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        written = [os.path.join(root, dirsigner.INDEX_FILE) for root, _, files
                   in os.walk(str(tmp_path)) if dirsigner.INDEX_FILE in files]
        assert written == [str(tmp_path / 'index.html'), str(tmp_path / 'v6.12' / 'index.html')]

    def test_excluded_symlinked_directories_are_not_listed(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'real/blob': 'content', 'keep/blob': 'content'})
        (tmp_path / 'skipme').symlink_to('real')
        dirsigner.sign_tree(str(tmp_path), ['skipme'], 'sha256', index=True)
        listed = [line.split('"')[1] for line in
                  (tmp_path / 'index.html').read_text().splitlines() if '<a href' in line]
        assert listed == ['../', 'keep/', 'real/'], 'excludes must apply to symlinked dirs'

    def test_excluded_symlinks_are_not_listed(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content'})
        (tmp_path / 'skip.txt').symlink_to('blob')
        dirsigner.sign_tree(str(tmp_path), ['*.txt'], 'sha256', index=True)
        assert 'skip.txt' not in (tmp_path / 'index.html').read_text()

    def test_an_index_html_symlink_is_left_alone(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content'})
        (tmp_path / 'index.html').symlink_to('blob')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        # We never create a symlink, so one here is somebody else's doing
        assert (tmp_path / 'index.html').is_symlink()
        assert (tmp_path / 'index.html').readlink().name == 'blob'
        # ...and nothing was written through it
        assert (tmp_path / 'blob').read_text() == 'content'

    def test_dangling_symlink_is_listed(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content'})
        (tmp_path / 'broken').symlink_to('gone')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        assert '<a href="broken">' in (tmp_path / 'index.html').read_text()

    def test_a_file_that_becomes_a_symlink_stops_being_signed(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'linux-latest.tar.xz': 'real file', 'linux-6.12.tar.xz': 'content'})
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        assert 'linux-latest.tar.xz' in read_status(tmp_path)

        os.unlink(tmp_path / 'linux-latest.tar.xz')
        (tmp_path / 'linux-latest.tar.xz').symlink_to('linux-6.12.tar.xz')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        assert list(read_status(tmp_path).keys()) == ['linux-6.12.tar.xz']
        assert 'linux-latest.tar.xz' in (tmp_path / 'index.html').read_text()

    def test_second_run_changes_nothing_with_symlinks(self, tmp_path: Path) -> None:
        make_tree(tmp_path, {'blob': 'content', 'sub/inner': 'inner'})
        (tmp_path / 'link').symlink_to('blob')
        (tmp_path / 'dirlink').symlink_to(tmp_path / 'sub')
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        before = {p: p.read_bytes() for p in sorted(tmp_path.rglob('*'))
                  if p.is_file() and not p.is_symlink()}
        dirsigner.sign_tree(str(tmp_path), [], 'sha256', index=True)
        after = {p: p.read_bytes() for p in sorted(tmp_path.rglob('*'))
                 if p.is_file() and not p.is_symlink()}
        assert before == after
