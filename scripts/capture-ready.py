#!/usr/bin/env python3
"""Publish a run-scoped T0 capture proof, then record ``capture_ready``.

``preflight_passed`` proves a strategy.  This helper proves the lane-specific
facts that exist only at fire time:

* phone: the exact config/env/binary/state/host are pinned, the configured
  listener bind is free, and the tunnel-free WSS route is coherent; no listener
  or admitted stream is falsely claimed;
* webcast: a fresh durable PCM proof is bound to the exact AccessPlan and
  RunPlan, with decoded bytes above its declared floor and HLS continuity.

The durable ``control/capture-ready.json`` is fsynced before eventctl appends
the lifecycle receipt.  A crash between those steps is safely recoverable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit


SCHEMA = 1
PLAN_SCHEMA = 3
PCM_SCHEMA = 1
MAX_PCM_AGE_SECONDS = 180
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class Refusal(Exception):
    pass


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def object_hash(value: dict, omit: str) -> str:
    return hashlib.sha256(
        canonical_bytes({k: v for k, v in value.items() if k != omit})
    ).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: object, field: str) -> dt.datetime:
    if not isinstance(value, str):
        raise Refusal(f"{field} must be a UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Refusal(f"{field} is not ISO-8601: {exc}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise Refusal(f"{field} must carry UTC")
    return parsed.astimezone(dt.timezone.utc)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, body: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        view = memoryview(body)
        while view:
            wrote = os.write(fd, view)
            if wrote <= 0:
                raise Refusal(f"short write while publishing {path}")
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
    try:
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def load_json(path: Path, what: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise Refusal(f"{what} must be a regular non-symlink file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Refusal(f"{what} does not parse as JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise Refusal(f"{what} must be a JSON object")
    return value


def run_eventctl(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(repo / "scripts" / "eventctl.py"), *args],
        cwd=repo,
        env={**os.environ, "EARSHOT_REPO": str(repo)},
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()
        raise Refusal(f"eventctl {args[0]} refused: {detail}")
    return proc.stdout


def context(raw_run: str) -> tuple[Path, Path, dict, dict, set[str]]:
    repo = Path(os.environ.get("EARSHOT_REPO", Path(__file__).parent.parent)).resolve()
    run = Path(raw_run)
    run = (run if run.is_absolute() else repo / run).resolve()
    root = (repo / "var" / "runs").resolve()
    try:
        relative = run.relative_to(root)
    except ValueError as exc:
        raise Refusal(f"run directory is outside canonical {root}") from exc
    if len(relative.parts) != 2:
        raise Refusal("run directory must be var/runs/<EVENT_ID>/<RUN_ID>")
    run_eventctl(repo, "verify", "--run-dir", str(run))
    status = json.loads(run_eventctl(repo, "status", "--run-dir", str(run), "--json"))
    plan = load_json(run / "control" / "run-plan.json", "RunPlan")
    if plan.get("schema") != PLAN_SCHEMA or plan.get("kind") != "run-plan":
        raise Refusal("capture-ready requires a schema-3 RunPlan")
    if relative.parts != (plan.get("event_id"), plan.get("run_id")):
        raise Refusal("RunPlan identity differs from canonical directory")
    state = status.get("state") or {}
    reached = {item.get("transition") for item in state.get("reached", [])}
    if "scheduled" not in reached:
        raise Refusal("RunPlan has not reached scheduled")
    if "launched" in reached:
        raise Refusal("RunPlan already records launched; capture proof cannot be renewed")
    if not state.get("active"):
        raise Refusal("RunPlan is terminal or archived")
    return repo, run, plan, status, reached


def resolve_repo_file(repo: Path, raw: str, field: str) -> Path:
    path = Path(raw)
    path = (path if path.is_absolute() else repo / path).resolve()
    try:
        path.relative_to(repo)
    except ValueError as exc:
        raise Refusal(f"{field} is outside repository: {path}") from exc
    if path.is_symlink() or not path.is_file():
        raise Refusal(f"{field} is not a regular non-symlink file: {path}")
    return path


def load_phone_config(path: Path, event_id: str) -> tuple[str, int]:
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise Refusal(f"phone config is invalid TOML: {exc}") from exc
    if (config.get("event") or {}).get("id") != event_id:
        raise Refusal("phone config event identity differs from RunPlan")
    binds = []
    for source in config.get("sources") or []:
        if isinstance(source, dict) and source.get("kind") == "twilio_stream":
            bind = source.get("bind")
            if not isinstance(bind, str) or ":" not in bind:
                raise Refusal("twilio source has no valid bind")
            host, raw_port = bind.rsplit(":", 1)
            try:
                port = int(raw_port)
            except ValueError as exc:
                raise Refusal("twilio bind port is not an integer") from exc
            binds.append((host, port))
    if len(binds) != 1:
        raise Refusal(f"phone capture proof requires exactly one twilio bind, got {binds}")
    host, port = binds[0]
    if host not in ("127.0.0.1", "0.0.0.0", "::1", "::"):
        raise Refusal(f"unsupported listener bind host {host!r}")
    if not 1 <= port <= 65535:
        raise Refusal("listener bind port is outside 1..65535")
    return host, port


def prove_bind_free(host: str, port: int) -> dict:
    # The caller checks immediately before starting the listener. `lsof` is
    # deliberately used instead of momentarily binding the port: constrained
    # service sandboxes may deny creating sockets even though the real runtime
    # unit has that permission, and the temporary bind cannot reserve the port
    # across the launcher handoff anyway. Launch repeats the ownership check.
    lsof = os.environ.get("EARSHOT_LSOF", "lsof")
    try:
        probe = subprocess.run(
            [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refusal(f"could not inspect configured bind {host}:{port}: {exc}") from exc
    owners = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
    if probe.returncode == 0 and owners:
        raise Refusal(
            f"configured listener bind {host}:{port} is owned by pid(s) {owners}"
        )
    if probe.returncode not in (0, 1) or probe.stderr.strip():
        raise Refusal(
            f"lsof could not prove configured listener bind free: "
            f"exit={probe.returncode} stderr={probe.stderr.strip()!r}"
        )
    return {
        "address": host,
        "port": port,
        "free": True,
        "method": "lsof-listener-owner-probe",
        "checked_utc": now_utc(),
    }


def phone_evidence(args: argparse.Namespace, repo: Path, plan: dict) -> dict:
    if plan.get("lane") != "phone" or plan.get("execution_enabled") is not True:
        raise Refusal("phone capture proof requires an execution-enabled phone RunPlan")
    if os.environ.get("EARSHOT_EXECUTION_HOST") != plan.get("execution_host_id"):
        raise Refusal("current execution host does not match RunPlan")
    config = resolve_repo_file(repo, plan["config_path"], "pinned config")
    if file_hash(config) != plan["config_sha256"]:
        raise Refusal("pinned config hash differs from RunPlan")
    if not plan.get("event_env_path") or not plan.get("event_env_sha256"):
        raise Refusal("phone RunPlan has no pinned event env")
    event_env = resolve_repo_file(repo, args.event_env, "event env")
    expected_env = resolve_repo_file(repo, plan["event_env_path"], "RunPlan event env")
    if event_env != expected_env or file_hash(event_env) != plan["event_env_sha256"]:
        raise Refusal("selected event env does not match the RunPlan path/hash")
    bind_host, bind_port = load_phone_config(config, plan["event_id"])
    if args.bind_port != bind_port:
        raise Refusal(
            f"launcher bind port {args.bind_port} differs from config {bind_port}"
        )
    parsed = urlsplit(args.stream_url)
    if (
        parsed.scheme != "wss"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != f"/e/{bind_port}"
    ):
        raise Refusal(
            f"stream URL must be credential-free wss://<host>/e/{bind_port}"
        )
    if "ngrok" in parsed.hostname.lower():
        raise Refusal("production phone capture proof refuses an ngrok endpoint")
    state_dir = Path(args.state_dir).resolve()
    expected_state = (repo / "state" / plan["event_id"]).resolve()
    if state_dir != expected_state or state_dir.is_symlink() or not state_dir.is_dir():
        raise Refusal(f"state directory must be the existing canonical {expected_state}")
    binary = Path(args.binary).resolve()
    expected_binary = expected_state / "earshot-pinned"
    if binary != expected_binary or binary.is_symlink() or not binary.is_file():
        raise Refusal(f"binary must be the regular preflight pin {expected_binary}")
    mode = binary.stat().st_mode
    if not mode & stat.S_IXUSR:
        raise Refusal("preflight-pinned binary is not executable")
    bind = prove_bind_free(bind_host, bind_port)
    return {
        "config": {"path": plan["config_path"], "sha256": plan["config_sha256"]},
        "event_env": {
            "path": plan["event_env_path"],
            "sha256": plan["event_env_sha256"],
        },
        "binary": {
            "path": binary.relative_to(repo).as_posix(),
            "sha256": file_hash(binary),
            "executable": True,
        },
        "state_dir": state_dir.relative_to(repo).as_posix(),
        "bind": bind,
        "stream_route": {
            "scheme": "wss",
            "host_sha256": hashlib.sha256(parsed.hostname.encode()).hexdigest(),
            "path": parsed.path,
            "coherent": True,
        },
        "listener_started": False,
        "current_stream_admitted": False,
    }


def validate_access(repo: Path, run: Path, plan: dict) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "access-plan.py"),
            "validate",
            "--run-dir",
            str(run),
        ],
        cwd=repo,
        env={**os.environ, "EARSHOT_REPO": str(repo)},
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode:
        raise Refusal(f"AccessPlan validation failed: {(proc.stderr or proc.stdout).strip()}")
    access = load_json(run / "control" / "access-plan.json", "AccessPlan")
    if access.get("run_plan_sha256") != plan.get("plan_sha256"):
        raise Refusal("AccessPlan does not bind this RunPlan")
    return access


def validate_pcm_proof(
    path: Path, repo: Path, run: Path, plan: dict, access: dict
) -> dict:
    expected_path = run / "control" / "pcm-proof.json"
    if path.resolve() != expected_path.resolve():
        raise Refusal(f"PCM proof must be run-scoped at {expected_path}")
    proof = load_json(path, "PCM proof")
    expected_fields = {
        "schema",
        "kind",
        "created_utc",
        "event_id",
        "run_id",
        "run_plan_sha256",
        "access_plan_sha256",
        "execution_host_id",
        "observed_host",
        "source_kind",
        "endpoint",
        "decoded_pcm",
        "continuity",
        "pcm_proof_sha256",
    }
    if set(proof) != expected_fields:
        raise Refusal("PCM proof fields do not match schema 1")
    if proof.get("schema") != PCM_SCHEMA or proof.get("kind") != "pcm-capture-proof":
        raise Refusal("unsupported PCM proof schema/kind")
    if proof.get("pcm_proof_sha256") != object_hash(proof, "pcm_proof_sha256"):
        raise Refusal("PCM proof self-hash mismatch")
    created = parse_utc(proof.get("created_utc"), "PCM proof created_utc")
    age = (dt.datetime.now(dt.timezone.utc) - created).total_seconds()
    if age < -5 or age > MAX_PCM_AGE_SECONDS:
        raise Refusal(
            f"PCM proof is not fresh ({age:.0f}s; max {MAX_PCM_AGE_SECONDS}s)"
        )
    identity = (
        proof.get("event_id"),
        proof.get("run_id"),
        proof.get("run_plan_sha256"),
        proof.get("access_plan_sha256"),
        proof.get("execution_host_id"),
    )
    expected = (
        plan["event_id"],
        plan["run_id"],
        plan["plan_sha256"],
        access["access_plan_sha256"],
        plan["execution_host_id"],
    )
    if identity != expected:
        raise Refusal("PCM proof identity does not bind RunPlan/AccessPlan/host")
    if os.environ.get("EARSHOT_EXECUTION_HOST") != plan["execution_host_id"]:
        raise Refusal("current execution host differs from PCM proof/RunPlan")
    decoded = proof.get("decoded_pcm")
    if not isinstance(decoded, dict) or set(decoded) != {
        "sample_format",
        "sample_rate_hz",
        "channels",
        "requested_seconds",
        "expected_bytes",
        "floor_bytes",
        "decoded_bytes",
    }:
        raise Refusal("PCM proof decoded_pcm shape is invalid")
    if (
        decoded["sample_format"] != "s16le"
        or decoded["sample_rate_hz"] != 16000
        or decoded["channels"] != 1
        or type(decoded["decoded_bytes"]) is not int
        or type(decoded["floor_bytes"]) is not int
        or decoded["decoded_bytes"] < decoded["floor_bytes"]
        or decoded["floor_bytes"] <= 0
    ):
        raise Refusal("PCM proof does not establish the declared 16kHz mono byte floor")
    continuity = proof.get("continuity")
    source_kind = proof.get("source_kind")
    # Field-name note: `distinct_media_sequences` is kept for schema stability,
    # but since 2026-08-13 pcm-gate feeds it the distinct playlist TAIL count
    # (falling back to media-sequence only when no segment lines exist) — the
    # CBRS/AMAT CDN class freezes #EXT-X-MEDIA-SEQUENCE while still appending.
    if source_kind == "hls":
        if continuity != {
            "status": "advanced",
            "distinct_media_sequences": continuity.get("distinct_media_sequences")
            if isinstance(continuity, dict)
            else None,
            "polls": continuity.get("polls") if isinstance(continuity, dict) else None,
            "window_seconds": continuity.get("window_seconds")
            if isinstance(continuity, dict)
            else None,
        }:
            raise Refusal("HLS PCM proof continuity shape is invalid")
        if (
            type(continuity["distinct_media_sequences"]) is not int
            or continuity["distinct_media_sequences"] < 2
            or type(continuity["polls"]) is not int
            or continuity["polls"] < 2
        ):
            raise Refusal("HLS PCM proof does not show an advancing playlist")
    elif source_kind == "youtube":
        if continuity != {
            "status": "not-applicable-youtube",
            "distinct_media_sequences": None,
            "polls": None,
            "window_seconds": None,
        }:
            raise Refusal("YouTube continuity declaration is invalid")
    else:
        raise Refusal(f"unsupported PCM proof source kind {source_kind!r}")
    endpoint = proof.get("endpoint")
    if not isinstance(endpoint, dict) or set(endpoint) != {
        "url_file",
        "url_file_sha256",
        "url_value_sha256",
    }:
        raise Refusal("PCM proof endpoint shape is invalid")
    url_file = endpoint["url_file"]
    if url_file is not None:
        if url_file not in (access.get("endpoint") or {}).get("url_files", []):
            raise Refusal("PCM proof URL file is not declared by AccessPlan")
        if not isinstance(url_file, str) or not url_file.startswith("$HOME/"):
            raise Refusal("PCM proof URL file is not portable")
        current = Path.home() / url_file[len("$HOME/") :]
        if current.is_symlink() or not current.is_file():
            raise Refusal("PCM proof URL file is no longer a regular file")
        if file_hash(current) != endpoint["url_file_sha256"]:
            raise Refusal("URL file changed after the PCM proof")
        first = current.read_text(errors="replace").splitlines()
        if not first or hashlib.sha256(first[0].encode()).hexdigest() != endpoint["url_value_sha256"]:
            raise Refusal("URL value changed after the PCM proof")
    elif endpoint["url_file_sha256"] is not None:
        raise Refusal("inline PCM proof must not carry a URL-file hash")
    if not SHA_RE.fullmatch(str(endpoint["url_value_sha256"])):
        raise Refusal("PCM proof URL value hash is invalid")
    return proof


def webcast_evidence(
    args: argparse.Namespace, repo: Path, run: Path, plan: dict
) -> dict:
    if plan.get("lane") != "webcast":
        raise Refusal("webcast capture proof requires a webcast RunPlan")
    access = validate_access(repo, run, plan)
    pcm_path = Path(args.pcm_proof).resolve()
    pcm = validate_pcm_proof(pcm_path, repo, run, plan, access)
    return {
        "access_plan": {
            "path": "control/access-plan.json",
            "sha256": access["access_plan_sha256"],
        },
        "pcm_proof": {
            "path": "control/pcm-proof.json",
            "sha256": pcm["pcm_proof_sha256"],
            "created_utc": pcm["created_utc"],
            "decoded_bytes": pcm["decoded_pcm"]["decoded_bytes"],
            "floor_bytes": pcm["decoded_pcm"]["floor_bytes"],
            "continuity_status": pcm["continuity"]["status"],
        },
        "listener_started": False,
        "current_media_proven": True,
    }


def build_artifact(plan: dict, lane_evidence: dict) -> dict:
    value = {
        "schema": SCHEMA,
        "kind": "capture-ready-proof",
        "event_id": plan["event_id"],
        "run_id": plan["run_id"],
        "run_plan_sha256": plan["plan_sha256"],
        "execution_host_id": plan["execution_host_id"],
        "lane": plan["lane"],
        "created_utc": now_utc(),
        "evidence": lane_evidence,
    }
    value["capture_ready_sha256"] = object_hash(value, "capture_ready_sha256")
    return value


def validate_existing(value: dict, candidate: dict, plan: dict) -> None:
    expected_fields = {
        "schema",
        "kind",
        "event_id",
        "run_id",
        "run_plan_sha256",
        "execution_host_id",
        "lane",
        "created_utc",
        "evidence",
        "capture_ready_sha256",
    }
    if set(value) != expected_fields:
        raise Refusal("existing capture-ready proof has unsupported fields")
    if value.get("schema") != SCHEMA or value.get("kind") != "capture-ready-proof":
        raise Refusal("existing capture-ready proof has unsupported schema/kind")
    if value.get("capture_ready_sha256") != object_hash(value, "capture_ready_sha256"):
        raise Refusal("existing capture-ready proof self-hash mismatch")
    parse_utc(value.get("created_utc"), "capture-ready created_utc")
    static_fields = {
        "event_id",
        "run_id",
        "run_plan_sha256",
        "execution_host_id",
        "lane",
    }
    if any(value.get(field) != candidate.get(field) for field in static_fields):
        raise Refusal("existing capture-ready proof differs from RunPlan identity")
    # Time-bearing bind checks are preserved on retry. Compare semantic facts
    # with the prior checked timestamp substituted into the fresh observation.
    compare = candidate.copy()
    compare["created_utc"] = value["created_utc"]
    evidence = json.loads(json.dumps(candidate["evidence"]))
    if plan["lane"] == "phone":
        evidence["bind"]["checked_utc"] = value["evidence"]["bind"]["checked_utc"]
    compare["evidence"] = evidence
    compare["capture_ready_sha256"] = object_hash(compare, "capture_ready_sha256")
    if value != compare:
        raise Refusal("existing capture-ready proof describes different T0 facts")


def record_capture_ready(repo: Path, run: Path, lane: str) -> None:
    run_eventctl(
        repo,
        "record",
        "--run-dir",
        str(run),
        "--transition",
        "capture_ready",
        "--actor",
        "capture-ready",
        "--detail",
        f"{lane} T0 capture evidence durably verified",
        "--artifact",
        "control/capture-ready.json",
    )


def command(args: argparse.Namespace) -> int:
    repo, run, plan, _status, reached = context(args.run_dir)
    if args.lane != plan.get("lane"):
        raise Refusal(f"requested lane {args.lane!r} differs from RunPlan")
    if args.lane == "phone":
        evidence = phone_evidence(args, repo, plan)
    else:
        evidence = webcast_evidence(args, repo, run, plan)
    candidate = build_artifact(plan, evidence)
    output = run / "control" / "capture-ready.json"
    if output.exists():
        current = load_json(output, "existing capture-ready proof")
        validate_existing(current, candidate, plan)
        proof_state = "already-valid"
    else:
        atomic_write(
            output,
            (json.dumps(candidate, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        proof_state = "published"
    if os.environ.get("_EARSHOT_CAPTURE_READY_FAIL_AFTER_PUBLISH") == "1":
        raise Refusal("injected failure after capture-ready proof publication")
    if "capture_ready" not in reached:
        record_capture_ready(repo, run, args.lane)
        receipt_state = "recorded"
    else:
        receipt_state = "already-recorded"
    final = json.loads(run_eventctl(repo, "status", "--run-dir", str(run), "--json"))
    final_reached = {
        item.get("transition") for item in final.get("state", {}).get("reached", [])
    }
    if "capture_ready" not in final_reached:
        raise Refusal("capture_ready receipt is not visible after reconciliation")
    print(
        json.dumps(
            {
                "schema": 1,
                "kind": "capture-ready-result",
                "event_id": plan["event_id"],
                "run_id": plan["run_id"],
                "lane": plan["lane"],
                "proof": proof_state,
                "receipt": receipt_state,
            },
            sort_keys=True,
        )
    )
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    sub = value.add_subparsers(dest="lane", required=True)
    phone = sub.add_parser("phone")
    phone.add_argument("--run-dir", required=True)
    phone.add_argument("--event-env", required=True)
    phone.add_argument("--bind-port", type=int, required=True)
    phone.add_argument("--stream-url", required=True)
    phone.add_argument("--state-dir", required=True)
    phone.add_argument("--binary", required=True)
    webcast = sub.add_parser("webcast")
    webcast.add_argument("--run-dir", required=True)
    webcast.add_argument("--pcm-proof", required=True)
    return value


def main() -> int:
    try:
        return command(parser().parse_args())
    except Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("REFUSED: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
