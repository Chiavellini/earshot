#!/usr/bin/env python3
"""eventctl.py — the typed event-run control plane (node control.operate).

One immutable run identity and one replayable, append-only lifecycle ledger
per event attempt, under var/runs/<EVENT_ID>/<RUN_ID>/:

  control/run-plan.json    door control.run.plan   — immutable; self-hashed
  control/receipts.jsonl   door control.run.receipts — hash-chained appends
  control/status.json      derived snapshot; NEVER authoritative

Born from casino143.md (2026-08-11 CAVA/CRWV): $48.50 of fills with no banked
audio, report.json overwritten per arm restart, order accounting that
disagreed with itself, and a host-absolute ACCOUNT_ENV that killed both
events' dials. This tool records what the existing entry points did; it is
NOT a third runtime composer — it never launches an arm, places a call, or
touches order policy.

Commands: init, record, compat, status, verify, init-date.
Exit codes: 0 success (including idempotent no-ops and a compat with no
active run), 1 refusal or integrity failure, 2 usage, 3 recoverable
truncated tail.

Stdlib only. Receipts are appended under an exclusive flock on the receipts
fd itself with O_APPEND and exactly one write; the chain is re-verified —
along with the current config/env hashes — before every typed transition.
compat receipts are legacy-journal observations: they live in the same chain
but are structurally excluded from the typed-state reducer, so they can
never advance or terminate typed state.
"""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from fnmatch import fnmatch
from pathlib import Path

PLAN_SCHEMA = 3
RECEIPT_SCHEMA = 1

# The `earshot prepare` template start time; surviving into an armed config
# was casino143 invariant 10's failure mode.
PLACEHOLDER_STARTS_AT = "2026-07-15T00:00:00Z"

EVENT_ID_RE = re.compile(r"^[A-Z0-9]+(-[A-Z0-9]+)*$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$")
MAX_ID_LEN = 64

TYPED_OUTCOMES = ("ok", "fail", "skipped")
COMPAT_OUTCOMES = ("ok", "fail", "warn", "info", "refused", "skipped")

TERMINALS = ("descope", "blocked", "aborted", "failed")
# Post-mortem tail stays legal after aborted/failed — a failed run must still
# be auditable. descope/blocked never ran, so only archived may follow.
POST_TERMINAL_ALLOWED = {
    "descope": {"archived"},
    "blocked": {"archived"},
    "aborted": {"evidence_consolidated", "evidence_pulled", "graded", "archived"},
    "failed": {"evidence_consolidated", "evidence_pulled", "graded", "archived"},
}

# ALL-of prerequisites; preflight_passed's set comes from the plan
# (required_prep_receipts, materialized once at init). ANY-of sets cover the
# post-mortem joins.
PREREQ_ALL = {
    "plan_compiled": [],
    "prepared": ["plan_compiled"],
    "admission_resolved": ["plan_compiled"],
    "access_acquired": ["plan_compiled"],
    "study_completed": ["plan_compiled"],
    "preflight_passed": None,  # plan.required_prep_receipts
    "scheduled": ["preflight_passed"],
    "capture_ready": ["preflight_passed"],
    "launched": ["scheduled", "capture_ready"],
    "source_attached": ["launched"],
    "supervising": ["source_attached"],
    "draining": ["supervising"],
    "completed": ["draining"],
    "graded": ["evidence_consolidated"],
}
PREREQ_ANY = {
    "evidence_consolidated": ["completed", "aborted", "failed"],
    "evidence_pulled": ["completed", "aborted", "failed"],
    "archived": ["graded", "descope", "blocked", "aborted", "failed"],
}
LANE_ONLY = {"admission_resolved": "phone", "access_acquired": "webcast"}
ALL_TRANSITIONS = (
    set(PREREQ_ALL) | set(PREREQ_ANY) | set(TERMINALS) | {"preflight_passed"}
)

# `evidence_pulled` is retained as the remote-transfer provenance fact and is
# a stronger form of consolidation. Grading accepts either fact, while local
# runs can record consolidation without inventing a host transfer.
PREREQ_EQUIVALENTS = {"evidence_consolidated": {"evidence_pulled"}}

# Run-relative artifact destinations.  Audio evidence receives every path its
# constructor requires; the FIX observation banker receives its independent
# raw receive tape.  `audio_wav_dir` is deliberately a directory destination:
# init creates only its parent because AudioEvidenceBank requires the WAV
# directory itself not to exist until it obtains the run-scoped evidence lease.
# This slice writes NONE of them — destinations only.
ARTIFACT_DESTINATIONS = {
    "receipts": "control/receipts.jsonl",
    "status": "control/status.json",
    "audio_raw": "capture/audio.raw",
    "audio_frames": "capture/audio.frames.jsonl",
    "audio_manifest": "capture/audio.manifest.json",
    "audio_commit": "capture/audio.commit.json",
    "audio_lock": "capture/audio.lock",
    "audio_wav_dir": "capture/wav",
    "book_tape": "book/kalshi-tape.jsonl",
    "order_evidence": "orders/order-evidence.jsonl",
    "fix_observation_tape": "orders/fix-observations.jsonl",
    "runtime_report": "report/report.json",
    "grade": "grade/verdict.json",
}

# Non-event and never-launched config shapes in configs/ (raw copies are
# audit artifacts; rehearsal-* is gate-exempt by convention; the rest are
# fixtures). Matched against the basename.
SKIP_CONFIG_GLOBS = (
    "*-raw.toml",
    "rehearsal-*",
    "bakeoff-*",
    "twilio-live-*",
    "webcast-*capture*",
    "pcm-lead-*",
    "m1*",
    "m2-*",
)

HOST_ABS_RE = re.compile(
    r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=["\']?(/Users/|/home/)',
    re.M,
)
ENV_CONFIG_RE = re.compile(r'^CONFIG_PATH="([^"]+)"', re.M)
ENV_DATE_RE = re.compile(r'^EVENT_DATE="(\d{4}-\d{2}-\d{2})"', re.M)
ENV_ACCOUNT_PROFILE_RE = re.compile(
    r'^\s*(?:export\s+)?EARSHOT_ACCOUNT_PROFILE=["\']([^"\']+)["\']\s*(?:#.*)?$',
    re.M,
)
ENV_ACCOUNT_RE = re.compile(
    r'^\s*(?:export\s+)?ACCOUNT_ENV=["\']([^"\']+)["\']\s*(?:#.*)?$',
    re.M,
)
HOST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class Refusal(Exception):
    """Exit 1. Nothing was modified."""


class IntegrityError(Exception):
    """Exit 1. The ledger, plan, or a pinned input failed verification."""


class RecoverableTail(Exception):
    """Exit 3. The final receipts line is torn; everything before it is valid.

    Repair is a MANUAL truncation to truncate_to bytes — eventctl never
    rewrites the ledger.
    """

    def __init__(self, msg, last_valid_sequence, truncate_to):
        super().__init__(msg)
        self.last_valid_sequence = last_valid_sequence
        self.truncate_to = truncate_to


# ---------------------------------------------------------------------------
# canonical hashing


def canon_bytes(obj):
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def sha_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    return sha_hex(path.read_bytes())


def hash_of(obj, omit: str) -> str:
    return sha_hex(canon_bytes({k: v for k, v in obj.items() if k != omit}))


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def short_host() -> str:
    return os.uname().nodename.split(".")[0]


def fsync_dir(path: Path):
    """Persist directory-entry changes made by rename/mkdir.

    fsyncing a file makes its bytes durable, but not the name that reaches
    those bytes. Run-plan publication is an identity boundary, so both are
    required before `init` can report success.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_durable(path: Path, data: bytes, mode=0o644):
    """Write one immutable/derived artifact via fsynced temp + rename.

    The temp name is process-specific so simultaneous initializers never
    overwrite one another's unpublished bytes. Callers serialize publication
    with the receipts lock when the destination is authoritative.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(tmp, flags, mode)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise IntegrityError(f"short write while publishing {path}")
            view = view[written:]
        os.fsync(fd)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------------------
# repo + identifier safety


def repo_root() -> Path:
    env = os.environ.get("EARSHOT_REPO")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def resolve_in_repo(raw: str, repo: Path, what: str):
    """Resolve raw against CWD then the repo root; refuse paths that land
    outside the repository. Returns (absolute, repo-relative POSIX str)."""
    for base in (Path.cwd(), repo):
        cand = (base / raw).resolve()
        if cand.is_file():
            try:
                return cand, cand.relative_to(repo).as_posix()
            except ValueError:
                raise Refusal(
                    f"{what} {raw!r} resolves outside the repository ({cand})"
                )
    # not found anywhere — still refuse with the in/out distinction
    cand = (Path.cwd() / raw).resolve()
    try:
        cand.relative_to(repo)
    except ValueError:
        raise Refusal(f"{what} {raw!r} resolves outside the repository ({cand})")
    raise Refusal(f"{what} {raw!r} does not exist")


def validate_event_id(event_id: str):
    if (
        not event_id
        or len(event_id) > MAX_ID_LEN
        or not EVENT_ID_RE.match(event_id)
    ):
        raise Refusal(
            f"unsafe EVENT_ID {event_id!r} (must match {EVENT_ID_RE.pattern},"
            f" <= {MAX_ID_LEN} chars)"
        )


def validate_run_id(run_id: str):
    if not run_id or len(run_id) > MAX_ID_LEN or not RUN_ID_RE.match(run_id):
        raise Refusal(
            f"unsafe RUN_ID {run_id!r} (must match {RUN_ID_RE.pattern},"
            f" <= {MAX_ID_LEN} chars)"
        )


def validate_rel_artifact(p: str):
    pp = Path(p)
    if pp.is_absolute() or ".." in pp.parts or p.strip() == "":
        raise Refusal(f"artifact path must be run-relative with no traversal: {p!r}")


# ---------------------------------------------------------------------------
# config / env parsing


def load_event_config(cfg_abs: Path):
    """Parse an event config. Returns dict(event_id, starts_at_utc, lane,
    capture_only). Every refusal here is a staging bug that must not survive
    into a run plan (casino143 invariants 8 and 10)."""
    raw = cfg_abs.read_bytes()
    try:
        cfg = tomllib.loads(raw.decode("utf-8", errors="replace"))
    except tomllib.TOMLDecodeError as e:
        raise Refusal(f"config {cfg_abs.name} does not parse as TOML: {e}")

    event = cfg.get("event")
    if not isinstance(event, dict):
        raise Refusal(f"config {cfg_abs.name} has no [event] section")
    event_id = event.get("id", "")
    validate_event_id(event_id)

    starts_at = event.get("starts_at", "")
    if not isinstance(starts_at, str) or not starts_at:
        raise Refusal(f"config {cfg_abs.name} has no [event].starts_at")
    if starts_at == PLACEHOLDER_STARTS_AT:
        raise Refusal(
            f"config {cfg_abs.name} carries the prepare-template placeholder"
            f" starts_at {starts_at} — set the real call time"
        )
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if re.match(r"\s*starts_at\s*=", line) and "TODO" in line:
            raise Refusal(
                f"config {cfg_abs.name} starts_at line carries a TODO marker"
            )
    try:
        parsed = dt.datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    except ValueError:
        raise Refusal(
            f"config {cfg_abs.name} starts_at {starts_at!r} is unparseable"
        )
    if parsed.tzinfo is None:
        raise Refusal(
            f"config {cfg_abs.name} starts_at {starts_at!r} is timezone-naive"
        )
    starts_utc = parsed.astimezone(dt.timezone.utc)

    sources = cfg.get("sources") or []
    kinds = {s.get("kind") for s in sources if isinstance(s, dict)}
    if "twilio_stream" in kinds:
        lane = "phone"
    elif kinds and kinds <= {"pipe"}:
        lane = "webcast"
    else:
        raise Refusal(
            f"config {cfg_abs.name}: unknown ingress lane for [[sources]]"
            f" kinds {sorted(k for k in kinds if k)!r} — refusing to guess"
        )

    keywords = cfg.get("keywords") or []
    capture_only = not keywords
    kalshi = cfg.get("kalshi") or {}
    kalshi_env = kalshi.get("env", "prod")
    if kalshi_env not in ("prod", "demo"):
        raise Refusal(f"config {cfg_abs.name}: unsupported Kalshi env {kalshi_env!r}")
    market_events = {
        str(k.get("market_ticker", "")).rsplit("-", 1)[0]
        for k in keywords if isinstance(k, dict) and "-" in str(k.get("market_ticker", ""))
    }
    if not capture_only and len(market_events) != 1:
        raise Refusal(
            f"config {cfg_abs.name}: executable keywords must share one market event"
        )
    return {
        "event_id": event_id,
        "starts_at_utc": starts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "starts_compact": starts_utc.strftime("%Y%m%dT%H%M%SZ"),
        "lane": lane,
        "capture_only": capture_only,
        "kalshi_env": kalshi_env,
        "market_event_ticker": next(iter(market_events), "capture-only"),
    }


def parse_event_env(env_abs: Path):
    """Regex scan — never sourced. Returns dict(config_path, event_date,
    account_profile, violations=[(var, prefix)]).

    EARSHOT_ACCOUNT_PROFILE is the explicit immutable-identity locator. A
    literal ACCOUNT_ENV remains a compatibility fallback for older generic
    envs; computed ACCOUNT_ENV search loops are deliberately not evaluated.
    """
    text = env_abs.read_text(errors="replace")
    cp = ENV_CONFIG_RE.search(text)
    ed = ENV_DATE_RE.search(text)
    profile = ENV_ACCOUNT_PROFILE_RE.search(text)
    account = ENV_ACCOUNT_RE.search(text)
    violations = [(m.group(1), m.group(2)) for m in HOST_ABS_RE.finditer(text)]
    return {
        "config_path": cp.group(1) if cp else None,
        "event_date": ed.group(1) if ed else None,
        "account_profile": (
            profile.group(1) if profile else (account.group(1) if account else None)
        ),
        "violations": violations,
    }


def check_env_portable(env_abs: Path, parsed):
    if parsed["violations"]:
        names = ", ".join(f"{v}={p}…" for v, p in parsed["violations"])
        raise Refusal(
            f"event env {env_abs.name} carries host-absolute paths ({names})"
            " — a box-executed env with a laptop path killed both 2026-08-11"
            " events; use $HOME or ${EARSHOT_REPO} forms"
        )


def resolve_execution_identity(repo, cfg_abs, env_abs, parsed, host_id,
                               account_fingerprint, identity_helper,
                               earshot_bin):
    """Return the explicit or externally provisioned (host, fingerprint).

    The generated event env is never sourced. The helper sources only the
    named external account env, re-derives the exact arm-selected signer using
    the pinned binary, compares it with the provisioned profile, and returns
    non-secret identity JSON. No network request is involved.
    """
    if bool(host_id) != bool(account_fingerprint):
        raise Refusal(
            "execution identity override is all-or-nothing: supply both"
            " --host-id and --account-fingerprint, or neither"
        )
    if host_id and account_fingerprint:
        if not HOST_ID_RE.fullmatch(host_id):
            raise Refusal("--host-id is not a stable 1-64 character target ID")
        if not FINGERPRINT_RE.fullmatch(account_fingerprint):
            raise Refusal("--account-fingerprint must be sha256:<64 lowercase hex>")
        return host_id, account_fingerprint

    if env_abs is None or parsed is None:
        raise Refusal(
            "execution-enabled plan needs an event env with literal"
            " EARSHOT_ACCOUNT_PROFILE (or explicit identity overrides)"
        )
    locator = parsed.get("account_profile")
    if not locator:
        raise Refusal(
            f"event env {env_abs.name} has no literal EARSHOT_ACCOUNT_PROFILE"
            " or ACCOUNT_ENV"
        )
    if not (
        locator.startswith("$HOME/")
        or locator.startswith("${HOME}/")
        or locator.startswith("~/")
    ):
        raise Refusal(
            f"event env {env_abs.name} account profile must be portable"
            " ($HOME/..., ${HOME}/..., or ~/...); generated envs must expose"
            " EARSHOT_ACCOUNT_PROFILE instead of a computed ACCOUNT_ENV"
        )

    helper = Path(identity_helper) if identity_helper else (
        repo / "scripts" / "execution-profile.sh"
    )
    if not helper.is_absolute():
        helper = (repo / helper).resolve()
    if not helper.is_file() or not os.access(helper, os.X_OK):
        raise Refusal(f"execution identity helper is not executable: {helper}")
    binary = Path(earshot_bin) if earshot_bin else (
        repo / "target" / "release" / "earshot"
    )
    if not binary.is_absolute():
        binary = (repo / binary).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise Refusal(f"pinned earshot binary is not executable: {binary}")

    result = subprocess.run(
        [
            str(helper), "resolve", "--account", locator,
            "--config", str(cfg_abs), "--bin", str(binary),
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        hint = detail[-1] if detail else f"exit {result.returncode}"
        raise Refusal(f"execution profile check failed for {env_abs.name}: {hint}")
    try:
        identity = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise Refusal("execution identity helper did not emit one JSON object")
    expected = {
        "schema", "kind", "execution_host_id", "account_fingerprint"
    }
    if not isinstance(identity, dict) or set(identity) != expected:
        raise Refusal("execution identity helper returned an unsupported shape")
    resolved_host = identity.get("execution_host_id")
    resolved_fingerprint = identity.get("account_fingerprint")
    if identity.get("schema") != 1 or identity.get("kind") != "execution-identity":
        raise Refusal("execution identity helper returned an unsupported schema/kind")
    if not isinstance(resolved_host, str) or not HOST_ID_RE.fullmatch(resolved_host):
        raise Refusal("execution identity helper returned an invalid target host ID")
    if (
        not isinstance(resolved_fingerprint, str)
        or not FINGERPRINT_RE.fullmatch(resolved_fingerprint)
    ):
        raise Refusal("execution identity helper returned an invalid fingerprint")
    return resolved_host, resolved_fingerprint


# ---------------------------------------------------------------------------
# plan


def build_plan(repo, event_id, run_id, cfg_info, cfg_rel, cfg_sha,
               env_rel, env_sha, host_id, account_fingerprint):
    required = ["prepared"]
    required.append(
        "admission_resolved" if cfg_info["lane"] == "phone" else "access_acquired"
    )
    if not cfg_info["capture_only"]:
        required.append("study_completed")
    plan = {
        "schema": PLAN_SCHEMA,
        "kind": "run-plan",
        "event_id": event_id,
        "run_id": run_id,
        "created_utc": now_utc(),
        "starts_at_utc": cfg_info["starts_at_utc"],
        "lane": cfg_info["lane"],
        "execution_enabled": not cfg_info["capture_only"],
        "kalshi_env": cfg_info["kalshi_env"],
        "market_event_ticker": cfg_info["market_event_ticker"],
        "execution_host_id": host_id,
        "capture_only": cfg_info["capture_only"],
        "config_path": cfg_rel,
        "config_sha256": cfg_sha,
        "event_env_path": env_rel,
        "event_env_sha256": env_sha,
        "required_prep_receipts": required,
        "artifacts": dict(ARTIFACT_DESTINATIONS),
        "account_fingerprint": account_fingerprint,
        "canonical_state_root": "state",
    }
    plan["plan_sha256"] = hash_of(plan, "plan_sha256")
    return plan


def plan_identity(plan):
    return {
        k: v
        for k, v in plan.items()
        if k not in ("created_utc", "plan_sha256")
    }


# ---------------------------------------------------------------------------
# ledger


class Ledger:
    """The only writer of control/receipts.jsonl. All appends run under an
    exclusive flock on the receipts fd itself (released on close/death);
    readers take a shared flock."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.control = run_dir / "control"
        self.plan_path = self.control / "run-plan.json"
        self.receipts_path = self.control / "receipts.jsonl"
        self.status_path = self.control / "status.json"

    # -- plan ---------------------------------------------------------------

    def load_plan(self):
        if not self.plan_path.is_file():
            raise Refusal(f"no run plan at {self.plan_path}")
        try:
            plan = json.loads(self.plan_path.read_text())
        except json.JSONDecodeError as e:
            raise IntegrityError(f"run plan does not parse: {e}")
        self._validate_plan_shape(plan)
        if plan.get("plan_sha256") != hash_of(plan, "plan_sha256"):
            raise IntegrityError(
                f"run plan self-hash mismatch at {self.plan_path} — the plan"
                " was mutated after init"
            )
        if self.run_dir.name != plan.get("run_id") or (
            self.run_dir.parent.name != plan.get("event_id")
        ):
            raise IntegrityError(
                "run plan identity does not match its directory"
                f" ({self.run_dir.parent.name}/{self.run_dir.name} vs"
                f" {plan.get('event_id')}/{plan.get('run_id')})"
            )
        return plan

    def _validate_plan_shape(self, plan):
        """Validate every identity-bearing field before trusting the plan.

        `plan_sha256` proves bytes did not change after publication. Shape and
        semantic checks prove those immutable bytes describe this protocol.
        """
        if not isinstance(plan, dict):
            raise IntegrityError("run plan is not a JSON object")
        expected = {
            "schema", "kind", "event_id", "run_id", "created_utc",
            "starts_at_utc", "lane", "execution_enabled", "kalshi_env",
            "market_event_ticker", "execution_host_id", "capture_only",
            "config_path", "config_sha256", "event_env_path",
            "event_env_sha256", "required_prep_receipts", "artifacts",
            "account_fingerprint", "canonical_state_root", "plan_sha256",
        }
        got = set(plan)
        if got != expected:
            raise IntegrityError(
                f"run plan fields do not match schema {PLAN_SCHEMA}"
                f" (missing={sorted(expected - got)}, unknown={sorted(got - expected)})"
            )
        if plan["schema"] != PLAN_SCHEMA or plan["kind"] != "run-plan":
            raise IntegrityError(
                f"unsupported run plan schema/kind"
                f" ({plan['schema']!r}/{plan['kind']!r})"
            )
        try:
            validate_event_id(plan["event_id"])
            validate_run_id(plan["run_id"])
        except Refusal as e:
            raise IntegrityError(str(e))
        if plan["lane"] not in ("phone", "webcast"):
            raise IntegrityError(f"unsupported run plan lane {plan['lane']!r}")
        if type(plan["execution_enabled"]) is not bool or type(plan["capture_only"]) is not bool:
            raise IntegrityError("run plan execution_enabled/capture_only must be booleans")
        if plan["execution_enabled"] == plan["capture_only"]:
            raise IntegrityError(
                "run plan execution_enabled must be the inverse of capture_only"
            )
        if plan["kalshi_env"] not in ("prod", "demo"):
            raise IntegrityError(f"unsupported Kalshi environment {plan['kalshi_env']!r}")
        for field in ("created_utc", "starts_at_utc"):
            value = plan[field]
            if not isinstance(value, str):
                raise IntegrityError(f"run plan {field} must be a UTC timestamp")
            try:
                parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise IntegrityError(f"run plan {field} is unparseable")
            if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
                raise IntegrityError(f"run plan {field} must be UTC")
        for field in ("config_path", "canonical_state_root"):
            try:
                validate_rel_artifact(plan[field])
            except (Refusal, TypeError) as e:
                raise IntegrityError(f"invalid run plan {field}: {e}")
        if not re.fullmatch(r"[0-9a-f]{64}", plan["config_sha256"] or ""):
            raise IntegrityError("run plan config_sha256 is not lowercase SHA-256")
        env_path, env_hash = plan["event_env_path"], plan["event_env_sha256"]
        if (env_path is None) != (env_hash is None):
            raise IntegrityError("run plan event env path/hash must appear together")
        if env_path is not None:
            try:
                validate_rel_artifact(env_path)
            except (Refusal, TypeError) as e:
                raise IntegrityError(f"invalid run plan event_env_path: {e}")
            if not re.fullmatch(r"[0-9a-f]{64}", env_hash or ""):
                raise IntegrityError("run plan event_env_sha256 is not lowercase SHA-256")
        required = plan["required_prep_receipts"]
        if not isinstance(required, list) or not required or not all(
            isinstance(x, str) for x in required
        ) or len(required) != len(set(required)):
            raise IntegrityError("run plan required_prep_receipts must be unique strings")
        lane_required = [
            "prepared",
            "admission_resolved" if plan["lane"] == "phone" else "access_acquired",
        ]
        if not plan["capture_only"]:
            lane_required.append("study_completed")
        if required != lane_required:
            raise IntegrityError(
                "run plan required_prep_receipts do not match lane/posture"
            )
        artifacts = plan["artifacts"]
        if not isinstance(artifacts, dict) or set(artifacts) != set(ARTIFACT_DESTINATIONS):
            raise IntegrityError(
                f"run plan artifact keys do not match schema {PLAN_SCHEMA}"
            )
        values = []
        for name, relative in artifacts.items():
            try:
                validate_rel_artifact(relative)
            except (Refusal, TypeError) as e:
                raise IntegrityError(f"invalid artifacts.{name}: {e}")
            values.append(relative)
        if len(values) != len(set(values)):
            raise IntegrityError("run plan artifact destinations are not unique")
        for field in ("market_event_ticker", "execution_host_id", "account_fingerprint"):
            if not isinstance(plan[field], str) or not plan[field]:
                raise IntegrityError(f"run plan {field} must be a non-empty string")
        if plan["capture_only"]:
            if plan["market_event_ticker"] != "capture-only":
                raise IntegrityError("capture-only run plan identity is inconsistent")
        elif not re.fullmatch(r"sha256:[0-9a-f]{64}", plan["account_fingerprint"]):
            raise IntegrityError("run plan account_fingerprint is not sha256:<64 hex>")
        if not re.fullmatch(r"[0-9a-f]{64}", plan["plan_sha256"] or ""):
            raise IntegrityError("run plan plan_sha256 is not lowercase SHA-256")

    # -- receipts parsing ---------------------------------------------------

    def _parse(self, data: bytes, plan):
        """Returns (receipts, torn|None). torn carries the recoverable-tail
        diagnosis; every receipt in the returned list parsed AND chained."""
        if not data:
            return [], None
        raw_lines = data.split(b"\n")
        trailing = raw_lines[-1]
        complete = raw_lines[:-1]
        torn = None
        offset_of = []  # byte offset just past each complete line's newline
        off = 0
        for ln in complete:
            off += len(ln) + 1
            offset_of.append(off)
        if trailing != b"":
            torn = {"reason": "no trailing newline", "fragment_bytes": len(trailing)}

        receipts = []
        for i, ln in enumerate(complete):
            try:
                rec = json.loads(ln)
                self._verify_link(rec, receipts, plan, ln)
                receipts.append(rec)
            except (json.JSONDecodeError, IntegrityError) as e:
                raise IntegrityError(
                    f"receipt line {i + 1} is invalid mid-chain: {e}"
                )
        valid_end = offset_of[len(receipts) - 1] if receipts else 0
        if torn is not None:
            torn["last_valid_sequence"] = (
                receipts[-1]["sequence"] if receipts else 0
            )
            torn["truncate_to"] = valid_end
        return receipts, torn

    def _verify_link(self, rec, prior, plan, raw_line: bytes):
        if not isinstance(rec, dict):
            raise IntegrityError("receipt is not an object")
        if rec.get("schema") != RECEIPT_SCHEMA:
            raise IntegrityError(f"receipt schema {rec.get('schema')!r}")
        if rec.get("kind") not in ("transition", "compat"):
            raise IntegrityError(f"receipt kind {rec.get('kind')!r}")
        expected = {
            "schema", "kind", "event", "run", "sequence", "ts", "actor",
            "host", "pid", "transition", "action", "outcome", "detail",
            "log", "artifacts", "previous_sha256", "receipt_sha256",
        }
        got = set(rec)
        if got != expected:
            raise IntegrityError(
                "receipt fields do not match schema 1"
                f" (missing={sorted(expected - got)}, unknown={sorted(got - expected)})"
            )
        if rec.get("event") != plan["event_id"] or rec.get("run") != plan["run_id"]:
            raise IntegrityError(
                "receipt event/run does not match the plan"
                f" ({rec.get('event')}/{rec.get('run')})"
            )
        want_seq = (prior[-1]["sequence"] + 1) if prior else 1
        if rec.get("sequence") != want_seq:
            raise IntegrityError(
                f"receipt sequence {rec.get('sequence')} != expected {want_seq}"
            )
        want_prev = prior[-1]["receipt_sha256"] if prior else plan["plan_sha256"]
        if rec.get("previous_sha256") != want_prev:
            raise IntegrityError(
                f"receipt {want_seq} previous_sha256 broken (chain anchored"
                " at plan_sha256)"
            )
        if rec.get("receipt_sha256") != hash_of(rec, "receipt_sha256"):
            raise IntegrityError(f"receipt {want_seq} self-hash mismatch")
        try:
            parsed_ts = dt.datetime.fromisoformat(rec["ts"].replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            raise IntegrityError(f"receipt {want_seq} timestamp is invalid")
        if parsed_ts.tzinfo is None or parsed_ts.utcoffset() != dt.timedelta(0):
            raise IntegrityError(f"receipt {want_seq} timestamp is not UTC")
        if not isinstance(rec["actor"], str) or not rec["actor"]:
            raise IntegrityError(f"receipt {want_seq} actor is empty")
        if not isinstance(rec["host"], str) or not rec["host"]:
            raise IntegrityError(f"receipt {want_seq} host is empty")
        if type(rec["pid"]) is not int or rec["pid"] <= 0:
            raise IntegrityError(f"receipt {want_seq} pid is invalid")
        if not isinstance(rec["detail"], str):
            raise IntegrityError(f"receipt {want_seq} detail is not a string")
        if rec["log"] is not None and not isinstance(rec["log"], str):
            raise IntegrityError(f"receipt {want_seq} log is invalid")
        if not isinstance(rec["artifacts"], list) or not all(
            isinstance(x, str) for x in rec["artifacts"]
        ):
            raise IntegrityError(f"receipt {want_seq} artifacts are invalid")
        for artifact in rec["artifacts"]:
            try:
                validate_rel_artifact(artifact)
            except Refusal as e:
                raise IntegrityError(f"receipt {want_seq} artifact is invalid: {e}")
        if rec["kind"] == "transition":
            if rec["transition"] not in ALL_TRANSITIONS or rec["action"] is not None:
                raise IntegrityError(f"receipt {want_seq} transition shape is invalid")
            if rec["outcome"] not in TYPED_OUTCOMES:
                raise IntegrityError(f"receipt {want_seq} typed outcome is invalid")
        else:
            if rec["transition"] is not None or not isinstance(rec["action"], str) or not rec["action"]:
                raise IntegrityError(f"receipt {want_seq} compat shape is invalid")
            if rec["outcome"] not in COMPAT_OUTCOMES:
                raise IntegrityError(f"receipt {want_seq} compat outcome is invalid")
        # the disk line must BE the canonical serialization
        if canon_bytes(rec) != raw_line:
            raise IntegrityError(
                f"receipt {want_seq} is not in canonical serialized form"
            )

    def read(self, plan, shared_lock=True):
        """Read + verify under LOCK_SH. Raises RecoverableTail on torn tail."""
        if not self.receipts_path.is_file():
            return []
        fd = os.open(self.receipts_path, os.O_RDONLY)
        try:
            if shared_lock:
                fcntl.flock(fd, fcntl.LOCK_SH)
            data = os.read(fd, os.fstat(fd).st_size or 1)
            while True:
                more = os.read(fd, 1 << 20)
                if not more:
                    break
                data += more
        finally:
            os.close(fd)
        receipts, torn = self._parse(data, plan)
        if torn is not None:
            raise RecoverableTail(
                "receipts tail is torn"
                f" ({torn['reason']}); {len(receipts)} valid receipts"
                f" preserved; repair: truncate {self.receipts_path} to"
                f" {torn['truncate_to']} bytes",
                torn["last_valid_sequence"],
                torn["truncate_to"],
            )
        return receipts

    def read_tolerant(self, plan):
        """For status: returns (receipts, recoverable_tail: bool)."""
        try:
            return self.read(plan), False
        except RecoverableTail as e:
            # re-read the valid prefix only
            data = self.receipts_path.read_bytes()[: e.truncate_to]
            receipts, _ = self._parse(data, plan)
            return receipts, True

    # -- append -------------------------------------------------------------

    def append(self, plan, repo, gate, build_fields, typed):
        """Locked read-validate-append. gate(plan, receipts) returns
        "append" or a no-op message; it raises Refusal for illegal
        transitions (file untouched)."""
        self.control.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            self.receipts_path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.lseek(fd, 0, os.SEEK_SET)
            chunks = []
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
            receipts, torn = self._parse(data, plan)
            if torn is not None:
                raise RecoverableTail(
                    "refusing to append after a torn tail (the fragment is"
                    f" evidence); {torn['last_valid_sequence']} valid"
                    f" receipts preserved; repair: truncate"
                    f" {self.receipts_path} to {torn['truncate_to']} bytes",
                    torn["last_valid_sequence"],
                    torn["truncate_to"],
                )
            if typed:
                self._check_pinned_inputs(plan, repo)
            verdict = gate(plan, receipts)
            if verdict != "append":
                return None, verdict, receipts
            fields = build_fields()
            rec = dict(fields)
            rec["schema"] = RECEIPT_SCHEMA
            rec["event"] = plan["event_id"]
            rec["run"] = plan["run_id"]
            rec["sequence"] = (receipts[-1]["sequence"] + 1) if receipts else 1
            rec["previous_sha256"] = (
                receipts[-1]["receipt_sha256"] if receipts else plan["plan_sha256"]
            )
            rec["receipt_sha256"] = hash_of(rec, "receipt_sha256")
            line = canon_bytes(rec) + b"\n"
            written = os.write(fd, line)
            if written != len(line):
                raise IntegrityError(
                    f"short write ({written}/{len(line)} bytes) on receipts"
                )
            os.fsync(fd)
            receipts.append(rec)
            self.write_snapshot(plan, receipts, recoverable_tail=False)
            return rec, None, receipts
        finally:
            os.close(fd)

    def _check_pinned_inputs(self, plan, repo):
        cfg = repo / plan["config_path"]
        if not cfg.is_file():
            raise Refusal(f"pinned config {plan['config_path']} is missing")
        if file_sha256(cfg) != plan["config_sha256"]:
            raise Refusal(
                f"pinned config {plan['config_path']} has drifted from the"
                " plan hash — a changed config is a NEW run: re-init (new"
                " RUN_ID) and record a terminal receipt on this one"
            )
        if plan["event_env_path"]:
            env = repo / plan["event_env_path"]
            if not env.is_file():
                raise Refusal(
                    f"pinned event env {plan['event_env_path']} is missing"
                )
            if file_sha256(env) != plan["event_env_sha256"]:
                raise Refusal(
                    f"pinned event env {plan['event_env_path']} has drifted"
                    " from the plan hash"
                )

    # -- snapshot -----------------------------------------------------------

    def write_snapshot(self, plan, receipts, recoverable_tail):
        state = reduce_state(plan, receipts)
        snap = {
            "schema": RECEIPT_SCHEMA,
            "derived": True,
            "authority": "control/receipts.jsonl",
            "generated_utc": now_utc(),
            "as_of_sequence": receipts[-1]["sequence"] if receipts else 0,
            "as_of_receipt_sha256": (
                receipts[-1]["receipt_sha256"] if receipts else plan["plan_sha256"]
            ),
            "event": plan["event_id"],
            "run": plan["run_id"],
            "lane": plan["lane"],
            "host": plan["execution_host_id"],
            "state": state,
            "integrity": {
                "ok": not recoverable_tail,
                "recoverable_tail": recoverable_tail,
                "receipts": len(receipts),
            },
        }
        atomic_write_durable(
            self.status_path,
            (json.dumps(snap, indent=2, sort_keys=True) + "\n").encode(),
        )


# ---------------------------------------------------------------------------
# typed-state reducer (pure; consumes kind=="transition" outcome=="ok" only)


def reduce_state(plan, receipts):
    reached = {}
    for r in receipts:
        if r.get("kind") != "transition" or r.get("outcome") != "ok":
            continue
        t = r.get("transition")
        if t and t not in reached:
            reached[t] = {"sequence": r["sequence"], "ts": r["ts"]}
    terminal = None
    for t in TERMINALS:
        if t in reached:
            terminal = t
            break
    closed = "archived" in reached
    active = terminal is None and not closed
    pending = {}
    for t, req in (("preflight_passed", plan["required_prep_receipts"]),
                   ("launched", PREREQ_ALL["launched"])):
        if t in reached:
            continue
        missing = [x for x in req if x not in reached]
        if missing and len(missing) < len(req):
            pending[t] = missing
    return {
        "reached": [
            {"transition": t, **info}
            for t, info in sorted(reached.items(), key=lambda kv: kv[1]["sequence"])
        ],
        "pending": pending,
        "terminal": (
            {"transition": terminal, "sequence": reached[terminal]["sequence"]}
            if terminal
            else None
        ),
        "archived": closed,
        "active": active,
    }


def reached_set(receipts):
    out = set()
    for r in receipts:
        if r.get("kind") == "transition" and r.get("outcome") == "ok":
            out.add(r.get("transition"))
    return out


def prerequisite_reached(name, reached):
    return name in reached or bool(PREREQ_EQUIVALENTS.get(name, set()) & reached)


def gate_typed(transition, outcome, plan):
    """Returns the gate callable for Ledger.append."""

    def gate(plan_, receipts):
        if transition not in ALL_TRANSITIONS:
            raise Refusal(f"unknown transition {transition!r}")
        lane_only = LANE_ONLY.get(transition)
        if lane_only and plan_["lane"] != lane_only:
            raise Refusal(
                f"transition {transition} is {lane_only}-lane only; this run"
                f" is {plan_['lane']}"
            )
        reached = reached_set(receipts)
        if outcome != "ok":
            if "archived" in reached:
                raise Refusal("run is archived — no further receipts")
            return "append"
        if transition in reached:
            return f"idempotent: {transition} already recorded"
        if "archived" in reached:
            raise Refusal("run is archived — no further receipts")
        terminal = next((t for t in TERMINALS if t in reached), None)
        if terminal is not None:
            if transition not in POST_TERMINAL_ALLOWED[terminal]:
                raise Refusal(
                    f"run is terminal ({terminal}); only"
                    f" {sorted(POST_TERMINAL_ALLOWED[terminal])} may follow"
                )
        if transition in TERMINALS:
            if terminal is not None:
                raise Refusal(f"run is already terminal ({terminal})")
            if transition in ("descope", "blocked") and "launched" in reached:
                raise Refusal(
                    f"{transition} is a pre-launch terminal; after launch use"
                    " aborted or failed"
                )
            return "append"
        if transition in PREREQ_ANY:
            anyreq = PREREQ_ANY[transition]
            if not any(x in reached for x in anyreq):
                raise Refusal(
                    f"illegal transition {transition}: requires one of"
                    f" {anyreq}, reached={sorted(reached)}"
                )
            if transition == "evidence_consolidated" and "evidence_pulled" in reached:
                return "idempotent: evidence_pulled already proves evidence_consolidated"
            return "append"
        allreq = (
            plan_["required_prep_receipts"]
            if transition == "preflight_passed"
            else PREREQ_ALL[transition]
        )
        missing = [x for x in allreq if not prerequisite_reached(x, reached)]
        if missing:
            raise Refusal(
                f"illegal transition {transition}: missing prerequisites"
                f" {missing}"
            )
        return "append"

    return gate


def gate_compat(action=None, detail=None, idempotent=False):
    """Return the compatibility-observation gate.

    Ordinary legacy observations remain append-every-time. Structured clients
    may opt into an action-unique append: the same action+detail is a no-op,
    while reusing that action with different bytes is an integrity refusal.
    The check runs under the lifecycle ledger's exclusive append lock, giving
    alert requests/delivery attempts/acknowledgements an atomic dedupe seam
    without letting them advance typed lifecycle state.
    """

    def gate(plan, receipts):
        if "archived" in reached_set(receipts):
            raise Refusal("run is archived — no further receipts")
        if not idempotent:
            return "append"
        matches = [
            receipt
            for receipt in receipts
            if receipt.get("kind") == "compat" and receipt.get("action") == action
        ]
        if not matches:
            return "append"
        if all(receipt.get("detail") == detail for receipt in matches):
            return f"idempotent: compatibility action {action!r} already recorded"
        raise Refusal(
            f"compatibility action {action!r} already exists with different detail"
        )

    return gate


# ---------------------------------------------------------------------------
# run resolution


def runs_root(args, repo):
    if getattr(args, "run_root", None):
        return Path(args.run_root).resolve()
    return repo / "var" / "runs"


def find_runs(root: Path, event_id: str):
    """Return canonical schema-3 attempts, preserving older attempts in place.

    Event-scoped commands select only the current lifecycle protocol.  A
    schema-1/2 RunPlan is immutable historical evidence, not an active
    candidate and not an integrity failure merely because a schema-3 attempt
    now coexists beside it.  Malformed files and unknown protocol shapes still
    fail closed; selected schema-3 plans receive the full ledger validation in
    ``run_activity``.
    """
    validate_event_id(event_id)
    ev_dir = root / event_id
    if not ev_dir.is_dir():
        return []
    out = []
    for child in sorted(ev_dir.iterdir()):
        if not child.is_dir():
            raise IntegrityError(
                f"unexpected non-directory run candidate under {ev_dir}: {child.name}"
            )
        try:
            validate_run_id(child.name)
        except Refusal as e:
            raise IntegrityError(
                f"invalid run directory candidate {child}: {e}"
            )
        plan_path = child / "control" / "run-plan.json"
        if not plan_path.is_file() or plan_path.is_symlink():
            raise IntegrityError(
                f"run candidate {child} has no control/run-plan.json"
            )
        try:
            candidate = json.loads(plan_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrityError(
                f"run candidate {child} run-plan.json does not parse: {exc}"
            ) from exc
        if not isinstance(candidate, dict):
            raise IntegrityError(
                f"run candidate {child} run-plan.json is not an object"
            )
        schema, kind = candidate.get("schema"), candidate.get("kind")
        if schema in (1, 2) and kind == "run-plan":
            continue
        if schema != PLAN_SCHEMA or kind != "run-plan":
            raise IntegrityError(
                f"run candidate {child} has unsupported schema/kind"
                f" ({schema!r}/{kind!r})"
            )
        out.append(child)
    return out


def run_activity(run_dir: Path):
    """(plan, receipts, active). Tolerant of a torn tail (prefix decides)."""
    ledger = Ledger(run_dir)
    plan = ledger.load_plan()
    receipts, _torn = ledger.read_tolerant(plan)
    reached = reached_set(receipts)
    active = not (set(TERMINALS) & reached) and "archived" not in reached
    return plan, receipts, active


def resolve_run_dir(args, repo, for_command):
    if getattr(args, "run_dir", None):
        return Path(args.run_dir).resolve()
    event_id = args.event
    root = runs_root(args, repo)
    runs = find_runs(root, event_id)
    if not runs:
        if for_command == "compat":
            return None
        raise Refusal(f"no runs for {event_id} under {root}")
    actives = []
    for rd in runs:
        _, _, active = run_activity(rd)
        if active:
            actives.append(rd)
    if len(actives) == 1:
        return actives[0]
    if len(actives) > 1:
        raise Refusal(
            f"ambiguous: {len(actives)} active runs for {event_id}:"
            f" {[r.name for r in actives]}"
        )
    if for_command == "compat":
        return None
    return runs[-1]  # RUN_ID sorts chronologically (starts_at prefix)


# ---------------------------------------------------------------------------
# commands


def init_one(repo, config, event_env, event, host_id, account_fingerprint,
             run_id, run_root, identity_helper=None, earshot_bin=None):
    cfg_abs, cfg_rel = resolve_in_repo(config, repo, "config")
    cfg_info = load_event_config(cfg_abs)
    if event and event != cfg_info["event_id"]:
        raise Refusal(
            f"--event {event} does not match config [event].id"
            f" {cfg_info['event_id']}"
        )
    env_abs = env_rel = env_sha = parsed_env = None
    if event_env:
        env_abs, env_rel = resolve_in_repo(event_env, repo, "event env")
        parsed_env = parse_event_env(env_abs)
        check_env_portable(env_abs, parsed_env)
        if parsed_env["config_path"]:
            declared, _ = resolve_in_repo(
                parsed_env["config_path"], repo, "env CONFIG_PATH"
            )
            if declared != cfg_abs:
                raise Refusal(
                    f"event env {env_abs.name} CONFIG_PATH points at"
                    f" {parsed_env['config_path']}, not {cfg_rel}"
                )
        env_sha = file_sha256(env_abs)
    cfg_sha = file_sha256(cfg_abs)
    # Schema belongs in the default identity.  A protocol upgrade must never
    # resolve to an old schema's directory and discover the collision only
    # after creating new evidence paths inside that immutable attempt.
    rid = run_id or (
        f"{cfg_info['starts_compact']}-p{PLAN_SCHEMA}-{cfg_sha[:12]}"
    )
    validate_run_id(rid)
    event_id = cfg_info["event_id"]
    run_dir = run_root / event_id / rid
    if not cfg_info["capture_only"]:
        host_id, account_fingerprint = resolve_execution_identity(
            repo, cfg_abs, env_abs, parsed_env, host_id,
            account_fingerprint, identity_helper, earshot_bin,
        )
    else:
        host_id = host_id or "capture-only"
        account_fingerprint = account_fingerprint or "capture-only"
    plan = build_plan(
        repo, event_id, rid, cfg_info, cfg_rel, cfg_sha, env_rel, env_sha,
        host_id, account_fingerprint,
    )
    ledger = Ledger(run_dir)

    # Existing attempt directories are immutable evidence.  Validate them
    # read-only before mkdir or O_CREAT.  In particular, an explicit --run-id
    # collision with a schema-1/schema-2 directory must not add schema-3
    # parents or a missing receipts file before it fails closed.
    existing_plan = None
    if run_dir.exists():
        if not run_dir.is_dir():
            raise IntegrityError(f"run path is not a directory: {run_dir}")
        if not ledger.plan_path.is_file():
            raise IntegrityError(
                f"existing run directory has no immutable plan: {run_dir}"
            )
        existing_plan = ledger.load_plan()
        if not ledger.receipts_path.is_file():
            raise IntegrityError(
                f"existing run directory has no receipts ledger:"
                f" {ledger.receipts_path}"
            )
        if plan_identity(existing_plan) != plan_identity(plan):
            diffs = [
                k
                for k in plan_identity(plan)
                if plan_identity(existing_plan).get(k) != plan_identity(plan)[k]
            ]
            raise Refusal(
                f"a DIFFERENT plan already exists at {ledger.plan_path}"
                f" (differs in: {diffs}); pass --run-id for a deliberate"
                " new attempt"
            )
    else:
        ledger.control.mkdir(parents=True, exist_ok=True)
        for relative in plan["artifacts"].values():
            (run_dir / relative).parent.mkdir(parents=True, exist_ok=True)

    open_flags = os.O_RDWR | os.O_APPEND
    if existing_plan is None:
        open_flags |= os.O_CREAT
    fd = os.open(ledger.receipts_path, open_flags, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if ledger.plan_path.is_file():
            existing = ledger.load_plan()
            if plan_identity(existing) != plan_identity(plan):
                diffs = [
                    k
                    for k in plan_identity(plan)
                    if plan_identity(existing).get(k) != plan_identity(plan)[k]
                ]
                raise Refusal(
                    f"a DIFFERENT plan already exists at {ledger.plan_path}"
                    f" (differs in: {diffs}); pass --run-id for a deliberate"
                    " new attempt"
                )
            plan = existing
            fresh = False
        else:
            atomic_write_durable(
                ledger.plan_path,
                (json.dumps(plan, indent=2, sort_keys=True) + "\n").encode(),
            )
            fresh = True
    finally:
        os.close(fd)
    # seq-1 receipt (idempotent when re-initing)
    rec, noop, _ = ledger.append(
        plan,
        repo,
        gate_typed("plan_compiled", "ok", plan),
        lambda: {
            "kind": "transition",
            "ts": now_utc(),
            "actor": "eventctl",
            "host": short_host(),
            "pid": os.getpid(),
            "transition": "plan_compiled",
            "action": None,
            "outcome": "ok",
            "detail": f"plan {'compiled' if fresh else 're-verified'} from"
                      f" {plan['config_path']}",
            "log": None,
            "artifacts": ["control/run-plan.json"],
        },
        typed=True,
    )
    return run_dir, ("initialized" if fresh else "already-initialized")


def cmd_init(args):
    repo = repo_root()
    run_root = (
        Path(args.run_root).resolve() if args.run_root else repo / "var" / "runs"
    )
    if args.run_id is not None:
        validate_run_id(args.run_id)
    run_dir, verdict = init_one(
        repo, args.config, args.event_env, args.event, args.host_id,
        args.account_fingerprint,
        args.run_id, run_root, args.identity_helper, args.earshot_bin,
    )
    print(f"{verdict}: {run_dir}")
    return 0


def cmd_record(args):
    repo = repo_root()
    run_dir = resolve_run_dir(args, repo, "record")
    ledger = Ledger(run_dir)
    plan = ledger.load_plan()
    for a in args.artifact or []:
        validate_rel_artifact(a)
    rec, noop, _ = ledger.append(
        plan,
        repo,
        gate_typed(args.transition, args.outcome, plan),
        lambda: {
            "kind": "transition",
            "ts": now_utc(),
            "actor": args.actor,
            "host": short_host(),
            "pid": os.getpid(),
            "transition": args.transition,
            "action": None,
            "outcome": args.outcome,
            "detail": args.detail,
            "log": args.log,
            "artifacts": args.artifact or [],
        },
        typed=True,
    )
    print(noop if noop else
          f"recorded: {args.transition} ({args.outcome}) seq {rec['sequence']}")
    return 0


def cmd_compat(args):
    repo = repo_root()
    if args.outcome not in COMPAT_OUTCOMES:
        raise Refusal(
            f"compat outcome {args.outcome!r} not in {COMPAT_OUTCOMES}"
        )
    run_dir = resolve_run_dir(args, repo, "compat")
    if run_dir is None:
        print(f"no active run for {args.event}; observation not bridged")
        return 0
    ledger = Ledger(run_dir)
    plan = ledger.load_plan()
    rec, noop, _ = ledger.append(
        plan,
        repo,
        gate_compat(args.action, args.detail, args.idempotent),
        lambda: {
            "kind": "compat",
            "ts": now_utc(),
            "actor": args.actor,
            "host": short_host(),
            "pid": os.getpid(),
            "transition": None,
            "action": args.action,
            "outcome": args.outcome,
            "detail": args.detail,
            "log": args.log,
            "artifacts": [],
        },
        typed=False,
    )
    if noop:
        print(noop)
    else:
        print(f"bridged: {args.actor}/{args.action} ({args.outcome})"
              f" seq {rec['sequence']}")
    return 0


def status_payload(run_dir):
    ledger = Ledger(run_dir)
    plan = ledger.load_plan()
    receipts, torn = ledger.read_tolerant(plan)
    state = reduce_state(plan, receipts)
    return {
        "schema": RECEIPT_SCHEMA,
        "event": plan["event_id"],
        "run": plan["run_id"],
        "lane": plan["lane"],
        "host": plan["execution_host_id"],
        "authority": {
            "location": "execution_host" if "scheduled" in reached_set(receipts) else "local_prefix",
            "execution_host_id": plan["execution_host_id"],
            "as_of_sequence": receipts[-1]["sequence"] if receipts else 0,
            "as_of_receipt_sha256": (
                receipts[-1]["receipt_sha256"] if receipts else plan["plan_sha256"]
            ),
            "remote_read_through": False,
            "remote_status": (
                "stale_prefix_possible" if "scheduled" in reached_set(receipts)
                and state["active"] else "not_required"
            ),
        },
        "state": state,
        "integrity": {
            "ok": not torn,
            "recoverable_tail": torn,
            "receipts": len(receipts),
        },
    }


def cmd_status(args):
    repo = repo_root()
    run_dir = resolve_run_dir(args, repo, "status")
    payload = status_payload(run_dir)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    st = payload["state"]
    print(
        f"run {payload['event']} / {payload['run']}"
        f" ({payload['lane']}, host={payload['host']})"
    )
    authority = payload["authority"]
    if authority["remote_status"] == "stale_prefix_possible":
        print(
            "   authority: STALE_PREFIX_POSSIBLE — execution host owns the"
            " post-schedule tail; pull terminal evidence before grading"
        )
    else:
        print(f"   authority: {authority['location']}")
    for r in st["reached"]:
        print(f"   ✓ {r['transition']:<20} seq {r['sequence']:<3} {r['ts']}")
    for gate, missing in st["pending"].items():
        print(f"   … {gate} waiting on: {', '.join(missing)}")
    term = st["terminal"]
    print(
        f"   terminal: {term['transition'] if term else 'none'}"
        f"   archived: {'yes' if st['archived'] else 'no'}"
        f"   active: {'yes' if st['active'] else 'no'}"
    )
    integ = payload["integrity"]
    tail = " RECOVERABLE-TAIL" if integ["recoverable_tail"] else ""
    print(f"   integrity: {integ['receipts']} receipts{tail}")
    return 0


def verify_one(run_dir, repo):
    ledger = Ledger(run_dir)
    plan = ledger.load_plan()
    receipts = ledger.read(plan)  # raises RecoverableTail / IntegrityError
    ledger._check_pinned_inputs(plan, repo)
    return len(receipts)


def cmd_verify(args):
    repo = repo_root()
    if args.run_dir:
        targets = [Path(args.run_dir).resolve()]
    else:
        try:
            targets = find_runs(runs_root(args, repo), args.event)
        except (IntegrityError, Refusal) as e:
            # Verify is a reporting command: keep integrity failures on its
            # established stdout verdict surface instead of letting the
            # top-level refusal handler move them to stderr. Mutating commands
            # still fail before selecting an ambiguous/corrupt sibling.
            print(f"FAILED: {args.event} — {e}")
            return 1
        if not targets:
            raise Refusal(f"no runs for {args.event}")
    has_fail = has_recoverable = False
    for rd in targets:
        try:
            n = verify_one(rd, repo)
            print(f"OK: {rd} — {n} receipts, chain + pinned inputs verified")
        except RecoverableTail as e:
            print(f"RECOVERABLE: {rd} — {e}")
            has_recoverable = True
        except (IntegrityError, Refusal) as e:
            print(f"FAILED: {rd} — {e}")
            has_fail = True
    return 1 if has_fail else (3 if has_recoverable else 0)


def cmd_init_date(args):
    repo = repo_root()
    try:
        want = dt.date.fromisoformat(args.date)
    except ValueError:
        raise Refusal(f"--date {args.date!r} is not YYYY-MM-DD")
    run_root = (
        Path(args.run_root).resolve() if args.run_root else repo / "var" / "runs"
    )
    cfg_dir = repo / "configs"
    refusals = []
    lines = []

    # envs first: CONFIG_PATH is the literal truth, EVENT_DATE the day scope
    env_by_cfg = {}
    env_conflicts = {}
    env_errors = []
    for envp in sorted(cfg_dir.glob("*.env")):
        parsed = parse_event_env(envp)
        cp = parsed["config_path"]
        if not cp:
            continue
        cand = (repo / cp).resolve()
        if cand in env_by_cfg and env_by_cfg[cand][0] != envp:
            env_conflicts.setdefault(cand, [env_by_cfg[cand][0]]).append(envp)
            continue
        env_by_cfg[cand] = (envp, parsed)

    seen_events = {}
    selected = []
    for cfgp in sorted(cfg_dir.glob("*.toml")):
        if any(fnmatch(cfgp.name, g) for g in SKIP_CONFIG_GLOBS):
            continue
        try:
            raw = tomllib.loads(cfgp.read_text(errors="replace"))
        except tomllib.TOMLDecodeError as e:
            refusals.append(f"{cfgp.name}: TOML parse error: {e}")
            continue
        if not isinstance(raw.get("event"), dict) or not raw["event"].get("id"):
            continue  # non-event fixture
        ev_id_raw = raw["event"].get("id", "")
        sa_raw = raw["event"].get("starts_at", "")
        if "__" in str(ev_id_raw) or "__" in str(sa_raw):
            continue  # sed template (__EVENT_TICKER__ convention), never live
        env_entry = env_by_cfg.get(cfgp.resolve())
        env_date = env_entry[1]["event_date"] if env_entry else None
        # Date scoping is LOOSE (a stale fixture from another day must not
        # page every night); the strict placeholder/TODO/naive refusals run
        # only on the day's selected events, where they are staging bugs.
        loose_date = None
        sa = raw["event"].get("starts_at")
        if isinstance(sa, str):
            try:
                loose = dt.datetime.fromisoformat(sa.replace("Z", "+00:00"))
                if loose.tzinfo is None:
                    loose = loose.replace(tzinfo=dt.timezone.utc)
                loose_date = loose.astimezone(dt.timezone.utc).date().isoformat()
            except ValueError:
                loose_date = None
        scope = env_date if env_date is not None else loose_date
        if scope is None:
            # date unknowable: surface it (silent skips hid the 08-06 slate)
            refusals.append(
                f"{cfgp.name}: cannot determine an event date"
                f" (starts_at={sa!r}, no EVENT_DATE env)"
            )
            continue
        if scope != args.date:
            continue
        try:
            info = load_event_config(cfgp)
        except Refusal as e:
            refusals.append(f"{cfgp.name}: {e}")
            continue
        ev = info["event_id"]
        seen_events.setdefault(ev, []).append(cfgp)
        selected.append((cfgp, env_entry, info))

    for cand, envs in env_conflicts.items():
        try:
            rel = cand.relative_to(repo)
        except ValueError:
            rel = cand
        names = ", ".join(e.name for e in envs)
        refusals.append(
            f"{rel}: referenced by multiple env files ({names}) — duplicated"
            " staging is itself the bug"
        )

    for envp, parsed in (v for v in env_by_cfg.values()):
        if parsed["event_date"] != args.date:
            continue
        cand = (repo / parsed["config_path"]).resolve()
        if not cand.is_file():
            refusals.append(
                f"{envp.name}: CONFIG_PATH {parsed['config_path']} does not"
                " exist — the timer will fire and fail at call time"
            )

    for cfgp, env_entry, info in selected:
        ev = info["event_id"]
        if len(seen_events[ev]) > 1:
            continue  # reported once below
        if cfgp.resolve() in env_conflicts:
            continue  # conflict refusal already recorded; do not init
        env_arg = None
        if env_entry:
            env_arg = str(env_entry[0])
        try:
            run_dir, verdict = init_one(
                repo, str(cfgp), env_arg, None, args.host_id,
                args.account_fingerprint, None, run_root,
                args.identity_helper, args.earshot_bin,
            )
            lines.append(f"{ev}: {verdict} {run_dir}")
        except (Refusal, IntegrityError, RecoverableTail) as e:
            refusals.append(f"{ev}: {e}")

    for ev, paths in seen_events.items():
        if len(paths) > 1:
            refusals.append(
                f"{ev}: ambiguous — {len(paths)} config files claim this"
                f" event ({', '.join(p.name for p in paths)})"
            )

    for ln in lines:
        print(ln)
    for r in refusals:
        print(f"REFUSED: {r}")
    print(
        f"init-date {args.date}: {len(lines)} initialized/verified,"
        f" {len(refusals)} refused"
    )
    return 1 if refusals else 0


# ---------------------------------------------------------------------------
# argparse


def build_parser():
    p = argparse.ArgumentParser(
        prog="eventctl.py",
        description="typed event-run control plane (control.operate)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_root(sp):
        sp.add_argument("--run-root", default=None,
                        help="run root (default <repo>/var/runs)")

    def add_identity(sp):
        # Runtime and launchers use EARSHOT_EXECUTION_HOST. EARSHOT_HOST_ID is
        # retained as a compatibility fallback for existing invocations.
        sp.add_argument(
            "--host-id",
            default=(
                os.environ.get("EARSHOT_EXECUTION_HOST")
                or os.environ.get("EARSHOT_HOST_ID")
            ),
        )
        sp.add_argument(
            "--account-fingerprint",
            default=os.environ.get("EARSHOT_KALSHI_ACCOUNT_FINGERPRINT"),
        )
        sp.add_argument(
            "--identity-helper",
            default=os.environ.get("EARSHOT_IDENTITY_HELPER"),
            help="profile resolver (default scripts/execution-profile.sh)",
        )
        sp.add_argument(
            "--earshot-bin",
            default=os.environ.get("EARSHOT_BIN"),
            help="pinned binary used to re-derive account identity",
        )

    sp = sub.add_parser("init", help="compile an immutable run plan")
    sp.add_argument("--config", required=True)
    sp.add_argument("--event-env", default=None)
    sp.add_argument("--event", default=None,
                    help="assert the config's [event].id")
    add_identity(sp)
    sp.add_argument("--run-id", default=None)
    add_root(sp)
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("record", help="append a typed lifecycle transition")
    sp.add_argument("--run-dir", default=None)
    sp.add_argument("--event", default=None)
    sp.add_argument("--transition", required=True)
    sp.add_argument("--outcome", choices=TYPED_OUTCOMES, default="ok")
    sp.add_argument("--actor", default="eventctl")
    sp.add_argument("--detail", default="")
    sp.add_argument("--log", default=None)
    sp.add_argument("--artifact", action="append", default=[])
    add_root(sp)
    sp.set_defaults(fn=cmd_record)

    sp = sub.add_parser(
        "compat", help="bridge a legacy journal line (observation only)"
    )
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--event", default=None)
    g.add_argument("--run-dir", default=None)
    sp.add_argument("--actor", required=True)
    sp.add_argument("--action", required=True)
    sp.add_argument("--outcome", required=True)
    sp.add_argument("--detail", default="")
    sp.add_argument("--log", default=None)
    sp.add_argument(
        "--idempotent",
        action="store_true",
        help="append action once; same detail is a no-op, differing detail refuses",
    )
    add_root(sp)
    sp.set_defaults(fn=cmd_compat)

    sp = sub.add_parser("status", help="derived run status (never writes)")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-dir", default=None)
    g.add_argument("--event", default=None)
    sp.add_argument("--json", action="store_true")
    add_root(sp)
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("verify", help="verify chain + pinned inputs")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-dir", default=None)
    g.add_argument("--event", default=None)
    add_root(sp)
    sp.set_defaults(fn=cmd_verify)

    sp = sub.add_parser(
        "init-date", help="compile run plans for a date's staged events"
    )
    sp.add_argument("--date", required=True)
    add_identity(sp)
    add_root(sp)
    sp.set_defaults(fn=cmd_init_date)

    return p


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:  # argparse usage errors; keep in-process callers safe
        return int(e.code or 0)
    if args.command in ("record",) and not (args.run_dir or args.event):
        print("REFUSED: record needs --run-dir or --event", file=sys.stderr)
        return 2
    try:
        return args.fn(args)
    except RecoverableTail as e:
        print(f"RECOVERABLE: {e}", file=sys.stderr)
        return 3
    except (Refusal,) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    except IntegrityError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
