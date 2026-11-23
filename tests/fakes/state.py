"""Damage state files the way real crashes and disks do, for corruption tests.

    from tests.fakes.state import corrupt
    corrupt(world.data_dir / "state.db", "truncate")
"""

from __future__ import annotations

from pathlib import Path

HOW = ("garbage", "truncate", "empty", "zero_bytes_tail", "delete")


def corrupt(path: Path, how: str = "garbage") -> Path:
    """garbage: replace with non-JSON/non-SQLite bytes; truncate: keep the first
    half (torn write); empty: zero-length file; zero_bytes_tail: overwrite the
    last 4 KiB with NULs (lost page); delete: remove the file. Missing files
    are created for every mode except `delete`."""
    if how not in HOW:
        raise ValueError(f"unknown corruption {how!r}; choose from {HOW}")
    path = Path(path)
    data = path.read_bytes() if path.exists() else b""
    if how == "delete":
        path.unlink(missing_ok=True)
    elif how == "garbage":
        path.write_bytes(b"{not json \x00\xff" + b"x" * 64)
    elif how == "truncate":
        path.write_bytes(data[: len(data) // 2])
    elif how == "empty":
        path.write_bytes(b"")
    else:
        n = min(4096, len(data))
        path.write_bytes(data[: len(data) - n] + b"\x00" * n if n else b"\x00" * 4096)
    return path
