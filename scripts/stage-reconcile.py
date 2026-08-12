#!/usr/bin/env python3
"""Reconcile incomplete staged inputs without crossing a RunPlan boundary.

This helper belongs to ``control.prepare``.  It performs two deliberately
small operations used by ``slate-stage.sh``:

* replace exactly one ``starts_at = ... # TODO`` line with a verified UTC
  call time; and
* classify a generated event env as ready, missing, or conflicting.

Before either operation can change staging inputs, every published schema-3
RunPlan under the configured run root is inspected.  A config referenced by a
RunPlan is immutable: its bytes must still match ``config_sha256`` and neither
the config nor its event-env association may be repaired in place.  The
RunPlan is never opened for writing.

Exit codes: 0 ready/reconciled, 1 refusal, 2 usage, 3 env is safely missing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
START_LINE_RE = re.compile(r'^(\s*starts_at\s*=\s*)"[^"]*".*$', re.M)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ENV_KEYS = (
    "CONFIG_PATH",
    "EVENT_DATE",
    "END_HHMM_LIVE",
    "COMPLETE_EARLIEST_HHMM",
    "BIND_PORT",
    "FIRE_HHMM",
)


class Refusal(Exception):
    pass


@dataclass(frozen=True)
class PlanRef:
    path: Path
    config_sha256: str
    event_env_path: str | None
    event_env_sha256: str | None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def strict_utc(value: str) -> str:
    if not UTC_RE.fullmatch(value):
        raise Refusal(f"call time {value!r} is not YYYY-MM-DDTHH:MM:SSZ")
    # Lexical validation above catches the contract shape; datetime validates
    # calendar and clock ranges without accepting offsets or naive values.
    import datetime as dt

    try:
        dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise Refusal(f"call time {value!r} is invalid: {exc}") from exc
    return value


def repo_relative(repo: Path, raw: str, what: str) -> tuple[Path, str]:
    repo = repo.resolve()
    path = Path(raw)
    absolute = path.resolve() if path.is_absolute() else (repo / path).resolve()
    try:
        relative = absolute.relative_to(repo).as_posix()
    except ValueError as exc:
        raise Refusal(f"{what} {raw!r} resolves outside repository {repo}") from exc
    if ".." in PurePosixPath(relative).parts:
        raise Refusal(f"unsafe {what} path {raw!r}")
    return absolute, relative


def load_plan_refs(repo: Path, run_root: Path, config_rel: str) -> list[PlanRef]:
    """Find immutable plans that pin config_rel; malformed plans fail closed."""
    if not run_root.exists():
        return []
    refs: list[PlanRef] = []
    for plan_path in sorted(run_root.glob("*/*/control/run-plan.json")):
        if plan_path.is_symlink():
            raise Refusal(
                f"cannot prove staging inputs mutable: RunPlan is a symlink {plan_path}"
            )
        try:
            plan = json.loads(plan_path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Refusal(
                f"cannot prove staging inputs mutable: unreadable RunPlan {plan_path}: {exc}"
            ) from exc
        if not isinstance(plan, dict):
            raise Refusal(f"cannot prove staging inputs mutable: {plan_path} is not an object")
        pinned = plan.get("config_path")
        if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
            # A legacy attempt for another config must not freeze the entire
            # staging universe. It is safe to ignore only when its own
            # recorded config_path proves it cannot own this target.
            if isinstance(pinned, str) and pinned != config_rel:
                continue
            raise Refusal(
                f"cannot prove staging inputs mutable: unsupported RunPlan "
                f"schema/kind in {plan_path}"
            )
        if not isinstance(pinned, str):
            raise Refusal(f"cannot prove staging inputs mutable: {plan_path} has no config_path")
        if pinned != config_rel:
            continue
        cfg_sha = plan.get("config_sha256")
        env_rel = plan.get("event_env_path")
        env_sha = plan.get("event_env_sha256")
        if not isinstance(cfg_sha, str) or not SHA256_RE.fullmatch(cfg_sha):
            raise Refusal(f"RunPlan {plan_path} has invalid config_sha256")
        if env_rel is not None and not isinstance(env_rel, str):
            raise Refusal(f"RunPlan {plan_path} has invalid event_env_path")
        if env_sha is not None and (
            not isinstance(env_sha, str) or not SHA256_RE.fullmatch(env_sha)
        ):
            raise Refusal(f"RunPlan {plan_path} has invalid event_env_sha256")
        refs.append(PlanRef(plan_path, cfg_sha, env_rel, env_sha))
    return refs


def validate_pinned_config(config: Path, refs: list[PlanRef]) -> None:
    if not refs:
        return
    if not config.is_file():
        raise Refusal(f"immutable config disappeared after RunPlan publication: {config}")
    actual = sha256(config)
    for ref in refs:
        if actual != ref.config_sha256:
            raise Refusal(
                f"immutable config drift: {config} no longer matches {ref.path}"
            )


def config_state(config: Path, expected_start: str) -> tuple[str, str]:
    if not config.is_file():
        return "missing", "config does not exist"
    text = config.read_text(errors="strict")
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Refusal(f"staged config {config} is invalid TOML: {exc}") from exc
    event = parsed.get("event")
    if not isinstance(event, dict) or not isinstance(event.get("starts_at"), str):
        raise Refusal(f"staged config {config} has no string [event].starts_at")
    lines = [line for line in text.splitlines() if re.match(r"^\s*starts_at\s*=", line)]
    if len(lines) != 1:
        raise Refusal(f"staged config {config} has {len(lines)} starts_at lines; expected one")
    current = event["starts_at"]
    has_todo = "TODO" in lines[0]
    if current == expected_start and not has_todo:
        return "ready", current
    if has_todo:
        return "incomplete", current
    return "conflict", current


def atomic_replace(path: Path, data: bytes) -> None:
    mode = path.stat().st_mode & 0o777
    fd, tmp_raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_raw)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def reconcile_config(
    repo: Path,
    run_root: Path,
    raw_config: str,
    expected: str,
    check_only: bool = False,
) -> str:
    expected = strict_utc(expected)
    config, config_rel = repo_relative(repo, raw_config, "config")
    refs = load_plan_refs(repo, run_root, config_rel)
    validate_pinned_config(config, refs)
    state, current = config_state(config, expected)
    if state == "missing":
        raise Refusal(f"cannot reconcile missing config {config_rel}")
    if state == "ready":
        return "PINNED-READY" if refs else "READY"
    if state == "conflict":
        raise Refusal(
            f"staged config {config_rel} has starts_at={current}, expected {expected}; "
            "only an explicit TODO is repairable"
        )
    if refs:
        names = ", ".join(str(ref.path) for ref in refs)
        raise Refusal(
            f"IMMUTABLE-INCOMPLETE: {config_rel} still has a TODO but is pinned by {names}; "
            "publish a deliberate new attempt instead of rewriting it"
        )
    if check_only:
        return "RECONCILABLE"
    text = config.read_text(errors="strict")
    replacement = START_LINE_RE.sub(lambda m: f'{m.group(1)}"{expected}"', text, count=1)
    if replacement == text:
        raise Refusal(f"could not replace TODO starts_at in {config_rel}")
    try:
        check = tomllib.loads(replacement)
    except tomllib.TOMLDecodeError as exc:
        raise Refusal(f"reconciled config would be invalid TOML: {exc}") from exc
    if check.get("event", {}).get("starts_at") != expected or "TODO" in next(
        line for line in replacement.splitlines() if re.match(r"^\s*starts_at\s*=", line)
    ):
        raise Refusal(f"reconciled config failed starts_at validation: {config_rel}")
    atomic_replace(config, replacement.encode())
    return "RECONCILED"


def guard_mutable(repo: Path, run_root: Path, raw_config: str) -> str:
    config, config_rel = repo_relative(repo, raw_config, "config")
    refs = load_plan_refs(repo, run_root, config_rel)
    validate_pinned_config(config, refs)
    if refs:
        names = ", ".join(str(ref.path) for ref in refs)
        raise Refusal(
            f"immutable staging input {config_rel} is pinned by {names}; "
            "a new attempt must use a new config path"
        )
    return "MUTABLE"


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(errors="strict").splitlines():
        match = re.match(r'^([A-Z][A-Z0-9_]*)=(?:"([^"]*)"|\'([^\']*)\'|([^\s#]+))', line)
        if not match:
            continue
        values[match.group(1)] = next(v for v in match.groups()[1:] if v is not None)
    return values


def check_env(
    repo: Path,
    run_root: Path,
    raw_config: str,
    raw_env: str,
    expected: dict[str, str],
) -> tuple[int, str]:
    config, config_rel = repo_relative(repo, raw_config, "config")
    env, env_rel = repo_relative(repo, raw_env, "event env")
    refs = load_plan_refs(repo, run_root, config_rel)
    validate_pinned_config(config, refs)
    if not env.exists():
        if refs:
            names = ", ".join(str(ref.path) for ref in refs)
            raise Refusal(
                f"IMMUTABLE-INCOMPLETE: {env_rel} is missing after {config_rel} was pinned by "
                f"{names}; publish a deliberate new attempt instead of retrofitting the plan"
            )
        return 3, "MISSING"
    if not env.is_file():
        raise Refusal(f"event env path is not a regular file: {env_rel}")
    values = parse_env(env)
    mismatches = [
        f"{key}={values.get(key)!r} (expected {want!r})"
        for key, want in expected.items()
        if values.get(key) != want
    ]
    if mismatches:
        raise Refusal(
            f"staged env {env_rel} is conflicting/incomplete: " + "; ".join(mismatches)
        )
    if refs:
        actual = sha256(env)
        for ref in refs:
            if ref.event_env_path != env_rel:
                raise Refusal(
                    f"immutable RunPlan {ref.path} pins event_env_path={ref.event_env_path!r}, "
                    f"not {env_rel!r}"
                )
            if ref.event_env_sha256 != actual:
                raise Refusal(f"immutable event env drift: {env_rel} no longer matches {ref.path}")
        return 0, "PINNED-READY"
    return 0, "READY"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stage-reconcile.py")
    p.add_argument("--repo", default=None)
    p.add_argument("--run-root", default=None)
    sub = p.add_subparsers(dest="command", required=True)

    cfg = sub.add_parser("config", help="repair one unpinned TODO starts_at")
    cfg.add_argument("--config", required=True)
    cfg.add_argument("--starts-at", required=True)
    cfg.add_argument("--check-only", action="store_true")

    guard = sub.add_parser("guard", help="prove a staging config path is not pinned")
    guard.add_argument("--config", required=True)

    env = sub.add_parser("env", help="classify one generated event env")
    env.add_argument("--config", required=True)
    env.add_argument("--env", required=True)
    env.add_argument("--event-date", required=True)
    env.add_argument("--fire-hhmm", required=True)
    env.add_argument("--end-hhmm", required=True)
    env.add_argument("--complete-hhmm", required=True)
    env.add_argument("--bind-port", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repo = Path(args.repo or os.environ.get("EARSHOT_REPO") or Path(__file__).resolve().parent.parent).resolve()
    run_root = Path(args.run_root).resolve() if args.run_root else repo / "var" / "runs"
    try:
        if args.command == "config":
            print(
                reconcile_config(
                    repo, run_root, args.config, args.starts_at, args.check_only
                )
            )
            return 0
        if args.command == "guard":
            print(guard_mutable(repo, run_root, args.config))
            return 0
        expected = {
            "CONFIG_PATH": repo_relative(repo, args.config, "config")[1],
            "EVENT_DATE": args.event_date,
            "END_HHMM_LIVE": args.end_hhmm,
            "COMPLETE_EARLIEST_HHMM": args.complete_hhmm,
            "BIND_PORT": args.bind_port,
            "FIRE_HHMM": args.fire_hhmm,
        }
        code, result = check_env(repo, run_root, args.config, args.env, expected)
        print(result)
        return code
    except (OSError, UnicodeDecodeError, Refusal) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
