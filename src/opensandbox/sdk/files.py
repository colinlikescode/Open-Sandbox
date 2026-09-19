"""Bounded, portable transfer archives and Docker build contexts."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from pathspec.patterns.gitwildmatch import GitWildMatchPattern

from opensandbox.errors import ValidationError

MAX_TRANSFER = 512 * 1024 * 1024


class DockerIgnore:
    """Docker rules are rooted at the context and negations may restore children.

    Git's matching primitive handles escapes/globstars/character classes, but its
    default unanchored single-name rules and parent-directory pruning don't match
    Docker semantics. Anchor every rule and apply them in order to every path.
    """

    def __init__(self, content: str):
        self.rules = []
        for raw in content.splitlines():
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            excluded = not value.startswith("!")
            pattern = value if excluded else value[1:]
            pattern = pattern.removeprefix("./").strip("/")
            if pattern in {"", "."}:
                continue
            self.rules.append((GitWildMatchPattern("/" + pattern), excluded))

    def ignores(self, path: str):
        excluded = False
        for rule, value in self.rules:
            if rule.match_file(path) is not None:
                excluded = value
        return excluded


def pack(path: Path, *, context=False, limit=MAX_TRANSFER) -> tuple[bytes, str | None]:
    path = path.expanduser()
    if path.is_symlink():
        raise ValidationError("Transfers do not follow symlinks")
    path = path.resolve()
    if not path.exists():
        raise ValidationError(f"Local path does not exist: {path}")
    dockerfile = path.name if context and path.is_file() else "Dockerfile" if context else None
    root = path.parent if path.is_file() else path
    if context and (dockerfile is None or not (root / dockerfile).is_file()):
        raise ValidationError("Project context must contain a Dockerfile")
    ignore = DockerIgnore("")
    if context:
        ignore_path = root / (str(dockerfile) + ".dockerignore")
        if not ignore_path.exists():
            ignore_path = root / ".dockerignore"
        if ignore_path.exists():
            ignore = DockerIgnore(ignore_path.read_text())
    buffer = io.BytesIO()
    total = 0
    entries = list(root.rglob("*")) if context or path.is_dir() else [path]
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for item in sorted(entries):
            relative = item.relative_to(root).as_posix()
            # Credentials and repository/tool caches aren't part of a useful build context.
            if context and any(
                part in {".git", ".venv", ".cache", "node_modules"}
                for part in item.relative_to(root).parts
            ):
                continue
            if context and relative != dockerfile and ignore.ignores(relative):
                continue
            if item.is_symlink():
                raise ValidationError(f"Transfers do not follow symlinks: {relative}")
            if not item.is_dir() and not item.is_file():
                raise ValidationError(f"Cannot transfer special file: {relative}")
            total += item.stat().st_size if item.is_file() else 0
            if total > limit:
                raise ValidationError("Transfer exceeds 512 MiB")
            name = (
                relative
                if context
                else str(Path(path.name) / relative)
                if path.is_dir()
                else path.name
            )
            archive.add(item, arcname=name, recursive=False, filter=_clean_header)
    if len(buffer.getbuffer()) > limit:
        raise ValidationError("Transfer archive exceeds 512 MiB")
    return buffer.getvalue(), dockerfile


def _clean_header(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode &= 0o777
    return info


def unpack(data: bytes, destination: Path, *, limit=MAX_TRANSFER):
    if len(data) > limit:
        raise ValidationError("Download exceeds transfer limit")
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = archive.getmembers()
        if len(members) > 100000:
            raise ValidationError("Too many archive entries")
        for member in members:
            if not member.isfile() and not member.isdir():
                raise ValidationError("Archive contains a link or special file")
            total += member.size
            if total > limit:
                raise ValidationError("Expanded archive exceeds transfer limit")
            # Explicitly check existing symlinks as well as lexical traversal.
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination.resolve()) or Path(member.name).is_absolute():
                raise ValidationError("Archive escapes download destination")
        archive.extractall(destination, members=members, filter="data")
