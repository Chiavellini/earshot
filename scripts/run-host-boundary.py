#!/usr/bin/env python3
"""Safely publish or reconcile one verified schema-3 RunPlan tree.

This is the filesystem half of the laptop/box authority handoff.  Transport
(rsync/ssh in production) must first place a checksum-stable snapshot at
``--incoming``.  This tool then verifies both trees through eventctl and does
exactly one of three things:

* atomically publishes an absent canonical destination;
* advances a destination whose receipt ledger is an exact byte prefix of the
  incoming authoritative extension; or
* accepts an incoming ledger that is an exact prefix of the destination (the
  normal repeated-pull case after local ``evidence_pulled``/``graded`` tails).

Neither a divergent receipt chain nor unexplained evidence bytes are merged.
All regular-file conflicts are checked before mutation.  During an advance,
evidence files are atomically published first and the exact receipt suffix is
appended last under ``flock`` + ``fsync``.  A crash can therefore leave extra
verified evidence beside the old valid ledger prefix, but can never advertise
receipts whose named evidence was only partially copied.

No network operation is implemented here.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


SCHEMA = 1
PLAN_SCHEMA = 3
RECEIPTS = "control/receipts.jsonl"
STATUS = "control/status.json"
LOCAL_ONLY = {"grade/verdict.json"}
APPEND_ONLY_EVIDENCE = {
    "capture/audio.raw",
    "capture/audio.frames.jsonl",
    "book/kalshi-tape.jsonl",
    "orders/order-evidence.jsonl",
    "orders/fix-observations.jsonl",
}


class BoundaryRefusal(Exception):
    pass


@dataclass(frozen=True)
class Inventory:
    files: dict[str, Path]
    dirs: set[str]


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def repo_root(raw: str | None) -> Path:
    root = Path(raw or os.environ.get("EARSHOT_REPO") or Path(__file__).resolve().parent.parent)
    root = root.resolve()
    if not root.is_dir() or root.is_symlink():
        raise BoundaryRefusal(f"repository root is not a real directory: {root}")
    return root


def ensure_within(path: Path, parent: Path, what: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(parent)
    except ValueError as exc:
        raise BoundaryRefusal(f"{what} escapes {parent}: {resolved}") from exc
    return resolved


def load_plan(run_dir: Path) -> dict:
    plan_path = run_dir / "control" / "run-plan.json"
    if not plan_path.is_file() or plan_path.is_symlink():
        raise BoundaryRefusal(f"missing regular RunPlan: {plan_path}")
    try:
        plan = json.loads(plan_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BoundaryRefusal(f"RunPlan is unreadable: {exc}") from exc
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA or plan.get("kind") != "run-plan":
        raise BoundaryRefusal("host boundary requires one schema-3 RunPlan")
    if run_dir.name != plan.get("run_id") or run_dir.parent.name != plan.get("event_id"):
        raise BoundaryRefusal("RunPlan identity does not match incoming directory names")
    return plan


def verify_eventctl(repo: Path, run_dir: Path) -> None:
    eventctl = repo / "scripts" / "eventctl.py"
    if not eventctl.is_file() or eventctl.is_symlink():
        raise BoundaryRefusal(f"eventctl is missing: {eventctl}")
    proc = subprocess.run(
        [sys.executable, str(eventctl), "verify", "--run-dir", str(run_dir)],
        cwd=repo,
        env={**os.environ, "EARSHOT_REPO": str(repo)},
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        detail = lines[-1] if lines else f"exit {proc.returncode}"
        raise BoundaryRefusal(f"eventctl refused {run_dir}: {detail}")


def inventory(root: Path) -> Inventory:
    if not root.is_dir() or root.is_symlink():
        raise BoundaryRefusal(f"run tree is not a real directory: {root}")
    files: dict[str, Path] = {}
    dirs: set[str] = set()
    for base, names, fnames in os.walk(root, topdown=True, followlinks=False):
        base_path = Path(base)
        rel_base = base_path.relative_to(root)
        names.sort()
        fnames.sort()
        for name in names:
            path = base_path / name
            rel = (rel_base / name).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise BoundaryRefusal(f"symlink in run tree: {path}")
            if not stat.S_ISDIR(mode):
                raise BoundaryRefusal(f"non-directory entry in run tree: {path}")
            dirs.add(rel)
        for name in fnames:
            path = base_path / name
            rel = (rel_base / name).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise BoundaryRefusal(f"symlink in run tree: {path}")
            if not stat.S_ISREG(mode):
                raise BoundaryRefusal(f"non-regular entry in run tree: {path}")
            files[rel] = path
    return Inventory(files, dirs)


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ledger_relation(incoming: bytes, destination: bytes) -> str:
    if incoming == destination:
        return "equal"
    if destination.startswith(incoming):
        return "incoming_prefix_destination"
    if incoming.startswith(destination):
        return "destination_prefix_incoming"
    return "divergent"


@dataclass
class ReconcilePlan:
    relation: str
    add: list[str]
    replace: list[str]
    receipt_suffix: bytes
    copy_status: bool


def plan_reconcile(incoming: Path, destination: Path) -> ReconcilePlan:
    src = inventory(incoming)
    dst = inventory(destination)
    if RECEIPTS not in src.files or RECEIPTS not in dst.files:
        raise BoundaryRefusal("both run trees must contain a receipts ledger")
    src_receipts = src.files[RECEIPTS].read_bytes()
    dst_receipts = dst.files[RECEIPTS].read_bytes()
    relation = ledger_relation(src_receipts, dst_receipts)
    if relation == "divergent":
        raise BoundaryRefusal("receipt ledgers diverge (neither is an exact byte prefix)")

    src_only = set(src.files) - set(dst.files)
    dst_only = set(dst.files) - set(src.files)
    unexplained_dst = dst_only - LOCAL_ONLY
    if unexplained_dst:
        raise BoundaryRefusal(
            f"destination has files absent from incoming: {sorted(unexplained_dst)}"
        )
    if relation == "incoming_prefix_destination" and src_only:
        raise BoundaryRefusal(
            "older incoming ledger carries files absent from its authoritative"
            f" destination extension: {sorted(src_only)}"
        )

    add: list[str] = []
    replace: list[str] = []
    for rel in sorted(set(src.files) & set(dst.files)):
        if rel in (RECEIPTS, STATUS):
            continue
        if file_digest(src.files[rel]) == file_digest(dst.files[rel]):
            continue
        if relation == "destination_prefix_incoming" and rel in APPEND_ONLY_EVIDENCE:
            old = dst.files[rel].read_bytes()
            new = src.files[rel].read_bytes()
            if new.startswith(old):
                replace.append(rel)
                continue
        raise BoundaryRefusal(f"file bytes differ: {rel}")

    if relation in ("equal", "destination_prefix_incoming"):
        add = sorted(src_only - {STATUS})
    elif src_only:
        raise BoundaryRefusal(f"source-only files are not safe for relation {relation}")

    extra_dirs = dst.dirs - src.dirs
    allowed_extra_dirs = {"grade"}
    if extra_dirs - allowed_extra_dirs:
        raise BoundaryRefusal(
            f"destination has directories absent from incoming: {sorted(extra_dirs)}"
        )

    suffix = b""
    if relation == "destination_prefix_incoming":
        suffix = src_receipts[len(dst_receipts):]
        if not suffix or not suffix.endswith(b"\n"):
            raise BoundaryRefusal("incoming receipt suffix is not complete canonical lines")
    return ReconcilePlan(
        relation=relation,
        add=add,
        replace=replace,
        receipt_suffix=suffix,
        copy_status=(relation != "incoming_prefix_destination" and STATUS in src.files),
    )


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mkdir_durable(path: Path) -> None:
    missing: list[Path] = []
    cur = path
    while not cur.exists():
        missing.append(cur)
        cur = cur.parent
    if not cur.is_dir() or cur.is_symlink():
        raise BoundaryRefusal(f"directory ancestor is unsafe: {cur}")
    for item in reversed(missing):
        item.mkdir()
        fsync_dir(item.parent)


def atomic_copy(source: Path, destination: Path, *, replace: bool) -> None:
    mkdir_durable(destination.parent)
    if not replace and (destination.exists() or destination.is_symlink()):
        raise BoundaryRefusal(f"destination appeared during publication: {destination}")
    tmp = destination.with_name(f".{destination.name}.boundary.{os.getpid()}.tmp")
    if tmp.exists() or tmp.is_symlink():
        raise BoundaryRefusal(f"stale boundary temp file exists: {tmp}")
    mode = stat.S_IMODE(source.stat().st_mode)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode or 0o600)
    try:
        with source.open("rb") as src:
            while True:
                block = src.read(1024 * 1024)
                if not block:
                    break
                view = memoryview(block)
                while view:
                    wrote = os.write(fd, view)
                    if wrote <= 0:
                        raise OSError("short write")
                    view = view[wrote:]
        os.fsync(fd)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(fd)
    if not replace and (destination.exists() or destination.is_symlink()):
        tmp.unlink(missing_ok=True)
        raise BoundaryRefusal(f"destination appeared during publication: {destination}")
    os.replace(tmp, destination)
    fsync_dir(destination.parent)


def append_receipt_suffix(destination: Path, expected_prefix: bytes, suffix: bytes) -> None:
    path = destination / RECEIPTS
    fd = os.open(path, os.O_RDWR | os.O_APPEND)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.lseek(fd, 0, os.SEEK_SET)
        current = b""
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            current += block
        if current != expected_prefix:
            raise BoundaryRefusal("destination ledger changed during reconciliation")
        view = memoryview(suffix)
        while view:
            wrote = os.write(fd, view)
            if wrote <= 0:
                raise OSError("short receipt append")
            view = view[wrote:]
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_tree(source: Path, destination: Path) -> None:
    src = inventory(source)
    mkdir_durable(destination)
    for rel in sorted(src.dirs, key=lambda value: (value.count("/"), value)):
        mkdir_durable(destination / rel)
    for rel in sorted(src.files):
        atomic_copy(src.files[rel], destination / rel, replace=False)


def publish_absent(repo: Path, incoming: Path, destination: Path) -> str:
    plan = load_plan(incoming)
    staging_root = repo / "var" / "host-boundary-staging"
    mkdir_durable(staging_root)
    work = staging_root / f"publish.{os.getpid()}" / plan["event_id"] / plan["run_id"]
    if work.exists() or work.is_symlink():
        raise BoundaryRefusal(f"stale publication path exists: {work}")
    try:
        copy_tree(incoming, work)
        verify_eventctl(repo, work)
        if destination.exists() or destination.is_symlink():
            raise BoundaryRefusal(f"canonical destination appeared during publication: {destination}")
        mkdir_durable(destination.parent)
        os.rename(work, destination)
        fsync_dir(destination.parent)
    finally:
        top = staging_root / f"publish.{os.getpid()}"
        if top.exists() and not top.is_symlink():
            shutil.rmtree(top)
    return "published"


def reconcile_existing(repo: Path, incoming: Path, destination: Path) -> tuple[str, str]:
    proposal = plan_reconcile(incoming, destination)
    if proposal.relation == "incoming_prefix_destination":
        return proposal.relation, "destination-already-extends-incoming"

    dst_prefix = (destination / RECEIPTS).read_bytes()
    for rel in proposal.add:
        atomic_copy(incoming / rel, destination / rel, replace=False)
    for rel in proposal.replace:
        atomic_copy(incoming / rel, destination / rel, replace=True)
    if proposal.receipt_suffix:
        append_receipt_suffix(destination, dst_prefix, proposal.receipt_suffix)
    if proposal.copy_status:
        atomic_copy(incoming / STATUS, destination / STATUS, replace=True)
    verify_eventctl(repo, destination)
    changed = bool(proposal.add or proposal.replace or proposal.receipt_suffix)
    return proposal.relation, "advanced" if changed else "already-equal"


def boundary_lock(repo: Path):
    root = repo / "var" / "host-boundary-staging"
    mkdir_durable(root)
    lock_path = root / ".boundary.lock"
    if lock_path.is_symlink():
        raise BoundaryRefusal(f"boundary lock is a symlink: {lock_path}")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def cmd_reconcile(args) -> int:
    repo = repo_root(args.repo)
    incoming = Path(args.incoming).resolve()
    inventory(incoming)
    verify_eventctl(repo, incoming)
    plan = load_plan(incoming)
    destination = Path(args.destination).resolve()
    expected = (repo / "var" / "runs" / plan["event_id"] / plan["run_id"]).resolve()
    if destination != expected:
        raise BoundaryRefusal(f"destination must be canonical {expected}, got {destination}")
    if plan.get("execution_host_id") != args.expected_host:
        raise BoundaryRefusal(
            f"RunPlan host {plan.get('execution_host_id')!r} does not match"
            f" --expected-host {args.expected_host!r}"
        )

    if args.check_only:
        if not destination.exists() and not destination.is_symlink():
            relation, action = "absent", "would-publish"
        else:
            if not destination.is_dir() or destination.is_symlink():
                raise BoundaryRefusal(f"canonical destination is unsafe: {destination}")
            verify_eventctl(repo, destination)
            destination_plan = load_plan(destination)
            if destination_plan.get("plan_sha256") != plan.get("plan_sha256"):
                raise BoundaryRefusal("incoming and destination RunPlans differ")
            proposal = plan_reconcile(incoming, destination)
            relation = proposal.relation
            if relation == "incoming_prefix_destination":
                action = "destination-already-extends-incoming"
            elif proposal.add or proposal.replace or proposal.receipt_suffix:
                action = "would-advance"
            else:
                action = "already-equal"
    else:
        lock_fd = boundary_lock(repo)
        try:
            if not destination.exists() and not destination.is_symlink():
                action = publish_absent(repo, incoming, destination)
                relation = "absent"
            else:
                if not destination.is_dir() or destination.is_symlink():
                    raise BoundaryRefusal(f"canonical destination is unsafe: {destination}")
                verify_eventctl(repo, destination)
                destination_plan = load_plan(destination)
                if destination_plan.get("plan_sha256") != plan.get("plan_sha256"):
                    raise BoundaryRefusal("incoming and destination RunPlans differ")
                relation, action = reconcile_existing(repo, incoming, destination)
            verify_eventctl(repo, destination)
        finally:
            os.close(lock_fd)

    result = {
        "schema": SCHEMA,
        "kind": "run-host-boundary-result",
        "event_id": plan["event_id"],
        "run_id": plan["run_id"],
        "plan_sha256": plan["plan_sha256"],
        "expected_host": args.expected_host,
        "relation": relation,
        "action": action,
        "check_only": args.check_only,
        "destination": str(destination),
    }
    print(canonical_json(result))
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("reconcile", help="publish or prefix-reconcile one staged run")
    sp.add_argument("--repo", default=None)
    sp.add_argument("--incoming", required=True)
    sp.add_argument("--destination", required=True)
    sp.add_argument("--expected-host", required=True)
    sp.add_argument("--check-only", action="store_true")
    sp.set_defaults(fn=cmd_reconcile)
    return ap


def main() -> int:
    try:
        args = parser().parse_args()
        return args.fn(args)
    except BoundaryRefusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"REFUSED: host-boundary I/O failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
