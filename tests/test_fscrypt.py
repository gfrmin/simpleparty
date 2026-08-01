"""fscrypt detection, and how a missing fscrypt tool gets reported.

Detection is answered by the kernel and must work with no fscrypt binary
installed; only unlock/lock need the CLI. These tests pin that split, and pin
the failure that used to hide a locked directory: treating "we couldn't ask"
as "this directory isn't encrypted".
"""

import subprocess

import pytest

from simpleparty import library
from simpleparty.render import render_locked_page


@pytest.fixture
def no_subprocess(monkeypatch):
    """Fail loudly if anything shells out — detection must not need the CLI."""
    def boom(*args, **kwargs):
        raise AssertionError(f'unexpected subprocess call: {args!r}')

    monkeypatch.setattr(library.subprocess, 'run', boom)
    monkeypatch.setattr(library.subprocess, 'Popen', boom)


def _completed(returncode, stdout='', stderr=''):
    return subprocess.CompletedProcess(['fscrypt'], returncode, stdout, stderr)


# --- kernel detection ---

def test_plain_dir_reported_unencrypted_without_binary(tmp_path, no_subprocess):
    assert library._probe_kernel_status(str(tmp_path)) == {
        'encrypted': False, 'unlocked': True,
    }


def test_unreadable_path_reported_unencrypted(tmp_path, no_subprocess):
    assert library._probe_kernel_status(str(tmp_path / 'nope')) == {
        'encrypted': False, 'unlocked': True,
    }


def test_get_status_answers_from_kernel_alone(tmp_path, no_subprocess):
    assert library.get_fscrypt_status(str(tmp_path))['encrypted'] is False


def test_status_dicts_are_not_shared(tmp_path, no_subprocess):
    first = library.get_fscrypt_status(str(tmp_path))
    first['encrypted'] = 'mutated'
    assert library.get_fscrypt_status(str(tmp_path))['encrypted'] is False


def test_has_encrypted_dir_false_for_plain_tree(tmp_path, no_subprocess):
    (tmp_path / 'sub').mkdir()
    assert library.has_encrypted_dir(str(tmp_path)) is False


# --- tool availability ---

def test_tool_error_when_binary_absent(monkeypatch):
    monkeypatch.setattr(library.shutil, 'which', lambda _: None)
    assert library._probe_fscrypt_tool() == library.FSCRYPT_NOT_INSTALLED


def test_tool_error_when_installed_but_unconfigured(monkeypatch):
    """The state a machine is in between `pacman -S fscrypt` and
    `sudo fscrypt setup` — previously indistinguishable from 'not encrypted'."""
    monkeypatch.setattr(library.shutil, 'which', lambda _: '/usr/bin/fscrypt')
    monkeypatch.setattr(library.subprocess, 'run', lambda *a, **kw: _completed(
        1, stderr='fscrypt: global config file does not exist. Run "sudo fscrypt setup".',
    ))
    assert library._probe_fscrypt_tool() == library.FSCRYPT_NOT_SET_UP


def test_no_tool_error_when_status_succeeds(monkeypatch):
    monkeypatch.setattr(library.shutil, 'which', lambda _: '/usr/bin/fscrypt')
    monkeypatch.setattr(library.subprocess, 'run',
                        lambda *a, **kw: _completed(0, stdout='MOUNTPOINT  DEVICE'))
    assert library._probe_fscrypt_tool() is None


def test_failed_status_is_logged_not_swallowed(tmp_path, monkeypatch, caplog):
    """Regression: a failing `fscrypt status` used to return 'not encrypted'
    with no trace, so a locked directory rendered as an ordinary folder."""
    monkeypatch.setattr(library.subprocess, 'run', lambda *a, **kw: _completed(
        1, stderr='fscrypt: permission denied'))
    with caplog.at_level('WARNING', logger='simpleparty.library'):
        status = library._probe_fscrypt_status(str(tmp_path))
    assert status['encrypted'] is False
    assert 'permission denied' in caplog.text


# --- what the user is told ---

def test_locked_page_offers_passphrase_when_tool_available():
    html = render_locked_page('more', 'more')
    assert 'type="password"' in html


def test_locked_page_explains_missing_tool_instead_of_dead_form():
    html = render_locked_page('more', 'more',
                              tool_error=library.FSCRYPT_NOT_INSTALLED)
    assert 'type="password"' not in html
    assert 'fscrypt is not installed' in html
    assert 'Install fscrypt' in html
    assert 'sudo fscrypt setup' in html


def test_locked_page_remedy_matches_the_actual_problem():
    """Someone who already installed fscrypt should not be told to install it."""
    html = render_locked_page('more', 'more', tool_error=library.FSCRYPT_NOT_SET_UP)
    assert 'fscrypt is not set up' in html
    assert 'Install fscrypt' not in html
    assert 'sudo fscrypt setup' in html
