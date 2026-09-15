"""Owner-only filesystem primitives, in one place.

Two copies of a 0600 write in a credential tool is one copy that can drift.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

FILE_MODE: Final = 0o600
DIR_MODE: Final = 0o700


def private_dir(path: Path) -> Path:
    # mkdir(parents=True) ignores `mode` for the intermediates it creates, so the
    # root would land at the umask default. chmod each level we own.
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(DIR_MODE)
    return path


def write_private(path: Path, text: str) -> None:
    # The temp name carries the pid: two processes writing the same credential
    # file must not share one inode, or the loser's stale fd writes straight
    # into the file the winner already committed and tears it. O_EXCL makes the
    # collision an error rather than a silent share.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def create_private_file(path: Path) -> None:
    """Create an empty owner-only file if absent. sqlite would create it 0644."""
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, FILE_MODE))
