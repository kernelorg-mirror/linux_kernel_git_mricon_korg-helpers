"""Tests for dirsigner.py."""

import functools
import hashlib
import importlib.util
import json
import os
import sys
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


class TestVersionSort:
    """Tests for the version-aware comparator."""

    def test_identical(self) -> None:
        assert dirsigner.version_sort('linux-6.1.tar.gz', 'linux-6.1.tar.gz') == 0

    def test_name_wins_over_version(self) -> None:
        assert dirsigner.version_sort('aaa-1.0.gz', 'zzz-9.0.gz') < 0
        assert dirsigner.version_sort('zzz-1.0.gz', 'aaa-9.0.gz') > 0

    def test_version_is_numeric_not_lexical(self) -> None:
        # This is the whole point of --version-sort: 1.10 comes after 1.2.
        assert dirsigner.version_sort('linux-1.2.tar.gz', 'linux-1.10.tar.gz') < 0

    def test_tail_breaks_the_tie(self) -> None:
        assert dirsigner.version_sort('linux-6.1.tar.gz', 'linux-6.1.tar.sign') < 0

    def test_sorts_a_realistic_listing(self) -> None:
        names = [
            'linux-6.12.10.tar.xz',
            'linux-6.12.2.tar.xz',
            'patch-6.12.1.xz',
            'linux-6.12.2.tar.sign',
            'linux-6.9.tar.xz',
        ]
        names.sort(key=functools.cmp_to_key(dirsigner.version_sort))
        assert names == [
            'linux-6.9.tar.xz',
            'linux-6.12.2.tar.sign',
            'linux-6.12.2.tar.xz',
            'linux-6.12.10.tar.xz',
            'patch-6.12.1.xz',
        ]


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

    def test_vsort_reorders(self, tmp_path: Path) -> None:
        names = ['linux-6.10.tar.xz', 'linux-6.2.tar.xz']
        dirsigner.write_sums_file(str(tmp_path), names, self._status(*names), 'sha256', vsort=True)
        lines = read_sums(tmp_path / 'sha256sums.asc')
        assert [line.split('  ')[1] for line in lines] == ['linux-6.2.tar.xz', 'linux-6.10.tar.xz']

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
