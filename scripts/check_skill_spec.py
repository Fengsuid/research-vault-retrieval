#!/usr/bin/env python3
"""Validate SKILL.md against the Agent Skills specification rules used in CI.

Checks:
  * YAML frontmatter exists and contains `name` and `description`;
  * `name` is 1-64 lowercase letters/digits/hyphens and matches the skill directory;
  * `description` is at most 1024 characters;
  * referenced package files exist.

Standard library only; read-only.
"""

from __future__ import annotations

import pathlib
import re
import sys


def main(root: pathlib.Path) -> int:
    root = root.resolve()
    skill = root / "SKILL.md"
    if not skill.is_file():
        print("SKILL.md: missing", file=sys.stderr)
        return 1
    text = skill.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    if not match:
        print("SKILL.md: missing YAML frontmatter", file=sys.stderr)
        return 1
    frontmatter = match.group(1)
    name = re.search(r"(?m)^name:\s*(.+)$", frontmatter)
    description = re.search(r"(?ms)^description:\s*(.+?)(?=\n[a-z_]+:|\Z)", frontmatter)
    if not name or not description:
        print("SKILL.md: name and description are required", file=sys.stderr)
        return 1
    value = name.group(1).strip().strip("\"'")
    if not re.fullmatch(r"[a-z0-9-]{1,64}", value):
        print("SKILL.md: invalid name %r" % value, file=sys.stderr)
        return 1
    if value != root.name:
        print("SKILL.md: name %r must match the directory %r" % (value, root.name), file=sys.stderr)
        return 1
    length = len(" ".join(description.group(1).strip().strip("\"'").split()))
    if length > 1024:
        print("SKILL.md: description is %d characters (max 1024)" % length, file=sys.stderr)
        return 1
    required = [
        "scripts/rv_retrieval.py",
        "references/retrieval-routing.md",
        "references/evidence-levels.md",
        "references/output-contract.md",
        "README.md",
        "LICENSE",
    ]
    missing = [item for item in required if not (root / item).is_file()]
    if missing:
        print("missing files: " + ", ".join(missing), file=sys.stderr)
        return 1
    print("OK: name=%s, description=%d chars, %d required files present"
          % (value, length, len(required)))
    return 0


if __name__ == "__main__":
    target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
    raise SystemExit(main(target))
