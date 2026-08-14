#!/usr/bin/env python3
"""Bind and validate an immutable, non-secret webcast AccessPlan.

This tool proves the *strategy* that will acquire media for one schema-3
RunPlan.  It deliberately does not resolve a URL, fetch a manifest, or claim
that current PCM exists.  That later, host-local fact belongs to the T0
``capture_ready`` transition.

``bind`` is the canonical schema-2 path.  It requires an explicit public,
registered-session, or signed-in-session prerequisite; registration and login
are never inferred or performed here.  ``create`` retains schema 1 only as a
compatibility constructor for already-integrated callers and is deliberately
rejected by the date-level canonical acquisition gate.

The only write is ``<run>/control/access-plan.json``. Publication is
crash-durable (fsync file, atomic replace, fsync directory), and an existing
different plan is a collision rather than an overwrite.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path


LEGACY_SCHEMA = 1
SCHEMA = 2
PLAN_SCHEMA = 3
HEX64 = re.compile(r"^[0-9a-f]{64}$")
VANTAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
ATTESTER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
SESSION_REF_RE = re.compile(
    r"^(?:browser-profile|external-session):[A-Za-z0-9][A-Za-z0-9._:/-]{0,111}$"
)
URL_FILE_RE = re.compile(
    r"(?:(?:\$HOME|\$\{HOME\}|~)/[A-Za-z0-9._/-]*[A-Za-z0-9._-])"
)
RESOLVER_KINDS = {
    "q4-api": "q4-resolve.sh",
    "youtube-live": "youtube-resolve.sh",
    "browser-network": "browser-resolve.py",
    "choruscall": "choruscall-resolve.py",
    "external-command": None,
    "preresolved": "urlfile-wait.sh",
    "static-public": None,
}
# A kind that MINTS a URL. `preresolved` consumes one, and `static-public` has
# no resolver at all, so neither can stand in as the other-host resolve leg.
PRERESOLVE_KINDS = sorted(
    k for k in RESOLVER_KINDS if k not in {"preresolved", "static-public"}
)
# The preresolved freshness bound is DERIVED, not guessed: the webcast fire
# window is T-75 (the scheduler lead, `schedule-proof.py`) through the resolver
# deadline (event start + 15 min), i.e. 90 minutes. It is a sanity bound only —
# the predicate that actually refuses a prior attempt's file is attempt
# identity (`--expect-plan-sha256`), because every attempt of one event reuses
# the same `$HOME/<slug>-stream.url` path.
PRERESOLVED_FRESH_SECS = 5400
EXPIRY_MODES = {"refresh-at-launch", "stable-public", "resolver-declared"}
ACCESS_MODES = {"public", "registered-session", "signed-in-session"}
SENSITIVE_NAME_RE = re.compile(
    r"(?:token|secret|password|passcode|cookie|authorization|api[-_]?key)", re.I
)


class Refusal(Exception):
    pass


def repo_root() -> Path:
    return Path(os.environ.get("EARSHOT_REPO") or Path(__file__).resolve().parent.parent).resolve()


def canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def object_hash(value, omit: str) -> str:
    return hashlib.sha256(canonical_bytes({k: v for k, v in value.items() if k != omit})).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(raw: str, name: str) -> dt.datetime:
    if not isinstance(raw, str) or not raw:
        raise Refusal(f"{name} must be a UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Refusal(f"{name} is not parseable: {raw!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise Refusal(f"{name} must carry UTC offset Z/+00:00")
    return parsed


def normalize_url_file(raw: str) -> str:
    if raw.startswith("${HOME}/"):
        raw = "$HOME/" + raw[len("${HOME}/"):]
    elif raw.startswith("~/"):
        raw = "$HOME/" + raw[2:]
    if not raw.startswith("$HOME/"):
        raise Refusal(f"URL file must use a portable $HOME/... path, got {raw!r}")
    rel = raw[len("$HOME/"):]
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        raise Refusal(f"unsafe URL file path {raw!r}")
    return "$HOME/" + rel


def resolve_repo_file(repo: Path, raw: str, name: str) -> tuple[Path, str]:
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    path = path.resolve()
    try:
        rel = path.relative_to(repo).as_posix()
    except ValueError as exc:
        raise Refusal(f"{name} must be inside the repository: {path}") from exc
    if not path.is_file() or path.is_symlink():
        raise Refusal(f"{name} is not a regular non-symlink file: {path}")
    return path, rel


def run_eventctl(repo: Path, *args: str) -> str:
    cmd = [sys.executable, str(repo / "scripts" / "eventctl.py"), *args]
    proc = subprocess.run(cmd, cwd=repo, text=True, capture_output=True, timeout=30,
                          env={**os.environ, "EARSHOT_REPO": str(repo)})
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise Refusal(f"eventctl {' '.join(args[:1])} refused: {detail[-1] if detail else proc.returncode}")
    return proc.stdout


def load_run(repo: Path, raw_run_dir: str) -> tuple[Path, dict]:
    run_dir = Path(raw_run_dir)
    if not run_dir.is_absolute():
        run_dir = repo / run_dir
    run_dir = run_dir.resolve()
    try:
        run_dir.relative_to(repo)
    except ValueError as exc:
        raise Refusal(f"run directory must be inside repository: {run_dir}") from exc
    run_eventctl(repo, "verify", "--run-dir", str(run_dir))
    status = json.loads(run_eventctl(repo, "status", "--run-dir", str(run_dir), "--json"))
    if not status.get("state", {}).get("active"):
        raise Refusal("RunPlan is not active")
    plan_path = run_dir / "control" / "run-plan.json"
    if not plan_path.is_file() or plan_path.is_symlink():
        raise Refusal(f"missing regular RunPlan: {plan_path}")
    try:
        plan = json.loads(plan_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise Refusal(f"RunPlan unreadable: {exc}") from exc
    if plan.get("schema") != PLAN_SCHEMA or plan.get("kind") != "run-plan":
        raise Refusal("AccessPlan requires one schema-3 RunPlan")
    if plan.get("lane") != "webcast":
        raise Refusal(f"AccessPlan is webcast-only; RunPlan lane is {plan.get('lane')!r}")
    if plan.get("plan_sha256") != object_hash(plan, "plan_sha256"):
        raise Refusal("RunPlan self-hash mismatch")
    if status.get("event") != plan.get("event_id") or status.get("run") != plan.get("run_id"):
        raise Refusal("RunPlan/status identity mismatch")
    return run_dir, plan


def config_sources(repo: Path, plan: dict) -> tuple[Path, list[dict], list[str]]:
    cfg, rel = resolve_repo_file(repo, plan["config_path"], "pinned config")
    if rel != plan["config_path"] or file_hash(cfg) != plan["config_sha256"]:
        raise Refusal("pinned config path/hash no longer matches the RunPlan")
    try:
        data = tomllib.loads(cfg.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise Refusal(f"config TOML is invalid: {exc}") from exc
    raw_sources = data.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise Refusal("webcast config must contain at least one [[sources]] entry")
    sources = []
    refs = set()
    for index, raw in enumerate(raw_sources):
        if not isinstance(raw, dict) or raw.get("kind") != "pipe":
            raise Refusal(f"webcast source {index} must be kind='pipe'")
        argv = raw.get("cmd")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise Refusal(f"webcast source {index} cmd must be a non-empty argv array")
        label = raw.get("label")
        if not isinstance(label, str) or not label:
            label = f"source-{index}"
        for token in argv:
            for match in URL_FILE_RE.findall(token):
                refs.add(normalize_url_file(match))
        sources.append({"index": index, "kind": "pipe", "label": label, "argv": argv})
    return cfg, sources, sorted(refs)


def parse_resolver_argv(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal(f"--resolver-argv-json is invalid JSON: {exc}") from exc
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise Refusal("--resolver-argv-json must be a non-empty-string JSON array")
    return value


def validate_argv_non_secret(argv: list[str], what: str):
    for index, arg in enumerate(argv):
        if re.search(r"://[^/@\s:]+:[^/@\s]+@", arg):
            raise Refusal(f"{what} embeds URL user-info credentials")
        name, sep, inline = arg.partition("=")
        if SENSITIVE_NAME_RE.search(name):
            value = inline if sep else (argv[index + 1] if index + 1 < len(argv) else "")
            if value and not value.startswith(("$", "${")):
                raise Refusal(
                    f"{what} embeds material for sensitive option {name!r};"
                    " use an environment/external secret reference"
                )


def argv_option(argv: list[str], flag: str) -> str | None:
    """Value following `flag`, "" when the flag is last, None when absent."""
    for index, arg in enumerate(argv):
        if arg == flag:
            return argv[index + 1] if index + 1 < len(argv) else ""
    return None


def argv_url_files(argv: list[str], what: str) -> list[str]:
    """URL files an argv names, refusing every spelling the runtime cannot expand.

    `arm-on-url.sh` expands a token only when it literally starts with `$HOME/`;
    `${HOME}/` and `~/` survive into the executed argv verbatim and make the
    resolver address `./${HOME}/...` under the repo. `normalize_url_file` folds
    all three spellings, so equality alone would accept a token that cannot work.
    """
    found = []
    for arg in argv:
        for match in URL_FILE_RE.findall(arg):
            if not match.startswith("$HOME/"):
                raise Refusal(
                    f"{what} must spell URL files as $HOME/... — the runtime"
                    f" expands no other form; got {match!r}"
                )
            found.append(normalize_url_file(match))
    return sorted(set(found))


def validate_preresolved_argv(
    argv: list[str],
    refs: list[str],
    deadline_utc: str | None,
    run_plan_sha256: str | None,
):
    waited = argv_url_files(argv, "preresolved resolver argv")
    if waited != list(refs):
        raise Refusal(
            "preresolved resolver must wait on exactly the URL files its sources"
            f" read (waits={waited}, sources={list(refs)})"
        )
    for flag, expected in (
        ("--deadline-utc", deadline_utc),
        ("--expect-plan-sha256", run_plan_sha256),
    ):
        if expected is None:
            continue
        got = argv_option(argv, flag)
        if got is None:
            raise Refusal(
                f"preresolved resolver argv must carry {flag} — the wait fails"
                " closed rather than inventing its own bound"
            )
        if got != expected:
            raise Refusal(
                f"preresolved resolver argv {flag}={got!r} contradicts the"
                f" AccessPlan ({expected!r})"
            )


def validate_resolver(
    kind: str,
    argv: list[str],
    refs: list[str],
    *,
    deadline_utc: str | None = None,
    run_plan_sha256: str | None = None,
):
    if kind not in RESOLVER_KINDS:
        raise Refusal(f"unsupported resolver kind {kind!r}")
    if kind == "static-public":
        if argv:
            raise Refusal("static-public must not carry resolver argv")
        if refs:
            raise Refusal("static-public is only for configs with embedded public endpoints")
        return
    if not argv:
        raise Refusal(f"resolver kind {kind} requires exact argv")
    validate_argv_non_secret(argv, "resolver argv")
    expected = RESOLVER_KINDS[kind]
    if expected and not any(Path(arg).name == expected for arg in argv):
        raise Refusal(f"resolver kind {kind} argv does not invoke {expected}")
    if not refs:
        raise Refusal(f"resolver kind {kind} requires a URL-file reference in source argv")
    if kind == "preresolved":
        validate_preresolved_argv(argv, refs, deadline_utc, run_plan_sha256)


def validate_preresolve(value, refs: list[str]):
    """The other-host leg that MINTS the URL a `preresolved` plan waits for."""
    if not isinstance(value, dict) or set(value) != {"kind", "argv"}:
        raise Refusal("AccessPlan preresolve shape is invalid")
    kind = value.get("kind")
    if kind not in PRERESOLVE_KINDS:
        raise Refusal(f"unsupported AccessPlan preresolve kind {kind!r}")
    argv = value.get("argv")
    if not isinstance(argv, list) or not argv or not all(
        isinstance(x, str) and x for x in argv
    ):
        raise Refusal("AccessPlan preresolve argv must be a non-empty-string array")
    validate_argv_non_secret(argv, "preresolve argv")
    expected = RESOLVER_KINDS[kind]
    if expected and not any(Path(arg).name == expected for arg in argv):
        raise Refusal(f"AccessPlan preresolve kind {kind} argv does not invoke {expected}")
    minted = argv_url_files(argv, "preresolve argv")
    if minted != list(refs):
        raise Refusal(
            "preresolve must mint exactly the URL files the plan waits for"
            f" (mints={minted}, plan={list(refs)})"
        )


def build_prerequisite(args, deadline: dt.datetime) -> dict:
    mode = args.access_mode
    if mode not in ACCESS_MODES:
        raise Refusal(f"unsupported access mode {mode!r}")
    if mode == "public":
        if args.attested_by or args.session_ref or args.session_expires_at_utc:
            raise Refusal(
                "public access must not carry session attestation fields"
            )
        return {
            "mode": "public",
            "requirement": "none",
            "satisfied": True,
            "attested_by": None,
            "session_reference": None,
            "session_expires_at_utc": None,
            "secret_material_embedded": False,
        }

    missing = [
        flag for flag, value in (
            ("--attested-by", args.attested_by),
            ("--session-ref", args.session_ref),
            ("--session-expires-at-utc", args.session_expires_at_utc),
        ) if not value
    ]
    if missing:
        raise Refusal(
            f"{mode} requires an explicit completed-session attestation: "
            + ", ".join(missing)
        )
    if not ATTESTER_RE.fullmatch(args.attested_by):
        raise Refusal("--attested-by must be a stable non-secret operator ID")
    if not SESSION_REF_RE.fullmatch(args.session_ref):
        raise Refusal(
            "--session-ref must be browser-profile:<label> or "
            "external-session:<label>, never a cookie/token/URL"
        )
    session_expires = parse_utc(
        args.session_expires_at_utc, "session prerequisite expiry"
    )
    if session_expires <= deadline:
        raise Refusal(
            "session prerequisite must remain valid beyond the resolver deadline"
        )
    return {
        "mode": "external-session",
        "requirement": (
            "registration-complete"
            if mode == "registered-session"
            else "signed-in-session"
        ),
        "satisfied": True,
        "attested_by": args.attested_by,
        "session_reference": args.session_ref,
        "session_expires_at_utc": args.session_expires_at_utc,
        "secret_material_embedded": False,
    }


RESOLVER_FIELDS = {
    "kind", "argv", "deadline_utc", "network_performed_at_compile",
    "secret_material_embedded", "credential_contract",
}


def resolver_fields_for(kind) -> set:
    """`preresolve` is admitted ONLY for the kind that owns it.

    Never `RESOLVER_FIELDS | {"preresolve"}` unconditionally, and never
    `set(resolver) - {"preresolve"}`: either would let a `q4-api` plan carry a
    silent extra block that changes its own hash and nothing rejects.
    """
    if kind == "preresolved":
        return RESOLVER_FIELDS | {"preresolve"}
    return RESOLVER_FIELDS


def resolver_block(kind: str, argv: list[str], deadline_utc: str, preresolve) -> dict:
    """The resolver object. The key is ABSENT — never null — for other kinds.

    `access_plan_sha256` covers every key, and it is transitively load-bearing
    (`pcm-gate.sh` bakes it into the PCM proof, `capture-ready.py` re-checks it),
    so a null placeholder on existing kinds would change every published plan's
    hash and break artifacts already on disk.
    """
    block = {
        "kind": kind,
        "argv": argv,
        "deadline_utc": deadline_utc,
        "network_performed_at_compile": False,
        "secret_material_embedded": False,
        "credential_contract": "environment-or-external-reference-only",
    }
    if preresolve is not None:
        block["preresolve"] = preresolve
    return block


def local_execution_host() -> str:
    """This machine's execution-host id, resolved exactly as `eventctl.py` does."""
    return (
        os.environ.get("EARSHOT_EXECUTION_HOST")
        or os.environ.get("EARSHOT_HOST_ID")
        or ""
    ).strip()


def box_execution_host() -> str:
    """The remote capture host id (seam S-B). Read-only here; lane S routes on it."""
    return (os.environ.get("EARSHOT_BOX_EXECUTION_HOST") or "earshot-useast1").strip()


def resolve_vantage(args, plan: dict):
    """Settle `capture.vantage`, refusing a silent default where it would lie.

    A defaulted vantage is indistinguishable from a deliberate one in the
    artifact, so us-east capture would pool with CDMX Δrace history without
    anyone choosing that (`venue-probe.sh` names the hazard).

    The refusal is deliberately narrow — a `preresolved` plan (cross-host by
    construction), or a plan compiled for the box while binding somewhere else.
    A blanket "any cross-host default is fatal" rule reddens callers this node
    does not own: `tests/typed_scheduler_boundary.sh` binds four plans for
    `earshot-test-host` before it exports an execution host, so an ambient
    `EARSHOT_EXECUTION_HOST` in the operator's shell would fail a sibling lane's
    suite for reasons that have nothing to do with it. Everything outside the
    two refusal cases gets a loud WARN instead, which carries the same
    information without holding another lane hostage.
    """
    if args.vantage:
        return
    plan_host = plan["execution_host_id"]
    if args.resolver_kind == "preresolved":
        raise Refusal(
            "preresolved plans are cross-host by construction (one host mints the"
            " URL, another captures it): name the capture vantage explicitly with"
            " --vantage rather than defaulting it to the execution host"
        )
    local_host = local_execution_host()
    if local_host and local_host != plan_host:
        if plan_host == box_execution_host():
            raise Refusal(
                f"RunPlan is compiled for the capture box {plan_host!r} but bind is"
                f" running on {local_host!r}: --vantage must be named explicitly so"
                " box capture is never pooled with laptop Δrace history"
            )
        print(
            f"WARN: --vantage defaulted to {plan_host!r} for a plan bound on"
            f" {local_host!r}; name the capture vantage explicitly if this run will"
            " be compared against another host's history",
            file=sys.stderr,
        )
    args.vantage = plan_host


def complete_preresolved_argv(
    argv: list[str], deadline_utc: str, run_plan_sha256: str
) -> list[str]:
    """Bake the plan's own deadline, attempt identity, and freshness into argv.

    These are derived facts, so bind fills them rather than trusting an operator
    to retype them; an operator-supplied value that disagrees is refused by
    `validate_preresolved_argv`. Living in argv makes them hash-covered, which
    is what stops a fire-time environment variable from widening the window.
    """
    completed = list(argv)
    for flag, value in (
        ("--deadline-utc", deadline_utc),
        ("--expect-plan-sha256", run_plan_sha256),
        ("--fresh-secs", str(PRERESOLVED_FRESH_SECS)),
    ):
        if argv_option(completed, flag) is None:
            completed.extend([flag, value])
    return completed


def build_preresolve(args, refs: list[str]) -> dict | None:
    kind = getattr(args, "preresolve_kind", None)
    raw = getattr(args, "preresolve_argv_json", None) or "[]"
    if args.resolver_kind != "preresolved":
        if kind or raw != "[]":
            raise Refusal(
                "--preresolve-kind/--preresolve-argv-json describe the other host's"
                " resolve leg and are only valid for resolver kind preresolved"
            )
        return None
    if not kind:
        raise Refusal(
            "resolver kind preresolved requires --preresolve-kind: the plan must"
            " record WHICH resolver mints the URL on the other host, or the push"
            " step is operator memory at T-75"
        )
    value = {"kind": kind, "argv": parse_resolver_argv(raw)}
    validate_preresolve(value, refs)
    return value


def build_artifact(
    repo: Path,
    run_dir: Path,
    plan: dict,
    args,
    *,
    schema: int,
    infer_url_files: bool = False,
) -> dict:
    _cfg, sources, discovered_refs = config_sources(repo, plan)
    requested_refs = sorted({normalize_url_file(x) for x in args.url_file})
    if infer_url_files and not requested_refs:
        requested_refs = discovered_refs
    if requested_refs != discovered_refs:
        raise Refusal(
            "declared URL files do not exactly match source argv"
            f" (declared={requested_refs}, source={discovered_refs})"
        )
    resolver_argv = parse_resolver_argv(args.resolver_argv_json)
    resolve_vantage(args, plan)
    if not VANTAGE_RE.fullmatch(args.vantage or ""):
        raise Refusal("--vantage must be a stable 1-128 character label")
    # The deadline is settled BEFORE the resolver is validated: a `preresolved`
    # argv must carry that exact deadline, and a resolver validated against an
    # unset default would be checked against the wrong bound.
    if not args.deadline_utc:
        starts = parse_utc(plan["starts_at_utc"], "RunPlan starts_at_utc")
        args.deadline_utc = (
            starts + dt.timedelta(minutes=15)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.resolver_kind == "preresolved":
        resolver_argv = complete_preresolved_argv(
            resolver_argv, args.deadline_utc, plan["plan_sha256"]
        )
    validate_resolver(
        args.resolver_kind,
        resolver_argv,
        discovered_refs,
        deadline_utc=args.deadline_utc,
        run_plan_sha256=plan["plan_sha256"],
    )
    preresolve = build_preresolve(args, discovered_refs)
    deadline = parse_utc(args.deadline_utc, "resolver deadline")
    starts = parse_utc(plan["starts_at_utc"], "RunPlan starts_at_utc")
    if deadline > starts + dt.timedelta(minutes=30):
        raise Refusal("resolver deadline may not extend more than 30 minutes past event start")
    if args.expiry_mode not in EXPIRY_MODES:
        raise Refusal(f"unsupported expiry mode {args.expiry_mode!r}")
    expires = None
    expires_at = None
    if args.expires_at_utc:
        expires_at = parse_utc(args.expires_at_utc, "endpoint expiry")
        expires = args.expires_at_utc
    if args.expiry_mode == "resolver-declared" and expires is None:
        raise Refusal("resolver-declared expiry requires --expires-at-utc")
    if args.expiry_mode != "resolver-declared" and expires is not None:
        raise Refusal(f"{args.expiry_mode} expiry must not carry --expires-at-utc")
    if expires_at is not None and expires_at <= deadline:
        raise Refusal("resolver-declared endpoint expiry must be after its resolver deadline")
    artifact = {
        "schema": schema,
        "kind": "webcast-access-plan",
        "created_utc": now_utc(),
        "event_id": plan["event_id"],
        "run_id": plan["run_id"],
        "run_plan_sha256": plan["plan_sha256"],
        "config_path": plan["config_path"],
        "config_sha256": plan["config_sha256"],
        "capture": {
            "host_id": plan["execution_host_id"],
            "vantage": args.vantage,
        },
        "resolver": resolver_block(
            args.resolver_kind, resolver_argv, args.deadline_utc, preresolve
        ),
        "endpoint": {
            "url_files": discovered_refs,
            "publication": "atomic-replace" if discovered_refs else "embedded-in-source-argv",
            "expiry_mode": args.expiry_mode,
            "expires_at_utc": expires,
        },
        "sources": sources,
        "current_media": {
            "proven": False,
            "pcm_proof": None,
            "belongs_to_transition": "capture_ready",
        },
    }
    if schema == SCHEMA:
        artifact["access_prerequisite"] = build_prerequisite(args, deadline)
    elif schema != LEGACY_SCHEMA:
        raise Refusal(f"unsupported AccessPlan schema {schema}")
    artifact["access_plan_sha256"] = object_hash(artifact, "access_plan_sha256")
    return artifact


def validate_prerequisite(value, deadline: dt.datetime):
    expected = {
        "mode", "requirement", "satisfied", "attested_by",
        "session_reference", "session_expires_at_utc",
        "secret_material_embedded",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise Refusal("AccessPlan access prerequisite shape is invalid")
    if value.get("satisfied") is not True:
        raise Refusal("AccessPlan access prerequisite is not satisfied")
    if value.get("secret_material_embedded") is not False:
        raise Refusal("AccessPlan access prerequisite must remain non-secret")
    mode = value.get("mode")
    requirement = value.get("requirement")
    if mode == "public":
        if requirement != "none" or any(
            value.get(field) is not None
            for field in (
                "attested_by", "session_reference", "session_expires_at_utc"
            )
        ):
            raise Refusal("public AccessPlan prerequisite is contradictory")
        return
    if mode != "external-session" or requirement not in {
        "registration-complete", "signed-in-session"
    }:
        raise Refusal("AccessPlan session prerequisite mode is invalid")
    if not ATTESTER_RE.fullmatch(value.get("attested_by") or ""):
        raise Refusal("AccessPlan session attester is invalid")
    if not SESSION_REF_RE.fullmatch(value.get("session_reference") or ""):
        raise Refusal("AccessPlan session reference is invalid")
    expires = parse_utc(
        value.get("session_expires_at_utc"), "session prerequisite expiry"
    )
    if expires <= deadline:
        raise Refusal(
            "AccessPlan session prerequisite expires before its resolver deadline"
        )


def validate_artifact(repo: Path, run_dir: Path, plan: dict, path: Path) -> dict:
    expected_path = run_dir / "control" / "access-plan.json"
    if path.resolve() != expected_path.resolve():
        raise Refusal(f"AccessPlan must be run-scoped at {expected_path}")
    if not path.is_file() or path.is_symlink():
        raise Refusal(f"AccessPlan is not a regular non-symlink file: {path}")
    raw = path.read_bytes()
    try:
        artifact = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal(f"AccessPlan JSON invalid: {exc}") from exc
    expected_fields = {
        "schema", "kind", "created_utc", "event_id", "run_id",
        "run_plan_sha256", "config_path", "config_sha256", "capture",
        "resolver", "endpoint", "sources", "current_media", "access_plan_sha256",
    }
    schema = artifact.get("schema") if isinstance(artifact, dict) else None
    if schema == SCHEMA:
        expected_fields.add("access_prerequisite")
    if not isinstance(artifact, dict) or set(artifact) != expected_fields:
        raise Refusal(f"AccessPlan fields do not match schema {schema!r}")
    if schema not in {LEGACY_SCHEMA, SCHEMA} or artifact.get("kind") != "webcast-access-plan":
        raise Refusal("unsupported AccessPlan schema/kind")
    parse_utc(artifact.get("created_utc"), "AccessPlan created_utc")
    if artifact.get("access_plan_sha256") != object_hash(artifact, "access_plan_sha256"):
        raise Refusal("AccessPlan self-hash mismatch")
    if raw != json.dumps(artifact, indent=2, sort_keys=True).encode() + b"\n":
        raise Refusal("AccessPlan bytes are not canonical pretty JSON")
    identity = (
        artifact.get("event_id"), artifact.get("run_id"),
        artifact.get("run_plan_sha256"), artifact.get("config_path"),
        artifact.get("config_sha256"),
    )
    expected_identity = (
        plan["event_id"], plan["run_id"], plan["plan_sha256"],
        plan["config_path"], plan["config_sha256"],
    )
    if identity != expected_identity:
        raise Refusal("AccessPlan does not bind the selected RunPlan/config")
    capture = artifact.get("capture")
    if not isinstance(capture, dict) or set(capture) != {"host_id", "vantage"}:
        raise Refusal("AccessPlan capture shape is invalid")
    if capture["host_id"] != plan["execution_host_id"] or not VANTAGE_RE.fullmatch(capture.get("vantage", "")):
        raise Refusal("AccessPlan capture host/vantage is invalid")
    _cfg, sources, refs = config_sources(repo, plan)
    if artifact.get("sources") != sources:
        raise Refusal("AccessPlan source argv drifted from the pinned config")
    endpoint = artifact.get("endpoint")
    if not isinstance(endpoint, dict) or set(endpoint) != {
        "url_files", "publication", "expiry_mode", "expires_at_utc"
    }:
        raise Refusal("AccessPlan endpoint shape is invalid")
    if endpoint["url_files"] != refs:
        raise Refusal("AccessPlan URL-file references drifted from source argv")
    expiry_mode = endpoint.get("expiry_mode")
    expires = endpoint.get("expires_at_utc")
    if expiry_mode not in EXPIRY_MODES:
        raise Refusal("AccessPlan expiry mode is invalid")
    resolver = artifact.get("resolver")
    if not isinstance(resolver, dict) or set(resolver) != resolver_fields_for(
        resolver.get("kind") if isinstance(resolver, dict) else None
    ):
        raise Refusal("AccessPlan resolver shape is invalid")
    if resolver.get("network_performed_at_compile") is not False:
        raise Refusal("AccessPlan may not claim a compile-time network acquisition")
    if resolver.get("secret_material_embedded") is not False or resolver.get(
        "credential_contract"
    ) != "environment-or-external-reference-only":
        raise Refusal("AccessPlan must remain non-secret")
    deadline = parse_utc(resolver.get("deadline_utc"), "resolver deadline")
    starts = parse_utc(plan["starts_at_utc"], "RunPlan starts_at_utc")
    if deadline > starts + dt.timedelta(minutes=30):
        raise Refusal("AccessPlan resolver deadline extends more than 30 minutes past event start")
    if expiry_mode == "resolver-declared":
        expires_at = parse_utc(expires, "endpoint expiry")
        if expires_at <= deadline:
            raise Refusal("AccessPlan endpoint expires before its resolver deadline")
    elif expires is not None:
        raise Refusal("AccessPlan expiry timestamp contradicts its mode")
    if not isinstance(resolver.get("argv"), list):
        raise Refusal("AccessPlan resolver argv is invalid")
    validate_resolver(
        resolver.get("kind"),
        resolver["argv"],
        refs,
        deadline_utc=resolver.get("deadline_utc"),
        run_plan_sha256=plan["plan_sha256"],
    )
    if resolver.get("kind") == "preresolved":
        validate_preresolve(resolver.get("preresolve"), refs)
    if schema == SCHEMA:
        validate_prerequisite(artifact.get("access_prerequisite"), deadline)
    current = artifact.get("current_media")
    if current != {
        "proven": False, "pcm_proof": None, "belongs_to_transition": "capture_ready"
    }:
        raise Refusal("AccessPlan must explicitly leave current media unproven")
    return artifact


def atomic_publish(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        view = memoryview(data)
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
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def publish_artifact(repo: Path, run_dir: Path, plan: dict, artifact: dict) -> int:
    out = run_dir / "control" / "access-plan.json"
    if out.exists():
        current = validate_artifact(repo, run_dir, plan, out)
        comparable = lambda v: {
            k: x for k, x in v.items()
            if k not in {"created_utc", "access_plan_sha256"}
        }
        if comparable(current) != comparable(artifact):
            raise Refusal(f"immutable AccessPlan collision at {out}")
        print(f"already-valid: {out} sha256={current['access_plan_sha256']}")
        return 0
    atomic_publish(out, json.dumps(artifact, indent=2, sort_keys=True).encode() + b"\n")
    validate_artifact(repo, run_dir, plan, out)
    print(f"created: {out} sha256={artifact['access_plan_sha256']}")
    return 0


def cmd_create(args) -> int:
    repo = repo_root()
    run_dir, plan = load_run(repo, args.run_dir)
    artifact = build_artifact(
        repo, run_dir, plan, args, schema=LEGACY_SCHEMA
    )
    out = Path(args.out).resolve() if args.out else run_dir / "control" / "access-plan.json"
    expected = run_dir / "control" / "access-plan.json"
    if out != expected:
        raise Refusal(f"AccessPlan output must be {expected}")
    return publish_artifact(repo, run_dir, plan, artifact)


def cmd_bind(args) -> int:
    """Canonical binding; never performs registration, login, or resolution."""
    repo = repo_root()
    run_dir, plan = load_run(repo, args.run_dir)
    artifact = build_artifact(
        repo, run_dir, plan, args, schema=SCHEMA, infer_url_files=True
    )
    return publish_artifact(repo, run_dir, plan, artifact)


def cmd_validate(args) -> int:
    repo = repo_root()
    run_dir, plan = load_run(repo, args.run_dir)
    path = Path(args.plan).resolve() if args.plan else run_dir / "control" / "access-plan.json"
    artifact = validate_artifact(repo, run_dir, plan, path)
    if args.canonical and artifact.get("schema") != SCHEMA:
        raise Refusal(
            "canonical operation requires schema-2 AccessPlan with an explicit "
            "public/session prerequisite; compile a new immutable RunPlan attempt"
        )
    prerequisite = artifact.get("access_prerequisite")
    print(json.dumps({
        "schema": 1,
        "kind": "access-plan-validation",
        "access_plan_schema": artifact["schema"],
        "event_id": artifact["event_id"],
        "run_id": artifact["run_id"],
        "access_plan": str(path),
        "access_plan_sha256": artifact["access_plan_sha256"],
        "capture_host_id": artifact["capture"]["host_id"],
        "vantage": artifact["capture"]["vantage"],
        "resolver_kind": artifact["resolver"]["kind"],
        "current_media_proven": False,
        "access_prerequisite_mode": (
            prerequisite["mode"] if prerequisite else "legacy-undeclared"
        ),
        "access_prerequisite_satisfied": bool(
            prerequisite and prerequisite.get("satisfied") is True
        ),
    }, sort_keys=True))
    return 0


def valid_date(raw: str) -> str:
    try:
        value = dt.date.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise Refusal(f"--date must be YYYY-MM-DD, got {raw!r}") from exc
    if value.isoformat() != raw:
        raise Refusal(f"--date must be canonical YYYY-MM-DD, got {raw!r}")
    return raw


def selected_run_plans(repo: Path, date: str) -> list[tuple[Path, dict]]:
    root = repo / "var" / "runs"
    selected = []
    if not root.is_dir():
        return selected
    for path in sorted(root.glob("*/*/control/run-plan.json")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            plan = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            # Corrupt plans are owned by the date-level run auditor. A plan
            # whose start cannot be read cannot be safely guessed onto a day.
            continue
        if (
            plan.get("schema") == PLAN_SCHEMA
            and plan.get("kind") == "run-plan"
            and isinstance(plan.get("starts_at_utc"), str)
            and plan["starts_at_utc"][:10] == date
        ):
            selected.append((path.parent.parent, plan))
    selected.sort(key=lambda item: (
        item[1].get("starts_at_utc", ""),
        item[1].get("event_id", ""),
        item[1].get("run_id", ""),
    ))
    return selected


def bind_next_action(repo: Path, run_dir: Path, plan: dict) -> str:
    rel = run_dir.relative_to(repo).as_posix()
    _cfg, _sources, refs = config_sources(repo, plan)
    ref_flags = " ".join(
        f"--url-file {shlex.quote(ref)}" for ref in refs
    )
    suffix = f" {ref_flags}" if ref_flags else ""
    return (
        "scripts/opsctl.sh bind-access "
        f"--run-dir {shlex.quote(rel)} --access-mode ACCESS_MODE "
        "--resolver-kind RESOLVER_KIND --resolver-argv-json JSON_ARGV"
        f"{suffix}"
    )


def cmd_check_day(args) -> int:
    """Read-only canonical acquisition join for one RunPlan UTC date."""
    repo = repo_root()
    date = valid_date(args.date)
    selected = selected_run_plans(repo, date)
    if not selected:
        raise Refusal(f"no schema-3 RunPlans start on UTC date {date}")

    passed = skipped_phone = skipped_terminal = failed = 0
    for run_dir, plan in selected:
        label = f"{plan.get('event_id')}/{plan.get('run_id')}"
        try:
            run_eventctl(repo, "verify", "--run-dir", str(run_dir))
            status = json.loads(run_eventctl(
                repo, "status", "--run-dir", str(run_dir), "--json"
            ))
        except (Refusal, json.JSONDecodeError) as exc:
            failed += 1
            print(f"ACCESS-DAY FAIL {label}: {exc}", file=sys.stderr)
            continue
        if not status.get("state", {}).get("active"):
            skipped_terminal += 1
            print(f"ACCESS-DAY SKIP {label}: terminal run")
            continue
        lane = plan.get("lane")
        if lane == "phone":
            skipped_phone += 1
            print(f"ACCESS-DAY SKIP {label}: phone lane uses admission strategy")
            continue
        if lane != "webcast":
            failed += 1
            print(f"ACCESS-DAY FAIL {label}: unsupported lane {lane!r}", file=sys.stderr)
            continue

        path = run_dir / "control" / "access-plan.json"
        if not path.is_file() or path.is_symlink():
            failed += 1
            print(
                f"ACCESS-DAY FAIL {label}: missing canonical schema-2 AccessPlan",
                file=sys.stderr,
            )
            try:
                next_action = bind_next_action(repo, run_dir, plan)
            except Refusal as exc:
                next_action = (
                    "repair the pinned webcast source declaration before binding: "
                    f"{exc}"
                )
            print(f"ACCESS-DAY NEXT {label}: {next_action}")
            continue
        try:
            artifact = validate_artifact(repo, run_dir, plan, path)
        except Refusal as exc:
            failed += 1
            print(f"ACCESS-DAY FAIL {label}: {exc}", file=sys.stderr)
            continue
        if artifact.get("schema") != SCHEMA:
            failed += 1
            print(
                f"ACCESS-DAY FAIL {label}: legacy schema-1 AccessPlan has no "
                "typed public/session prerequisite; immutable artifact cannot be upgraded",
                file=sys.stderr,
            )
            print(
                f"ACCESS-DAY NEXT {label}: compile a new immutable RunPlan attempt, "
                "then run bind-access; never overwrite this run",
            )
            continue
        passed += 1
        prerequisite = artifact["access_prerequisite"]
        print(
            f"ACCESS-DAY PASS {label}: schema=2 "
            f"mode={prerequisite['mode']} requirement={prerequisite['requirement']}"
        )

    print(
        f"ACCESS-DAY SUMMARY date={date} selected={len(selected)} pass={passed} "
        f"phone={skipped_phone} terminal={skipped_terminal} fail={failed}"
    )
    return 1 if failed else 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    def resolver_arguments(command, *, canonical: bool):
        command.add_argument("--run-dir", required=True)
        command.add_argument("--vantage", required=not canonical)
        command.add_argument(
            "--resolver-kind", choices=sorted(RESOLVER_KINDS), required=True
        )
        command.add_argument("--resolver-argv-json", default="[]")
        command.add_argument(
            "--preresolve-kind",
            choices=PRERESOLVE_KINDS,
            help="resolver kind that mints the URL on the OTHER host"
            " (required for --resolver-kind preresolved)",
        )
        command.add_argument(
            "--preresolve-argv-json",
            default="[]",
            help="exact argv of that other-host resolve leg",
        )
        command.add_argument("--url-file", action="append", default=[])
        command.add_argument("--deadline-utc", required=not canonical)
        command.add_argument(
            "--expiry-mode",
            choices=sorted(EXPIRY_MODES),
            required=not canonical,
            default="refresh-at-launch" if canonical else None,
        )
        command.add_argument("--expires-at-utc")

    create = sub.add_parser("create", help="compile one immutable AccessPlan")
    resolver_arguments(create, canonical=False)
    create.add_argument("--out")
    create.set_defaults(func=cmd_create)
    bind = sub.add_parser(
        "bind",
        help=(
            "canonically bind a schema-2 plan after explicitly classifying "
            "public or human-session access"
        ),
    )
    resolver_arguments(bind, canonical=True)
    bind.add_argument("--access-mode", choices=sorted(ACCESS_MODES), required=True)
    bind.add_argument("--attested-by")
    bind.add_argument("--session-ref")
    bind.add_argument("--session-expires-at-utc")
    bind.set_defaults(func=cmd_bind)
    validate = sub.add_parser("validate", help="validate plan + current pinned inputs")
    validate.add_argument("--run-dir", required=True)
    validate.add_argument("--plan")
    validate.add_argument(
        "--canonical",
        action="store_true",
        help="require schema 2 with an explicit satisfied access prerequisite",
    )
    validate.set_defaults(func=cmd_validate)
    check_day = sub.add_parser(
        "check-day",
        help="require canonical acquisition binding for every active webcast RunPlan on a UTC date",
    )
    check_day.add_argument("--date", required=True)
    check_day.set_defaults(func=cmd_check_day)
    return ap


def main() -> int:
    ap = parser()
    args = ap.parse_args()
    try:
        return args.func(args)
    except Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
