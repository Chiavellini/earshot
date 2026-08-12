#!/usr/bin/env python3
"""Validate and render EARSHOT's deliberately small operations surface."""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
import tomllib
from pathlib import Path, PurePosixPath


SCHEMA = 2
SECTIONS = (
    "operator_command",
    "internal_adapter",
    "retired_entrypoint",
    "historical_script",
)
DATED = re.compile(
    r"\d{2}(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\d{2}", re.I
)
TRADE_CAPABLE = re.compile(
    r"(?:^|[;&|()]|\bexec\s+|\bsetsid\s+|\bnohup\s+|\btimeout\s+[^\n]*\s+)"
    r"(?:[\"']?\$[^\s]+[\"']?/)?(?:scripts/)?(?:launch-event|launch-webcast|"
    r"event-autorun|webcast-autorun|arm-on-url|run-arm|arm-when-url|"
    r"twilio-dial|twilio-operator-dial)\.sh(?:\s|$)|"
    r"\bearshot\s+arm\b|"
    r"\b(?:systemctl|launchctl)\b[^\n]*\b(?:enable|start|bootstrap|load)\b"
)
ALLOWED_HOSTS = {"laptop", "box", "either"}


def normalize_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("path must be a non-empty string")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or str(p) != value:
        raise ValueError(f"path is not canonical repository-relative: {value!r}")
    return value


def executable_lines(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    docstring_lines: set[int] = set()
    if path.suffix == ".py":
        try:
            tree = ast.parse(text)
            for node in ast.walk(tree):
                body = getattr(node, "body", None)
                if (
                    isinstance(body, list)
                    and body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstring_lines.update(
                        range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1)
                    )
        except SyntaxError:
            pass
    lines: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or lineno in docstring_lines:
            continue
        lines.append(line)
    return "\n".join(lines)


def validate(repo: Path, manifest_path: Path) -> tuple[dict, list[str]]:
    problems: list[str] = []
    try:
        data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, [f"cannot parse {manifest_path}: {exc}"]

    if set(data) != {"schema_version", "authority", *SECTIONS}:
        problems.append("manifest top-level keys must be exact")
    if data.get("schema_version") != SCHEMA:
        problems.append(f"schema_version must be {SCHEMA}")
    if data.get("authority") != "canonical-event-operations":
        problems.append("authority must be canonical-event-operations")

    classified: dict[str, str] = {}
    names: set[str] = set()
    expected_fields = {
        "operator_command": {"name", "argv", "arguments", "host", "purpose"},
        "internal_adapter": {"path", "owner"},
        "retired_entrypoint": {"path", "replacement", "reason"},
        "historical_script": {"path", "reason"},
    }
    for section in SECTIONS:
        rows = data.get(section, [])
        if not isinstance(rows, list):
            problems.append(f"{section} must be an array of tables")
            continue
        for index, row in enumerate(rows):
            where = f"{section}[{index}]"
            if not isinstance(row, dict) or set(row) != expected_fields[section]:
                problems.append(f"{where} fields must be {sorted(expected_fields[section])}")
                continue
            if section == "operator_command":
                argv = row["argv"]
                if (
                    not isinstance(argv, list)
                    or len(argv) != 2
                    or argv[0] != "scripts/opsctl.sh"
                    or not all(isinstance(value, str) and value for value in argv)
                ):
                    problems.append(
                        f"{where}.argv must be exactly "
                        "['scripts/opsctl.sh', '<command>']"
                    )
                    continue
                rel = argv[0]
                if row["name"] != argv[1]:
                    problems.append(f"{where}.name must equal argv command")
                if not isinstance(row["arguments"], str):
                    problems.append(f"{where}.arguments must be a string")
            else:
                try:
                    rel = normalize_path(row["path"])
                except ValueError as exc:
                    problems.append(f"{where}: {exc}")
                    continue
            try:
                rel = normalize_path(rel)
            except ValueError as exc:
                problems.append(f"{where}: {exc}")
                continue
            if section != "operator_command" and rel in classified:
                problems.append(f"{rel} classified twice ({classified[rel]}, {section})")
            if section != "operator_command":
                classified[rel] = section
            path = repo / rel
            if not path.is_file() or path.is_symlink():
                problems.append(f"{rel} must exist as a regular, non-symlink file")
            elif section != "historical_script" and not os.access(path, os.X_OK):
                problems.append(f"{rel} must be executable")
            if section == "operator_command":
                name = row["name"]
                if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", name):
                    problems.append(f"{where}.name is invalid")
                elif name in names:
                    problems.append(f"duplicate operator command {name}")
                names.add(name)
                if row["host"] not in ALLOWED_HOSTS:
                    problems.append(f"{where}.host must be one of {sorted(ALLOWED_HOSTS)}")
                if DATED.search(PurePosixPath(rel).name):
                    problems.append(f"operator command may not be date-specific: {rel}")
            if section == "retired_entrypoint" and path.is_file():
                full_body = path.read_text(encoding="utf-8", errors="replace")
                body = executable_lines(path)
                if "EARSHOT_RETIRED_ENTRYPOINT" not in full_body:
                    problems.append(f"retired entrypoint is not fail-closed: {rel}")
                if TRADE_CAPABLE.search(body):
                    problems.append(f"retired entrypoint still contains an executable live path: {rel}")

    scripts = repo / "scripts"
    for path in sorted(scripts.iterdir()):
        if not path.is_file():
            continue
        rel = path.relative_to(repo).as_posix()
        if rel == "scripts/ops-surface-check.py":
            continue
        body = executable_lines(path)
        if DATED.search(path.name) and rel not in classified:
            problems.append(f"dated top-level script is not classified historical: {rel}")
        if TRADE_CAPABLE.search(body) and rel not in classified:
            problems.append(f"trade/schedule-capable script is outside the operations surface: {rel}")

    return data, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="architecture/operations.toml")
    parser.add_argument("--repo", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parent.parent
    manifest = Path(args.manifest)
    if not manifest.is_absolute():
        manifest = repo / manifest
    data, problems = validate(repo, manifest)
    if problems:
        for problem in sorted(set(problems)):
            print(f"ops-surface: ERROR: {problem}", file=sys.stderr)
        print(f"ops-surface: FAILED ({len(set(problems))} error(s))", file=sys.stderr)
        return 1
    if args.list:
        print("Canonical operator commands:")
        for row in data["operator_command"]:
            invocation = " ".join(row["argv"] + ([row["arguments"]] if row["arguments"] else []))
            print(f"  {invocation:<76} [{row['host']}]  {row['purpose']}")
        print("\nEverything else is an internal adapter, retired entrypoint, or historical evidence.")
    else:
        print(
            "ops-surface: PASS "
            f"({len(data['operator_command'])} operator, "
            f"{len(data['internal_adapter'])} internal, "
            f"{len(data['retired_entrypoint'])} retired, "
            f"{len(data['historical_script'])} historical)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
