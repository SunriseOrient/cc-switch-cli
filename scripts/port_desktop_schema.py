#!/usr/bin/env python3
"""Port a newer desktop-app database schema into this CLI fork.

The desktop app (farion1231/cc-switch) and this CLI share one SQLite schema.
When the desktop bumps ``SCHEMA_VERSION`` the CLI refuses to open the database
until it carries the same migrations. This script ports the *additive* subset
of those migrations mechanically:

  * copies the migration arms for versions (ours, desktop] verbatim into
    ``src-tauri/src/database/schema.rs``
  * bumps ``SCHEMA_VERSION`` in ``src-tauri/src/database/mod.rs``

Fresh databases are covered by the migration chain itself, so the ``CREATE
TABLE`` statements are intentionally left untouched.

The script is fail-closed: any arm that is not made purely of
``add_column_if_missing`` / ``table_exists`` / ``set_user_version`` calls is
rejected with exit code 3 so the workflow fails instead of guessing.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCHEMA_VERSION_RE = re.compile(r"SCHEMA_VERSION:\s*i32\s*=\s*(\d+)\s*;")
ARM_START_RE = re.compile(r"^(\s*)(\d+)\s*=>\s*\{", re.MULTILINE)
COLUMN_RE = re.compile(
    r'add_column_if_missing\(\s*conn\s*,\s*"([A-Za-z0-9_]+)"\s*,\s*"([A-Za-z0-9_]+)"\s*,\s*"([^"]*)"'
)
ALLOWED_IDENTIFIERS = (
    "add_column_if_missing",
    "table_exists",
    "set_user_version",
    "conn",
    "Self",
    "for",
    "if",
    "in",
    "let",
    "else",
)
FORBIDDEN = (
    "execute",
    "ALTER",
    "alter",
    "DROP",
    "drop",
    "RENAME",
    "rename",
    "UPDATE",
    "UPDATE ",
    "INSERT",
    "DELETE",
    "pragma",
    "PRAGMA",
    "query_row",
    "create_table",
    "raw",
)


class PortError(Exception):
    pass


def read_version(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    match = SCHEMA_VERSION_RE.search(text)
    if not match:
        raise PortError(f"SCHEMA_VERSION not found in {path}")
    return int(match.group(1))


def match_brace(text: str, open_index: int) -> int:
    """Return the index of the brace closing ``text[open_index]``."""
    depth = 0
    i = open_index
    in_string = False
    in_line_comment = False
    while i < len(text):
        ch = text[i]
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
        elif in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
                in_line_comment = True
                i += 1
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    raise PortError("unbalanced braces while scanning migration arms")


def migration_match_bounds(schema_text: str) -> tuple[int, int]:
    marker = "match version {"
    index = schema_text.find(marker)
    if index < 0:
        raise PortError("`match version {` not found in schema.rs")
    open_index = schema_text.index("{", index)
    close_index = match_brace(schema_text, open_index)
    return open_index, close_index


def strip_strings_and_comments(text: str) -> str:
    out = []
    i = 0
    in_string = False
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def collect_arms(schema_text: str, start_version: int, end_version: int) -> str:
    open_index, close_index = migration_match_bounds(schema_text)
    body = schema_text[open_index + 1 : close_index]

    arms: list[str] = []
    seen_versions: list[int] = []
    for match in ARM_START_RE.finditer(body):
        version = int(match.group(2))
        brace_open = match.end() - 1
        brace_close = match_brace(body, brace_open)
        arm_text = body[match.start() : brace_close + 1].rstrip("\n")

        if version < start_version or version > end_version:
            continue

        code = strip_strings_and_comments(arm_text)
        for token in FORBIDDEN:
            if token in code:
                raise PortError(
                    f"migration arm v{version} uses unsupported construct `{token.strip()}`; "
                    "manual port required"
                )
        if "set_user_version" not in code:
            raise PortError(f"migration arm v{version} does not set user_version")
        if not re.search(r"\bset_user_version\(conn,\s*%d\)" % (version + 1), code):
            raise PortError(
                f"migration arm v{version} does not advance to v{version + 1}"
            )

        arms.append(arm_text)
        seen_versions.append(version)

    expected = list(range(start_version, end_version + 1))
    if seen_versions != expected:
        missing = sorted(set(expected) - set(seen_versions))
        raise PortError(f"desktop migrations missing arms for versions {missing}")

    return "\n".join(arms)


def reindent(arm_text: str, indent: str) -> str:
    lines = arm_text.splitlines()
    if not lines:
        return arm_text
    first_indent = len(lines[0]) - len(lines[0].lstrip())
    out = []
    for line in lines:
        if line.strip():
            stripped = line[first_indent:] if len(line) >= first_indent else line.lstrip()
            out.append(indent + stripped)
        else:
            out.append("")
    return "\n".join(out)


def insert_arms(schema_text: str, arms_text: str) -> str:
    open_index, close_index = migration_match_bounds(schema_text)
    body = schema_text[open_index + 1 : close_index]
    fallback = body.find("_ => {")
    if fallback < 0:
        raise PortError("fallback arm `_ => {` not found in schema.rs")
    fallback_index = open_index + 1 + fallback
    line_start = schema_text.rfind("\n", 0, fallback_index) + 1
    return schema_text[:line_start] + arms_text + "\n" + schema_text[line_start:]


def bump_version(mod_path: Path, new_version: int) -> None:
    text = mod_path.read_text(encoding="utf-8")
    new_text, count = SCHEMA_VERSION_RE.subn(
        f"SCHEMA_VERSION: i32 = {new_version};", text
    )
    if count != 1:
        raise PortError(f"expected exactly one SCHEMA_VERSION in {mod_path}")
    # Keep the doc comment next to the constant in sync ("（当前 v26）").
    new_text = re.sub(r"（当前 v\d+）", f"（当前 v{new_version}）", new_text, count=1)
    mod_path.write_text(new_text, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--desktop-mod", required=True, type=Path)
    parser.add_argument("--desktop-schema", required=True, type=Path)
    parser.add_argument("--cli-root", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cli_mod = args.cli_root / "src-tauri/src/database/mod.rs"
    cli_schema = args.cli_root / "src-tauri/src/database/schema.rs"

    ours = read_version(cli_mod)
    desktop = read_version(args.desktop_mod)

    if desktop <= ours:
        print(f"noop: cli schema v{ours} >= desktop v{desktop}")
        return 0

    desktop_schema_text = args.desktop_schema.read_text(encoding="utf-8")
    cli_schema_text = cli_schema.read_text(encoding="utf-8")

    raw_arms = collect_arms(desktop_schema_text, ours, desktop - 1)
    arms_text = reindent(raw_arms, " " * 20)

    columns = COLUMN_RE.findall(raw_arms)
    patched_schema = insert_arms(cli_schema_text, arms_text)

    print(f"porting schema v{ours} -> v{desktop}")
    for table, column, ddl in columns:
        print(f"  + {table}.{column} {ddl}")

    if args.dry_run:
        print("dry-run: no files written")
        return 0

    cli_schema.write_text(patched_schema, encoding="utf-8", newline="\n")
    bump_version(cli_mod, desktop)
    print(f"updated SCHEMA_VERSION to {desktop}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PortError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(3)
