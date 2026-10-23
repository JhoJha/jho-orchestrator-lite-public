#!/usr/bin/env python3
"""Machine-safe Git status parsing shared by Operational v1 components."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


class GitStatusError(RuntimeError):
    pass


def parse_porcelain_v1_z(data: bytes) -> list[dict[str, Any]]:
    """Parse ``git status --porcelain=v1 -z`` without path heuristics.

    With ``-z``, rename/copy records contain destination in the status record and
    origin in the following NUL field. Git performs no C-style path quoting.
    """
    if not isinstance(data, bytes):
        raise GitStatusError("porcelain input must be bytes")
    fields = data.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    records: list[dict[str, Any]] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4 or field[2:3] != b" ":
            raise GitStatusError("malformed porcelain v1 -z record")
        status = field[:2].decode("ascii", "strict")
        destination = field[3:].decode("utf-8", "surrogateescape")
        if not destination:
            raise GitStatusError("empty path in porcelain record")
        origin = None
        if "R" in status or "C" in status:
            if index >= len(fields):
                raise GitStatusError("rename/copy record missing origin")
            origin = fields[index].decode("utf-8", "surrogateescape")
            index += 1
            if not origin:
                raise GitStatusError("rename/copy record has empty origin")
        records.append(
            {
                "status": status,
                "path": destination,
                "origin": origin,
                "destination": destination if origin is not None else None,
            }
        )
    return records


def status_records(repo: Path, *, timeout: float = 20.0) -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=str(repo),
            capture_output=True,
            text=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitStatusError("git status timeout") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"git status failed").decode(
            "utf-8", "replace"
        )
        raise GitStatusError(detail[:600])
    return parse_porcelain_v1_z(proc.stdout or b"")


def changed_paths(records: list[dict[str, Any]]) -> list[str]:
    """Flatten status records, retaining both sides of every rename/copy."""
    paths: list[str] = []
    seen: set[str] = set()
    for record in records:
        for key in ("origin", "path"):
            value = record.get(key)
            if isinstance(value, str) and value and value not in seen:
                seen.add(value)
                paths.append(value)
    return paths
