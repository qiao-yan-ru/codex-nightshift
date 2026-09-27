#!/usr/bin/env python3
"""为当前 Git worktree 的 HEAD、已跟踪 diff 和未跟踪文件生成稳定指纹。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def fingerprint(root: Path) -> dict[str, str]:
    head = git(root, "rev-parse", "HEAD").decode().strip()
    status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    diff = git(root, "diff", "--binary", "HEAD")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z")
    untracked_digest = hashlib.sha256()
    for raw_path in sorted(item for item in untracked.split(b"\0") if item):
        path = root / raw_path.decode("utf-8", "surrogateescape")
        untracked_digest.update(len(raw_path).to_bytes(8, "big"))
        untracked_digest.update(raw_path)
        if path.is_symlink():
            data = os.readlink(path).encode("utf-8", "surrogateescape")
            untracked_digest.update(b"<symlink>")
            untracked_digest.update(len(data).to_bytes(8, "big"))
            untracked_digest.update(data)
        elif path.is_file():
            data = path.read_bytes()
            untracked_digest.update(len(data).to_bytes(8, "big"))
            untracked_digest.update(data)
        else:
            untracked_digest.update(b"<not-a-regular-file>")

    parts = {
        "head": head,
        "status_sha256": hashlib.sha256(status).hexdigest(),
        "diff_sha256": hashlib.sha256(diff).hexdigest(),
        "untracked_sha256": untracked_digest.hexdigest(),
    }
    combined = hashlib.sha256()
    for key in ("head", "status_sha256", "diff_sha256", "untracked_sha256"):
        combined.update(key.encode())
        combined.update(b"\0")
        combined.update(parts[key].encode())
        combined.update(b"\0")
    parts["fingerprint"] = combined.hexdigest()
    return parts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worktree")
    parser.add_argument("output")
    args = parser.parse_args()
    root = Path(args.worktree).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(fingerprint(root), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
