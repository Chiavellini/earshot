#!/usr/bin/env python3
"""Bind preflight inputs to one active RunPlan and publish its lifecycle proof.

``inspect`` is local/read-only apart from run-scoped proof artifacts.  It
validates exactly one active schema-3 RunPlan and either a webcast AccessPlan
or a phone AdmissionStrategy.  It never resolves media, dials, pages, or
opens the decision admission interlock.

``finalize`` consumes the exact binding produced by ``inspect``.  Successful
checks append truthful prerequisite receipts, durably publish the structured
preflight report, and only then append ``preflight_passed``.  A report write
failure can therefore never return PASS or advance the final lifecycle gate.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path


SCHEMA = 1
PLAN_SCHEMA = 3
HEX64 = re.compile(r"^[0-9a-f]{64}$")
EVENT_RE = re.compile(r"^KXEARNINGSMENTION([A-Z0-9]+)")
PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
PREFIX_RE = re.compile(r"^[a-z0-9-]{1,24}$")
REHEARSAL_RE = re.compile(r"^rehearsed-(\d{4}-\d{2}-\d{2})$")
CAPABILITY_PATHS = (
    "scripts/event-autorun.sh",
    "scripts/redial-supervisor.sh",
    "scripts/operator-redial.sh",
    "scripts/operator-page.sh",
    "scripts/twilio-operator-dial.sh",
)
HELD_PATH = "scripts/held-admission-receipt.py"
CAPABILITY_MARKERS = {
    "scripts/event-autorun.sh": (
        "EARSHOT_FIRST_DROP_DIAL_SCRIPT",
        "first-drop operator fallback: ARMED",
    ),
    "scripts/redial-supervisor.sh": (
        "--first-drop-dial-script",
        "FIRST-DROP-OPERATOR",
    ),
    "scripts/operator-redial.sh": ("--assert-admitted",),
    "scripts/operator-page.sh": ("--assert-admitted",),
    "scripts/twilio-operator-dial.sh": ("inbound_track",),
    HELD_PATH: ("held_admission_assertion",),
}


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


def now() -> dt.datetime:
    override = os.environ.get("EARSHOT_PREFLIGHT_NOW_UTC")
    if override:
        return parse_utc(override, "EARSHOT_PREFLIGHT_NOW_UTC")
    return dt.datetime.now(dt.timezone.utc)


def now_text() -> str:
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(raw: str, name: str) -> dt.datetime:
    if not isinstance(raw, str) or not raw:
        raise Refusal(f"{name} must be a UTC timestamp")
    try:
        value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Refusal(f"{name} is unparseable: {raw!r}") from exc
    if value.tzinfo is None or value.utcoffset() != dt.timedelta(0):
        raise Refusal(f"{name} must be UTC")
    return value


def repo_relative(repo: Path, path: Path, name: str) -> str:
    try:
        return path.resolve().relative_to(repo).as_posix()
    except ValueError as exc:
        raise Refusal(f"{name} must be inside the repository: {path}") from exc


def resolve_file(repo: Path, raw: str, name: str) -> tuple[Path, str]:
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    path = path.resolve()
    rel = repo_relative(repo, path, name)
    if not path.is_file() or path.is_symlink():
        raise Refusal(f"{name} is not a regular non-symlink file: {path}")
    return path, rel


def run_eventctl(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(repo / "scripts" / "eventctl.py"), *args],
        cwd=repo, text=True, capture_output=True, timeout=30,
        env={**os.environ, "EARSHOT_REPO": str(repo)},
    )
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        raise Refusal(f"eventctl {args[0]} refused: {lines[-1] if lines else proc.returncode}")
    return proc.stdout


def load_config(repo: Path, raw_config: str) -> tuple[Path, str, dict]:
    config, rel = resolve_file(repo, raw_config, "config")
    try:
        parsed = tomllib.loads(config.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise Refusal(f"config TOML invalid: {exc}") from exc
    event_id = parsed.get("event", {}).get("id")
    if not isinstance(event_id, str) or not event_id:
        raise Refusal("config has no [event].id")
    return config, rel, parsed


def load_selected_run(repo: Path, event_id: str, requested: str | None) -> tuple[Path, dict, dict]:
    # --event is load-bearing: eventctl refuses when zero or multiple active
    # runs exist.  An explicit --run-dir only narrows which one the caller
    # expected; it cannot bypass that uniqueness check.
    active = json.loads(run_eventctl(repo, "status", "--event", event_id, "--json"))
    if not active.get("state", {}).get("active"):
        raise Refusal("selected RunPlan is not active")
    run_dir = repo / "var" / "runs" / event_id / active["run"]
    if requested:
        supplied = Path(requested)
        if not supplied.is_absolute():
            supplied = repo / supplied
        if supplied.resolve() != run_dir.resolve():
            raise Refusal(f"--run-dir is not the event's unique active run ({run_dir})")
    run_eventctl(repo, "verify", "--run-dir", str(run_dir))
    plan_path = run_dir / "control" / "run-plan.json"
    if not plan_path.is_file() or plan_path.is_symlink():
        raise Refusal(f"missing regular RunPlan: {plan_path}")
    try:
        plan = json.loads(plan_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise Refusal(f"RunPlan unreadable: {exc}") from exc
    if plan.get("schema") != PLAN_SCHEMA or plan.get("kind") != "run-plan":
        raise Refusal("preflight proof requires a schema-3 RunPlan")
    if plan.get("plan_sha256") != object_hash(plan, "plan_sha256"):
        raise Refusal("RunPlan self-hash mismatch")
    if (plan.get("event_id"), plan.get("run_id")) != (event_id, active.get("run")):
        raise Refusal("RunPlan/eventctl identity mismatch")
    return run_dir.resolve(), plan, active


def executable_hashes(repo: Path, rels) -> list[dict]:
    result = []
    for rel in rels:
        path = repo / rel
        if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
            raise Refusal(f"required admission capability is not executable: {rel}")
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            raise Refusal(f"required admission capability is unreadable: {rel}: {exc}") from exc
        missing = [marker for marker in CAPABILITY_MARKERS.get(rel, ()) if marker not in text]
        if missing:
            raise Refusal(
                f"required admission capability contract marker missing from {rel}: "
                + ", ".join(missing)
            )
        result.append({"path": rel, "sha256": file_hash(path)})
    return result


def operator_capability(repo: Path, config: dict) -> list[dict]:
    block = config.get("operator_handler")
    if not isinstance(block, dict) or block.get("enabled") is not True:
        raise Refusal("phone strategy needs [operator_handler] enabled=true")
    cell = block.get("cell_number")
    prefix = block.get("conference_prefix", "")
    if not isinstance(cell, str) or not PHONE_RE.fullmatch(cell):
        raise Refusal("operator handler cell_number is invalid")
    if not isinstance(prefix, str) or (prefix and not PREFIX_RE.fullmatch(prefix)):
        raise Refusal("operator handler conference_prefix is invalid")
    return executable_hashes(repo, CAPABILITY_PATHS)


def issuer_key(event_id: str) -> str:
    match = EVENT_RE.match(event_id)
    if not match:
        raise Refusal(f"cannot derive issuer registry key from {event_id!r}")
    return match.group(1)


def parse_date(raw, name: str) -> dt.date:
    if not isinstance(raw, str):
        raise Refusal(f"{name} is missing")
    try:
        return dt.datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise Refusal(f"{name} must be YYYY-MM-DD") from exc


def admission_strategy(repo: Path, plan: dict, config: dict, registry_path: Path) -> dict:
    if not registry_path.is_file() or registry_path.is_symlink():
        raise Refusal(f"canonical admission registry is unreadable: {registry_path}")
    try:
        registry = tomllib.loads(registry_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise Refusal(f"canonical admission registry invalid: {exc}") from exc
    key = issuer_key(plan["event_id"])
    entry = registry.get(key)
    if entry is not None and not isinstance(entry, dict):
        raise Refusal(f"registry entry {key} is invalid")
    entry = entry or {}
    observed = entry.get("admission", "missing")
    reservation = entry.get("reservation_window") is True
    observed_date = entry.get("admission_probed")
    today = now().date()
    freshness = "not-applicable"
    age_days = None
    held_fresh = False
    if observed == "held":
        try:
            probed = parse_date(observed_date, f"{key}.admission_probed")
            age_days = (today - probed).days
            held_fresh = 0 <= age_days <= 30
            freshness = "fresh" if held_fresh else ("future" if age_days < 0 else "stale")
        except Refusal:
            freshness = "malformed"

    facts = {
        "issuer": key,
        "registry_path": repo_relative(repo, registry_path, "registry"),
        "registry_sha256": file_hash(registry_path),
        "observed_admission": observed,
        "admission_probed": observed_date if isinstance(observed_date, str) else None,
        "held_freshness": freshness,
        "held_age_days": age_days,
        "reservation_window": reservation,
    }
    current_stream = {
        "admitted": False,
        "proof": None,
        "belongs_to": "runtime current-stream admission receipt",
    }

    if held_fresh:
        capability = executable_hashes(repo, (HELD_PATH,))
        mode = "fresh_probe_held"
        operational = {
            "initial_route": "plain-listen-only",
            "current_stream_gate": "held-admission-receipt.py after source_attached",
            "capability": capability,
        }
        probe_gate = None
    elif observed == "operator_screened":
        match = REHEARSAL_RE.fullmatch(str(entry.get("operator_handler", "")))
        if not match:
            raise Refusal(f"{key} operator-screened strategy lacks rehearsed-YYYY-MM-DD evidence")
        rehearsal_date = parse_date(match.group(1), f"{key}.operator_handler rehearsal")
        if rehearsal_date > today:
            raise Refusal(f"{key} operator rehearsal is future-dated")
        capability = operator_capability(repo, config)
        mode = "rehearsed_operator_first"
        operational = {
            "initial_route": "human-first-operator",
            "current_stream_gate": "explicit operator-page.sh --assert-admitted",
            "rehearsal": match.group(0),
            "capability": capability,
        }
        probe_gate = None
    elif observed in {"missing", "unknown", "untested", "held"}:
        # A stale/malformed held observation is a historical fact, never a
        # reusable held proof.  It can only fall back to the same fail-closed
        # unknown strategy; the runtime interlock stays closed throughout.
        capability = operator_capability(repo, config)
        operational = {
            "initial_route": "plain-listen-only",
            "on_first_qualifying_drop": "latch-human-first-operator-replacement",
            "current_stream_gate": "explicit operator-page.sh --assert-admitted",
            "capability": capability,
        }
        if reservation:
            mode = "reservation_window_first_drop_fallback"
            starts = parse_utc(plan["starts_at_utc"], "RunPlan starts_at_utc")
            opens = starts - dt.timedelta(minutes=90)
            closes = starts - dt.timedelta(minutes=35)
            instant = now()
            status = "pending-until-t35" if instant < closes else "fallback-activated-at-t35"
            probe_gate = {
                "opens_utc": opens.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "deadline_utc": closes.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": status,
                "underwrites_admission_resolved": False,
                "fallback_underwrites_admission_resolved": True,
            }
        else:
            mode = "first_drop_operator_fallback"
            probe_gate = None
    else:
        raise Refusal(
            f"registry admission={observed!r} cannot establish a safe strategy"
            " (dropped/passcode-required/unsupported outcomes need new evidence)"
        )

    strategy = {
        "schema": SCHEMA,
        "kind": "phone-admission-strategy",
        "generated_utc": now_text(),
        "event_id": plan["event_id"],
        "run_id": plan["run_id"],
        "run_plan_sha256": plan["plan_sha256"],
        "config_path": plan["config_path"],
        "config_sha256": plan["config_sha256"],
        "mode": mode,
        "registry_facts": facts,
        "probe_gate": probe_gate,
        "operational_strategy": operational,
        "current_stream": current_stream,
    }
    strategy["admission_strategy_sha256"] = object_hash(strategy, "admission_strategy_sha256")
    return strategy


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
    parent_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def publish_bound(path: Path, value: dict, hash_field: str, reached: set[str], gate: str):
    data = json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
    if path.exists():
        if not path.is_file() or path.is_symlink():
            raise Refusal(f"proof destination is not a regular file: {path}")
        try:
            current = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise Refusal(f"existing proof artifact is corrupt: {path}: {exc}") from exc
        if current.get(hash_field) != object_hash(current, hash_field):
            raise Refusal(f"existing proof artifact self-hash is corrupt: {path}")
        if current == value:
            return current
        comparable = lambda item: {
            k: v for k, v in item.items() if k not in {"generated_utc", hash_field}
        }
        if comparable(current) == comparable(value):
            # Reuse the byte-identical evidence already named by any prior
            # receipt. A new wall-clock generation time is not new evidence.
            return current
        if gate in reached:
            raise Refusal(f"cannot replace {path.name} after {gate} was recorded")
    atomic_publish(path, data)
    loaded = json.loads(path.read_text())
    if loaded.get(hash_field) != object_hash(loaded, hash_field):
        raise Refusal(f"post-publication self-check failed for {path}")
    return loaded


def reached_set(active: dict) -> set[str]:
    return {row["transition"] for row in active.get("state", {}).get("reached", [])}


def validate_access(repo: Path, run_dir: Path, raw: str | None) -> tuple[Path, dict]:
    path = Path(raw).resolve() if raw else run_dir / "control" / "access-plan.json"
    proc = subprocess.run(
        [sys.executable, str(repo / "scripts" / "access-plan.py"), "validate",
         "--run-dir", str(run_dir), "--plan", str(path), "--canonical"],
        cwd=repo, text=True, capture_output=True, timeout=30,
        env={**os.environ, "EARSHOT_REPO": str(repo)},
    )
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        raise Refusal(f"AccessPlan invalid: {lines[-1] if lines else proc.returncode}")
    try:
        validation = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Refusal("AccessPlan validator emitted invalid JSON") from exc
    return path, validation


def study_input(repo: Path, plan: dict) -> tuple[str | None, str | None]:
    if "study_completed" not in plan.get("required_prep_receipts", []):
        return None, None
    path = repo / "configs" / ".readiness" / (Path(plan["config_path"]).stem + ".json")
    if not path.is_file() or path.is_symlink():
        raise Refusal(f"required keyword study is missing: {path}")
    return repo_relative(repo, path, "study artifact"), file_hash(path)


def build_binding(repo: Path, config_raw: str, run_raw: str | None,
                  access_raw: str | None, registry_raw: str) -> tuple[Path, dict, dict]:
    config_path, config_rel, config = load_config(repo, config_raw)
    event_id = config["event"]["id"]
    run_dir, plan, active = load_selected_run(repo, event_id, run_raw)
    if plan["config_path"] != config_rel or plan["config_sha256"] != file_hash(config_path):
        raise Refusal("selected config path/hash does not match the active RunPlan")
    reached = reached_set(active)
    if plan["lane"] == "webcast":
        proof_path, validation = validate_access(repo, run_dir, access_raw)
        proof = {
            "kind": "webcast-access-plan",
            "path": repo_relative(repo, proof_path, "AccessPlan"),
            "sha256": file_hash(proof_path),
            "semantic_sha256": validation["access_plan_sha256"],
            "mode": "validated-acquisition-strategy",
            "current_media_proven": False,
        }
        lane_transition = "access_acquired"
    elif plan["lane"] == "phone":
        registry_path, _ = resolve_file(repo, registry_raw, "admission registry")
        strategy = admission_strategy(repo, plan, config, registry_path)
        proof_path = run_dir / "control" / "admission-strategy.json"
        strategy = publish_bound(
            proof_path, strategy, "admission_strategy_sha256", reached,
            "admission_resolved"
        )
        proof = {
            "kind": "phone-admission-strategy",
            "path": repo_relative(repo, proof_path, "AdmissionStrategy"),
            "sha256": file_hash(proof_path),
            "semantic_sha256": strategy["admission_strategy_sha256"],
            "mode": strategy["mode"],
            "current_stream_admitted": False,
        }
        lane_transition = "admission_resolved"
    else:
        raise Refusal(f"unsupported RunPlan lane {plan['lane']!r}")
    study_path, study_sha = study_input(repo, plan)
    binding = {
        "schema": SCHEMA,
        "kind": "preflight-input-binding",
        "generated_utc": now_text(),
        "event_id": plan["event_id"],
        "run_id": plan["run_id"],
        "run_plan_sha256": plan["plan_sha256"],
        "run_dir": repo_relative(repo, run_dir, "run directory"),
        "lane": plan["lane"],
        "config_path": plan["config_path"],
        "config_sha256": plan["config_sha256"],
        "required_prep_receipts": plan["required_prep_receipts"],
        "lane_transition": lane_transition,
        "lane_proof": proof,
        "study_artifact": study_path,
        "study_sha256": study_sha,
    }
    binding["binding_sha256"] = object_hash(binding, "binding_sha256")
    return run_dir, binding, active


def cmd_inspect(args) -> int:
    repo = repo_root()
    run_dir, binding, active = build_binding(
        repo, args.config, args.run_dir, args.access_plan, args.registry
    )
    out = Path(args.out).resolve() if args.out else run_dir / "control" / "preflight-inputs.json"
    expected = run_dir / "control" / "preflight-inputs.json"
    if out != expected:
        raise Refusal(f"preflight binding output must be {expected}")
    binding = publish_bound(
        out, binding, "binding_sha256", reached_set(active),
        binding["lane_transition"]
    )
    print(json.dumps({
        "schema": SCHEMA,
        "kind": "preflight-inspection",
        "event_id": binding["event_id"],
        "run_id": binding["run_id"],
        "run_dir": binding["run_dir"],
        "binding": repo_relative(repo, out, "binding"),
        "binding_sha256": binding["binding_sha256"],
        "lane": binding["lane"],
        "strategy_mode": binding["lane_proof"]["mode"],
    }, sort_keys=True))
    return 0


def load_binding(repo: Path, raw: str) -> tuple[Path, dict, Path, dict]:
    path, _ = resolve_file(repo, raw, "preflight binding")
    try:
        binding = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise Refusal(f"preflight binding invalid JSON: {exc}") from exc
    if binding.get("schema") != SCHEMA or binding.get("kind") != "preflight-input-binding":
        raise Refusal("unsupported preflight binding schema/kind")
    if binding.get("binding_sha256") != object_hash(binding, "binding_sha256"):
        raise Refusal("preflight binding self-hash mismatch")
    expected_path = repo / binding["run_dir"] / "control" / "preflight-inputs.json"
    if path != expected_path.resolve():
        raise Refusal("preflight binding path does not match its run identity")
    run_dir, plan, active = load_selected_run(repo, binding["event_id"], binding["run_dir"])
    if (
        binding.get("run_id") != plan["run_id"]
        or binding.get("run_plan_sha256") != plan["plan_sha256"]
        or binding.get("config_path") != plan["config_path"]
        or binding.get("config_sha256") != plan["config_sha256"]
        or binding.get("required_prep_receipts") != plan["required_prep_receipts"]
    ):
        raise Refusal("preflight binding no longer matches the active RunPlan")
    proof = binding.get("lane_proof", {})
    proof_path, proof_rel = resolve_file(repo, proof.get("path", ""), "lane proof")
    if proof_rel != proof.get("path") or file_hash(proof_path) != proof.get("sha256"):
        raise Refusal("lane proof bytes changed after inspection")
    if plan["lane"] == "webcast":
        access_path, validation = validate_access(repo, run_dir, str(proof_path))
        if validation["access_plan_sha256"] != proof.get("semantic_sha256"):
            raise Refusal("AccessPlan semantic hash changed after inspection")
    else:
        strategy = json.loads(proof_path.read_text())
        if strategy.get("admission_strategy_sha256") != proof.get("semantic_sha256"):
            raise Refusal("AdmissionStrategy semantic hash changed after inspection")
        registry_path = repo / strategy["registry_facts"]["registry_path"]
        if file_hash(registry_path) != strategy["registry_facts"]["registry_sha256"]:
            raise Refusal("admission registry changed after inspection; inspect again")
        for cap in strategy["operational_strategy"].get("capability", []):
            if file_hash(repo / cap["path"]) != cap["sha256"]:
                raise Refusal(f"admission capability changed after inspection: {cap['path']}")
    study = binding.get("study_artifact")
    if study:
        study_path, study_rel = resolve_file(repo, study, "study artifact")
        if study_rel != study or file_hash(study_path) != binding.get("study_sha256"):
            raise Refusal("keyword study changed after inspection")
    return path, binding, run_dir, active


def record(repo: Path, run_dir: Path, transition: str, detail: str, artifacts: list[str]):
    args = ["record", "--run-dir", str(run_dir), "--transition", transition,
            "--actor", "preflight-event.sh", "--detail", detail]
    for artifact in artifacts:
        args.extend(["--artifact", artifact])
    run_eventctl(repo, *args)


def durable_report(repo: Path, raw_result: str, report: dict):
    result = Path(raw_result)
    if not result.is_absolute():
        result = repo / result
    result = result.resolve()
    repo_relative(repo, result, "preflight result")
    if result.exists() and (not result.is_file() or result.is_symlink()):
        raise Refusal(f"preflight result destination is not a regular file: {result}")
    atomic_publish(result, json.dumps(report, indent=1, sort_keys=True).encode() + b"\n")
    try:
        reread = json.loads(result.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise Refusal(f"preflight result durability self-check failed: {exc}") from exc
    if reread != report:
        raise Refusal("preflight result durability self-check changed bytes")
    return result, repo_relative(repo, result, "preflight result")


def failed_report(report: dict, message: str) -> dict:
    out = dict(report)
    out["pass"] = False
    out["fails"] = int(out.get("fails", 0)) + 1
    out["publication_error"] = message
    return out


def cmd_finalize(args) -> int:
    repo = repo_root()
    try:
        draft_path, _ = resolve_file(repo, args.report_draft, "report draft")
        report = json.loads(draft_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise Refusal(f"report draft invalid: {exc}") from exc
    if not isinstance(report, dict) or report.get("schema") != 1 or type(report.get("pass")) is not bool:
        raise Refusal("report draft must be a schema-1 object with boolean pass")

    if not report["pass"]:
        durable_report(repo, args.result_json, report)
        print("published-failed-preflight")
        return 0

    binding_path, binding, run_dir, _active = load_binding(repo, args.binding)
    binding_rel = repo_relative(repo, binding_path, "binding")
    proof_rel = binding["lane_proof"]["path"]
    if report.get("event_id") != binding["event_id"]:
        raise Refusal("report draft event_id does not match the proof binding")
    if report.get("config") != binding["config_path"]:
        raise Refusal("report draft config path does not match the proof binding")
    if report.get("config_sha256") != binding["config_sha256"]:
        raise Refusal("report draft config hash does not match the proof binding")
    try:
        record(repo, run_dir, "prepared",
               f"exact schema-3 config passed full preflight checks; binding={binding['binding_sha256']}",
               [binding["config_path"], binding_rel])
        record(repo, run_dir, binding["lane_transition"],
               f"strategy={binding['lane_proof']['mode']}; no T0/current-stream proof claimed",
               [proof_rel, binding_rel])
        if "study_completed" in binding["required_prep_receipts"]:
            record(repo, run_dir, "study_completed",
                   f"keyword study sha256={binding['study_sha256']}",
                   [binding["study_artifact"], binding_rel])
    except Refusal as exc:
        # Prerequisite receipts already appended are truthful and idempotent;
        # publish a red verdict and leave preflight_passed absent.
        durable_report(repo, args.result_json, failed_report(report, str(exc)))
        raise

    enriched = dict(report)
    enriched.update({
        "run_id": binding["run_id"],
        "run_plan_sha256": binding["run_plan_sha256"],
        "preflight_binding": binding_rel,
        "preflight_binding_sha256": binding["binding_sha256"],
        "lane": binding["lane"],
        "lane_strategy_mode": binding["lane_proof"]["mode"],
        "current_media_proven": False if binding["lane"] == "webcast" else None,
        "current_stream_admitted": False if binding["lane"] == "phone" else None,
    })
    try:
        _result_path, result_rel = durable_report(repo, args.result_json, enriched)
    except (OSError, Refusal) as exc:
        # No final receipt has been written.  Propagate nonzero: a would-be
        # PASS whose report cannot be persisted is a failed preflight.
        raise Refusal(f"could not durably publish preflight result: {exc}") from exc
    try:
        record(repo, run_dir, "preflight_passed",
               f"durable report + exact proof binding {binding['binding_sha256']}",
               [result_rel, binding_rel])
    except Refusal as exc:
        try:
            durable_report(repo, args.result_json, failed_report(enriched, str(exc)))
        except Exception as rewrite_exc:
            raise Refusal(
                f"preflight receipt failed ({exc}); additionally could not publish red repair ({rewrite_exc})"
            ) from exc
        raise
    print(json.dumps({
        "schema": SCHEMA,
        "kind": "preflight-finalization",
        "event_id": binding["event_id"],
        "run_id": binding["run_id"],
        "result": result_rel,
        "binding": binding_rel,
        "strategy_mode": binding["lane_proof"]["mode"],
    }, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="validate and bind preflight inputs")
    inspect.add_argument("--config", required=True)
    inspect.add_argument("--run-dir")
    inspect.add_argument("--access-plan")
    inspect.add_argument("--registry", default="configs/dial-ins.toml")
    inspect.add_argument("--out")
    inspect.set_defaults(func=cmd_inspect)
    finalize = sub.add_parser("finalize", help="publish report and typed receipts")
    finalize.add_argument("--binding", required=True)
    finalize.add_argument("--report-draft", required=True)
    finalize.add_argument("--result-json", required=True)
    finalize.set_defaults(func=cmd_finalize)
    return ap


def main() -> int:
    ap = parser()
    args = ap.parse_args()
    try:
        return args.func(args)
    except Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError, KeyError, TypeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
