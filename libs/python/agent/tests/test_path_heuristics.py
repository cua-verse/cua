"""Windows-path detection in ``MilestoneTool._is_windows_path`` and
``analyze_image._is_remote_path``.

Regression guard: both helpers previously rejected forward-slash drive
paths (``C:/Users/…``) — valid on Windows and routinely emitted by
agents (JSON escaping is easier without backslashes). analyze_image fell
through to the local-filesystem branch and returned ``file not found``.
"""

import pytest

from cua_bench.agents.openclaw.analyze_image import _is_remote_path
from cua_bench.agents.openclaw.milestone import MilestoneTool


# Build a MilestoneTool without an interface — the helper is pure.
_MILESTONE = MilestoneTool.__new__(MilestoneTool)


_WINDOWS_LIKE = [
    # Drive-letter with either separator.
    "C:\\Users\\User\\Desktop\\output\\1.png",
    "C:/Users/User/Desktop/output/1.png",
    "D:\\games\\mota",
    "d:/games/mota",
    "E:\\",
    "E:/",
    # UNC shares.
    "\\\\server\\share\\file.png",
    # Any path containing a backslash is treated as Windows-style.
    "relative\\subdir\\file.txt",
]

_POSIX_LIKE = [
    # Pure POSIX paths — must NOT be classified as Windows/remote.
    "/home/user/file.png",
    "/tmp/output/1.png",
    "relative/subdir/file.txt",
    "./output/1.png",
    "file.png",
]


@pytest.mark.parametrize("path", _WINDOWS_LIKE)
def test_is_remote_path_detects_windows(path):
    assert _is_remote_path(path) is True


@pytest.mark.parametrize("path", _POSIX_LIKE)
def test_is_remote_path_rejects_posix(path):
    assert _is_remote_path(path) is False


@pytest.mark.parametrize("path", _WINDOWS_LIKE)
def test_milestone_is_windows_detects_windows(path):
    assert _MILESTONE._is_windows_path(path) is True


@pytest.mark.parametrize("path", _POSIX_LIKE)
def test_milestone_is_windows_rejects_posix(path):
    assert _MILESTONE._is_windows_path(path) is False


def test_both_helpers_agree_on_forward_slash_drive_path():
    """Core regression: ``C:/…`` must be treated as Windows by BOTH helpers.

    Asymmetric classification caused MilestoneTool to save to the remote VM
    (via ``posixpath.dirname`` + VM slash-normalisation, happens to work)
    while analyze_image tried to read the same path locally and errored
    out.
    """
    path = "C:/Users/User/Desktop/game/GAME_MOTA_24_EZ/output/1.png"
    assert _is_remote_path(path) is True
    assert _MILESTONE._is_windows_path(path) is True
