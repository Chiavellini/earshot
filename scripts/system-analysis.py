#!/usr/bin/env python3
"""Deterministic, refusal-first EARSHOT system performance analysis.

Consumes only banked artifacts. See docs/SYSTEM-ANALYSIS.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
import tempfile
import unittest
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
RECORD_TYPE = "earshot.system_analysis"
OBSERVATION_TYPE = "earshot.observation"
TOOL_VERSION = "system-analysis-v1"
DEFAULT_BOOTSTRAP_REPETITIONS = 2_000
DEFAULT_CONFIDENCE = 0.95
MAX_CONFIDENCE = 0.999999
MIN_CONTINUOUS_BOOTSTRAP_CLUSTERS = 20
MIN_BOOTSTRAP_TAIL_DRAWS = 20
PROVENANCE_ORDER = {
    "legacy_unbound": 1,
    "provenance_bound": 2,
    "validator_recomputed": 3,
}
# A generic ObservationRecord can bind bytes, but it cannot appoint its own
# validator.  Entries added here must name analyzer-owned code that parses a
# versioned method/raw schema and recomputes the metric from those bytes.
REGISTERED_METRIC_VALIDATORS: dict[str, dict[str, Any]] = {}
VALID_DIRECTIONS = {"higher_is_better", "lower_is_better", "neutral"}
METRIC_RE = re.compile(r"^[a-z][a-z0-9_.]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

EVIDENCE_INTEGRITY_METRICS = {
    "runtime.report_file_present",
    "runtime.report_parseable",
    "runtime.pipeline_report_complete",
    "runtime.partial_report",
    "runtime.exit_observed",
    "runtime.exit_acceptable",
    "benchmark.report_file_present",
    "benchmark.report_parseable",
    "benchmark.cell_selected",
    "observation.file_present",
    "observation.parseable",
    "observation.schema_valid",
}

COHORT_STATS = {"pooled_ratio", "mean", "median", "p95", "min", "max"}
COMPARISON_STATS = {
    "paired_delta_mean",
    "paired_delta_median",
    "paired_improvement_mean",
    "paired_improvement_median",
    "paired_win_rate",
}

PROCESSING_EDGES = [
    "capture→preproc",
    "preproc→chunk",
    "chunk→detect",
]
DETECTION_EDGES = [
    "detect→decision",
    "capture→decision",
]


class AnalysisError(ValueError):
    """The spec or an adapter request is structurally invalid."""


@dataclass(frozen=True)
class Sample:
    metric: str
    value: float
    unit: str
    direction: str
    cohort: str
    input_id: str
    cluster_id: str
    pair_id: str | None
    sample_key: str
    comparison_signature: str | None
    provenance_grade: str
    aggregation: str = "distribution"
    numerator: float | None = None
    denominator: float | None = None
    pairable: bool = True
    measurement_uncertainty: float = 0.0


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AnalysisError(f"value is not canonical-JSON serializable: {exc}") from exc


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def ensure_object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalysisError(f"{where} must be an object")
    return value


def reject_unknown(obj: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise AnalysisError(f"{where} has unknown field(s): {', '.join(unknown)}")


def finite_number(value: Any, where: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisError(f"{where} must be a number")
    try:
        out = float(value)
    except (OverflowError, ValueError) as exc:
        raise AnalysisError(f"{where} cannot be represented as a finite number") from exc
    if not math.isfinite(out):
        raise AnalysisError(f"{where} must be finite")
    if nonnegative and out < 0:
        raise AnalysisError(f"{where} must be nonnegative")
    return out


def integer(value: Any, where: str, *, minimum: int = 0) -> int:
    number = finite_number(value, where)
    if not number.is_integer() or number < minimum:
        raise AnalysisError(f"{where} must be an integer >= {minimum}")
    return int(number)


def json_integer(value: Any, where: str, *, minimum: int = 0) -> int:
    """Validate an exact JSON integer without first rounding it through float."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AnalysisError(f"{where} must be an integer >= {minimum}")
    return value


def required_string(obj: dict[str, Any], key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AnalysisError(f"{where}.{key} must be a non-empty string")
    if "\x00" in value:
        raise AnalysisError(f"{where}.{key} may not contain NUL")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise AnalysisError(f"{where}.{key} may not contain control characters")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise AnalysisError(f"{where}.{key} must contain valid Unicode scalar values") from exc
    return value


def metric_name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not METRIC_RE.fullmatch(value):
        raise AnalysisError(f"{where} must match {METRIC_RE.pattern}")
    return value


def edge_slug(edge: str) -> str:
    value = edge.replace("→", "_to_").replace("->", "_to_")
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return value or "unknown"


def quantile(values: Iterable[float], q: float) -> float:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        raise AnalysisError("quantile requested from an empty sample")
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    weight = pos - lo
    lower, upper = ordered[lo], ordered[hi]
    if lower == upper:
        return lower
    # Same-sign interpolation is stable as lower + fraction * range. For
    # opposite signs, the convex form avoids overflowing upper-lower.
    value = (
        lower + (upper - lower) * weight
        if (lower >= 0) == (upper >= 0)
        else lower * (1.0 - weight) + upper * weight
    )
    if not math.isfinite(value):
        raise AnalysisError("quantile interpolation overflowed")
    return value


def finite_sum(values: Iterable[float], where: str) -> float:
    try:
        value = math.fsum(float(item) for item in values)
    except (OverflowError, ValueError) as exc:
        raise AnalysisError(f"{where} overflows a finite aggregate") from exc
    if not math.isfinite(value):
        raise AnalysisError(f"{where} overflows a finite aggregate")
    return value


def pooled_ratio(samples: list[Sample]) -> float:
    numerators = [float(s.numerator) for s in samples if s.numerator is not None]
    denominators = [float(s.denominator) for s in samples if s.denominator is not None]
    scale = max(denominators, default=0.0)
    if scale <= 0:
        raise AnalysisError("pooled_ratio denominator is zero")
    # Scaling before summation keeps valid, finite per-record counts from
    # overflowing merely because several records are pooled.
    numerator = finite_sum((value / scale for value in numerators), "pooled numerator")
    denominator = finite_sum((value / scale for value in denominators), "pooled denominator")
    if denominator <= 0:
        raise AnalysisError("pooled_ratio denominator is zero")
    value = numerator / denominator
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise AnalysisError("pooled_ratio produced an invalid proportion")
    return value


def distribution(values: Iterable[float]) -> dict[str, float]:
    vals = [float(v) for v in values]
    if not vals:
        raise AnalysisError("distribution requested from an empty sample")
    mean = statistics.fmean(vals)
    if not math.isfinite(mean):
        raise AnalysisError("distribution mean overflowed")
    return {
        "min": min(vals),
        "p25": quantile(vals, 0.25),
        "p50": quantile(vals, 0.50),
        "p75": quantile(vals, 0.75),
        "p95": quantile(vals, 0.95),
        "p99": quantile(vals, 0.99),
        "max": max(vals),
        "mean": mean,
    }


def wilson_interval(proportion: float, independent_n: int, confidence: float) -> tuple[float, float]:
    """Wilson score interval using independent clusters as effective n."""
    if independent_n <= 0:
        raise AnalysisError("Wilson interval requires a positive independent sample count")
    if not 0.0 <= proportion <= 1.0:
        raise AnalysisError("Wilson interval requires a proportion inside [0, 1]")
    transformed_probability = 0.5 + confidence / 2.0
    if not 0.5 < transformed_probability < 1.0:
        raise AnalysisError("confidence is too close to 1 for a finite Wilson interval")
    try:
        z = statistics.NormalDist().inv_cdf(transformed_probability)
    except statistics.StatisticsError as exc:
        raise AnalysisError("confidence cannot produce a finite Wilson interval") from exc
    n = float(independent_n)
    denominator = 1.0 + z * z / n
    center = (proportion + z * z / (2.0 * n)) / denominator
    radius = z * math.sqrt(proportion * (1.0 - proportion) / n + z * z / (4.0 * n * n)) / denominator
    return max(0.0, center - radius), min(1.0, center + radius)


def statistic(samples: list[Sample], stat: str) -> float:
    if not samples:
        raise AnalysisError("statistic requested from an empty sample")
    values = [s.value for s in samples]
    if stat == "pooled_ratio":
        if not all(
            s.aggregation == "ratio"
            and s.numerator is not None
            and s.denominator is not None
            for s in samples
        ):
            raise AnalysisError("pooled_ratio requires ratio samples")
        return pooled_ratio(samples)
    if stat == "mean":
        value = statistics.fmean(values)
        if not math.isfinite(value):
            raise AnalysisError("mean statistic overflowed")
        return value
    if stat == "median":
        return quantile(values, 0.5)
    if stat == "p95":
        return quantile(values, 0.95)
    if stat == "min":
        return min(values)
    if stat == "max":
        return max(values)
    raise AnalysisError(f"unsupported cohort statistic {stat!r}")


def bootstrap_ci(
    samples: list[Sample],
    stat: str,
    confidence: float,
    repetitions: int,
    seed_material: str,
) -> dict[str, Any]:
    clusters: dict[str, list[Sample]] = defaultdict(list)
    for sample in samples:
        clusters[sample.cluster_id].append(sample)
    cluster_ids = sorted(clusters)
    if len(cluster_ids) < 2:
        return {
            "available": False,
            "method": "cluster_bootstrap",
            "reason": "fewer than two independent clusters",
        }

    if stat == "pooled_ratio" and not (
        len(samples) == len(cluster_ids)
        and all(
            sample.aggregation == "ratio"
            and sample.denominator == 1.0
            and sample.numerator in {0.0, 1.0}
            for sample in samples
        )
    ):
        return {
            "available": False,
            "method": "wilson_score",
            "reason": (
                "proof-safe pooled-ratio bounds require exactly one Bernoulli "
                "trial per independent cluster"
            ),
        }
    if stat != "pooled_ratio" and len(cluster_ids) < MIN_CONTINUOUS_BOOTSTRAP_CLUSTERS:
        return {
            "available": False,
            "method": "cluster_bootstrap",
            "reason": (
                "continuous percentile bootstrap requires at least "
                f"{MIN_CONTINUOUS_BOOTSTRAP_CLUSTERS} independent clusters"
            ),
        }
    expected_tail_draws = repetitions * (1.0 - confidence) / 2.0
    if stat != "pooled_ratio" and expected_tail_draws < MIN_BOOTSTRAP_TAIL_DRAWS:
        return {
            "available": False,
            "method": "cluster_bootstrap",
            "reason": (
                "continuous percentile bootstrap requires at least "
                f"{MIN_BOOTSTRAP_TAIL_DRAWS} expected resamples in each tail; "
                "increase bootstrap_repetitions or lower confidence"
            ),
        }

    seed = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(repetitions):
        selected = [rng.choice(cluster_ids) for _ in cluster_ids]
        draw: list[Sample] = []
        for cluster in selected:
            draw.extend(clusters[cluster])
        estimates.append(statistic(draw, stat))
    alpha = (1.0 - confidence) / 2.0
    uncertainty_bound = max((s.measurement_uncertainty for s in samples), default=0.0)
    lower = quantile(estimates, alpha)
    upper = quantile(estimates, 1.0 - alpha)
    if stat == "pooled_ratio":
        # A percentile bootstrap collapses to [1,1] after all-success samples.
        # Widen it by a Wilson bound whose n is the number of independent
        # clusters, never the (possibly correlated) within-run denominator.
        wilson_lower, wilson_upper = wilson_interval(
            statistic(samples, "pooled_ratio"), len(cluster_ids), confidence
        )
        lower = min(lower, wilson_lower)
        upper = max(upper, wilson_upper)
    widened_lower = lower - uncertainty_bound
    widened_upper = upper + uncertainty_bound
    if not math.isfinite(widened_lower) or not math.isfinite(widened_upper):
        raise AnalysisError("measurement uncertainty overflowed the confidence interval")
    return {
        "available": True,
        "method": "deterministic_cluster_bootstrap",
        "confidence": confidence,
        "repetitions": repetitions,
        # Conservatively widen the sampling interval by the largest stated
        # measurement error bound. This is intentionally not RSS: clock/error
        # terms may be systematic across the whole run.
        "measurement_uncertainty_bound": uncertainty_bound,
        "lower": widened_lower,
        "upper": widened_upper,
    }


def effective_sample_count(samples: list[Sample], stat: str) -> float | int:
    if stat == "pooled_ratio" and samples and all(s.denominator is not None for s in samples):
        return finite_sum(
            (float(s.denominator) for s in samples), "effective ratio denominator"
        )
    return len(samples)


def provenance_at_least(sample: Sample, minimum: str) -> bool:
    return PROVENANCE_ORDER[sample.provenance_grade] >= PROVENANCE_ORDER[minimum]


def comparison_signature(control_signature: str | None, context: Any, extra: Any = None) -> str | None:
    if control_signature is None:
        return None
    return canonical_hash(
        {
            "controls": control_signature,
            "context": context,
            "extra": extra,
        }
    )


def snapshot_file_bytes(
    path: Path, cache: dict[Path, tuple[bytes | None, str | None]]
) -> tuple[bytes | None, str | None]:
    resolved = path.resolve()
    if resolved not in cache:
        try:
            cache[resolved] = (resolved.read_bytes(), None)
        except OSError as exc:
            cache[resolved] = (None, str(exc))
    return cache[resolved]


def load_controls(
    item: dict[str, Any],
    base_dir: Path,
    where: str,
    byte_cache: dict[Path, tuple[bytes | None, str | None]],
) -> tuple[list[dict[str, Any]], str | None]:
    raw = item.get("controls", [])
    if not isinstance(raw, list):
        raise AnalysisError(f"{where}.controls must be an array")
    audits: list[dict[str, Any]] = []
    signatures: list[dict[str, str]] = []
    roles: set[str] = set()
    complete = bool(raw)
    for index, entry in enumerate(raw):
        entry_where = f"{where}.controls[{index}]"
        obj = ensure_object(entry, entry_where)
        reject_unknown(obj, {"role", "path"}, entry_where)
        role = required_string(obj, "role", entry_where)
        path_text = required_string(obj, "path", entry_where)
        if role in roles:
            raise AnalysisError(f"{where}.controls repeats role {role!r}")
        roles.add(role)
        path = (base_dir / path_text).resolve()
        payload, snapshot_error = snapshot_file_bytes(path, byte_cache)
        exists = payload is not None
        digest = None
        error = snapshot_error
        if exists:
            digest = hashlib.sha256(payload).hexdigest()
        audits.append(
            {"role": role, "path": path_text, "exists": exists, "sha256": digest, "error": error}
        )
        if exists and digest is not None:
            signatures.append({"role": role, "sha256": digest})
        else:
            complete = False
    if not complete:
        return audits, None
    return audits, canonical_hash(sorted(signatures, key=lambda row: row["role"]))


def read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return parse_json_bytes(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return None, str(exc)


def strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def reject_json_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant {token!r} is not permitted")


def parse_json_bytes(payload: bytes) -> tuple[Any | None, str | None]:
    try:
        return (
            json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=strict_json_object,
                parse_constant=reject_json_constant,
            ),
            None,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        return None, str(exc)


def validate_clock_anchor(
    value: Any,
    *,
    expected_host_vantage: str,
    expected_host_boot_id: str | None,
) -> dict[str, Any]:
    envelope = ensure_object(value, "clock anchor")
    reject_unknown(
        envelope,
        {
            "schema_version",
            "record_type",
            "host_boot_id",
            "host_vantage",
            "clock_kind",
            "anchors",
            "fit",
        },
        "clock anchor",
    )
    if envelope.get("schema_version") != 1 or envelope.get("record_type") != "earshot.clock_anchor":
        raise AnalysisError("clock anchor schema_version/record_type is invalid")
    host_boot_id = required_string(envelope, "host_boot_id", "clock anchor")
    host_vantage = required_string(envelope, "host_vantage", "clock anchor")
    if host_vantage != expected_host_vantage:
        raise AnalysisError("clock anchor host_vantage does not match the observation")
    if expected_host_boot_id is not None and host_boot_id != expected_host_boot_id:
        raise AnalysisError("clock anchor host_boot_id does not match the observation")
    if envelope.get("clock_kind") != "monotonic_raw":
        raise AnalysisError("clock anchor clock_kind must be monotonic_raw")
    anchors = envelope.get("anchors")
    if not isinstance(anchors, list) or len(anchors) < 2:
        raise AnalysisError("clock anchor requires at least two anchors")
    parsed: list[tuple[int, int]] = []
    for index, raw_anchor in enumerate(anchors):
        anchor = ensure_object(raw_anchor, f"clock anchor.anchors[{index}]")
        reject_unknown(anchor, {"monotonic_ns", "utc_ns"}, f"clock anchor.anchors[{index}]")
        parsed.append(
            (
                json_integer(anchor.get("monotonic_ns"), f"clock anchor.anchors[{index}].monotonic_ns"),
                json_integer(anchor.get("utc_ns"), f"clock anchor.anchors[{index}].utc_ns"),
            )
        )
    if any(
        later_mono <= earlier_mono or later_utc <= earlier_utc
        for (earlier_mono, earlier_utc), (later_mono, later_utc) in zip(parsed, parsed[1:])
    ):
        raise AnalysisError("clock anchor timestamps must be strictly increasing")
    fit = ensure_object(envelope.get("fit"), "clock anchor.fit")
    reject_unknown(fit, {"rate", "offset_ns", "max_residual_ns"}, "clock anchor.fit")
    rate = finite_number(fit.get("rate"), "clock anchor.fit.rate")
    if rate <= 0:
        raise AnalysisError("clock anchor.fit.rate must be positive")
    offset_ns = json_integer(fit.get("offset_ns"), "clock anchor.fit.offset_ns", minimum=-(10**30))
    max_residual_ns = finite_number(
        fit.get("max_residual_ns"), "clock anchor.fit.max_residual_ns", nonnegative=True
    )
    try:
        decimal_rate = Decimal(str(rate))
        decimal_offset = Decimal(offset_ns)
        residual_limit = Decimal(str(max_residual_ns))
        for monotonic_ns, utc_ns in parsed:
            residual = abs(
                decimal_rate * Decimal(monotonic_ns) + decimal_offset - Decimal(utc_ns)
            )
            if residual > residual_limit:
                raise AnalysisError("clock anchor fit residual exceeds max_residual_ns")
    except (InvalidOperation, OverflowError) as exc:
        raise AnalysisError("clock anchor fit cannot be evaluated") from exc
    return {
        "record_type": "earshot.clock_anchor",
        "host_boot_id": host_boot_id,
        "host_vantage": host_vantage,
        "clock_kind": "monotonic_raw",
        "anchor_count": len(parsed),
        "rate": rate,
        "offset_ns": offset_ns,
        "max_residual_ns": max_residual_ns,
    }


class Extractor:
    def __init__(self, spec_dir: Path):
        self.spec_dir = spec_dir
        self.samples: list[Sample] = []
        self.input_records: list[dict[str, Any]] = []
        self.refusals: list[dict[str, str]] = []
        self.warnings: set[str] = set()
        self.snapshots: dict[str, tuple[Any | None, str | None]] = {}
        self.byte_snapshots: dict[Path, tuple[bytes | None, str | None]] = {}

    def refuse(self, input_id: str, code: str, reason: str) -> None:
        self.refusals.append({"input_id": input_id, "code": code, "reason": reason})

    def emit(
        self,
        audit: dict[str, Any],
        name: str,
        value: float,
        unit: str,
        direction: str,
        *,
        aggregation: str = "distribution",
        numerator: float | None = None,
        denominator: float | None = None,
        sample_key: str = "",
        pairable: bool = True,
        comparison_extra: Any = None,
        measurement_uncertainty: float = 0.0,
    ) -> None:
        metric_name(name, f"metric from input {audit['id']}")
        value = finite_number(value, f"{audit['id']}:{name}")
        if not isinstance(unit, str) or not unit:
            raise AnalysisError(f"{audit['id']}:{name} has an empty unit")
        if direction not in VALID_DIRECTIONS:
            raise AnalysisError(f"{audit['id']}:{name} has invalid direction {direction!r}")
        if aggregation not in {"distribution", "ratio"}:
            raise AnalysisError(f"{audit['id']}:{name} has invalid aggregation {aggregation!r}")
        if aggregation == "ratio":
            if numerator is None or denominator is None or denominator <= 0:
                raise AnalysisError(f"{audit['id']}:{name} ratio requires denominator > 0")
            finite_number(numerator, f"{audit['id']}:{name}.numerator")
            finite_number(denominator, f"{audit['id']}:{name}.denominator", nonnegative=True)
            if numerator < 0 or numerator > denominator:
                raise AnalysisError(
                    f"{audit['id']}:{name} ratio numerator must be inside [0, denominator]"
                )
        signature = audit.get("comparison_signature")
        if comparison_extra is not None:
            signature = comparison_signature(
                audit.get("control_signature"), audit.get("comparison_context", {}), comparison_extra
            )
        self.samples.append(
            Sample(
                metric=name,
                value=value,
                unit=unit,
                direction=direction,
                cohort=audit["cohort"],
                input_id=audit["id"],
                cluster_id=audit["cluster_identity"],
                pair_id=audit.get("pair_id"),
                sample_key=sample_key,
                comparison_signature=signature,
                provenance_grade=audit["provenance_grade"],
                aggregation=aggregation,
                numerator=numerator,
                denominator=denominator,
                pairable=pairable,
                measurement_uncertainty=finite_number(
                    measurement_uncertainty,
                    f"{audit['id']}:{name}.measurement_uncertainty",
                    nonnegative=True,
                ),
            )
        )

    def emit_ratio(
        self,
        audit: dict[str, Any],
        name: str,
        numerator: float,
        denominator: float,
        direction: str,
        *,
        sample_key: str = "",
        pairable: bool = True,
    ) -> None:
        if denominator <= 0:
            self.refuse(audit["id"], "zero_denominator", f"{name} denominator is zero")
            return
        if numerator < 0 or numerator > denominator:
            self.refuse(
                audit["id"],
                "invalid_ratio",
                f"{name} numerator {numerator} is outside [0, {denominator}]",
            )
            return
        self.emit(
            audit,
            name,
            numerator / denominator,
            "proportion",
            direction,
            aggregation="ratio",
            numerator=numerator,
            denominator=denominator,
            sample_key=sample_key,
            pairable=pairable,
        )

    def extract(self, item: dict[str, Any], index: int) -> None:
        where = f"inputs[{index}]"
        reject_unknown(
            item,
            {"id", "kind", "cohort", "pair_id", "path", "selector", "controls", "context"},
            where,
        )
        input_id = required_string(item, "id", where)
        kind = required_string(item, "kind", where)
        cohort = required_string(item, "cohort", where)
        if cohort not in {"baseline", "candidate"}:
            raise AnalysisError(f"{where}.cohort must be baseline or candidate")
        path_text = required_string(item, "path", where)
        pair_id = item.get("pair_id")
        if pair_id is not None and (not isinstance(pair_id, str) or not pair_id.strip()):
            raise AnalysisError(f"{where}.pair_id must be a non-empty string when present")
        if pair_id is not None and any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in pair_id
        ):
            raise AnalysisError(f"{where}.pair_id may not contain control characters")
        context = item.get("context", {})
        if not isinstance(context, dict):
            raise AnalysisError(f"{where}.context must be an object")
        controls, control_sig = load_controls(
            item, self.spec_dir, where, self.byte_snapshots
        )
        if kind == "observation_record" and pair_id is not None:
            control_roles = {control["role"] for control in controls}
            required_pair_roles = {"measurement_protocol", "pair_manifest"}
            missing_pair_roles = sorted(required_pair_roles - control_roles)
            if missing_pair_roles:
                raise AnalysisError(
                    f"{where} paired observation requires control role(s): "
                    + ", ".join(missing_pair_roles)
                )
        path = (self.spec_dir / path_text).resolve()
        payload, snapshot_error = snapshot_file_bytes(path, self.byte_snapshots)
        exists = payload is not None
        digest = None
        hash_error = snapshot_error
        if exists:
            assert payload is not None
            digest = hashlib.sha256(payload).hexdigest()
            self.snapshots[input_id] = parse_json_bytes(payload)
        else:
            self.snapshots[input_id] = (None, snapshot_error or "file does not exist")
        audit: dict[str, Any] = {
            "id": input_id,
            "kind": kind,
            "cohort": cohort,
            "pair_id": pair_id,
            "path": path_text,
            "exists": exists,
            "sha256": digest,
            "hash_error": hash_error,
            "selector": item.get("selector", {}),
            "parseable": None,
            "status": "pending",
            "performance_eligible": False,
            "provenance_grade": "legacy_unbound",
            "controls": controls,
            "control_signature": control_sig,
            "comparison_context": context,
            "comparison_signature": comparison_signature(control_sig, context),
            "cluster_identity": (
                f"{kind}:sha256:{digest}"
                if digest is not None
                else f"{kind}:expected:{path_text}"
            ),
        }
        self.input_records.append(audit)
        if kind == "runtime_report":
            sample_start = len(self.samples)
            try:
                self.extract_runtime(item, path, audit)
            except (AnalysisError, OverflowError) as exc:
                del self.samples[sample_start:]
                audit["status"] = "invalid"
                self.emit_ratio(
                    audit,
                    "runtime.report_file_present",
                    int(audit["exists"]),
                    1,
                    "higher_is_better",
                )
                self.emit_ratio(
                    audit,
                    "runtime.report_parseable",
                    int(bool(audit.get("parseable"))),
                    1,
                    "higher_is_better",
                )
                self.emit_ratio(
                    audit, "runtime.pipeline_report_complete", 0, 1, "higher_is_better"
                )
                self.emit_ratio(audit, "runtime.exit_observed", 0, 1, "higher_is_better")
                self.emit_ratio(audit, "runtime.exit_acceptable", 0, 1, "higher_is_better")
                self.refuse(audit["id"], "malformed_runtime_report", str(exc))
        elif kind == "benchmark_report":
            sample_start = len(self.samples)
            try:
                self.extract_benchmark(item, path, audit)
            except (AnalysisError, OverflowError) as exc:
                del self.samples[sample_start:]
                audit["status"] = "invalid"
                self.emit_ratio(
                    audit,
                    "benchmark.report_file_present",
                    int(audit["exists"]),
                    1,
                    "higher_is_better",
                )
                self.emit_ratio(
                    audit,
                    "benchmark.report_parseable",
                    int(bool(audit.get("parseable"))),
                    1,
                    "higher_is_better",
                )
                self.emit_ratio(audit, "benchmark.cell_selected", 0, 1, "higher_is_better")
                self.refuse(audit["id"], "malformed_benchmark_report", str(exc))
        elif kind == "observation_record":
            sample_start = len(self.samples)
            try:
                self.extract_observation(item, path, audit)
            except (AnalysisError, OverflowError) as exc:
                # Observation content is evidence, not configuration. A bad
                # record stays visible as a refused input and must not abort or
                # erase the rest of the declared denominator.
                del self.samples[sample_start:]
                audit["provenance_grade"] = "legacy_unbound"
                audit["status"] = "invalid"
                self.emit_ratio(
                    audit,
                    "observation.file_present",
                    int(audit["exists"]),
                    1,
                    "higher_is_better",
                )
                self.emit_ratio(
                    audit,
                    "observation.parseable",
                    int(bool(audit.get("parseable"))),
                    1,
                    "higher_is_better",
                )
                self.emit_ratio(audit, "observation.schema_valid", 0, 1, "higher_is_better")
                self.refuse(audit["id"], "malformed_observation", str(exc))
        else:
            raise AnalysisError(f"{where}.kind has unsupported value {kind!r}")

    def extract_runtime(self, item: dict[str, Any], path: Path, audit: dict[str, Any]) -> None:
        selector = item.get("selector", {})
        selector = ensure_object(selector, f"input {audit['id']}.selector")
        reject_unknown(selector, {"acceptable_exit_reasons", "required_edges"}, f"input {audit['id']}.selector")
        acceptable = selector.get("acceptable_exit_reasons", ["clean_drain", "stop_file", "sigterm"])
        required_edges = selector.get("required_edges")
        if not isinstance(acceptable, list) or not acceptable or not all(isinstance(v, str) for v in acceptable):
            raise AnalysisError(f"input {audit['id']}.acceptable_exit_reasons must be a non-empty string array")
        if required_edges is not None and (
            not isinstance(required_edges, list)
            or not all(isinstance(v, str) and v for v in required_edges)
        ):
            raise AnalysisError(f"input {audit['id']}.required_edges must be a string array")

        self.emit_ratio(audit, "runtime.report_file_present", int(audit["exists"]), 1, "higher_is_better")
        if not audit["exists"]:
            audit["parseable"] = False
            audit["status"] = "missing"
            self.emit_ratio(audit, "runtime.report_parseable", 0, 1, "higher_is_better")
            self.emit_ratio(audit, "runtime.pipeline_report_complete", 0, 1, "higher_is_better")
            self.emit_ratio(audit, "runtime.exit_observed", 0, 1, "higher_is_better")
            self.emit_ratio(audit, "runtime.exit_acceptable", 0, 1, "higher_is_better")
            self.refuse(audit["id"], "missing_artifact", f"expected runtime report is missing: {audit['path']}")
            return
        data, error = self.snapshots[audit["id"]]
        parseable = error is None and isinstance(data, dict)
        audit["parseable"] = parseable
        self.emit_ratio(audit, "runtime.report_parseable", int(parseable), 1, "higher_is_better")
        if not parseable:
            audit["status"] = "invalid"
            self.emit_ratio(audit, "runtime.pipeline_report_complete", 0, 1, "higher_is_better")
            self.emit_ratio(audit, "runtime.exit_observed", 0, 1, "higher_is_better")
            self.emit_ratio(audit, "runtime.exit_acceptable", 0, 1, "higher_is_better")
            self.refuse(audit["id"], "invalid_json", error or "runtime report root is not an object")
            return
        report = ensure_object(data, f"runtime report {audit['path']}")
        audit["status"] = "loaded"
        self.warnings.add(
            "runtime_report inputs are legacy_unbound: current reports do not bind run identity, config, model, binary, source, or clock hashes"
        )
        def nonnegative_number(value: Any) -> bool:
            try:
                finite_number(value, "runtime numeric field", nonnegative=True)
                return True
            except AnalysisError:
                return False

        complete = (
            not bool(report.get("partial_report"))
            and isinstance(report.get("event_id"), str)
            and bool(report.get("event_id"))
            and nonnegative_number(report.get("frames_in"))
            and nonnegative_number(report.get("chunks_in"))
            and isinstance(report.get("edges"), dict)
            and isinstance(report.get("detections"), list)
            and isinstance(report.get("drops"), dict)
            and all(
                nonnegative_number(report["drops"].get(key))
                for key in ("source_to_preproc", "preproc_to_chunker", "chunker_to_detector")
            )
        )
        self.emit_ratio(audit, "runtime.pipeline_report_complete", int(complete), 1, "higher_is_better")
        self.emit_ratio(audit, "runtime.partial_report", int(bool(report.get("partial_report"))), 1, "lower_is_better")

        exit_record = report.get("exit")
        exit_observed = isinstance(exit_record, dict) and isinstance(exit_record.get("reason"), str)
        exit_acceptable = False
        self.emit_ratio(audit, "runtime.exit_observed", int(exit_observed), 1, "higher_is_better")
        if exit_observed:
            exit_acceptable = exit_record["reason"] in acceptable
            self.emit_ratio(
                audit,
                "runtime.exit_acceptable",
                int(exit_acceptable),
                1,
                "higher_is_better",
            )
            if not exit_acceptable:
                self.refuse(
                    audit["id"],
                    "unacceptable_exit",
                    f"exit reason {exit_record['reason']!r} is outside the declared acceptable set",
                )
        else:
            self.emit_ratio(audit, "runtime.exit_acceptable", 0, 1, "higher_is_better")
            self.refuse(audit["id"], "missing_exit", "runtime report has no typed exit reason")

        frames = report.get("frames_in")
        chunks = report.get("chunks_in")
        if isinstance(frames, (int, float)) and not isinstance(frames, bool) and frames >= 0:
            frames_f = finite_number(frames, f"{audit['id']}.frames_in", nonnegative=True)
            self.emit(audit, "runtime.frames_in", frames_f, "frames", "neutral")
            self.emit_ratio(audit, "runtime.ingress_present", int(frames_f > 0), 1, "higher_is_better")
        else:
            frames_f = None
            self.refuse(audit["id"], "missing_frames", "frames_in is missing or invalid")
        if isinstance(chunks, (int, float)) and not isinstance(chunks, bool) and chunks >= 0:
            self.emit(audit, "runtime.chunks_in", float(chunks), "chunks", "neutral")

        detections = report.get("detections")
        if isinstance(detections, list):
            self.emit(audit, "runtime.detections", len(detections), "detections", "neutral")
            for det_index, det in enumerate(detections):
                if not isinstance(det, dict):
                    self.refuse(audit["id"], "malformed_detection", f"detections[{det_index}] is not an object")
                    continue
                latency_us = det.get("detect_latency_us")
                if isinstance(latency_us, (int, float)) and not isinstance(latency_us, bool) and latency_us >= 0:
                    self.emit(
                        audit,
                        "runtime.chunk_first_capture_to_detection_emit_ms",
                        float(latency_us) / 1000.0,
                        "ms",
                        "lower_is_better",
                        sample_key=f"detection-{det_index}",
                        pairable=False,
                    )
        else:
            self.refuse(audit["id"], "missing_detections", "detections is missing or invalid")

        if required_edges is None:
            required_edges = list(PROCESSING_EDGES)
            if isinstance(detections, list) and detections:
                required_edges.extend(DETECTION_EDGES)

        drops = report.get("drops")
        if isinstance(drops, dict):
            try:
                source_drop = finite_number(drops.get("source_to_preproc"), "source_to_preproc", nonnegative=True)
                preproc_drop = finite_number(drops.get("preproc_to_chunker"), "preproc_to_chunker", nonnegative=True)
                detector_drop = finite_number(drops.get("chunker_to_detector"), "chunker_to_detector", nonnegative=True)
            except AnalysisError as exc:
                self.refuse(audit["id"], "malformed_drops", str(exc))
            else:
                total_drop = finite_sum(
                    (source_drop, preproc_drop, detector_drop),
                    f"{audit['id']} total drops",
                )
                self.emit(audit, "runtime.total_drops", total_drop, "frames", "lower_is_better")
                self.emit(audit, "runtime.ingress_drops", source_drop, "frames", "lower_is_better")
                self.emit(
                    audit,
                    "runtime.interior_drops",
                    preproc_drop + detector_drop,
                    "frames",
                    "lower_is_better",
                )
                if frames_f is not None and frames_f > 0:
                    self.emit(
                        audit,
                        "runtime.reported_drop_pct",
                        (total_drop / frames_f) * 100.0,
                        "percent",
                        "lower_is_better",
                    )
                else:
                    self.refuse(audit["id"], "zero_drop_denominator", "cannot compute report-contract drop percentage")
        else:
            self.refuse(audit["id"], "missing_drops", "drops is missing or invalid")

        legs = report.get("per_leg")
        if isinstance(legs, list) and legs:
            dead = sum(1 for leg in legs if isinstance(leg, dict) and leg.get("died") is not None)
            self.emit_ratio(audit, "runtime.dead_leg_fraction", dead, len(legs), "lower_is_better")

        orders = report.get("orders")
        if isinstance(orders, list):
            intents = report.get("intents", len(orders))
            sent = report.get("sent")
            acked = report.get("acked")
            if not isinstance(sent, (int, float)) or isinstance(sent, bool):
                sent = sum(1 for order in orders if isinstance(order, dict) and order.get("send_ts") is not None)
            if not isinstance(acked, (int, float)) or isinstance(acked, bool):
                acked = sum(1 for order in orders if isinstance(order, dict) and order.get("order_id"))
            if isinstance(intents, (int, float)) and not isinstance(intents, bool):
                self.emit(audit, "runtime.intents", float(intents), "intents", "neutral")
            self.emit(audit, "runtime.sent", float(sent), "attempts", "neutral")
            self.emit(audit, "runtime.acked", float(acked), "attempts", "neutral")
            if sent > 0:
                self.emit_ratio(audit, "runtime.sent_ack_fraction", float(acked), float(sent), "higher_is_better")
            elif acked:
                self.refuse(
                    audit["id"],
                    "inconsistent_send_ack",
                    "report has acknowledgements but zero observed sends",
                )
        lost_races = report.get("lost_races")
        if isinstance(lost_races, (int, float)) and not isinstance(lost_races, bool):
            self.emit(audit, "runtime.lost_races", float(lost_races), "intents", "lower_is_better")

        edges = report.get("edges")
        if not isinstance(edges, dict):
            self.refuse(audit["id"], "missing_edges", "edges is missing or invalid")
            return
        for required_edge in required_edges:
            slug = edge_slug(required_edge)
            if required_edge not in edges:
                self.refuse(
                    audit["id"],
                    "missing_required_edge",
                    f"required telemetry edge {required_edge!r} is absent",
                )
            self.emit_ratio(
                audit,
                f"runtime.edge.{slug}.present",
                int(required_edge in edges),
                1,
                "higher_is_better",
            )
        fields = ["p50_us", "p90_us", "p95_us", "p99_us", "max_us"]
        for edge, stats in sorted(edges.items()):
            if not isinstance(edge, str) or not isinstance(stats, dict):
                self.refuse(audit["id"], "malformed_edge", f"invalid edge row {edge!r}")
                continue
            slug = edge_slug(edge)
            count = stats.get("count")
            if isinstance(count, (int, float)) and not isinstance(count, bool) and count >= 0:
                self.emit(audit, f"runtime.edge.{slug}.sample_count", float(count), "samples", "neutral")
            for field in fields:
                value = stats.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    quant = field.removesuffix("_us")
                    self.emit(
                        audit,
                        f"runtime.edge.{slug}.{quant}_ms",
                        float(value) / 1000.0,
                        "ms",
                        "lower_is_better",
                    )
        audit["performance_eligible"] = bool(complete and exit_observed and exit_acceptable) and not any(
            row["input_id"] == audit["id"] for row in self.refusals
        )

    def extract_benchmark(self, item: dict[str, Any], path: Path, audit: dict[str, Any]) -> None:
        selector = ensure_object(item.get("selector", {}), f"input {audit['id']}.selector")
        reject_unknown(selector, {"engine", "chunk_ms", "model_dir", "tier", "phrases"}, f"input {audit['id']}.selector")
        required_string(selector, "engine", f"input {audit['id']}.selector")
        integer(selector.get("chunk_ms"), f"input {audit['id']}.selector.chunk_ms", minimum=1)
        tier = required_string(selector, "tier", f"input {audit['id']}.selector").lower()
        if tier not in {"fast", "confirmed"}:
            raise AnalysisError(f"input {audit['id']}.selector.tier must be fast or confirmed")
        phrases = selector.get("phrases")
        if phrases is not None and (
            not isinstance(phrases, list)
            or not phrases
            or not all(isinstance(v, str) and v for v in phrases)
        ):
            raise AnalysisError(f"input {audit['id']}.selector.phrases must be a non-empty string array")
        if phrases is not None and len(set(phrases)) != len(phrases):
            raise AnalysisError(f"input {audit['id']}.selector.phrases contains duplicates")

        self.emit_ratio(audit, "benchmark.report_file_present", int(audit["exists"]), 1, "higher_is_better")
        if not audit["exists"]:
            audit["parseable"] = False
            audit["status"] = "missing"
            self.emit_ratio(audit, "benchmark.report_parseable", 0, 1, "higher_is_better")
            self.emit_ratio(audit, "benchmark.cell_selected", 0, 1, "higher_is_better")
            self.refuse(audit["id"], "missing_artifact", f"expected benchmark report is missing: {audit['path']}")
            return
        data, error = self.snapshots[audit["id"]]
        parseable = error is None and isinstance(data, dict)
        audit["parseable"] = parseable
        self.emit_ratio(audit, "benchmark.report_parseable", int(parseable), 1, "higher_is_better")
        if not parseable:
            audit["status"] = "invalid"
            self.emit_ratio(audit, "benchmark.cell_selected", 0, 1, "higher_is_better")
            self.refuse(audit["id"], "invalid_json", error or "benchmark report root is not an object")
            return
        report = ensure_object(data, f"benchmark report {audit['path']}")
        audit["status"] = "loaded"
        if not audit["controls"] and audit["sha256"] is not None:
            auto = [{"role": "benchmark_invocation", "path": audit["path"], "exists": True, "sha256": audit["sha256"]}]
            audit["controls"] = auto
            audit["control_signature"] = canonical_hash(
                [{"role": "benchmark_invocation", "sha256": audit["sha256"]}]
            )

        cells = report.get("cells")
        if not isinstance(cells, list):
            self.emit_ratio(audit, "benchmark.cell_selected", 0, 1, "higher_is_better")
            self.refuse(audit["id"], "missing_cells", "benchmark report has no cells array")
            return

        def matches(cell: Any) -> bool:
            if not isinstance(cell, dict):
                return False
            if str(cell.get("engine", "")).lower() != str(selector["engine"]).lower():
                return False
            if cell.get("chunk_ms") != selector["chunk_ms"]:
                return False
            if "model_dir" in selector and cell.get("model_dir") != selector.get("model_dir"):
                return False
            return True

        selected = [cell for cell in cells if matches(cell)]
        self.emit_ratio(audit, "benchmark.cell_selected", int(len(selected) == 1), 1, "higher_is_better")
        if len(selected) != 1:
            self.refuse(
                audit["id"],
                "ambiguous_cell",
                f"benchmark selector matched {len(selected)} cells; expected exactly one",
            )
            return
        cell = selected[0]
        rows = cell.get("rows")
        if not isinstance(rows, list):
            self.refuse(audit["id"], "missing_rows", "selected benchmark cell has no rows")
            return
        selected_rows = [
            row
            for row in rows
            if isinstance(row, dict)
            and str(row.get("tier", "")).lower() == tier
            and (phrases is None or row.get("phrase") in phrases)
        ]
        if not selected_rows:
            self.refuse(audit["id"], "empty_rows", "no benchmark rows match tier/phrase selector")
            return
        invalid_phrase_rows = [
            index
            for index, row in enumerate(selected_rows)
            if not isinstance(row.get("phrase"), str) or not row["phrase"].strip()
        ]
        if invalid_phrase_rows:
            self.refuse(
                audit["id"],
                "invalid_benchmark_phrase",
                "selected benchmark row(s) have an empty or non-string phrase: "
                + ", ".join(str(index) for index in invalid_phrase_rows),
            )
            return
        row_phrases = [row["phrase"] for row in selected_rows]
        duplicates = sorted(phrase for phrase, count in Counter(row_phrases).items() if count > 1)
        if duplicates:
            self.refuse(
                audit["id"],
                "duplicate_benchmark_rows",
                f"selected cell repeats phrase/tier row(s): {', '.join(duplicates)}",
            )
            return
        if phrases is not None and set(row_phrases) != set(phrases):
            missing = sorted(set(phrases) - set(row_phrases))
            self.refuse(
                audit["id"],
                "missing_requested_phrases",
                f"selected cell lacks requested phrase row(s): {', '.join(missing)}",
            )
            return

        comparison_context = {
            "declared": item.get("context", {}),
            "split": report.get("split"),
            "paced": report.get("paced"),
            "calls": report.get("calls"),
            "corpus_hours": report.get("corpus_hours"),
            "tier": tier,
            "phrases": sorted(phrases) if phrases is not None else sorted(
                str(row.get("phrase")) for row in selected_rows
            ),
        }
        audit["comparison_context"] = comparison_context
        audit["comparison_signature"] = comparison_signature(
            audit["control_signature"], comparison_context
        )

        totals = {"true_positives": 0.0, "misses": 0.0, "false_alarms": 0.0}
        for row in selected_rows:
            for key in totals:
                totals[key] += finite_number(row.get(key), f"{audit['id']}.{key}", nonnegative=True)
        tp, misses, false_alarms = (
            totals["true_positives"],
            totals["misses"],
            totals["false_alarms"],
        )
        self.emit(audit, "benchmark.true_positives", tp, "occurrences", "neutral")
        self.emit(audit, "benchmark.misses", misses, "occurrences", "lower_is_better")
        self.emit(audit, "benchmark.false_alarms", false_alarms, "detections", "lower_is_better")
        self.emit_ratio(audit, "benchmark.recall", tp, tp + misses, "higher_is_better")
        self.emit_ratio(audit, "benchmark.precision", tp, tp + false_alarms, "higher_is_better")
        corpus_hours = report.get("corpus_hours")
        if isinstance(corpus_hours, (int, float)) and not isinstance(corpus_hours, bool) and corpus_hours > 0:
            self.emit(
                audit,
                "benchmark.false_alarms_per_hour",
                false_alarms / float(corpus_hours),
                "alarms_per_hour",
                "lower_is_better",
            )
        else:
            self.refuse(audit["id"], "invalid_corpus_hours", "corpus_hours is missing or zero")

        latency_fields = {
            "latency_p50_ms": "benchmark.phrase_onset_to_emit_p50_ms",
            "latency_p95_ms": "benchmark.phrase_onset_to_emit_p95_ms",
            "latency_max_ms": "benchmark.phrase_onset_to_emit_max_ms",
            "emit_lag_p50_ms": "benchmark.phrase_engine_emit_lag_p50_ms",
            "emit_lag_p95_ms": "benchmark.phrase_engine_emit_lag_p95_ms",
            "emit_lag_max_ms": "benchmark.phrase_engine_emit_lag_max_ms",
            "emit_lag_min_ms": "benchmark.phrase_engine_emit_lag_min_ms",
            "consumed_lag_p50_ms": "benchmark.phrase_consumed_lag_p50_ms",
            "consumed_lag_p95_ms": "benchmark.phrase_consumed_lag_p95_ms",
        }
        for row in selected_rows:
            phrase = str(row.get("phrase", ""))
            for field, name in latency_fields.items():
                value = row.get(field)
                if value is None:
                    continue
                value_f = finite_number(value, f"{audit['id']}.{phrase}.{field}")
                comparison_extra = None
                if field.startswith("emit_lag_"):
                    # audio_pos_ms is explicitly engine-defined. Binding the
                    # engine here prevents a cross-engine comparison that the
                    # runtime contract says is invalid.
                    comparison_extra = {"engine": str(cell.get("engine", "")).lower()}
                self.emit(
                    audit,
                    name,
                    value_f,
                    "ms",
                    "lower_is_better",
                    sample_key=phrase,
                    comparison_extra=comparison_extra,
                )
        self.warnings.add(
            "benchmark_report inputs are legacy_unbound: current reports do not bind config, model, corpus, audio, or tool hashes"
        )
        # Reaching this point proves the selected cell/row set is structurally
        # usable. Undefined optional ratios (for example precision with zero
        # detections) stay as metric-level refusals; a gate for another metric
        # relies on its own all-expected coverage check.
        audit["performance_eligible"] = True

    def extract_observation(self, item: dict[str, Any], path: Path, audit: dict[str, Any]) -> None:
        if item.get("selector") not in (None, {}):
            raise AnalysisError(f"input {audit['id']}.selector is not used for observation_record")
        self.emit_ratio(audit, "observation.file_present", int(audit["exists"]), 1, "higher_is_better")
        if not audit["exists"]:
            audit["parseable"] = False
            audit["status"] = "missing"
            self.emit_ratio(audit, "observation.parseable", 0, 1, "higher_is_better")
            self.emit_ratio(audit, "observation.schema_valid", 0, 1, "higher_is_better")
            self.refuse(audit["id"], "missing_artifact", f"expected observation is missing: {audit['path']}")
            return
        data, error = self.snapshots[audit["id"]]
        parseable = error is None and isinstance(data, dict)
        audit["parseable"] = parseable
        self.emit_ratio(audit, "observation.parseable", int(parseable), 1, "higher_is_better")
        if not parseable:
            audit["status"] = "invalid"
            self.emit_ratio(audit, "observation.schema_valid", 0, 1, "higher_is_better")
            self.refuse(audit["id"], "invalid_json", error or "observation root is not an object")
            return
        record = ensure_object(data, f"observation {audit['path']}")
        # Output collision protection must not depend on the observation being
        # valid. Bank every syntactically recoverable declared provenance path
        # before schema/hash checks, so a malformed claim cannot authorize
        # overwriting the very evidence it points at.
        declared_provenance_paths: list[str] = []
        raw_provenance = record.get("provenance")
        if isinstance(raw_provenance, list):
            for raw_entry in raw_provenance:
                if not isinstance(raw_entry, dict):
                    continue
                declared_path = raw_entry.get("path")
                if (
                    isinstance(declared_path, str)
                    and declared_path.strip()
                    and "\x00" not in declared_path
                ):
                    try:
                        declared_path.encode("utf-8")
                    except UnicodeError:
                        continue
                    declared_provenance_paths.append(declared_path)
        audit["declared_provenance_paths"] = sorted(set(declared_provenance_paths))
        reject_unknown(
            record,
            {
                "schema_version",
                "record_type",
                "observation_id",
                "cluster_id",
                "status",
                "method",
                "clock_domain",
                "host_vantage",
                "provenance",
                "metrics",
                "attributes",
                "refusal_reason",
            },
            f"observation {audit['path']}",
        )
        valid = record.get("schema_version") == 1 and record.get("record_type") == OBSERVATION_TYPE
        if not valid:
            self.emit_ratio(audit, "observation.schema_valid", 0, 1, "higher_is_better")
            audit["status"] = "invalid"
            self.refuse(audit["id"], "invalid_schema", "observation schema_version/record_type is invalid")
            return
        required_string(record, "observation_id", f"observation {audit['path']}")
        cluster_id = required_string(record, "cluster_id", f"observation {audit['path']}")
        required_string(record, "method", f"observation {audit['path']}")
        clock_domain = required_string(record, "clock_domain", f"observation {audit['path']}")
        if clock_domain not in {
            "monotonic",
            "source_relative",
            "utc_anchored",
            "not_applicable",
        }:
            raise AnalysisError(f"observation {audit['path']}.clock_domain is invalid")
        required_string(record, "host_vantage", f"observation {audit['path']}")
        provenance = record.get("provenance")
        if not isinstance(provenance, list) or not provenance:
            self.emit_ratio(audit, "observation.schema_valid", 0, 1, "higher_is_better")
            audit["status"] = "invalid"
            self.refuse(audit["id"], "missing_provenance", "observation provenance must be non-empty")
            return
        roles: set[str] = set()
        role_counts: Counter[str] = Counter()
        provenance_audit: list[dict[str, Any]] = []
        audit["observation_provenance"] = provenance_audit
        seen_provenance: set[tuple[str, str]] = set()
        clock_anchor_records: list[Any] = []
        for p_index, row in enumerate(provenance):
            obj = ensure_object(row, f"observation.provenance[{p_index}]")
            reject_unknown(obj, {"role", "path", "sha256"}, f"observation.provenance[{p_index}]")
            role = required_string(obj, "role", f"observation.provenance[{p_index}]")
            path_text = required_string(obj, "path", f"observation.provenance[{p_index}]")
            digest = required_string(obj, "sha256", f"observation.provenance[{p_index}]")
            identity = (role, path_text)
            if identity in seen_provenance:
                raise AnalysisError(f"observation provenance repeats {role!r} path {path_text!r}")
            seen_provenance.add(identity)
            if not SHA256_RE.fullmatch(digest):
                raise AnalysisError(f"observation provenance role {role!r} has invalid sha256")
            source_path = (path.parent / path_text).resolve()
            source_payload, source_error = snapshot_file_bytes(
                source_path, self.byte_snapshots
            )
            if source_payload is None:
                raise AnalysisError(
                    f"observation provenance role {role!r} cannot be read: {source_error}"
                )
            actual_digest = hashlib.sha256(source_payload).hexdigest()
            if actual_digest != digest:
                raise AnalysisError(
                    f"observation provenance role {role!r} hash mismatch for {path_text!r}"
                )
            roles.add(role)
            role_counts[role] += 1
            provenance_audit.append({"role": role, "path": path_text, "sha256": digest})
            if role == "clock_anchor":
                clock_anchor_record, clock_error = parse_json_bytes(source_payload)
                if clock_error is not None:
                    raise AnalysisError(f"clock anchor {path_text!r} is not valid JSON: {clock_error}")
                clock_anchor_records.append(clock_anchor_record)
        required_roles = {"measurement_spec", "raw_evidence", "tool"}
        if clock_domain == "utc_anchored":
            required_roles.add("clock_anchor")
        missing_roles = sorted(required_roles - roles)
        if missing_roles:
            self.emit_ratio(audit, "observation.schema_valid", 0, 1, "higher_is_better")
            audit["status"] = "invalid"
            self.refuse(
                audit["id"],
                "incomplete_provenance",
                f"observation provenance missing role(s): {', '.join(missing_roles)}",
            )
            return
        repeated_singletons = sorted(
            role for role in ("measurement_spec", "tool") if role_counts[role] != 1
        )
        if repeated_singletons:
            raise AnalysisError(
                "observation provenance requires exactly one entry for role(s): "
                + ", ".join(repeated_singletons)
            )
        if len(clock_anchor_records) > 1:
            raise AnalysisError("observations may cite at most one clock_anchor record")
        if clock_domain == "utc_anchored" and len(clock_anchor_records) != 1:
            raise AnalysisError("utc_anchored observations require exactly one clock_anchor record")
        attributes = ensure_object(
            record.get("attributes", {}), f"observation {audit['path']}.attributes"
        )
        canonical_bytes(attributes)
        if clock_anchor_records:
            expected_host_boot_id = required_string(
                attributes,
                "host_boot_id",
                f"observation {audit['path']}.attributes",
            )
            audit["clock_anchor"] = validate_clock_anchor(
                clock_anchor_records[0],
                expected_host_vantage=record["host_vantage"],
                expected_host_boot_id=expected_host_boot_id,
            )

        # Hash/schema validation binds the assertion to exact bytes; it does
        # not prove that the asserted scalar was derived from those bytes.
        # Only analyzer-owned registered validators may emit the strictly
        # higher validator_recomputed grade.
        audit["provenance_grade"] = "provenance_bound"
        self.warnings.add(
            "observation_record inputs are provenance_bound: the generic adapter does not recompute declared metrics from raw evidence"
        )
        audit["observation_id"] = record["observation_id"]
        audit["cluster_identity"] = f"observation-cluster:{cluster_id}"
        audit["method"] = record["method"]
        audit["clock_domain"] = clock_domain
        audit["host_vantage"] = record["host_vantage"]
        audit["observation_provenance"] = provenance_audit
        status = record.get("status")
        if status not in {"measured", "censored", "invalid"}:
            raise AnalysisError(f"observation {audit['path']}.status is invalid")
        if status == "measured" and record.get("refusal_reason") is not None:
            raise AnalysisError("measured observations may not carry refusal_reason")
        self.emit_ratio(audit, "observation.schema_valid", 1, 1, "higher_is_better")
        if status != "measured":
            reason = record.get("refusal_reason")
            if not isinstance(reason, str) or not reason:
                raise AnalysisError(f"observation status {status!r} requires refusal_reason")
            audit["status"] = status
            self.refuse(audit["id"], f"observation_{status}", reason)
            return

        metrics = record.get("metrics")
        if not isinstance(metrics, list) or not metrics:
            raise AnalysisError(f"observation {audit['path']}.metrics must be non-empty")
        has_race_metric = any(
            isinstance(raw_metric, dict)
            and isinstance(raw_metric.get("name"), str)
            and raw_metric["name"].startswith("race.")
            for raw_metric in metrics
        )
        clock_uncertainty_ms: float | None = None
        if has_race_metric:
            if clock_domain != "utc_anchored":
                raise AnalysisError("race.* metrics require clock_domain = utc_anchored")
            if attributes.get("clock_model") != "two_anchor_affine":
                raise AnalysisError("race.* metrics require attributes.clock_model = two_anchor_affine")
            clock_uncertainty_ms = finite_number(
                attributes.get("clock_uncertainty_ms"),
                "race observation attributes.clock_uncertainty_ms",
                nonnegative=True,
            )
            for key in ("run_id", "host_boot_id", "competitor_event_id", "sign_convention"):
                required_string(attributes, key, "race observation attributes")
            if cluster_id != attributes["run_id"]:
                raise AnalysisError("race.* observation cluster_id must equal attributes.run_id")
        if clock_domain == "utc_anchored":
            clock_anchor_audit = audit["clock_anchor"]
            if has_race_metric and float(clock_uncertainty_ms or 0.0) < (
                float(clock_anchor_audit["max_residual_ns"]) / 1_000_000.0
            ):
                raise AnalysisError(
                    "race clock_uncertainty_ms is smaller than the validated clock-anchor residual"
                )
        observation_context = {
            "declared": item.get("context", {}),
            "method": record["method"],
            "clock_domain": clock_domain,
            "host_vantage": record["host_vantage"],
            "clock_model": attributes.get("clock_model"),
            "host_boot_id": attributes.get("host_boot_id"),
            "competitor_event_id": attributes.get("competitor_event_id"),
            "sign_convention": attributes.get("sign_convention"),
            "method_provenance": sorted(
                (
                    {"role": row["role"], "sha256": row["sha256"]}
                    for row in provenance_audit
                    if row["role"] in {"measurement_spec", "tool"}
                ),
                key=lambda row: (row["role"], row["sha256"]),
            ),
        }
        audit["comparison_context"] = observation_context
        audit["comparison_signature"] = comparison_signature(
            audit["control_signature"], observation_context
        )
        audit["attributes"] = attributes
        audit["status"] = "loaded"
        seen_metric_keys: set[tuple[str, str]] = set()
        for metric_index, raw_metric in enumerate(metrics):
            obj = ensure_object(raw_metric, f"observation.metrics[{metric_index}]")
            reject_unknown(
                obj,
                {
                    "name",
                    "value",
                    "unit",
                    "direction",
                    "sample_key",
                    "numerator",
                    "denominator",
                    "uncertainty",
                },
                f"observation.metrics[{metric_index}]",
            )
            name = metric_name(obj.get("name"), f"observation.metrics[{metric_index}].name")
            if name.startswith(("runtime.", "benchmark.", "observation.")):
                raise AnalysisError(
                    f"observation metric {name!r} uses a namespace reserved for a built-in adapter"
                )
            if name.startswith("race.") and name != "race.executable_margin_ms":
                raise AnalysisError(
                    f"observation metric {name!r} is not an admitted race assertion"
                )
            unit = required_string(obj, "unit", f"observation metric {name}")
            direction = required_string(obj, "direction", f"observation metric {name}")
            if direction not in VALID_DIRECTIONS:
                raise AnalysisError(f"observation metric {name} has invalid direction")
            measurement_uncertainty = finite_number(
                obj.get("uncertainty", 0.0),
                f"observation metric {name}.uncertainty",
                nonnegative=True,
            )
            if name == "race.executable_margin_ms":
                if unit != "ms" or direction != "higher_is_better":
                    raise AnalysisError(
                        "race.executable_margin_ms requires unit=ms and direction=higher_is_better"
                    )
                if attributes.get("sign_convention") != "competitor_time_minus_our_executable_time":
                    raise AnalysisError(
                        "race.executable_margin_ms requires sign_convention = "
                        "competitor_time_minus_our_executable_time"
                    )
                measurement_uncertainty = max(
                    measurement_uncertainty, float(clock_uncertainty_ms or 0.0)
                )
            sample_key = obj.get("sample_key", "")
            if not isinstance(sample_key, str):
                raise AnalysisError(f"observation metric {name}.sample_key must be a string")
            if any(
                ord(character) < 0x20 or ord(character) == 0x7F
                for character in sample_key
            ):
                raise AnalysisError(
                    f"observation metric {name}.sample_key may not contain control characters"
                )
            metric_key = (name, sample_key)
            if metric_key in seen_metric_keys:
                raise AnalysisError(
                    f"observation repeats metric/sample_key {name!r}/{sample_key!r}"
                )
            seen_metric_keys.add(metric_key)
            numerator, denominator = obj.get("numerator"), obj.get("denominator")
            if numerator is None and denominator is None:
                value = finite_number(obj.get("value"), f"observation metric {name}")
                self.emit(
                    audit,
                    name,
                    value,
                    unit,
                    direction,
                    sample_key=sample_key,
                    measurement_uncertainty=measurement_uncertainty,
                )
            elif numerator is not None and denominator is not None:
                numerator_f = finite_number(numerator, f"observation metric {name}.numerator")
                denominator_f = finite_number(
                    denominator, f"observation metric {name}.denominator", nonnegative=True
                )
                if denominator_f <= 0:
                    raise AnalysisError(f"observation metric {name} denominator must be positive")
                value = numerator_f / denominator_f
                if "value" in obj:
                    declared_value = finite_number(
                        obj.get("value"), f"observation metric {name}"
                    )
                    if not math.isclose(
                        declared_value, value, rel_tol=1e-9, abs_tol=1e-12
                    ):
                        raise AnalysisError(
                            f"observation metric {name} ratio/value is inconsistent"
                        )
                self.emit(
                    audit,
                    name,
                    value,
                    unit,
                    direction,
                    aggregation="ratio",
                    numerator=numerator_f,
                    denominator=denominator_f,
                    sample_key=sample_key,
                    measurement_uncertainty=measurement_uncertainty,
                )
            else:
                raise AnalysisError(f"observation metric {name} needs both numerator and denominator")
        audit["performance_eligible"] = not any(
            row["input_id"] == audit["id"] for row in self.refusals
        )


def group_samples(samples: list[Sample]) -> dict[tuple[str, str, str, str], list[Sample]]:
    groups: dict[tuple[str, str, str, str], list[Sample]] = defaultdict(list)
    identities: dict[str, tuple[str, str]] = {}
    for sample in samples:
        identity = (sample.unit, sample.direction)
        previous = identities.setdefault(sample.metric, identity)
        if previous != identity:
            raise AnalysisError(
                f"metric {sample.metric!r} changes unit/direction: {previous!r} vs {identity!r}"
            )
        groups[(sample.cohort, sample.metric, sample.unit, sample.direction)].append(sample)
    return groups


def summarize_metrics(
    samples: list[Sample], spec_hash: str, repetitions: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for (cohort, name, unit, direction), metric_samples in sorted(group_samples(samples).items()):
        ratio = all(s.aggregation == "ratio" for s in metric_samples)
        if any(s.aggregation == "ratio" for s in metric_samples) and not ratio:
            raise AnalysisError(f"metric {name!r} mixes ratio and distribution samples")
        estimate_stat = "pooled_ratio" if ratio else "median"
        estimate = statistic(metric_samples, estimate_stat)
        summary: dict[str, Any] = {
            "cohort": cohort,
            "metric": name,
            "unit": unit,
            "direction": direction,
            "aggregation": "pooled_ratio" if ratio else "distribution",
            "sample_count": len(metric_samples),
            "cluster_count": len({s.cluster_id for s in metric_samples}),
            "provenance_counts": dict(sorted(Counter(s.provenance_grade for s in metric_samples).items())),
            "measurement_uncertainty_max": max(
                (s.measurement_uncertainty for s in metric_samples), default=0.0
            ),
            "estimate": {"stat": estimate_stat, "value": estimate},
            "distribution": distribution(s.value for s in metric_samples),
            "uncertainty": bootstrap_ci(
                metric_samples,
                estimate_stat,
                DEFAULT_CONFIDENCE,
                repetitions,
                f"{spec_hash}|summary|{cohort}|{name}|{unit}|{direction}",
            ),
        }
        if ratio:
            summary["ratio_counts"] = {
                "numerator": finite_sum(
                    (float(s.numerator) for s in metric_samples),
                    f"{cohort}:{name} pooled numerator",
                ),
                "denominator": finite_sum(
                    (float(s.denominator) for s in metric_samples),
                    f"{cohort}:{name} pooled denominator",
                ),
            }
        out.append(summary)
    return out


def pair_metric_samples(
    samples: list[Sample],
    metric: str,
    minimum_provenance: str,
    allowed_input_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str | None, str | None]:
    metric_samples = [
        s
        for s in samples
        if s.metric == metric
        and (allowed_input_ids is None or s.input_id in allowed_input_ids)
    ]
    selected = [
        sample
        for sample in metric_samples
        if provenance_at_least(sample, minimum_provenance)
    ]
    units = {s.unit for s in selected}
    directions = {s.direction for s in selected}
    if len(units) > 1 or len(directions) > 1:
        raise AnalysisError(f"metric {metric!r} has inconsistent unit/direction")
    unit = next(iter(units), None)
    direction = next(iter(directions), None)
    by_cohort: dict[str, dict[tuple[str, str], Sample]] = {
        "baseline": {},
        "candidate": {},
    }
    duplicates: set[tuple[str, tuple[str, str]]] = set()
    refusals: list[dict[str, str]] = []
    ineligible_tokens: dict[str, set[tuple[str, str]]] = {
        "baseline": set(),
        "candidate": set(),
    }
    for sample in metric_samples:
        if provenance_at_least(sample, minimum_provenance):
            continue
        if sample.pairable and sample.pair_id is not None:
            ineligible_tokens[sample.cohort].add((sample.pair_id, sample.sample_key))
        refusals.append(
            {
                "pair_id": sample.pair_id or "",
                "input_id": sample.input_id,
                "sample_key": sample.sample_key,
                "reason": (
                    f"sample provenance {sample.provenance_grade!r} is below "
                    f"required {minimum_provenance!r}"
                ),
            }
        )
    for sample in selected:
        if not sample.pairable:
            refusals.append(
                {
                    "pair_id": sample.pair_id or "",
                    "input_id": sample.input_id,
                    "sample_key": sample.sample_key,
                    "reason": "metric sample is declared unpairable",
                }
            )
            continue
        if sample.pair_id is None:
            refusals.append(
                {
                    "pair_id": "",
                    "input_id": sample.input_id,
                    "sample_key": sample.sample_key,
                    "reason": "input has no pair_id",
                }
            )
            continue
        token = (sample.pair_id, sample.sample_key)
        cohort_map = by_cohort[sample.cohort]
        if token in cohort_map:
            duplicates.add((sample.cohort, token))
        else:
            cohort_map[token] = sample
    for cohort, token in sorted(duplicates):
        by_cohort[cohort].pop(token, None)
        refusals.append(
            {
                "pair_id": token[0],
                "sample_key": token[1],
                "reason": f"duplicate {cohort} sample",
            }
        )
    baseline_tokens = set(by_cohort["baseline"]) | ineligible_tokens["baseline"]
    candidate_tokens = set(by_cohort["candidate"]) | ineligible_tokens["candidate"]
    baseline_only = sorted(set(by_cohort["baseline"]) - candidate_tokens)
    candidate_only = sorted(set(by_cohort["candidate"]) - baseline_tokens)
    for token in baseline_only:
        refusals.append(
            {
                "pair_id": token[0],
                "sample_key": token[1],
                "reason": "candidate sample missing",
            }
        )
    for token in candidate_only:
        refusals.append(
            {
                "pair_id": token[0],
                "sample_key": token[1],
                "reason": "baseline sample missing",
            }
        )
    pairs: list[dict[str, Any]] = []
    for token in sorted(set(by_cohort["baseline"]) & set(by_cohort["candidate"])):
        baseline = by_cohort["baseline"][token]
        candidate = by_cohort["candidate"][token]
        if baseline.comparison_signature is None or candidate.comparison_signature is None:
            refusals.append(
                {
                    "pair_id": token[0],
                    "sample_key": token[1],
                    "reason": "control artifacts were not supplied",
                }
            )
            continue
        if baseline.comparison_signature != candidate.comparison_signature:
            refusals.append(
                {
                    "pair_id": token[0],
                    "sample_key": token[1],
                    "reason": "control/context signatures differ",
                }
            )
            continue
        delta = candidate.value - baseline.value
        if not math.isfinite(delta):
            refusals.append(
                {
                    "pair_id": token[0],
                    "sample_key": token[1],
                    "reason": "candidate-minus-baseline overflowed",
                }
            )
            continue
        if direction == "higher_is_better":
            improvement = delta
        elif direction == "lower_is_better":
            improvement = -delta
        else:
            improvement = None
        pair_uncertainty = baseline.measurement_uncertainty + candidate.measurement_uncertainty
        if not math.isfinite(pair_uncertainty):
            refusals.append(
                {
                    "pair_id": token[0],
                    "sample_key": token[1],
                    "reason": "measurement uncertainty overflowed",
                }
            )
            continue
        pair_key = "paired-sample:" + canonical_hash(
            {"pair_id": token[0], "sample_key": token[1]}
        )
        pairs.append(
            {
                "pair_key": pair_key,
                "pair_id": token[0],
                "sample_key": token[1],
                "baseline_cluster_id": baseline.cluster_id,
                "candidate_cluster_id": candidate.cluster_id,
                "baseline_input_id": baseline.input_id,
                "candidate_input_id": candidate.input_id,
                "baseline": baseline.value,
                "candidate": candidate.value,
                "candidate_minus_baseline": delta,
                "improvement": improvement,
                "measurement_uncertainty": pair_uncertainty,
                "provenance": min(
                    (baseline.provenance_grade, candidate.provenance_grade),
                    key=lambda grade: PROVENANCE_ORDER[grade],
                ),
            }
        )
    # Pair observations are independent only when neither source side is
    # shared. Build connected components over source-cluster identities: a
    # star such as B-C1, B-C2, ... is one cluster, not N independent pairs.
    parents: dict[str, str] = {}

    def find(vertex: str) -> str:
        parents.setdefault(vertex, vertex)
        while parents[vertex] != vertex:
            parents[vertex] = parents[parents[vertex]]
            vertex = parents[vertex]
        return vertex

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    for pair in pairs:
        union(pair["baseline_cluster_id"], pair["candidate_cluster_id"])
    component_members: dict[str, set[str]] = defaultdict(set)
    for vertex in parents:
        component_members[find(vertex)].add(vertex)
    for pair in pairs:
        root = find(pair["baseline_cluster_id"])
        pair["cluster_id"] = "paired-component:" + canonical_hash(
            sorted(component_members[root])
        )
    return pairs, sorted(
        refusals,
        key=lambda row: (
            row.get("pair_id", ""),
            row.get("input_id", ""),
            row.get("sample_key", ""),
            row["reason"],
        ),
    ), unit, direction


def pair_values_as_samples(
    pairs: list[dict[str, Any]], metric: str, stat: str, unit: str, direction: str
) -> list[Sample]:
    values: list[Sample] = []
    for pair in pairs:
        if stat.startswith("paired_delta_"):
            value = pair["candidate_minus_baseline"]
            out_direction = "neutral"
        elif stat.startswith("paired_improvement_"):
            if pair["improvement"] is None:
                raise AnalysisError(f"{stat} is invalid for neutral metric {metric!r}")
            value = pair["improvement"]
            out_direction = "higher_is_better"
        elif stat == "paired_win_rate":
            if pair["improvement"] is None:
                raise AnalysisError(f"paired_win_rate is invalid for neutral metric {metric!r}")
            value = float(pair["improvement"] > 0)
            out_direction = "higher_is_better"
        else:
            raise AnalysisError(f"unsupported comparison statistic {stat!r}")
        values.append(
            Sample(
                metric=metric,
                value=float(value),
                unit="proportion" if stat == "paired_win_rate" else unit,
                direction=out_direction,
                cohort="comparison",
                input_id=pair["pair_key"],
                cluster_id=str(pair["cluster_id"]),
                pair_id=pair["pair_id"],
                sample_key=pair["sample_key"],
                comparison_signature="paired",
                provenance_grade=pair["provenance"],
                aggregation="ratio" if stat == "paired_win_rate" else "distribution",
                numerator=float(value) if stat == "paired_win_rate" else None,
                denominator=1.0 if stat == "paired_win_rate" else None,
                # A win is categorical. Dimensional uncertainty (for example
                # milliseconds) cannot widen a proportion; callers must first
                # resolve whether the improvement interval crosses zero.
                measurement_uncertainty=(
                    0.0
                    if stat == "paired_win_rate"
                    else float(pair.get("measurement_uncertainty", 0.0))
                ),
            )
        )
    return values


def build_comparisons(samples: list[Sample], spec_hash: str, repetitions: int) -> list[dict[str, Any]]:
    metrics = sorted({s.metric for s in samples if s.cohort == "baseline"} & {s.metric for s in samples if s.cohort == "candidate"})
    out: list[dict[str, Any]] = []
    for metric in metrics:
        pairs, refusals, unit, direction = pair_metric_samples(samples, metric, "legacy_unbound")
        record: dict[str, Any] = {
            "metric": metric,
            "unit": unit,
            "direction": direction,
            "pair_count": len(pairs),
            "cluster_count": len({pair["cluster_id"] for pair in pairs}),
            "refused_pairs": refusals,
            "pairs": pairs,
        }
        if pairs:
            delta_samples = pair_values_as_samples(pairs, metric, "paired_delta_median", unit or "", direction or "neutral")
            record["candidate_minus_baseline"] = {
                "distribution": distribution(s.value for s in delta_samples),
                "median_ci": bootstrap_ci(
                    delta_samples,
                    "median",
                    DEFAULT_CONFIDENCE,
                    repetitions,
                    f"{spec_hash}|comparison|{metric}|delta",
                ),
            }
            if direction != "neutral":
                improvement_samples = pair_values_as_samples(
                    pairs, metric, "paired_improvement_median", unit or "", direction or "neutral"
                )
                record["improvement"] = {
                    "distribution": distribution(s.value for s in improvement_samples),
                    "nominal_win_rate": sum(s.value > 0 for s in improvement_samples)
                    / len(improvement_samples),
                    "win_rate_bounds_from_measurement_uncertainty": {
                        "lower": sum(
                            pair["improvement"] > pair["measurement_uncertainty"]
                            for pair in pairs
                        )
                        / len(pairs),
                        "upper": sum(
                            pair["improvement"] >= -pair["measurement_uncertainty"]
                            for pair in pairs
                        )
                        / len(pairs),
                        "uncertain_pairs": sum(
                            abs(pair["improvement"]) <= pair["measurement_uncertainty"]
                            for pair in pairs
                        ),
                    },
                    "median_ci": bootstrap_ci(
                        improvement_samples,
                        "median",
                        DEFAULT_CONFIDENCE,
                        repetitions,
                        f"{spec_hash}|comparison|{metric}|improvement",
                    ),
                }
        else:
            record["status"] = "not_resolvable"
        out.append(record)
    return out


def compare(op: str, value: float, threshold: float) -> bool:
    return {
        ">=": value >= threshold,
        ">": value > threshold,
        "<=": value <= threshold,
        "<": value < threshold,
        "==": value == threshold,
    }[op]


def decide_with_interval(op: str, threshold: float, lower: float, upper: float) -> str:
    if op == ">=":
        if lower >= threshold:
            return "supported"
        if upper < threshold:
            return "rejected"
    elif op == ">":
        if lower > threshold:
            return "supported"
        if upper <= threshold:
            return "rejected"
    elif op == "<=":
        if upper <= threshold:
            return "supported"
        if lower > threshold:
            return "rejected"
    elif op == "<":
        if upper < threshold:
            return "supported"
        if lower >= threshold:
            return "rejected"
    else:
        return "not_resolvable"
    return "not_resolvable"


RUNG_NAMES = {
    0: "unclassified",
    1: "evidence_integrity",
    2: "ingress",
    3: "detection",
    4: "action_path",
    5: "race_outcome",
}


def metric_rung(metric: str) -> int:
    if metric in EVIDENCE_INTEGRITY_METRICS:
        return 1
    if metric.startswith("benchmark."):
        return 3
    if metric.startswith("runtime."):
        if metric.startswith(
            (
                "runtime.frames_in",
                "runtime.chunks_in",
                "runtime.ingress_",
                "runtime.total_drops",
                "runtime.interior_drops",
                "runtime.reported_drop_pct",
                "runtime.dead_leg_fraction",
                "runtime.edge.capture_to_preproc.",
                "runtime.edge.preproc_to_chunk.",
                "runtime.edge.chunk_to_detect.",
            )
        ):
            return 2
        if metric.startswith(
            (
                "runtime.detections",
                "runtime.chunk_first_capture_to_detection_emit_ms",
                "runtime.edge.detect_to_decision.",
                "runtime.edge.capture_to_decision.",
            )
        ):
            return 3
        return 4
    validator = REGISTERED_METRIC_VALIDATORS.get(metric)
    return int(validator["rung"]) if validator is not None else 0


def apply_proof_ladder(results: list[dict[str, Any]]) -> None:
    """Require prior proof on the exact same declared evidence lineage."""
    for rung in range(2, 6):
        current = [result for result in results if result["rung"] == rung]
        if not current:
            continue
        for result in current:
            lineage_ids = result.get("input_ids", [])
            lineage_priors = [
                prior
                for prior in results
                if prior is not result and prior.get("input_ids", []) == lineage_ids
            ]
            missing = [
                prior_rung
                for prior_rung in range(1, rung)
                if not any(prior["rung"] == prior_rung for prior in lineage_priors)
            ]
            failed = [
                prior["id"]
                for prior in lineage_priors
                if 1 <= prior["rung"] < rung
                and (
                    prior.get("verdict") != "supported"
                    or not prior.get("proof_eligible", False)
                )
            ]
            result["proof_ladder"] = {
                "required_prior_rungs": [RUNG_NAMES[prior] for prior in range(1, rung)],
                "missing_prior_rungs": [RUNG_NAMES[prior] for prior in missing],
                "unsupported_prior_gates": sorted(failed),
                "lineage_input_ids": lineage_ids,
            }
            if missing or failed:
                if result.get("verdict") != "not_resolvable":
                    result["diagnostic_verdict"] = result.get("verdict")
                    result["verdict"] = "not_resolvable"
                    result["reason"] = "proof ladder prerequisites are missing, unresolved, rejected, or exploratory"


def evaluate_gates(
    raw_gates: list[Any],
    samples: list[Sample],
    input_records: list[dict[str, Any]],
    spec_hash: str,
    repetitions: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    records_by_id = {row["id"]: row for row in input_records}
    for index, raw_gate in enumerate(raw_gates):
        where = f"gates[{index}]"
        gate = ensure_object(raw_gate, where)
        reject_unknown(
            gate,
            {
                "id",
                "scope",
                "cohort",
                "metric",
                "stat",
                "op",
                "threshold",
                "unit",
                "min_samples",
                "min_clusters",
                "decision",
                "minimum_provenance",
                "confidence",
                "input_validity",
                "input_ids",
            },
            where,
        )
        gate_id = required_string(gate, "id", where)
        if gate_id in seen:
            raise AnalysisError(f"duplicate gate id {gate_id!r}")
        seen.add(gate_id)
        scope = required_string(gate, "scope", where)
        if scope not in {"cohort", "comparison"}:
            raise AnalysisError(f"{where}.scope must be cohort or comparison")
        metric = metric_name(gate.get("metric"), f"{where}.metric")
        stat = required_string(gate, "stat", where)
        allowed_stats = COHORT_STATS if scope == "cohort" else COMPARISON_STATS
        if stat not in allowed_stats:
            raise AnalysisError(
                f"{where}.stat must be one of {', '.join(sorted(allowed_stats))}"
            )
        op = required_string(gate, "op", where)
        if op not in {">=", ">", "<=", "<", "=="}:
            raise AnalysisError(f"{where}.op is invalid")
        threshold = finite_number(gate.get("threshold"), f"{where}.threshold")
        expected_unit = required_string(gate, "unit", where)
        min_samples = integer(gate.get("min_samples", 1), f"{where}.min_samples", minimum=1)
        min_clusters = integer(gate.get("min_clusters", 1), f"{where}.min_clusters", minimum=1)
        decision = gate.get("decision", "confidence_bound")
        if decision not in {"confidence_bound", "point"}:
            raise AnalysisError(f"{where}.decision must be confidence_bound or point")
        declared_minimum = gate.get("minimum_provenance", "validator_recomputed")
        if declared_minimum not in PROVENANCE_ORDER:
            raise AnalysisError(f"{where}.minimum_provenance is invalid")
        # Integrity metrics are analyzer-produced denominator facts. Filtering
        # their zeroes by leaf provenance would turn invalid inputs into
        # survivors, so they always include legacy_unbound rows.
        minimum = (
            "legacy_unbound" if metric in EVIDENCE_INTEGRITY_METRICS else declared_minimum
        )
        confidence = finite_number(gate.get("confidence", DEFAULT_CONFIDENCE), f"{where}.confidence")
        if not 0.5 < confidence <= MAX_CONFIDENCE:
            raise AnalysisError(
                f"{where}.confidence must be greater than 0.5 and at most {MAX_CONFIDENCE}"
            )
        input_validity = gate.get("input_validity", "all_expected")
        if input_validity not in {"all_expected", "metric_only"}:
            raise AnalysisError(f"{where}.input_validity must be all_expected or metric_only")
        raw_input_ids = gate.get("input_ids")
        if raw_input_ids is not None and (
            not isinstance(raw_input_ids, list)
            or not raw_input_ids
            or not all(isinstance(value, str) and value for value in raw_input_ids)
            or len(set(raw_input_ids)) != len(raw_input_ids)
        ):
            raise AnalysisError(f"{where}.input_ids must be a non-empty unique string array")
        if raw_input_ids is not None:
            unknown = sorted(set(raw_input_ids) - set(records_by_id))
            if unknown:
                raise AnalysisError(f"{where}.input_ids names unknown input(s): {', '.join(unknown)}")

        rung = metric_rung(metric)
        downgrades: list[str] = []
        if metric not in EVIDENCE_INTEGRITY_METRICS:
            if declared_minimum == "legacy_unbound":
                downgrades.append("legacy_provenance_allowed")
            elif declared_minimum == "provenance_bound":
                downgrades.append("unvalidated_scalar_allowed")
            if input_validity != "all_expected":
                downgrades.append("survivor_subset_allowed")
            if decision != "confidence_bound":
                downgrades.append("point_decision")
        result: dict[str, Any] = {
            "id": gate_id,
            "scope": scope,
            "metric": metric,
            "stat": stat,
            "op": op,
            "threshold": threshold,
            "unit": expected_unit,
            "decision": decision,
            "minimum_provenance": minimum,
            "min_samples": min_samples,
            "min_clusters": min_clusters,
            "input_validity": input_validity,
            "rung": rung,
            "rung_name": RUNG_NAMES[rung],
            "proof_eligible": not downgrades,
            "downgrades": downgrades,
        }
        validator = REGISTERED_METRIC_VALIDATORS.get(metric)
        if validator is not None:
            result["validator_id"] = validator["id"]
        if declared_minimum != minimum:
            result["declared_minimum_provenance"] = declared_minimum

        if scope == "cohort":
            cohort = gate.get("cohort", "candidate")
            if cohort not in {"baseline", "candidate"}:
                raise AnalysisError(f"{where}.cohort must be baseline or candidate")
            result["cohort"] = cohort
            relevant_records = (
                [records_by_id[input_id] for input_id in raw_input_ids]
                if raw_input_ids is not None
                else [row for row in input_records if row["cohort"] == cohort]
            )
            if any(row["cohort"] != cohort for row in relevant_records):
                raise AnalysisError(f"{where}.input_ids must all belong to cohort {cohort!r}")
        else:
            relevant_records = (
                [records_by_id[input_id] for input_id in raw_input_ids]
                if raw_input_ids is not None
                else list(input_records)
            )
            cohorts = {row["cohort"] for row in relevant_records}
            if not {"baseline", "candidate"}.issubset(cohorts):
                raise AnalysisError(f"{where}.input_ids must include baseline and candidate inputs")
        relevant_ids = {row["id"] for row in relevant_records}
        result["input_ids"] = sorted(relevant_ids)

        if input_validity == "all_expected":
            if metric not in EVIDENCE_INTEGRITY_METRICS:
                invalid_inputs = sorted(
                    row["id"]
                    for row in relevant_records
                    if not row.get("performance_eligible", False)
                )
                if invalid_inputs:
                    result.update(
                        verdict="not_resolvable",
                        reason="one or more expected inputs failed an earlier evidence-validity rung",
                        invalid_inputs=invalid_inputs,
                    )
                    results.append(result)
                    continue
            missing_metric_inputs = sorted(
                input_id
                for input_id in relevant_ids
                if not any(sample.input_id == input_id and sample.metric == metric for sample in samples)
            )
            if missing_metric_inputs:
                result.update(
                    verdict="not_resolvable",
                    reason="one or more expected inputs did not emit the gated metric",
                    missing_metric_inputs=missing_metric_inputs,
                )
                results.append(result)
                continue

        if (
            metric not in EVIDENCE_INTEGRITY_METRICS
            and minimum == "validator_recomputed"
            and validator is None
        ):
            result.update(
                verdict="not_resolvable",
                reason=(
                    "no registered derivation validator can produce "
                    "validator_recomputed provenance for this metric"
                ),
            )
            results.append(result)
            continue

        if scope == "cohort":
            candidates = [
                sample
                for sample in samples
                if sample.metric == metric
                and sample.cohort == cohort
                and sample.input_id in relevant_ids
            ]
            insufficient_provenance = [
                sample
                for sample in candidates
                if not provenance_at_least(sample, minimum)
            ]
            if input_validity == "all_expected" and insufficient_provenance:
                result.update(
                    verdict="not_resolvable",
                    reason=(
                        "one or more expected metric samples do not meet the "
                        "gate's provenance requirement"
                    ),
                    insufficient_provenance_inputs=sorted(
                        {sample.input_id for sample in insufficient_provenance}
                    ),
                    insufficient_provenance_samples=sorted(
                        (
                            {
                                "input_id": sample.input_id,
                                "sample_key": sample.sample_key,
                                "provenance_grade": sample.provenance_grade,
                            }
                            for sample in insufficient_provenance
                        ),
                        key=lambda row: (
                            row["input_id"],
                            row["sample_key"],
                            row["provenance_grade"],
                        ),
                    ),
                )
                results.append(result)
                continue
            eligible = [sample for sample in candidates if provenance_at_least(sample, minimum)]
            units = {sample.unit for sample in eligible}
            if len(units) > 1:
                raise AnalysisError(f"metric {metric!r} has inconsistent units")
            actual_unit = next(iter(units), None)
            result["observed_samples"] = len(candidates)
            result["eligible_samples"] = len(eligible)
            result["eligible_clusters"] = len({sample.cluster_id for sample in eligible})
            if not eligible:
                result.update(verdict="not_resolvable", reason="metric absent or no sample meets provenance requirement")
                results.append(result)
                continue
            if actual_unit != expected_unit:
                raise AnalysisError(
                    f"gate {gate_id!r} expects unit {expected_unit!r}, metric uses {actual_unit!r}"
                )
            sample_count = effective_sample_count(eligible, stat)
            cluster_count = len({sample.cluster_id for sample in eligible})
            result["effective_sample_count"] = sample_count
            if sample_count < min_samples or cluster_count < min_clusters:
                result.update(verdict="not_resolvable", reason="minimum sample/cluster count not met")
                results.append(result)
                continue
            estimate = statistic(eligible, stat)
            stat_samples = eligible
            bootstrap_stat = stat
        else:
            pairs, pair_refusals, unit, direction = pair_metric_samples(
                samples, metric, minimum, relevant_ids
            )
            result["refused_pairs"] = pair_refusals
            if input_validity == "all_expected" and pair_refusals:
                result.update(
                    verdict="not_resolvable",
                    reason="one or more expected candidate/baseline pairs were refused",
                )
                results.append(result)
                continue
            if not pairs:
                result.update(
                    eligible_samples=0,
                    eligible_clusters=0,
                    verdict="not_resolvable",
                    reason="no verified candidate/baseline pairs",
                )
                results.append(result)
                continue
            if stat.startswith("paired_improvement_") or stat == "paired_win_rate":
                if direction == "neutral":
                    raise AnalysisError(f"{stat} is invalid for neutral metric {metric!r}")
            if stat == "paired_win_rate":
                uncertain_pairs = sorted(
                    (
                        {
                            "pair_id": pair["pair_id"],
                            "sample_key": pair["sample_key"],
                        }
                        for pair in pairs
                        if abs(float(pair["improvement"]))
                        <= pair["measurement_uncertainty"]
                    ),
                    key=lambda row: (row["pair_id"], row["sample_key"]),
                )
                if uncertain_pairs:
                    result.update(
                        verdict="not_resolvable",
                        reason="measurement uncertainty prevents classifying one or more pairs as wins/losses",
                        uncertain_pairs=uncertain_pairs,
                    )
                    results.append(result)
                    continue
            stat_samples = pair_values_as_samples(
                pairs, metric, stat, unit or "", direction or "neutral"
            )
            actual_unit = stat_samples[0].unit
            if actual_unit != expected_unit:
                raise AnalysisError(
                    f"gate {gate_id!r} expects unit {expected_unit!r}, comparison uses {actual_unit!r}"
                )
            sample_count = len(stat_samples)
            cluster_count = len({sample.cluster_id for sample in stat_samples})
            result["eligible_samples"] = len(stat_samples)
            result["eligible_clusters"] = cluster_count
            result["effective_sample_count"] = sample_count
            if sample_count < min_samples or cluster_count < min_clusters:
                result.update(verdict="not_resolvable", reason="minimum paired sample/cluster count not met")
                results.append(result)
                continue
            if stat.endswith("_mean"):
                bootstrap_stat = "mean"
            elif stat.endswith("_median"):
                bootstrap_stat = "median"
            else:
                bootstrap_stat = "pooled_ratio"
            estimate = statistic(stat_samples, bootstrap_stat)

        result["estimate"] = estimate
        measurement_uncertainty = max(
            (sample.measurement_uncertainty for sample in stat_samples), default=0.0
        )
        result["measurement_uncertainty_bound"] = measurement_uncertainty
        if decision == "point":
            if measurement_uncertainty > 0:
                interval_lower = estimate - measurement_uncertainty
                interval_upper = estimate + measurement_uncertainty
                if not math.isfinite(interval_lower) or not math.isfinite(interval_upper):
                    raise AnalysisError(
                        f"gate {gate_id!r} measurement interval overflows finite JSON"
                    )
                result["measurement_interval"] = {
                    "lower": interval_lower,
                    "upper": interval_upper,
                }
                result["verdict"] = decide_with_interval(
                    op,
                    threshold,
                    interval_lower,
                    interval_upper,
                )
                if result["verdict"] == "not_resolvable":
                    result["reason"] = "measurement uncertainty overlaps the threshold"
            else:
                result["verdict"] = "supported" if compare(op, estimate, threshold) else "rejected"
            results.append(result)
            continue
        if op == "==":
            result.update(verdict="not_resolvable", reason="equality has no confidence-bound decision rule")
            results.append(result)
            continue
        uncertainty = bootstrap_ci(
            stat_samples,
            bootstrap_stat,
            confidence,
            repetitions,
            f"{spec_hash}|gate|{gate_id}|{metric}|{bootstrap_stat}",
        )
        result["uncertainty"] = uncertainty
        if not uncertainty["available"]:
            result.update(verdict="not_resolvable", reason=uncertainty["reason"])
        else:
            result["verdict"] = decide_with_interval(
                op, threshold, uncertainty["lower"], uncertainty["upper"]
            )
            if result["verdict"] == "not_resolvable":
                result["reason"] = "confidence interval overlaps the threshold"
        results.append(result)
    apply_proof_ladder(results)
    return results


def validate_spec(spec: Any) -> dict[str, Any]:
    obj = ensure_object(spec, "spec")
    reject_unknown(
        obj,
        {
            "schema_version",
            "analysis_id",
            "as_of",
            "hypothesis",
            "inputs",
            "gates",
            "bootstrap_repetitions",
            "notes",
        },
        "spec",
    )
    if obj.get("schema_version") != SCHEMA_VERSION:
        raise AnalysisError(f"spec.schema_version must be {SCHEMA_VERSION}")
    required_string(obj, "analysis_id", "spec")
    required_string(obj, "as_of", "spec")
    required_string(obj, "hypothesis", "spec")
    inputs = obj.get("inputs")
    gates = obj.get("gates", [])
    if not isinstance(inputs, list) or not inputs:
        raise AnalysisError("spec.inputs must be a non-empty array")
    if not isinstance(gates, list):
        raise AnalysisError("spec.gates must be an array")
    repetitions = integer(
        obj.get("bootstrap_repetitions", DEFAULT_BOOTSTRAP_REPETITIONS),
        "spec.bootstrap_repetitions",
        minimum=200,
    )
    if repetitions > 20_000:
        raise AnalysisError("spec.bootstrap_repetitions must be <= 20000")
    return obj


def mark_duplicate_artifacts(extractor: Extractor) -> None:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in extractor.input_records:
        artifact_identity = (
            f"sha256:{record['sha256']}"
            if record.get("sha256") is not None
            else f"expected:{record['path']}"
        )
        groups[
            (
                record["kind"],
                artifact_identity,
                canonical_hash(record.get("selector", {})),
            )
        ].append(record)
    for records in groups.values():
        if len(records) < 2:
            continue
        duplicate_ids = sorted(record["id"] for record in records)
        reason = "same artifact/selector is declared more than once: " + ", ".join(duplicate_ids)
        for record in records:
            record["performance_eligible"] = False
            record["duplicate_artifact_inputs"] = duplicate_ids
            extractor.refuse(record["id"], "duplicate_artifact_input", reason)


def mark_metric_identity_conflicts(extractor: Extractor) -> None:
    identities: dict[str, dict[tuple[str, str, str], set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for sample in extractor.samples:
        identities[sample.metric][
            (sample.unit, sample.direction, sample.aggregation)
        ].add(sample.input_id)
    conflicts = {
        metric: variants
        for metric, variants in identities.items()
        if len(variants) > 1
    }
    if not conflicts:
        return
    records_by_id = {record["id"]: record for record in extractor.input_records}
    for metric, variants in sorted(conflicts.items()):
        rendered = ", ".join(
            f"{unit}/{direction}/{aggregation}"
            for unit, direction, aggregation in sorted(variants)
        )
        affected = sorted(
            {input_id for input_ids in variants.values() for input_id in input_ids}
        )
        reason = (
            f"metric {metric!r} changes unit/direction/aggregation across inputs: "
            f"{rendered}"
        )
        for input_id in affected:
            record = records_by_id[input_id]
            record["performance_eligible"] = False
            record["status"] = "invalid"
            extractor.refuse(input_id, "metric_identity_conflict", reason)
    extractor.samples = [
        sample for sample in extractor.samples if sample.metric not in conflicts
    ]


def analyze(spec: Any, spec_path: Path, tool_path: Path | None = None) -> dict[str, Any]:
    script = (tool_path or Path(__file__)).resolve()
    tool_hash = sha256_file(script) if script.is_file() else canonical_hash({"tool": TOOL_VERSION})
    obj = validate_spec(spec)
    spec_hash = canonical_hash(obj)
    repetitions = int(obj.get("bootstrap_repetitions", DEFAULT_BOOTSTRAP_REPETITIONS))
    extractor = Extractor(spec_path.resolve().parent)
    ids: set[str] = set()
    for index, raw_input in enumerate(obj["inputs"]):
        item = ensure_object(raw_input, f"inputs[{index}]")
        input_id = required_string(item, "id", f"inputs[{index}]")
        if input_id in ids:
            raise AnalysisError(f"duplicate input id {input_id!r}")
        ids.add(input_id)
        extractor.extract(item, index)
    mark_duplicate_artifacts(extractor)
    mark_metric_identity_conflicts(extractor)

    summaries = summarize_metrics(extractor.samples, spec_hash, repetitions)
    comparisons = build_comparisons(extractor.samples, spec_hash, repetitions)
    gates = evaluate_gates(
        obj.get("gates", []),
        extractor.samples,
        extractor.input_records,
        spec_hash,
        repetitions,
    )
    downgrade_ledger = [
        {"gate_id": gate["id"], "code": downgrade}
        for gate in gates
        for downgrade in gate.get("downgrades", [])
    ]
    posture = "exploratory" if downgrade_ledger else "proof_required"
    diagnostic_gate_verdicts = [
        gate.get("diagnostic_verdict", gate["verdict"]) for gate in gates
    ]
    if not diagnostic_gate_verdicts:
        diagnostic_verdict = "not_resolvable"
    elif any(value == "rejected" for value in diagnostic_gate_verdicts):
        diagnostic_verdict = "rejected"
    elif all(value == "supported" for value in diagnostic_gate_verdicts):
        diagnostic_verdict = "supported"
    else:
        diagnostic_verdict = "not_resolvable"

    if not gates:
        verdict = "not_resolvable"
        overall_reason = "no declared gates"
    elif any(
        gate["verdict"] == "rejected" and gate.get("proof_eligible", False)
        for gate in gates
    ):
        verdict = "rejected"
        overall_reason = "at least one proof-eligible gate was rejected"
    elif all(
        gate["verdict"] == "supported" and gate.get("proof_eligible", False)
        for gate in gates
    ):
        verdict = "supported"
        overall_reason = "every declared proof-grade gate was supported"
    else:
        verdict = "not_resolvable"
        overall_reason = (
            "at least one gate is unresolved or exploratory and no proof-eligible gate was rejected"
        )

    record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": RECORD_TYPE,
        "analysis_id": obj["analysis_id"],
        "as_of": obj["as_of"],
        "hypothesis": obj["hypothesis"],
        "spec_sha256": spec_hash,
        "tool": {"name": "system-analysis", "version": TOOL_VERSION, "sha256": tool_hash},
        "posture": posture,
        "downgrades": downgrade_ledger,
        "verdict": verdict,
        "diagnostic_verdict": diagnostic_verdict,
        "verdict_reason": overall_reason,
        "input_records": extractor.input_records,
        "refusals": sorted(
            extractor.refusals,
            key=lambda row: (row["input_id"], row["code"], row["reason"]),
        ),
        "warnings": sorted(extractor.warnings),
        "metric_summaries": summaries,
        "comparisons": comparisons,
        "gates": gates,
    }
    # Make every malformed/unrepresentable output a typed analysis refusal,
    # never a late json.dumps traceback after verdict computation.
    canonical_bytes(record)
    return record


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_name = fh.name
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name is not None and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def protected_evidence_paths(
    spec: dict[str, Any], spec_path: Path, input_records: list[dict[str, Any]]
) -> set[Path]:
    base_dir = spec_path.resolve().parent
    protected = {spec_path.resolve(), Path(__file__).resolve()}
    for item in spec["inputs"]:
        protected.add((base_dir / item["path"]).resolve())
        for control in item.get("controls", []):
            if isinstance(control, dict) and isinstance(control.get("path"), str):
                protected.add((base_dir / control["path"]).resolve())
    for record in input_records:
        observation_parent = (base_dir / record["path"]).resolve().parent
        for declared_path in record.get("declared_provenance_paths", []):
            protected.add((observation_parent / declared_path).resolve())
        for provenance in record.get("observation_provenance", []):
            protected.add((observation_parent / provenance["path"]).resolve())
    return protected


def example_spec() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "analysis_id": "example-system-performance",
        "as_of": "2026-08-11",
        "hypothesis": "candidate improves paired latency without losing reliability",
        "notes": "Exploratory runnable template: legacy runtime reports and a point gate cannot produce a proof verdict.",
        "inputs": [
            {
                "id": "baseline-run-1",
                "kind": "runtime_report",
                "cohort": "baseline",
                "pair_id": "replay-1",
                "path": "artifacts/baseline-report.json",
                "controls": [{"role": "audio", "path": "artifacts/replay.wav"}],
            },
            {
                "id": "candidate-run-1",
                "kind": "runtime_report",
                "cohort": "candidate",
                "pair_id": "replay-1",
                "path": "artifacts/candidate-report.json",
                "controls": [{"role": "audio", "path": "artifacts/replay.wav"}],
            },
        ],
        "gates": [
            {
                "id": "capture-to-decision-improves",
                "scope": "comparison",
                "metric": "runtime.edge.capture_to_decision.p95_ms",
                "stat": "paired_improvement_median",
                "op": ">=",
                "threshold": 25.0,
                "unit": "ms",
                "min_samples": 1,
                "min_clusters": 1,
                "decision": "point",
                "minimum_provenance": "legacy_unbound",
            }
        ],
    }


class SelfTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tool = self.root / "tool.py"
        self.tool.write_text("tool-v1\n", encoding="utf-8")
        self.observation_provenance: list[dict[str, str]] = []
        for role, name in (
            ("measurement_spec", "measurement-spec.json"),
            ("raw_evidence", "raw-evidence.jsonl"),
            ("clock_anchor", "clock-anchor.json"),
            ("tool", "producer-tool.py"),
        ):
            source = self.root / name
            if role == "clock_anchor":
                source.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "record_type": "earshot.clock_anchor",
                            "host_boot_id": "fixture-boot",
                            "host_vantage": "fixture",
                            "clock_kind": "monotonic_raw",
                            "anchors": [
                                {"monotonic_ns": 1_000_000_000, "utc_ns": 1_700_000_000_000_000_000},
                                {"monotonic_ns": 2_000_000_000, "utc_ns": 1_700_000_001_000_000_000},
                            ],
                            "fit": {
                                "rate": 1.0,
                                "offset_ns": 1_699_999_999_000_000_000,
                                "max_residual_ns": 0,
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            else:
                source.write_text(f"{role}-fixture\n", encoding="utf-8")
            self.observation_provenance.append(
                {"role": role, "path": name, "sha256": sha256_file(source)}
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_json(self, name: str, value: Any) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def full_report(self, p95_us: int = 200_000, frames: int = 90, drops: int = 10) -> dict[str, Any]:
        return {
            "event_id": "fixture",
            "chunk_ms": 80,
            "frames_in": frames,
            "chunks_in": 10,
            "detections": [{"detect_latency_us": 125_000}],
            "drops": {
                "source_to_preproc": drops,
                "preproc_to_chunker": 0,
                "chunker_to_detector": 0,
            },
            "edges": {
                "capture→preproc": {"count": 10, "p50_us": 1, "p90_us": 2, "p95_us": 3, "p99_us": 4, "max_us": 5},
                "preproc→chunk": {"count": 10, "p50_us": 1, "p90_us": 2, "p95_us": 3, "p99_us": 4, "max_us": 5},
                "chunk→detect": {"count": 10, "p50_us": 1, "p90_us": 2, "p95_us": 3, "p99_us": 4, "max_us": 5},
                "detect→decision": {"count": 1, "p50_us": 1, "p90_us": 2, "p95_us": 3, "p99_us": 4, "max_us": 5},
                "capture→decision": {"count": 1, "p50_us": p95_us // 2, "p90_us": p95_us, "p95_us": p95_us, "p99_us": p95_us, "max_us": p95_us},
            },
            "orders": [],
            "lost_races": 0,
            "exit": {"reason": "clean_drain"},
        }

    def benchmark_row(
        self, phrase: str, *, true_positives: int, misses: int, lag_ms: int
    ) -> dict[str, Any]:
        return {
            "phrase": phrase,
            "tier": "Fast",
            "true_positives": true_positives,
            "misses": misses,
            "false_alarms": 0,
            "emit_lag_p50_ms": lag_ms,
            "emit_lag_p95_ms": lag_ms,
            "emit_lag_max_ms": lag_ms,
            "emit_lag_min_ms": lag_ms,
        }

    def benchmark_report(self, call_id: str, phrases: list[str]) -> dict[str, Any]:
        return {
            "split": "dev",
            "paced": False,
            "corpus_hours": 1.0,
            "calls": [call_id],
            "cells": [
                {
                    "engine": "streaming_ctc",
                    "chunk_ms": 80,
                    "model_dir": "models/base",
                    "rows": [
                        self.benchmark_row(
                            phrase, true_positives=8, misses=2, lag_ms=200
                        )
                        for phrase in phrases
                    ],
                },
                {
                    "engine": "streaming_ctc",
                    "chunk_ms": 80,
                    "model_dir": "models/candidate",
                    "rows": [
                        self.benchmark_row(
                            phrase, true_positives=9, misses=1, lag_ms=100
                        )
                        for phrase in phrases
                    ],
                },
            ],
        }

    def benchmark_inputs(self, report_name: str, pair_id: str) -> list[dict[str, Any]]:
        common = {"kind": "benchmark_report", "pair_id": pair_id, "path": report_name}
        return [
            {
                **common,
                "id": f"{pair_id}-base",
                "cohort": "baseline",
                "selector": {
                    "engine": "streaming_ctc",
                    "chunk_ms": 80,
                    "model_dir": "models/base",
                    "tier": "fast",
                },
            },
            {
                **common,
                "id": f"{pair_id}-candidate",
                "cohort": "candidate",
                "selector": {
                    "engine": "streaming_ctc",
                    "chunk_ms": 80,
                    "model_dir": "models/candidate",
                    "tier": "fast",
                },
            },
        ]

    def base_spec(self, inputs: list[dict[str, Any]], gates: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "analysis_id": "fixture",
            "as_of": "2026-08-11",
            "hypothesis": "fixture hypothesis",
            "bootstrap_repetitions": 200,
            "inputs": inputs,
            "gates": gates,
        }

    def analyze_spec(self, spec: dict[str, Any]) -> dict[str, Any]:
        return analyze(spec, self.root / "spec.json", self.tool)

    def summary(self, record: dict[str, Any], metric: str, cohort: str = "candidate") -> dict[str, Any]:
        return next(
            row
            for row in record["metric_summaries"]
            if row["metric"] == metric and row["cohort"] == cohort
        )

    def test_missing_expected_report_stays_in_denominator(self) -> None:
        spec = self.base_spec(
            [{"id": "missing", "kind": "runtime_report", "cohort": "candidate", "path": "no.json"}],
            [
                {
                    "id": "presence",
                    "scope": "cohort",
                    "metric": "runtime.report_file_present",
                    "stat": "pooled_ratio",
                    "op": ">=",
                    "threshold": 1,
                    "unit": "proportion",
                    "minimum_provenance": "legacy_unbound",
                    "decision": "point",
                }
            ],
        )
        record = self.analyze_spec(spec)
        self.assertEqual(record["verdict"], "rejected")
        self.assertEqual(self.summary(record, "runtime.report_file_present")["estimate"]["value"], 0)
        self.assertEqual(record["refusals"][0]["code"], "missing_artifact")

    def test_partial_report_does_not_invent_zero_ingress(self) -> None:
        self.write_json("partial.json", {"event_id": "x", "partial_report": True, "exit": {"reason": "sigterm"}})
        spec = self.base_spec(
            [{"id": "partial", "kind": "runtime_report", "cohort": "candidate", "path": "partial.json"}],
            [],
        )
        record = self.analyze_spec(spec)
        self.assertEqual(self.summary(record, "runtime.pipeline_report_complete")["estimate"]["value"], 0)
        self.assertFalse(any(row["metric"] == "runtime.ingress_present" for row in record["metric_summaries"]))

    def test_json_parser_rejects_duplicate_keys_and_nonfinite_constants(self) -> None:
        for payload in (
            b'{"value": 1, "value": 2}',
            b'{"value": NaN}',
            b'{"value": Infinity}',
            b'{"value": -Infinity}',
        ):
            value, error = parse_json_bytes(payload)
            self.assertIsNone(value)
            self.assertIsNotNone(error)

        (self.root / "nonfinite.json").write_text(
            '{"event_id": NaN}', encoding="utf-8"
        )
        record = self.analyze_spec(
            self.base_spec(
                [
                    {
                        "id": "nonfinite",
                        "kind": "runtime_report",
                        "cohort": "candidate",
                        "path": "nonfinite.json",
                    }
                ],
                [],
            )
        )
        self.assertEqual(
            self.summary(record, "runtime.report_parseable")["estimate"]["value"],
            0,
        )

    def test_drop_percentage_preserves_telemetry_contract(self) -> None:
        self.write_json("run.json", self.full_report(frames=90, drops=10))
        spec = self.base_spec(
            [{"id": "run", "kind": "runtime_report", "cohort": "candidate", "path": "run.json"}],
            [],
        )
        record = self.analyze_spec(spec)
        self.assertAlmostEqual(
            self.summary(record, "runtime.reported_drop_pct")["estimate"]["value"],
            100 * 10 / 90,
        )

    def test_malformed_core_fields_cannot_report_complete(self) -> None:
        report = self.full_report()
        del report["drops"]["chunker_to_detector"]
        self.write_json("malformed.json", report)
        spec = self.base_spec(
            [{"id": "run", "kind": "runtime_report", "cohort": "candidate", "path": "malformed.json"}],
            [],
        )
        record = self.analyze_spec(spec)
        self.assertEqual(
            self.summary(record, "runtime.pipeline_report_complete")["estimate"]["value"], 0
        )

    def test_legacy_evidence_cannot_pass_default_proof_gate(self) -> None:
        self.write_json("run.json", self.full_report())
        spec = self.base_spec(
            [{"id": "run", "kind": "runtime_report", "cohort": "candidate", "path": "run.json"}],
            [
                {
                    "id": "complete",
                    "scope": "cohort",
                    "metric": "runtime.edge.capture_to_decision.p95_ms",
                    "stat": "median",
                    "op": ">=",
                    "threshold": 500,
                    "unit": "ms",
                    "decision": "point",
                }
            ],
        )
        record = self.analyze_spec(spec)
        self.assertEqual(record["verdict"], "not_resolvable")
        self.assertIn("provenance", record["gates"][0]["reason"])

    def test_mismatched_controls_refuse_pair(self) -> None:
        self.write_json("base.json", self.full_report(200_000))
        self.write_json("candidate.json", self.full_report(100_000))
        (self.root / "control-a").write_text("a", encoding="utf-8")
        (self.root / "control-b").write_text("b", encoding="utf-8")
        inputs = [
            {"id": "b", "kind": "runtime_report", "cohort": "baseline", "pair_id": "p", "path": "base.json", "controls": [{"role": "audio", "path": "control-a"}]},
            {"id": "c", "kind": "runtime_report", "cohort": "candidate", "pair_id": "p", "path": "candidate.json", "controls": [{"role": "audio", "path": "control-b"}]},
        ]
        gate = {
            "id": "latency",
            "scope": "comparison",
            "metric": "runtime.edge.capture_to_decision.p95_ms",
            "stat": "paired_improvement_median",
            "op": ">=",
            "threshold": 50,
            "unit": "ms",
            "minimum_provenance": "legacy_unbound",
            "decision": "point",
        }
        record = self.analyze_spec(self.base_spec(inputs, [gate]))
        self.assertEqual(record["verdict"], "not_resolvable")
        self.assertIn("pairs were refused", record["gates"][0]["reason"])

    def test_lower_latency_becomes_positive_improvement(self) -> None:
        self.write_json("base.json", self.full_report(200_000))
        self.write_json("candidate.json", self.full_report(100_000))
        (self.root / "audio").write_text("same", encoding="utf-8")
        control = [{"role": "audio", "path": "audio"}]
        inputs = [
            {"id": "b", "kind": "runtime_report", "cohort": "baseline", "pair_id": "p", "path": "base.json", "controls": control},
            {"id": "c", "kind": "runtime_report", "cohort": "candidate", "pair_id": "p", "path": "candidate.json", "controls": control},
        ]
        gate = {
            "id": "latency",
            "scope": "comparison",
            "metric": "runtime.edge.capture_to_decision.p95_ms",
            "stat": "paired_improvement_median",
            "op": ">=",
            "threshold": 100,
            "unit": "ms",
            "minimum_provenance": "legacy_unbound",
            "decision": "point",
        }
        record = self.analyze_spec(self.base_spec(inputs, [gate]))
        self.assertEqual(record["verdict"], "not_resolvable")
        self.assertEqual(record["diagnostic_verdict"], "supported")
        self.assertEqual(record["gates"][0]["diagnostic_verdict"], "supported")
        self.assertEqual(record["gates"][0]["estimate"], 100)

    def observation(self, observation_id: str, value: float) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "record_type": "earshot.observation",
            "observation_id": observation_id,
            "cluster_id": observation_id,
            "status": "measured",
            "method": "planted race margin",
            "clock_domain": "utc_anchored",
            "host_vantage": "fixture",
            "provenance": self.observation_provenance,
            "metrics": [
                {
                    "name": "ingress.audio_valid",
                    "value": 1.0,
                    "unit": "proportion",
                    "direction": "higher_is_better",
                    "sample_key": observation_id,
                    "numerator": 1,
                    "denominator": 1,
                },
                {
                    "name": "detection.words_valid",
                    "value": 1.0,
                    "unit": "proportion",
                    "direction": "higher_is_better",
                    "sample_key": observation_id,
                    "numerator": 1,
                    "denominator": 1,
                },
                {
                    "name": "action.path_valid",
                    "value": 1.0,
                    "unit": "proportion",
                    "direction": "higher_is_better",
                    "sample_key": observation_id,
                    "numerator": 1,
                    "denominator": 1,
                },
                {
                    "name": "race.executable_margin_ms",
                    "value": value,
                    "unit": "ms",
                    "direction": "higher_is_better",
                    "sample_key": observation_id,
                }
            ],
            "attributes": {
                "clock_model": "two_anchor_affine",
                "clock_uncertainty_ms": 1.0,
                "run_id": observation_id,
                "host_boot_id": "fixture-boot",
                "competitor_event_id": f"competitor-{observation_id}",
                "sign_convention": "competitor_time_minus_our_executable_time",
            },
        }

    def test_provenance_bound_scalar_is_diagnostic_only(self) -> None:
        self.write_json("o1.json", self.observation("o1", 100))
        self.write_json("o2.json", self.observation("o2", 120))
        inputs = [
            {"id": "o1", "kind": "observation_record", "cohort": "candidate", "path": "o1.json"},
            {"id": "o2", "kind": "observation_record", "cohort": "candidate", "path": "o2.json"},
        ]
        gate = {
            "id": "race-diagnostic",
            "scope": "cohort",
            "cohort": "candidate",
            "input_ids": ["o1", "o2"],
            "metric": "race.executable_margin_ms",
            "stat": "median",
            "op": ">=",
            "threshold": 50,
            "unit": "ms",
            "min_samples": 2,
            "min_clusters": 2,
            "minimum_provenance": "provenance_bound",
            "decision": "point",
        }
        record = self.analyze_spec(
            self.base_spec(inputs, [gate])
        )
        self.assertEqual(record["verdict"], "not_resolvable")
        self.assertEqual(record["diagnostic_verdict"], "supported")
        self.assertEqual(record["posture"], "exploratory")
        self.assertEqual(record["gates"][0]["rung"], 0)
        self.assertIn("unvalidated_scalar_allowed", record["gates"][0]["downgrades"])
        self.assertEqual(
            self.summary(record, "race.executable_margin_ms")["provenance_counts"],
            {"provenance_bound": 2},
        )

    def test_ratio_value_can_be_recomputed_from_counts(self) -> None:
        observation = self.observation("ratio", 100)
        observation["metrics"].append(
            {
                "name": "quality.coverage",
                "unit": "proportion",
                "direction": "higher_is_better",
                "sample_key": "ratio",
                "numerator": 1,
                "denominator": 2,
            }
        )
        self.write_json("ratio.json", observation)
        record = self.analyze_spec(
            self.base_spec(
                [
                    {
                        "id": "ratio",
                        "kind": "observation_record",
                        "cohort": "candidate",
                        "path": "ratio.json",
                    }
                ],
                [],
            )
        )
        self.assertEqual(
            self.summary(record, "quality.coverage")["estimate"]["value"], 0.5
        )

    def test_missing_expected_input_blocks_downstream_gate(self) -> None:
        self.write_json("o1.json", self.observation("o1", 100))
        self.write_json("o2.json", self.observation("o2", 120))
        inputs = [
            {"id": "o1", "kind": "observation_record", "cohort": "candidate", "path": "o1.json"},
            {"id": "o2", "kind": "observation_record", "cohort": "candidate", "path": "o2.json"},
            {"id": "missing", "kind": "observation_record", "cohort": "candidate", "path": "missing.json"},
        ]
        gate = {
            "id": "wins",
            "scope": "cohort",
            "metric": "race.executable_margin_ms",
            "stat": "median",
            "op": ">=",
            "threshold": 50,
            "unit": "ms",
            "min_samples": 2,
            "min_clusters": 2,
        }
        record = self.analyze_spec(self.base_spec(inputs, [gate]))
        self.assertEqual(record["verdict"], "not_resolvable")
        self.assertEqual(record["gates"][0]["invalid_inputs"], ["missing"])

    def test_observation_hash_claim_is_reverified(self) -> None:
        observation = self.observation("o1", 100)
        (self.root / "raw-evidence.jsonl").write_text("tampered-after-record\n", encoding="utf-8")
        self.write_json("tampered.json", observation)
        spec = self.base_spec(
            [{"id": "o1", "kind": "observation_record", "cohort": "candidate", "path": "tampered.json"}],
            [],
        )
        record = self.analyze_spec(spec)
        self.assertTrue(any(row["code"] == "malformed_observation" for row in record["refusals"]))
        self.assertFalse(
            any(row["metric"] == "race.executable_margin_ms" for row in record["metric_summaries"])
        )

    def test_benchmark_cells_pair_only_inside_exact_invocation(self) -> None:
        def row(tp: int, misses: int, false_alarms: int, lag: int) -> dict[str, Any]:
            return {
                "phrase": "alpha",
                "tier": "Fast",
                "true_positives": tp,
                "misses": misses,
                "false_alarms": false_alarms,
                "recall": tp / (tp + misses),
                "precision": tp / (tp + false_alarms),
                "fa_per_hour": false_alarms,
                "latency_p50_ms": None,
                "latency_p95_ms": None,
                "latency_max_ms": None,
                "emit_lag_p50_ms": lag,
                "emit_lag_p95_ms": lag,
                "emit_lag_max_ms": lag,
                "emit_lag_min_ms": lag,
                "consumed_lag_p50_ms": lag,
                "consumed_lag_p95_ms": lag,
            }

        report = {
            "split": "dev",
            "paced": False,
            "corpus_hours": 1.0,
            "calls": ["call-a"],
            "cells": [
                {"engine": "streaming_ctc", "chunk_ms": 80, "model_dir": "models/base", "rows": [row(8, 2, 1, 200)], "fa_autopsy": []},
                {"engine": "streaming_ctc", "chunk_ms": 80, "model_dir": "models/candidate", "rows": [row(9, 1, 1, 100)], "fa_autopsy": []},
            ],
        }
        self.write_json("bench.json", report)
        common = {"kind": "benchmark_report", "pair_id": "same-invocation", "path": "bench.json"}
        inputs = [
            {**common, "id": "base", "cohort": "baseline", "selector": {"engine": "streaming_ctc", "chunk_ms": 80, "model_dir": "models/base", "tier": "fast"}},
            {**common, "id": "candidate", "cohort": "candidate", "selector": {"engine": "streaming_ctc", "chunk_ms": 80, "model_dir": "models/candidate", "tier": "fast"}},
        ]
        gate = {
            "id": "recall",
            "scope": "comparison",
            "metric": "benchmark.recall",
            "stat": "paired_improvement_median",
            "op": ">=",
            "threshold": 0.09,
            "unit": "proportion",
            "minimum_provenance": "legacy_unbound",
            "decision": "point",
        }
        record = self.analyze_spec(self.base_spec(inputs, [gate]))
        self.assertEqual(record["verdict"], "not_resolvable")
        self.assertEqual(record["diagnostic_verdict"], "supported")
        self.assertAlmostEqual(record["gates"][0]["estimate"], 0.1)

    def test_malformed_benchmark_row_is_a_refusal_not_process_abort(self) -> None:
        report = {
            "split": "dev",
            "paced": False,
            "corpus_hours": 1.0,
            "calls": ["call-a"],
            "cells": [
                {
                    "engine": "streaming_ctc",
                    "chunk_ms": 80,
                    "model_dir": None,
                    "rows": [
                        {
                            "phrase": "alpha",
                            "tier": "Fast",
                            "misses": 1,
                            "false_alarms": 0,
                        }
                    ],
                    "fa_autopsy": [],
                }
            ],
        }
        self.write_json("bad-bench.json", report)
        spec = self.base_spec(
            [
                {
                    "id": "bad",
                    "kind": "benchmark_report",
                    "cohort": "candidate",
                    "path": "bad-bench.json",
                    "selector": {
                        "engine": "streaming_ctc",
                        "chunk_ms": 80,
                        "model_dir": None,
                        "tier": "fast",
                    },
                }
            ],
            [],
        )
        record = self.analyze_spec(spec)
        self.assertTrue(
            any(row["code"] == "malformed_benchmark_report" for row in record["refusals"])
        )
        self.assertEqual(record["input_records"][0]["status"], "invalid")

    def test_integrity_gate_keeps_missing_runtime_zero(self) -> None:
        self.write_json("full.json", self.full_report())
        inputs = [
            {"id": "full", "kind": "runtime_report", "cohort": "candidate", "path": "full.json"},
            {"id": "missing", "kind": "runtime_report", "cohort": "candidate", "path": "missing.json"},
        ]
        gate = {
            "id": "complete",
            "scope": "cohort",
            "metric": "runtime.pipeline_report_complete",
            "stat": "pooled_ratio",
            "op": ">=",
            "threshold": 1.0,
            "unit": "proportion",
            "decision": "point",
        }
        record = self.analyze_spec(self.base_spec(inputs, [gate]))
        self.assertEqual(record["verdict"], "rejected")
        self.assertEqual(record["gates"][0]["estimate"], 0.5)
        self.assertEqual(record["gates"][0]["eligible_samples"], 2)

    def test_valid_input_omitting_target_metric_blocks_gate(self) -> None:
        self.write_json("complete.json", self.full_report())
        without_p95 = self.full_report()
        del without_p95["edges"]["capture→decision"]["p95_us"]
        self.write_json("without-p95.json", without_p95)
        inputs = [
            {"id": "complete", "kind": "runtime_report", "cohort": "candidate", "path": "complete.json"},
            {"id": "without", "kind": "runtime_report", "cohort": "candidate", "path": "without-p95.json"},
        ]
        gate = {
            "id": "latency",
            "scope": "cohort",
            "metric": "runtime.edge.capture_to_decision.p95_ms",
            "stat": "median",
            "op": "<=",
            "threshold": 500,
            "unit": "ms",
            "minimum_provenance": "legacy_unbound",
            "decision": "point",
        }
        record = self.analyze_spec(self.base_spec(inputs, [gate]))
        self.assertEqual(record["verdict"], "not_resolvable")
        self.assertEqual(record["gates"][0]["missing_metric_inputs"], ["without"])

    def test_reported_drop_percentage_can_exceed_one_hundred(self) -> None:
        self.write_json("saturated.json", self.full_report(frames=1, drops=2))
        record = self.analyze_spec(
            self.base_spec(
                [{"id": "run", "kind": "runtime_report", "cohort": "candidate", "path": "saturated.json"}],
                [],
            )
        )
        self.assertEqual(
            self.summary(record, "runtime.reported_drop_pct")["estimate"]["value"], 200.0
        )

    def test_phrase_rows_from_one_invocation_are_one_cluster(self) -> None:
        phrases = [f"word-{index}" for index in range(5)]
        self.write_json("five-phrases.json", self.benchmark_report("call-one", phrases))
        inputs = self.benchmark_inputs("five-phrases.json", "p1")
        gate = {
            "id": "latency",
            "scope": "comparison",
            "metric": "benchmark.phrase_engine_emit_lag_p95_ms",
            "stat": "paired_improvement_median",
            "op": ">=",
            "threshold": 50,
            "unit": "ms",
            "min_samples": 5,
            "min_clusters": 5,
            "minimum_provenance": "legacy_unbound",
            "decision": "point",
        }
        record = self.analyze_spec(self.base_spec(inputs, [gate]))
        self.assertEqual(record["gates"][0]["eligible_samples"], 5)
        self.assertEqual(record["gates"][0]["eligible_clusters"], 1)
        self.assertEqual(record["gates"][0]["verdict"], "not_resolvable")

    def test_paired_win_rate_uses_independent_cluster_wilson_bound(self) -> None:
        inputs: list[dict[str, Any]] = []
        for index in (1, 2):
            name = f"bench-{index}.json"
            pair_id = f"p{index}"
            self.write_json(name, self.benchmark_report(f"call-{index}", ["alpha"]))
            inputs.extend(self.benchmark_inputs(name, pair_id))
        gate = {
            "id": "wins",
            "scope": "comparison",
            "metric": "benchmark.recall",
            "stat": "paired_win_rate",
            "op": ">=",
            "threshold": 0.99,
            "unit": "proportion",
            "min_samples": 2,
            "min_clusters": 2,
            "minimum_provenance": "legacy_unbound",
        }
        record = self.analyze_spec(self.base_spec(inputs, [gate]))
        result = record["gates"][0]
        self.assertEqual(result["estimate"], 1.0)
        self.assertLess(result["uncertainty"]["lower"], 0.99)
        self.assertEqual(result["verdict"], "not_resolvable")

    def test_pooled_ratio_bound_refuses_multiple_trials_per_cluster(self) -> None:
        samples = [
            Sample(
                metric="quality.win_rate",
                value=value,
                unit="proportion",
                direction="higher_is_better",
                cohort="comparison",
                input_id=f"sample-{index}",
                cluster_id=cluster_id,
                pair_id=f"pair-{index}",
                sample_key="",
                comparison_signature="paired",
                provenance_grade="validator_recomputed",
                aggregation="ratio",
                numerator=value,
                denominator=1.0,
            )
            for index, (cluster_id, value) in enumerate(
                (("component-a", 1.0), ("component-a", 1.0),
                 ("component-b", 0.0), ("component-b", 0.0))
            )
        ]
        interval = bootstrap_ci(samples, "pooled_ratio", 0.95, 200, "fixture")
        self.assertFalse(interval["available"])
        self.assertIn("one Bernoulli trial", interval["reason"])

    def test_continuous_bootstrap_requires_twenty_independent_clusters(self) -> None:
        samples = [
            Sample(
                metric="quality.latency_ms",
                value=10.0,
                unit="ms",
                direction="lower_is_better",
                cohort="candidate",
                input_id=f"run-{index}",
                cluster_id=f"run-{index}",
                pair_id=None,
                sample_key="",
                comparison_signature=None,
                provenance_grade="validator_recomputed",
            )
            for index in range(MIN_CONTINUOUS_BOOTSTRAP_CLUSTERS - 1)
        ]
        interval = bootstrap_ci(samples, "median", 0.95, 200, "fixture")
        self.assertFalse(interval["available"])
        self.assertIn(
            str(MIN_CONTINUOUS_BOOTSTRAP_CLUSTERS), interval["reason"]
        )
        samples.append(
            Sample(
                metric="quality.latency_ms",
                value=10.0,
                unit="ms",
                direction="lower_is_better",
                cohort="candidate",
                input_id="run-19",
                cluster_id="run-19",
                pair_id=None,
                sample_key="",
                comparison_signature=None,
                provenance_grade="validator_recomputed",
            )
        )
        unresolved_tail = bootstrap_ci(samples, "median", 0.95, 200, "fixture")
        self.assertFalse(unresolved_tail["available"])
        self.assertIn("expected resamples in each tail", unresolved_tail["reason"])
        resolved = bootstrap_ci(samples, "median", 0.95, 2_000, "fixture")
        self.assertTrue(resolved["available"])

    def test_confidence_too_close_to_one_is_a_typed_error(self) -> None:
        with self.assertRaises(AnalysisError):
            wilson_interval(1.0, 2, math.nextafter(1.0, 0.0))

        self.write_json("confidence.json", self.full_report())
        gate = {
            "id": "confidence",
            "scope": "cohort",
            "metric": "runtime.pipeline_report_complete",
            "stat": "pooled_ratio",
            "op": ">=",
            "threshold": 1,
            "unit": "proportion",
            "confidence": math.nextafter(1.0, 0.0),
        }
        with self.assertRaises(AnalysisError):
            self.analyze_spec(
                self.base_spec(
                    [
                        {
                            "id": "confidence",
                            "kind": "runtime_report",
                            "cohort": "candidate",
                            "path": "confidence.json",
                        }
                    ],
                    [gate],
                )
            )

    def test_one_refused_pair_blocks_surviving_pairs(self) -> None:
        for name, value in (
            ("b1.json", 200_000),
            ("c1.json", 100_000),
            ("b2.json", 210_000),
            ("c2.json", 110_000),
            ("b3.json", 220_000),
            ("c3.json", 120_000),
        ):
            self.write_json(name, self.full_report(value))
        for name, value in (("same", "same"), ("left", "left"), ("right", "right")):
            (self.root / name).write_text(value, encoding="utf-8")
        inputs: list[dict[str, Any]] = []
        for index in (1, 2, 3):
            baseline_control = "same" if index < 3 else "left"
            candidate_control = "same" if index < 3 else "right"
            inputs.extend(
                [
                    {"id": f"b{index}", "kind": "runtime_report", "cohort": "baseline", "pair_id": f"p{index}", "path": f"b{index}.json", "controls": [{"role": "audio", "path": baseline_control}]},
                    {"id": f"c{index}", "kind": "runtime_report", "cohort": "candidate", "pair_id": f"p{index}", "path": f"c{index}.json", "controls": [{"role": "audio", "path": candidate_control}]},
                ]
            )
        gate = {
            "id": "latency",
            "scope": "comparison",
            "metric": "runtime.edge.capture_to_decision.p95_ms",
            "stat": "paired_improvement_median",
            "op": ">=",
            "threshold": 50,
            "unit": "ms",
            "min_samples": 2,
            "min_clusters": 2,
            "minimum_provenance": "legacy_unbound",
            "decision": "point",
        }
        result = self.analyze_spec(self.base_spec(inputs, [gate]))["gates"][0]
        self.assertEqual(result["verdict"], "not_resolvable")
        self.assertEqual(len(result["refused_pairs"]), 1)
        self.assertIn("pairs were refused", result["reason"])

    def test_unpairable_expected_samples_cannot_hide_behind_surviving_pair(self) -> None:
        common = {
            "metric": "quality.latency_ms",
            "unit": "ms",
            "direction": "lower_is_better",
            "sample_key": "",
            "comparison_signature": "same-controls",
            "provenance_grade": "legacy_unbound",
        }
        samples = [
            Sample(
                value=100,
                cohort="baseline",
                input_id="baseline",
                cluster_id="baseline-run",
                pair_id="pair-1",
                **common,
            ),
            Sample(
                value=50,
                cohort="candidate",
                input_id="candidate",
                cluster_id="candidate-run",
                pair_id="pair-1",
                **common,
            ),
            Sample(
                value=40,
                cohort="candidate",
                input_id="missing-pair-id",
                cluster_id="unlabelled-run",
                pair_id=None,
                **common,
            ),
            Sample(
                value=110,
                cohort="baseline",
                input_id="declared-unpairable",
                cluster_id="unpairable-run",
                pair_id="pair-2",
                pairable=False,
                **common,
            ),
        ]
        records = [
            {"id": sample.input_id, "cohort": sample.cohort, "performance_eligible": True}
            for sample in samples
        ]
        gate = {
            "id": "paired-latency",
            "scope": "comparison",
            "metric": "quality.latency_ms",
            "stat": "paired_improvement_median",
            "op": ">=",
            "threshold": 40,
            "unit": "ms",
            "minimum_provenance": "legacy_unbound",
            "decision": "point",
        }
        strict = evaluate_gates([gate], samples, records, "spec", 200)[0]
        self.assertEqual(strict["verdict"], "not_resolvable")
        self.assertEqual(
            {row["reason"] for row in strict["refused_pairs"]},
            {"input has no pair_id", "metric sample is declared unpairable"},
        )

        survivor_gate = {**gate, "input_validity": "metric_only"}
        survivor = evaluate_gates([survivor_gate], samples, records, "spec", 200)[0]
        self.assertEqual(survivor["verdict"], "supported")
        self.assertFalse(survivor["proof_eligible"])

    def test_all_expected_cohort_rejects_lower_provenance_survivor(self) -> None:
        metric = "quality.validated_score"
        REGISTERED_METRIC_VALIDATORS[metric] = {
            "id": "fixture-validator-v1",
            "rung": 2,
        }
        try:
            samples = [
                Sample(
                    metric=metric,
                    value=value,
                    unit="score",
                    direction="higher_is_better",
                    cohort="candidate",
                    input_id=input_id,
                    cluster_id=f"cluster-{input_id}",
                    pair_id=None,
                    sample_key="",
                    comparison_signature=None,
                    provenance_grade=grade,
                )
                for input_id, value, grade in (
                    ("verified-a", 1.0, "validator_recomputed"),
                    ("verified-b", 1.0, "validator_recomputed"),
                    ("bound-only", 0.0, "provenance_bound"),
                )
            ]
            records = [
                {
                    "id": sample.input_id,
                    "cohort": "candidate",
                    "performance_eligible": True,
                }
                for sample in samples
            ]
            gate = {
                "id": "validated-score",
                "scope": "cohort",
                "metric": metric,
                "stat": "mean",
                "op": ">=",
                "threshold": 0.9,
                "unit": "score",
                "min_samples": 2,
                "min_clusters": 2,
            }
            result = evaluate_gates([gate], samples, records, "spec", 200)[0]
            self.assertEqual(result["verdict"], "not_resolvable")
            self.assertEqual(result["insufficient_provenance_inputs"], ["bound-only"])
        finally:
            REGISTERED_METRIC_VALIDATORS.pop(metric, None)

    def test_all_expected_pair_rejects_lower_provenance_samples(self) -> None:
        metric = "quality.validated_latency_ms"
        REGISTERED_METRIC_VALIDATORS[metric] = {
            "id": "fixture-validator-v1",
            "rung": 3,
        }
        try:
            common = {
                "metric": metric,
                "unit": "ms",
                "direction": "lower_is_better",
                "sample_key": "",
                "comparison_signature": "same-controls",
            }
            samples = [
                Sample(
                    value=100,
                    cohort="baseline",
                    input_id="verified-base",
                    cluster_id="verified-base-run",
                    pair_id="verified-pair",
                    provenance_grade="validator_recomputed",
                    **common,
                ),
                Sample(
                    value=50,
                    cohort="candidate",
                    input_id="verified-candidate",
                    cluster_id="verified-candidate-run",
                    pair_id="verified-pair",
                    provenance_grade="validator_recomputed",
                    **common,
                ),
                Sample(
                    value=90,
                    cohort="baseline",
                    input_id="bound-base",
                    cluster_id="bound-base-run",
                    pair_id="bound-pair",
                    provenance_grade="provenance_bound",
                    **common,
                ),
                Sample(
                    value=45,
                    cohort="candidate",
                    input_id="bound-candidate",
                    cluster_id="bound-candidate-run",
                    pair_id="bound-pair",
                    provenance_grade="provenance_bound",
                    **common,
                ),
            ]
            records = [
                {
                    "id": sample.input_id,
                    "cohort": sample.cohort,
                    "performance_eligible": True,
                }
                for sample in samples
            ]
            gate = {
                "id": "validated-latency",
                "scope": "comparison",
                "metric": metric,
                "stat": "paired_improvement_median",
                "op": ">=",
                "threshold": 40,
                "unit": "ms",
                "decision": "point",
            }
            result = evaluate_gates([gate], samples, records, "spec", 200)[0]
            self.assertEqual(result["verdict"], "not_resolvable")
            self.assertEqual(len(result["refused_pairs"]), 2)
            self.assertTrue(
                all("below required" in row["reason"] for row in result["refused_pairs"])
            )
        finally:
            REGISTERED_METRIC_VALIDATORS.pop(metric, None)

    def test_unvalidated_rejection_is_top_level_diagnostic_only(self) -> None:
        self.write_json("diagnostic-race.json", self.observation("diagnostic-race", 100))
        gate = {
            "id": "race-diagnostic",
            "scope": "cohort",
            "metric": "race.executable_margin_ms",
            "stat": "median",
            "op": ">=",
            "threshold": 1000,
            "unit": "ms",
            "minimum_provenance": "provenance_bound",
            "decision": "point",
        }
        record = self.analyze_spec(
            self.base_spec(
                [
                    {
                        "id": "diagnostic-race",
                        "kind": "observation_record",
                        "cohort": "candidate",
                        "path": "diagnostic-race.json",
                    }
                ],
                [gate],
            )
        )
        self.assertEqual(record["gates"][0]["verdict"], "rejected")
        self.assertFalse(record["gates"][0]["proof_eligible"])
        self.assertEqual(record["verdict"], "not_resolvable")
        self.assertEqual(record["diagnostic_verdict"], "rejected")

    def test_integrity_gate_includes_malformed_observation_zero(self) -> None:
        self.write_json("good.json", self.observation("good", 100))
        bad = json.loads(json.dumps(self.observation("bad", 100)))
        bad["provenance"][0]["sha256"] = "0" * 64
        self.write_json("bad.json", bad)
        inputs = [
            {"id": "good", "kind": "observation_record", "cohort": "candidate", "path": "good.json"},
            {"id": "bad", "kind": "observation_record", "cohort": "candidate", "path": "bad.json"},
        ]
        gate = {
            "id": "schema",
            "scope": "cohort",
            "metric": "observation.schema_valid",
            "stat": "pooled_ratio",
            "op": ">=",
            "threshold": 1,
            "unit": "proportion",
            "decision": "point",
        }
        record = self.analyze_spec(self.base_spec(inputs, [gate]))
        self.assertEqual(record["verdict"], "rejected")
        self.assertEqual(record["gates"][0]["estimate"], 0.5)

    def test_duplicate_observation_metric_is_refused(self) -> None:
        observation = self.observation("duplicate", 100)
        observation["metrics"].append(dict(observation["metrics"][0]))
        self.write_json("duplicate.json", observation)
        record = self.analyze_spec(
            self.base_spec(
                [{"id": "duplicate", "kind": "observation_record", "cohort": "candidate", "path": "duplicate.json"}],
                [],
            )
        )
        self.assertTrue(
            any(row["code"] == "malformed_observation" for row in record["refusals"])
        )

    def test_cross_input_metric_identity_conflict_is_a_typed_refusal(self) -> None:
        left = self.observation("left-identity", 100)
        right = self.observation("right-identity", 100)
        left["metrics"].append(
            {
                "name": "quality.speed",
                "value": 1,
                "unit": "ms",
                "direction": "lower_is_better",
                "sample_key": "left",
            }
        )
        right["metrics"].append(
            {
                "name": "quality.speed",
                "value": 1,
                "unit": "seconds",
                "direction": "lower_is_better",
                "sample_key": "right",
            }
        )
        left["metrics"].append(
            {
                "name": "quality.mixed_aggregation",
                "value": 0.5,
                "unit": "proportion",
                "direction": "higher_is_better",
                "sample_key": "left",
            }
        )
        right["metrics"].append(
            {
                "name": "quality.mixed_aggregation",
                "value": 0.5,
                "unit": "proportion",
                "direction": "higher_is_better",
                "sample_key": "right",
                "numerator": 1,
                "denominator": 2,
            }
        )
        self.write_json("left-identity.json", left)
        self.write_json("right-identity.json", right)
        record = self.analyze_spec(
            self.base_spec(
                [
                    {"id": "left", "kind": "observation_record", "cohort": "candidate", "path": "left-identity.json"},
                    {"id": "right", "kind": "observation_record", "cohort": "candidate", "path": "right-identity.json"},
                ],
                [],
            )
        )
        self.assertEqual(
            sum(row["code"] == "metric_identity_conflict" for row in record["refusals"]),
            4,
        )
        self.assertFalse(
            any(
                row["metric"] in {"quality.speed", "quality.mixed_aggregation"}
                for row in record["metric_summaries"]
            )
        )

    def test_benchmark_row_requires_nonempty_phrase(self) -> None:
        report = self.benchmark_report("call", ["alpha"])
        report["cells"][0]["rows"][0]["phrase"] = None
        self.write_json("bad-phrase.json", report)
        input_record = self.benchmark_inputs("bad-phrase.json", "p")[0]
        record = self.analyze_spec(self.base_spec([input_record], []))
        self.assertTrue(
            any(row["code"] == "invalid_benchmark_phrase" for row in record["refusals"])
        )

    def test_duplicate_artifact_inputs_are_ineligible(self) -> None:
        self.write_json("one.json", self.observation("one", 100))
        inputs = [
            {"id": "alias-a", "kind": "observation_record", "cohort": "candidate", "path": "one.json"},
            {"id": "alias-b", "kind": "observation_record", "cohort": "candidate", "path": "one.json"},
        ]
        gate = {
            "id": "race",
            "scope": "cohort",
            "metric": "race.executable_margin_ms",
            "stat": "median",
            "op": ">=",
            "threshold": 1,
            "unit": "ms",
            "decision": "point",
        }
        record = self.analyze_spec(self.base_spec(inputs, [gate]))
        self.assertEqual(record["verdict"], "not_resolvable")
        self.assertEqual(
            sum(row["code"] == "duplicate_artifact_input" for row in record["refusals"]),
            2,
        )

    def test_proof_ladder_rejects_disjoint_input_lineages(self) -> None:
        results = [
            {
                "id": f"rung-{rung}",
                "rung": rung,
                "input_ids": [f"disjoint-run-{rung}"],
                "verdict": "supported",
                "proof_eligible": True,
            }
            for rung in range(1, 6)
        ]
        apply_proof_ladder(results)
        race = results[-1]
        self.assertEqual(race["verdict"], "not_resolvable")
        self.assertEqual(race["diagnostic_verdict"], "supported")
        self.assertEqual(
            race["proof_ladder"]["missing_prior_rungs"],
            ["evidence_integrity", "ingress", "detection", "action_path"],
        )

    def test_proof_ladder_accepts_one_exact_input_lineage(self) -> None:
        results = [
            {
                "id": f"rung-{rung}",
                "rung": rung,
                "input_ids": ["joined-run-a", "joined-run-b"],
                "verdict": "supported",
                "proof_eligible": True,
            }
            for rung in range(1, 6)
        ]
        apply_proof_ladder(results)
        self.assertEqual(results[-1]["verdict"], "supported")
        self.assertEqual(results[-1]["proof_ladder"]["missing_prior_rungs"], [])

    def test_invalid_stat_is_rejected_before_data_dependent_paths(self) -> None:
        gate = {
            "id": "typo",
            "scope": "cohort",
            "metric": "runtime.frames_in",
            "stat": "medain",
            "op": ">=",
            "threshold": 1,
            "unit": "frames",
        }
        with self.assertRaises(AnalysisError):
            self.analyze_spec(
                self.base_spec(
                    [{"id": "missing", "kind": "runtime_report", "cohort": "candidate", "path": "missing.json"}],
                    [gate],
                )
            )

    def test_neutral_metric_paired_win_rate_is_typed_error(self) -> None:
        common = {
            "metric": "quality.neutral",
            "value": 1.0,
            "unit": "score",
            "direction": "neutral",
            "cluster_id": "cluster",
            "pair_id": "pair",
            "sample_key": "",
            "comparison_signature": "same",
            "provenance_grade": "legacy_unbound",
        }
        samples = [
            Sample(cohort="baseline", input_id="b", **common),
            Sample(cohort="candidate", input_id="c", **common),
        ]
        records = [
            {"id": "b", "cohort": "baseline", "performance_eligible": True},
            {"id": "c", "cohort": "candidate", "performance_eligible": True},
        ]
        gate = {
            "id": "wins",
            "scope": "comparison",
            "metric": "quality.neutral",
            "stat": "paired_win_rate",
            "op": ">=",
            "threshold": 0.5,
            "unit": "proportion",
            "minimum_provenance": "legacy_unbound",
            "decision": "point",
        }
        with self.assertRaises(AnalysisError):
            evaluate_gates([gate], samples, records, "spec", 200)

    def test_shared_pair_source_collapses_dependency_component(self) -> None:
        samples: list[Sample] = []
        for index in range(3):
            common = {
                "metric": "quality.latency_ms",
                "unit": "ms",
                "direction": "lower_is_better",
                "pair_id": f"pair-{index}",
                "sample_key": "",
                "comparison_signature": "same-controls",
                "provenance_grade": "validator_recomputed",
            }
            samples.extend(
                [
                    Sample(
                        value=100.0,
                        cohort="baseline",
                        input_id=f"baseline-{index}",
                        cluster_id="shared-baseline-run",
                        **common,
                    ),
                    Sample(
                        value=50.0,
                        cohort="candidate",
                        input_id=f"candidate-{index}",
                        cluster_id=f"candidate-run-{index}",
                        **common,
                    ),
                ]
            )
        pairs, refusals, _, _ = pair_metric_samples(
            samples, "quality.latency_ms", "validator_recomputed"
        )
        self.assertFalse(refusals)
        self.assertEqual(len(pairs), 3)
        self.assertEqual(len({pair["cluster_id"] for pair in pairs}), 1)

    def test_pair_identity_uses_tuples_not_delimiter_serialization(self) -> None:
        samples: list[Sample] = []
        for index, (pair_id, sample_key) in enumerate(
            (("a\x1fb", "c"), ("a", "b\x1fc"))
        ):
            common = {
                "metric": "quality.latency_ms",
                "unit": "ms",
                "direction": "lower_is_better",
                "pair_id": pair_id,
                "sample_key": sample_key,
                "comparison_signature": "same-controls",
                "provenance_grade": "validator_recomputed",
            }
            samples.extend(
                [
                    Sample(value=100, cohort="baseline", input_id=f"b{index}", cluster_id=f"b-run-{index}", **common),
                    Sample(value=50, cohort="candidate", input_id=f"c{index}", cluster_id=f"c-run-{index}", **common),
                ]
            )
        pairs, refusals, _, _ = pair_metric_samples(
            samples, "quality.latency_ms", "validator_recomputed"
        )
        self.assertFalse(refusals)
        self.assertEqual(len(pairs), 2)
        self.assertEqual(
            {(pair["pair_id"], pair["sample_key"]) for pair in pairs},
            {("a\x1fb", "c"), ("a", "b\x1fc")},
        )
        self.assertEqual(len({pair["pair_key"] for pair in pairs}), 2)

    def test_finite_leaf_ratios_cannot_overflow_json_output(self) -> None:
        for observation_id in ("huge-a", "huge-b"):
            observation = self.observation(observation_id, 100)
            observation["metrics"].append(
                {
                    "name": "quality.huge_ratio",
                    "value": 1.0,
                    "unit": "proportion",
                    "direction": "higher_is_better",
                    "sample_key": observation_id,
                    "numerator": 1e308,
                    "denominator": 1e308,
                }
            )
            self.write_json(f"{observation_id}.json", observation)
        inputs = [
            {"id": observation_id, "kind": "observation_record", "cohort": "candidate", "path": f"{observation_id}.json"}
            for observation_id in ("huge-a", "huge-b")
        ]
        with self.assertRaises(AnalysisError):
            self.analyze_spec(self.base_spec(inputs, []))

    def test_unacceptable_exit_integrity_gate_rejects(self) -> None:
        report = self.full_report()
        report["exit"] = {"reason": "panic"}
        self.write_json("panic.json", report)
        gate = {
            "id": "exit",
            "scope": "cohort",
            "metric": "runtime.exit_acceptable",
            "stat": "pooled_ratio",
            "op": ">=",
            "threshold": 1,
            "unit": "proportion",
            "decision": "point",
        }
        record = self.analyze_spec(
            self.base_spec(
                [{"id": "panic", "kind": "runtime_report", "cohort": "candidate", "path": "panic.json"}],
                [gate],
            )
        )
        self.assertEqual(record["verdict"], "rejected")
        self.assertEqual(record["gates"][0]["estimate"], 0.0)

    def test_clock_anchor_semantics_are_validated_after_hashing(self) -> None:
        clock_path = self.root / "clock-anchor.json"
        anchor, error = read_json(clock_path)
        self.assertIsNone(error)
        anchor["anchors"][1]["monotonic_ns"] = 500_000_000
        clock_path.write_text(json.dumps(anchor), encoding="utf-8")
        observation = json.loads(json.dumps(self.observation("bad-clock", 100)))
        for provenance in observation["provenance"]:
            if provenance["role"] == "clock_anchor":
                provenance["sha256"] = sha256_file(clock_path)
        self.write_json("bad-clock.json", observation)
        record = self.analyze_spec(
            self.base_spec(
                [{"id": "bad-clock", "kind": "observation_record", "cohort": "candidate", "path": "bad-clock.json"}],
                [],
            )
        )
        self.assertTrue(
            any(row["code"] == "malformed_observation" for row in record["refusals"])
        )
        self.assertEqual(record["input_records"][0]["status"], "invalid")

    def test_clock_anchor_requires_observation_boot_binding(self) -> None:
        observation = self.observation("missing-boot", 100)
        observation["metrics"] = [
            {
                "name": "quality.clock_bound",
                "value": 1,
                "unit": "score",
                "direction": "higher_is_better",
                "sample_key": "missing-boot",
            }
        ]
        observation["attributes"].pop("host_boot_id")
        self.write_json("missing-boot.json", observation)
        record = self.analyze_spec(
            self.base_spec(
                [
                    {
                        "id": "missing-boot",
                        "kind": "observation_record",
                        "cohort": "candidate",
                        "path": "missing-boot.json",
                    }
                ],
                [],
            )
        )
        self.assertTrue(
            any(row["code"] == "malformed_observation" for row in record["refusals"])
        )

    def test_protected_paths_cover_spec_input_and_provenance(self) -> None:
        self.write_json("observation.json", self.observation("protected", 100))
        spec_path = self.root / "spec.json"
        spec = self.base_spec(
            [{"id": "protected", "kind": "observation_record", "cohort": "candidate", "path": "observation.json"}],
            [],
        )
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        record = analyze(spec, spec_path, self.tool)
        protected = protected_evidence_paths(spec, spec_path, record["input_records"])
        self.assertIn(spec_path.resolve(), protected)
        self.assertIn((self.root / "observation.json").resolve(), protected)
        self.assertIn((self.root / "raw-evidence.jsonl").resolve(), protected)

    def test_paired_observations_require_recognized_controls(self) -> None:
        self.write_json("base-observation.json", self.observation("base", 100))
        self.write_json("candidate-observation.json", self.observation("candidate", 110))
        (self.root / "dummy").write_text("same", encoding="utf-8")
        inputs = [
            {"id": "base", "kind": "observation_record", "cohort": "baseline", "pair_id": "pair", "path": "base-observation.json", "controls": [{"role": "audio", "path": "dummy"}]},
            {"id": "candidate", "kind": "observation_record", "cohort": "candidate", "pair_id": "pair", "path": "candidate-observation.json", "controls": [{"role": "audio", "path": "dummy"}]},
        ]
        with self.assertRaises(AnalysisError):
            self.analyze_spec(self.base_spec(inputs, []))

    def test_main_refuses_output_collision_without_overwriting_spec(self) -> None:
        spec_path = self.root / "collision-spec.json"
        spec = self.base_spec(
            [{"id": "missing", "kind": "runtime_report", "cohort": "candidate", "path": "missing.json"}],
            [],
        )
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        original = spec_path.read_bytes()
        with self.assertRaises(AnalysisError):
            main(["--spec", str(spec_path), "--output", str(spec_path)])
        self.assertEqual(spec_path.read_bytes(), original)

    def test_malformed_provenance_cannot_authorize_evidence_overwrite(self) -> None:
        observation = json.loads(json.dumps(self.observation("bad-hash", 100)))
        raw_path = self.root / "raw-evidence.jsonl"
        for provenance in observation["provenance"]:
            if provenance["role"] == "raw_evidence":
                provenance["sha256"] = "0" * 64
        self.write_json("bad-hash.json", observation)
        spec_path = self.root / "bad-hash-spec.json"
        spec = self.base_spec(
            [{"id": "bad-hash", "kind": "observation_record", "cohort": "candidate", "path": "bad-hash.json"}],
            [],
        )
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        original = raw_path.read_bytes()
        with self.assertRaises(AnalysisError):
            main(["--spec", str(spec_path), "--output", str(raw_path)])
        self.assertEqual(raw_path.read_bytes(), original)

    def test_record_is_deterministic(self) -> None:
        self.write_json("run.json", self.full_report())
        spec = self.base_spec(
            [{"id": "run", "kind": "runtime_report", "cohort": "candidate", "path": "run.json"}],
            [],
        )
        self.assertEqual(
            canonical_bytes(self.analyze_spec(spec)), canonical_bytes(self.analyze_spec(spec))
        )


def run_self_test() -> bool:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SelfTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result.wasSuccessful()


class UsageParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(64, f"{self.prog}: error: {message}\n")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = UsageParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--spec", type=Path, help="AnalysisSpec v1 JSON")
    action.add_argument("--self-test", action="store_true", help="run offline planted controls")
    action.add_argument("--example", action="store_true", help="print an example AnalysisSpec v1")
    parser.add_argument("--output", type=Path, help="atomic SystemAnalysisRecord JSON output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.self_test:
        if args.output:
            raise AnalysisError("--output is only valid with --spec")
        return 0 if run_self_test() else 1
    if args.example:
        if args.output:
            raise AnalysisError("--output is only valid with --spec")
        print(json.dumps(example_spec(), indent=2, sort_keys=True))
        return 0
    assert args.spec is not None
    if not args.spec.is_file():
        raise AnalysisError(f"spec does not exist: {args.spec}")
    spec, error = read_json(args.spec)
    if error is not None:
        raise AnalysisError(f"cannot read spec {args.spec}: {error}")
    record = analyze(spec, args.spec)
    if args.output:
        output_path = args.output.resolve()
        if output_path in protected_evidence_paths(spec, args.spec, record["input_records"]):
            raise AnalysisError(
                f"output path collides with the spec, tool, input, control, or provenance evidence: {args.output}"
            )
        atomic_write_json(args.output, record)
        print(f"VERDICT {record['verdict']}  {record['analysis_id']}  {args.output}")
    else:
        print(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return {"supported": 0, "rejected": 1, "not_resolvable": 2}[record["verdict"]]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AnalysisError, OSError) as exc:
        print(f"system-analysis: {exc}", file=sys.stderr)
        raise SystemExit(64)
