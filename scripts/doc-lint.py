#!/usr/bin/env python3
"""Validate EARSHOT's node registry and documentation contracts.

The registry is intentionally the only ownership authority. This check fails
when implementation files fall between nodes, when two nodes claim the same
file, or when a documented contract no longer agrees with its endpoints.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import tomllib
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote


ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
H2_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
SPEC_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(?:P\d+|§[ \t]*\d+(?:\.\d+)*)", re.IGNORECASE)
SPEC_RANGE_RE = re.compile(
    r"§[ \t]*(?P<start>\d+)[ \t]*[–—-][ \t]*§?[ \t]*(?P<end>\d+)"
)
CITATION_RE = re.compile(
    r"(?<![\w-])(?:meta-)?spec\b[ \t`*_(:,;-]*"
    r"(?P<token>P\d+|§[ \t]*\d+(?:\.\d+)*)",
    re.IGNORECASE,
)
INLINE_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*(?P<target><[^>]+>|[^)\s]+)")
REFERENCE_LINK_RE = re.compile(
    r"^[ \t]{0,3}\[[^\]]+\]:[ \t]*(?P<target><[^>]+>|\S+)",
    re.MULTILINE,
)
URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
ALLOWED_STATUSES = {"active", "experimental", "legacy", "deprecated", "planned"}
ALLOWED_DOC_STYLES = {"node", "layer"}
ALLOWED_CONTRACT_KINDS = {"artifact", "event", "capability", "stream"}
ALLOWED_CONTRACT_MATURITIES = {"implemented", "compatibility", "planned"}
ALLOWED_CLAIM_STATES = {"active", "blocked", "complete_pending_commit"}
CURRENT_STATE_DOCS = (
    "docs/STATUS.md",
    "docs/ROADMAP.md",
    "docs/work/ACTIVE.md",
)

# Dial-in digits never enter the repository (CLAUDE.md; docs/DIAL-INS.md:135).
# scripts/hunt-daily.sh already asserts this for the one artifact it commits;
# the registry itself had no such guard and had accumulated seven leaks.
DIAL_IN_REGISTRY = "configs/dial-ins.toml"
# Keys whose values are provenance, not prose: URLs, dates, and probe artifact
# identifiers. Digits are meaningful there and are not dialable on their own.
DIGIT_SAFE_KEYS = {
    "admission_probed",
    "evidence_src",
    "last_verified",
    "number_src",
    "operator_handler",
    "probe_artifact",
    "reconfirmed",
    "settlement_url",
}
# Strip URLs first — bare hosts included, so a Q4 attendee id in
# events.q4inc.com/attendee/915225528 is not mistaken for a passcode. A guard
# that fires on every clean run is a guard everyone learns to bypass.
URLISH_RE = re.compile(r"https?://\S+|\b[\w-]+(?:\.[\w-]+)+/\S+")
# 6..15 digits, not 6..10: an E.164 number is 11 digits (+1 plus ten), so the
# {6,10} bound used elsewhere silently lets the most dialable form of all pass.
PHONEY_RE = re.compile(r"\b\d{3}[-.\s/]\d{3}[-.\s/]\d{4}\b|\b\d{6,15}\b")


class Problems:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, message: str) -> None:
        self.items.append(message)

    def emit(self) -> int:
        if not self.items:
            return 0
        for message in sorted(set(self.items)):
            print(f"doc-lint: ERROR: {message}", file=sys.stderr)
        print(
            f"doc-lint: FAILED ({len(set(self.items))} error(s))",
            file=sys.stderr,
        )
        return 1


def normalized_token(token: str) -> str:
    token = re.sub(r"[ \t]+", "", token)
    return token.upper() if token[:1].upper() == "P" else token


def canonical_spec_anchors(text: str) -> tuple[set[str], set[str]]:
    """Return recovered and explicitly unrecovered canonical SPEC anchors.

    Only an anchor-leading Markdown heading or bold list label declares an
    anchor. Incidental tokens in prose are deliberately ignored so adding
    "P999" to a paragraph cannot self-authorize a citation.
    """
    recovered: set[str] = set()
    unrecovered: set[str] = set()
    for line in text.splitlines():
        heading = re.match(r"^[ \t]{0,3}#{2,6}[ \t]+(?P<label>.+?)\s*$", line)
        bold_item = re.match(
            r"^[ \t]{0,3}[-*+][ \t]+\*\*(?P<label>[^*]+)\*\*",
            line,
        )
        declaration = heading or bold_item
        if declaration is None:
            continue
        label = declaration.group("label").strip()
        if not re.match(r"^(?:P\d+|§[ \t]*\d+)", label, re.IGNORECASE):
            continue

        tokens = {
            normalized_token(match.group(0))
            for match in SPEC_TOKEN_RE.finditer(label)
        }
        for match in SPEC_RANGE_RE.finditer(label):
            start = int(match.group("start"))
            end = int(match.group("end"))
            if start <= end:
                tokens.update(f"§{number}" for number in range(start, end + 1))

        destination = unrecovered if "unrecovered" in label.lower() else recovered
        destination.update(tokens)

    # An explicit unrecovered declaration always wins over another mention.
    recovered.difference_update(unrecovered)
    return recovered, unrecovered


def relative_name(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and value != ""


def string_field(
    record: dict[str, Any],
    field: str,
    label: str,
    problems: Problems,
) -> str | None:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        problems.add(f"{label} requires non-empty string field {field!r}")
        return None
    return value


def string_list_field(
    record: dict[str, Any],
    field: str,
    label: str,
    problems: Problems,
    *,
    nonempty: bool = False,
) -> list[str]:
    value = record.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        problems.add(f"{label} requires a list of non-empty strings in {field!r}")
        return []
    if nonempty and not value:
        problems.add(f"{label} requires at least one entry in {field!r}")
    duplicates = sorted({item for item in value if value.count(item) > 1})
    for duplicate in duplicates:
        problems.add(f"{label} repeats {field} entry {duplicate!r}")
    return value


def load_registry(path: Path, problems: Problems) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        problems.add(f"registry does not exist: {path}")
        return None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        problems.add(f"cannot read registry {path}: {exc}")
        return None
    if data.get("schema_version") != 1:
        problems.add("registry schema_version must be integer 1")
    return data


def validate_records(
    data: dict[str, Any],
    problems: Problems,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    raw_nodes = data.get("node")
    raw_contracts = data.get("contract")
    if not isinstance(raw_nodes, list):
        problems.add("registry must contain one or more [[node]] tables")
        raw_nodes = []
    if not isinstance(raw_contracts, list):
        problems.add("registry must contain one or more [[contract]] tables")
        raw_contracts = []

    nodes: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_nodes, start=1):
        label = f"node[{index}]"
        if not isinstance(raw, dict):
            problems.add(f"{label} must be a table")
            continue
        node_id = string_field(raw, "id", label, problems)
        string_field(raw, "job", label, problems)
        status = string_field(raw, "status", label, problems)
        doc = string_field(raw, "doc", label, problems)
        style = string_field(raw, "doc_style", label, problems)
        string_list_field(raw, "owns", label, problems, nonempty=True)
        string_list_field(raw, "inputs", label, problems)
        string_list_field(raw, "outputs", label, problems)
        if node_id is None:
            continue
        if not ID_RE.fullmatch(node_id):
            problems.add(f"{label} has invalid id {node_id!r}")
        if node_id in nodes:
            problems.add(f"duplicate node id {node_id!r}")
            continue
        if status is not None and status not in ALLOWED_STATUSES:
            problems.add(
                f"node {node_id!r} has unsupported status {status!r}; "
                f"choose from {sorted(ALLOWED_STATUSES)}"
            )
        if style is not None and style not in ALLOWED_DOC_STYLES:
            problems.add(f"node {node_id!r} has unsupported doc_style {style!r}")
        if doc is not None and (not safe_relative(doc) or not doc.endswith(".md")):
            problems.add(f"node {node_id!r} doc must be a relative Markdown path: {doc!r}")
        nodes[node_id] = raw

    contracts: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_contracts, start=1):
        label = f"contract[{index}]"
        if not isinstance(raw, dict):
            problems.add(f"{label} must be a table")
            continue
        contract_id = string_field(raw, "id", label, problems)
        string_field(raw, "producer", label, problems)
        kind = string_field(raw, "kind", label, problems)
        maturity = string_field(raw, "maturity", label, problems)
        string_field(raw, "door", label, problems)
        string_list_field(raw, "consumers", label, problems)
        if contract_id is None:
            continue
        if not ID_RE.fullmatch(contract_id):
            problems.add(f"{label} has invalid id {contract_id!r}")
        if contract_id in contracts:
            problems.add(f"duplicate contract id {contract_id!r}")
            continue
        if kind is not None and kind not in ALLOWED_CONTRACT_KINDS:
            problems.add(
                f"contract {contract_id!r} has unsupported kind {kind!r}; "
                f"choose from {sorted(ALLOWED_CONTRACT_KINDS)}"
            )
        if maturity is not None and maturity not in ALLOWED_CONTRACT_MATURITIES:
            problems.add(
                f"contract {contract_id!r} has unsupported maturity {maturity!r}; "
                f"choose from {sorted(ALLOWED_CONTRACT_MATURITIES)}"
            )
        contracts[contract_id] = raw
    return nodes, contracts


def repository_candidates(root: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return {
            relative_name(root, path)
            for path in root.rglob("*")
            if path.is_file()
        }
    return {
        raw.decode("utf-8", "surrogateescape")
        for raw in result.stdout.split(b"\0")
        if raw and (root / raw.decode("utf-8", "surrogateescape")).is_file()
    }


def governed_files(root: Path, data: dict[str, Any], problems: Problems) -> set[str]:
    lint = data.get("lint")
    ownership_policy = lint.get("ownership") if isinstance(lint, dict) else None
    if not isinstance(ownership_policy, dict):
        problems.add("registry requires a [lint.ownership] table")
        return set()
    include_globs = string_list_field(
        ownership_policy,
        "governed_globs",
        "[lint.ownership]",
        problems,
        nonempty=True,
    )
    exclude_globs = string_list_field(
        ownership_policy,
        "excluded_globs",
        "[lint.ownership]",
        problems,
        nonempty=True,
    )
    reincluded = string_list_field(
        ownership_policy,
        "reincluded_paths",
        "[lint.ownership]",
        problems,
    )
    root_files = ownership_policy.get("govern_root_files")
    if not isinstance(root_files, bool):
        problems.add("[lint.ownership] requires boolean govern_root_files")
        root_files = False

    candidates = repository_candidates(root)
    included: set[str] = set()
    for pattern in include_globs:
        if not safe_relative(pattern):
            problems.add(f"[lint.ownership] has unsafe governed glob {pattern!r}")
            continue
        try:
            included.update(
                relative_name(root, path)
                for path in root.glob(pattern)
                if path.is_file() and relative_name(root, path) in candidates
            )
        except (OSError, ValueError) as exc:
            problems.add(f"[lint.ownership] has invalid governed glob {pattern!r}: {exc}")
    if root_files:
        included.update(path for path in candidates if len(PurePosixPath(path).parts) == 1)

    excluded: set[str] = set()
    for pattern in exclude_globs:
        if not safe_relative(pattern):
            problems.add(f"[lint.ownership] has unsafe excluded glob {pattern!r}")
            continue
        try:
            excluded.update(
                relative_name(root, path)
                for path in root.glob(pattern)
                if path.is_file()
            )
        except (OSError, ValueError) as exc:
            problems.add(f"[lint.ownership] has invalid excluded glob {pattern!r}: {exc}")

    governed = included - excluded
    for rel in reincluded:
        if not safe_relative(rel):
            problems.add(f"[lint.ownership] has unsafe reincluded path {rel!r}")
        elif rel not in candidates or not (root / rel).is_file():
            problems.add(f"[lint.ownership] reincluded path does not exist in first-party candidates: {rel}")
        else:
            governed.add(rel)
    return governed


def validate_paths(
    root: Path,
    data: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    problems: Problems,
) -> tuple[int, int, set[str], dict[str, str]]:
    docs: dict[str, list[str]] = defaultdict(list)
    ownership: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for node_id, node in nodes.items():
        doc = node.get("doc")
        if isinstance(doc, str) and safe_relative(doc):
            docs[doc].append(node_id)
            if not (root / doc).is_file():
                problems.add(f"node {node_id!r} doc does not exist: {doc}")
            else:
                ownership[doc].append((node_id, "contract doc"))

        owns = node.get("owns")
        if not isinstance(owns, list):
            continue
        for pattern in owns:
            if not isinstance(pattern, str):
                continue
            if not safe_relative(pattern):
                problems.add(f"node {node_id!r} owns unsafe or absolute path glob {pattern!r}")
                continue
            try:
                matches = sorted(path for path in root.glob(pattern) if path.is_file())
            except (OSError, ValueError) as exc:
                problems.add(f"node {node_id!r} owns invalid path glob {pattern!r}: {exc}")
                continue
            if not matches:
                problems.add(f"node {node_id!r} owns path glob with no files: {pattern}")
            for match in matches:
                rel = relative_name(root, match)
                ownership[rel].append((node_id, pattern))

    for doc, node_ids in docs.items():
        if len(node_ids) > 1:
            problems.add(f"contract doc {doc} is shared by nodes {sorted(node_ids)}")

    for path, claims in ownership.items():
        if len(claims) > 1:
            rendered = ", ".join(f"{node} ({pattern})" for node, pattern in claims)
            problems.add(f"owned path {path} has multiple claims: {rendered}")

    governed = governed_files(root, data, problems)
    for path in sorted(ownership):
        if path not in governed:
            problems.add(f"node ownership claim falls outside the governed first-party universe: {path}")
    for path in sorted(governed):
        claims = ownership.get(path, [])
        if not claims:
            problems.add(f"governed first-party file has no owning node: {path}")
        elif len(claims) != 1:
            problems.add(f"governed first-party file must have exactly one owning node: {path}")
    resolved_owners = {
        path: claims[0][0]
        for path, claims in ownership.items()
        if path in governed and len(claims) == 1
    }
    return len(docs), len(governed), governed, resolved_owners


def load_claims(path: Path, problems: Problems) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        problems.add(f"active-claim authority does not exist: {path}")
        return None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        problems.add(f"cannot read active-claim authority {path}: {exc}")
        return None
    if data.get("schema_version") != 1:
        problems.add("claims schema_version must be integer 1")
    return data


def repository_dirty_paths(root: Path, problems: Problems) -> set[str]:
    try:
        result = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        problems.add(f"cannot derive dirty-path claims from git status: {exc}")
        return set()

    dirty: set[str] = set()
    records = result.stdout.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            problems.add(f"cannot parse git porcelain record: {record!r}")
            continue
        status = record[:2].decode("ascii", "replace")
        path = record[3:].decode("utf-8", "surrogateescape")
        dirty.add(path)
        if "R" in status or "C" in status:
            # Porcelain -z emits the source path as the following NUL record.
            if index < len(records) and records[index]:
                source = records[index].decode("utf-8", "surrogateescape")
                index += 1
                dirty.add(source)
    return dirty


def validate_claims(
    root: Path,
    data: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    governed: set[str],
    owners: dict[str, str],
    problems: Problems,
    *,
    dirty_paths: set[str] | None = None,
) -> int:
    raw_claims = data.get("claim")
    if not isinstance(raw_claims, list) or not raw_claims:
        problems.add("claims authority must contain one or more [[claim]] tables")
        return 0

    claim_ids: set[str] = set()
    node_claims: dict[str, list[str]] = defaultdict(list)
    path_claims: dict[str, list[str]] = defaultdict(list)
    for index, raw in enumerate(raw_claims, start=1):
        label = f"claim[{index}]"
        if not isinstance(raw, dict):
            problems.add(f"{label} must be a table")
            continue
        claim_id = string_field(raw, "id", label, problems)
        node_id = string_field(raw, "node", label, problems)
        string_field(raw, "worker", label, problems)
        string_field(raw, "branch", label, problems)
        state = string_field(raw, "state", label, problems)
        paths = string_list_field(raw, "paths", label, problems, nonempty=True)
        verification = string_list_field(
            raw,
            "verification",
            label,
            problems,
            nonempty=True,
        )
        string_list_field(raw, "evidence", label, problems, nonempty=True)
        contract_change = raw.get("contract_change")
        if not isinstance(contract_change, bool):
            problems.add(f"{label} requires boolean contract_change")
            contract_change = False

        if claim_id is not None:
            if not ID_RE.fullmatch(claim_id):
                problems.add(f"{label} has invalid id {claim_id!r}")
            if claim_id in claim_ids:
                problems.add(f"duplicate claim id {claim_id!r}")
            claim_ids.add(claim_id)
        claim_label = claim_id or label

        if state is not None and state not in ALLOWED_CLAIM_STATES:
            problems.add(
                f"{claim_label} has unsupported state {state!r}; "
                f"choose from {sorted(ALLOWED_CLAIM_STATES)}"
            )
        if node_id is not None:
            if node_id not in nodes:
                problems.add(f"{claim_label} references unknown node {node_id!r}")
            node_claims[node_id].append(claim_label)

        for rel in paths:
            normalized = PurePosixPath(rel).as_posix()
            if (
                not safe_relative(rel)
                or normalized != rel
                or any(character in rel for character in "*?[")
            ):
                problems.add(
                    f"{claim_label} path must be an exact normalized repository file: {rel!r}"
                )
                continue
            path = root / rel
            if not path.is_file():
                problems.add(f"{claim_label} claims a missing or non-file path: {rel}")
                continue
            if rel not in governed:
                problems.add(f"{claim_label} claims a file outside the governed universe: {rel}")
                continue
            owner = owners.get(rel)
            if node_id in nodes and owner != node_id:
                problems.add(
                    f"{claim_label} claims {rel}, owned by {owner!r}, "
                    f"but the claim node is {node_id!r}"
                )
            path_claims[rel].append(claim_label)

        if contract_change and node_id in nodes:
            contract_doc = nodes[node_id].get("doc")
            if isinstance(contract_doc, str) and contract_doc not in paths:
                problems.add(
                    f"{claim_label} changes a contract but does not claim its "
                    f"node contract doc: {contract_doc}"
                )
            if not verification:
                problems.add(
                    f"{claim_label} changes a contract but has no verification target"
                )

    for node_id, labels in sorted(node_claims.items()):
        if len(labels) > 1:
            problems.add(
                f"node {node_id!r} has multiple open claims: {', '.join(sorted(labels))}"
            )
    for rel, labels in sorted(path_claims.items()):
        if len(labels) > 1:
            problems.add(
                f"open path {rel} has duplicate claims: {', '.join(sorted(labels))}"
            )

    dirty = repository_dirty_paths(root, problems) if dirty_paths is None else dirty_paths
    for rel in sorted(dirty & governed):
        labels = path_claims.get(rel, [])
        if not labels:
            problems.add(f"dirty governed path has no open claim: {rel}")
        elif len(labels) != 1:
            problems.add(f"dirty governed path must have exactly one open claim: {rel}")
    return len(raw_claims)


def validate_contracts(
    nodes: dict[str, dict[str, Any]],
    contracts: dict[str, dict[str, Any]],
    problems: Problems,
) -> None:
    for node_id, node in nodes.items():
        for direction in ("inputs", "outputs"):
            refs = node.get(direction)
            if not isinstance(refs, list):
                continue
            for contract_id in refs:
                if isinstance(contract_id, str) and contract_id not in contracts:
                    problems.add(
                        f"node {node_id!r} {direction} references unknown contract {contract_id!r}"
                    )

    for contract_id, contract in contracts.items():
        producer = contract.get("producer")
        consumers = contract.get("consumers")
        if isinstance(producer, str):
            if producer not in nodes:
                problems.add(
                    f"contract {contract_id!r} references unknown producer node {producer!r}"
                )
            elif contract_id not in nodes[producer].get("outputs", []):
                problems.add(
                    f"contract {contract_id!r} producer {producer!r} does not list it in outputs"
                )
        if not isinstance(consumers, list):
            continue
        for consumer in consumers:
            if not isinstance(consumer, str):
                continue
            if consumer not in nodes:
                problems.add(
                    f"contract {contract_id!r} references unknown consumer node {consumer!r}"
                )
            elif contract_id not in nodes[consumer].get("inputs", []):
                problems.add(
                    f"contract {contract_id!r} consumer {consumer!r} does not list it in inputs"
                )

    for node_id, node in nodes.items():
        for contract_id in node.get("outputs", []):
            contract = contracts.get(contract_id)
            if contract is not None and contract.get("producer") != node_id:
                problems.add(
                    f"node {node_id!r} outputs {contract_id!r}, but its producer is "
                    f"{contract.get('producer')!r}"
                )
        for contract_id in node.get("inputs", []):
            contract = contracts.get(contract_id)
            if contract is not None and node_id not in contract.get("consumers", []):
                problems.add(
                    f"node {node_id!r} inputs {contract_id!r}, but is absent from its consumers"
                )


def validate_dependency_cycles(
    nodes: dict[str, dict[str, Any]],
    contracts: dict[str, dict[str, Any]],
    problems: Problems,
) -> int:
    """Reject cycles in the producer-to-consumer node dependency graph."""
    graph: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    edge_contracts: dict[tuple[str, str], set[str]] = defaultdict(set)
    for contract_id in sorted(contracts):
        contract = contracts[contract_id]
        producer = contract.get("producer")
        consumers = contract.get("consumers")
        if not isinstance(producer, str) or producer not in nodes or not isinstance(consumers, list):
            continue
        for consumer in consumers:
            if not isinstance(consumer, str) or consumer not in nodes:
                continue
            graph[producer].add(consumer)
            edge_contracts[(producer, consumer)].add(contract_id)

    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node_id: str) -> None:
        nonlocal index
        indices[node_id] = index
        lowlinks[node_id] = index
        index += 1
        stack.append(node_id)
        on_stack.add(node_id)

        for consumer in sorted(graph[node_id]):
            if consumer not in indices:
                visit(consumer)
                lowlinks[node_id] = min(lowlinks[node_id], lowlinks[consumer])
            elif consumer in on_stack:
                lowlinks[node_id] = min(lowlinks[node_id], indices[consumer])

        if lowlinks[node_id] != indices[node_id]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node_id:
                break
        components.append(tuple(sorted(component)))

    for node_id in sorted(nodes):
        if node_id not in indices:
            visit(node_id)

    cyclic = sorted(
        component
        for component in components
        if len(component) > 1 or component[0] in graph[component[0]]
    )
    for component in cyclic:
        members = set(component)
        internal_edges: list[str] = []
        for producer in component:
            for consumer in sorted(graph[producer] & members):
                labels = ",".join(sorted(edge_contracts[(producer, consumer)]))
                internal_edges.append(f"{producer} -[{labels}]-> {consumer}")
        problems.add(
            "dependency cycle (strongly connected component): "
            f"nodes=[{', '.join(component)}]; edges=[{'; '.join(internal_edges)}]"
        )
    return len(cyclic)


def configured_headings(
    data: dict[str, Any],
    key: str,
    problems: Problems,
) -> list[str]:
    lint = data.get("lint")
    if not isinstance(lint, dict):
        problems.add("registry requires a [lint] table")
        return []
    return string_list_field(lint, key, "[lint]", problems, nonempty=True)


def validate_headings(
    root: Path,
    data: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    problems: Problems,
) -> None:
    requirements = {
        "node": configured_headings(data, "node_required_headings", problems),
        "layer": configured_headings(data, "layer_required_headings", problems),
    }
    for node_id, node in nodes.items():
        doc = node.get("doc")
        style = node.get("doc_style")
        if not isinstance(doc, str) or not isinstance(style, str):
            continue
        path = root / doc
        if not path.is_file() or style not in requirements:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            problems.add(f"cannot read node {node_id!r} doc {doc}: {exc}")
            continue
        headings = [
            re.sub(r"[ \t]+#+[ \t]*$", "", match.group(1)).strip()
            for match in H2_RE.finditer(text)
        ]
        for required in requirements[style]:
            if style == "node":
                present = required in headings
            else:
                present = any(
                    heading == required
                    or heading.startswith(required + " ")
                    or heading.startswith(required + ":")
                    or heading.startswith(required + " (")
                    for heading in headings
                )
            if not present:
                problems.add(
                    f"node {node_id!r} doc {doc} is missing H2 heading {required!r} "
                    f"for doc_style={style}"
                )


def citation_files(
    root: Path,
    data: dict[str, Any],
    problems: Problems,
) -> list[Path]:
    lint = data.get("lint")
    if not isinstance(lint, dict):
        return []
    globs = string_list_field(lint, "citation_globs", "[lint]", problems, nonempty=True)
    files: set[Path] = set()
    for pattern in globs:
        if not safe_relative(pattern):
            problems.add(f"[lint] has unsafe citation glob {pattern!r}")
            continue
        try:
            files.update(path for path in root.glob(pattern) if path.is_file())
        except (OSError, ValueError) as exc:
            problems.add(f"[lint] has invalid citation glob {pattern!r}: {exc}")
    return sorted(files)


def validate_spec_citations(
    root: Path,
    data: dict[str, Any],
    problems: Problems,
) -> int:
    spec = root / "docs" / "SPEC.md"
    if not spec.is_file():
        problems.add("spec citation authority does not exist: docs/SPEC.md")
        return 0
    try:
        spec_text = spec.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        problems.add(f"cannot read docs/SPEC.md: {exc}")
        return 0
    recovered, unrecovered = canonical_spec_anchors(spec_text)
    citations = 0
    for path in citation_files(root, data, problems):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            problems.add(f"cannot scan citations in {relative_name(root, path)}: {exc}")
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in CITATION_RE.finditer(line):
                citations += 1
                token = normalized_token(match.group("token"))
                if token in unrecovered:
                    problems.add(
                        f"{relative_name(root, path)}:{line_number} cites spec {token}, "
                        "but docs/SPEC.md declares that anchor unrecovered"
                    )
                elif token not in recovered:
                    problems.add(
                        f"{relative_name(root, path)}:{line_number} cites spec {token}, "
                        "but docs/SPEC.md has no recovered canonical anchor"
                    )
    return citations


def repository_markdown_files(root: Path) -> list[Path]:
    excluded = {".git", "target", "vendor"}
    files: list[Path] = []
    for path in root.rglob("*.md"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in excluded or part.startswith(".venv") for part in rel.parts):
            continue
        files.append(path)
    return sorted(files)


def local_link_path(root: Path, source: Path, raw_target: str) -> Path | None:
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    if not target or target.startswith("#") or target.startswith("//"):
        return None
    if URI_SCHEME_RE.match(target):
        return None
    target = unquote(target.split("#", 1)[0].split("?", 1)[0])
    if not target:
        return None
    if target.startswith("/"):
        return root / target.lstrip("/")
    return source.parent / target


def require_as_of_frontmatter(root: Path, rel: str, problems: Problems) -> None:
    path = root / rel
    if not path.is_file():
        problems.add(f"canonical current-state doc does not exist: {rel}")
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        problems.add(f"cannot read canonical current-state doc {rel}: {exc}")
        return
    if not lines or lines[0].strip() != "---":
        problems.add(f"canonical current-state doc {rel} requires YAML frontmatter")
        return
    try:
        closing = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        problems.add(f"canonical current-state doc {rel} has unclosed YAML frontmatter")
        return
    as_of = next(
        (
            match.group(1).strip()
            for line in lines[1:closing]
            if (match := re.match(r"^as_of[ \t]*:[ \t]*(.+)$", line))
        ),
        "",
    )
    if not as_of:
        problems.add(f"canonical current-state doc {rel} requires non-empty as_of frontmatter")


def validate_canonical_docs(root: Path, problems: Problems) -> tuple[int, int]:
    for rel in CURRENT_STATE_DOCS:
        require_as_of_frontmatter(root, rel, problems)

    markdown_files = repository_markdown_files(root)
    links_checked = 0
    for source in markdown_files:
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            problems.add(f"cannot scan Markdown links in {relative_name(root, source)}: {exc}")
            continue
        matches = list(INLINE_LINK_RE.finditer(text)) + list(REFERENCE_LINK_RE.finditer(text))
        for match in sorted(matches, key=lambda item: item.start()):
            raw_target = match.group("target")
            destination = local_link_path(root, source, raw_target)
            if destination is None:
                continue
            links_checked += 1
            if not destination.exists():
                line_number = text.count("\n", 0, match.start()) + 1
                problems.add(
                    f"{relative_name(root, source)}:{line_number} has broken local Markdown "
                    f"target {raw_target!r}"
                )
    return len(markdown_files), links_checked


def scan_digit_leaks(value: Any, key: str | None, trail: str, found: list[str]) -> None:
    """Collect dialable-looking digit runs from a parsed registry subtree."""
    if isinstance(value, dict):
        for child_key, child in value.items():
            scan_digit_leaks(child, child_key, f"{trail}.{child_key}" if trail else child_key, found)
    elif isinstance(value, list):
        for child in value:
            scan_digit_leaks(child, key, trail, found)
    elif isinstance(value, str) and key not in DIGIT_SAFE_KEYS:
        for match in PHONEY_RE.finditer(URLISH_RE.sub(" ", value)):
            found.append(f"{trail}: {match.group(0)}")


def validate_dial_in_digits(root: Path, problems: Problems) -> int:
    """Fail when a phone number or passcode reaches the dial-in registry.

    The registry records *that* a venue publishes a way in and *where* that was
    read from. The digits themselves rotate per call and stay in the gitignored
    state/hunt/ artifacts, so a leak here is both a policy break and a value
    that goes stale silently.
    """
    path = root / DIAL_IN_REGISTRY
    if not path.is_file():
        return 0
    try:
        with path.open("rb") as handle:
            registry = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        problems.add(f"cannot read {DIAL_IN_REGISTRY} for the digit-leak check: {exc}")
        return 0
    found: list[str] = []
    scan_digit_leaks(registry, None, "", found)
    for leak in sorted(set(found)):
        problems.add(
            f"{DIAL_IN_REGISTRY} carries a dialable digit string in prose "
            f"({leak}) — record the provenance in number_src/evidence_src instead"
        )
    return len(found)


def validate_operations_surface(root: Path, problems: Problems) -> None:
    """Keep event operations small enough to remain a procedure, not folklore."""
    checker = root / "scripts" / "ops-surface-check.py"
    manifest = root / "architecture" / "operations.toml"
    if not checker.is_file() or not manifest.is_file():
        # validate_paths already reports the exact missing governed path.
        return
    try:
        result = subprocess.run(
            [sys.executable, str(checker), "--repo", str(root)],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        problems.add(f"operations surface check could not run: {exc}")
        return
    if result.returncode == 0:
        return
    lines = (result.stderr or result.stdout).splitlines()
    detail = next((line for line in lines if "ERROR:" in line), "checker failed")
    problems.add(f"operations surface is invalid: {detail}")


def parse_args() -> argparse.Namespace:
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=default_root,
        help="repository root (default: inferred from this script)",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("architecture/nodes.toml"),
        help="registry path, relative to --root unless absolute",
    )
    parser.add_argument(
        "--claims",
        type=Path,
        default=Path("architecture/claims.toml"),
        help="active-claim path, relative to --root unless absolute",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run deterministic synthetic parser/graph checks and exit",
    )
    return parser.parse_args()


def run_self_tests() -> None:
    synthetic_spec = """\
## Principles
### P1 — recovered principle
### P2 — unrecovered
An ordinary paragraph mentions P999 and §77.
## Notes mentioning P998
## §1–§2 — reserved [unrecovered]
## §3 — recovered section
- **§3.1 — recovered subsection.** Details.
"""
    recovered, unrecovered = canonical_spec_anchors(synthetic_spec)
    assert recovered == {"P1", "§3", "§3.1"}, recovered
    assert unrecovered == {"P2", "§1", "§2"}, unrecovered
    assert "P999" not in recovered | unrecovered
    assert "P998" not in recovered | unrecovered
    assert "§77" not in recovered | unrecovered

    # Digit-leak scanner: real leaks caught, provenance and identifiers spared.
    leaks: list[str] = []
    scan_digit_leaks(
        {
            "LEAK": {
                "evidence": "dial 1-888-317-6003 code 3937444",
                "admission_detail": "held 4572s; +18002160480 rang 55s",
            },
            "CLEAN": {
                "evidence": "webcast at events.q4inc.com/attendee/915225528, 2 s segments",
                "number_src": "https://example.com/ir/888-317-6003",
                "probe_artifact": "CallSid CAdf6b601752f28876b7db0c3536773e83",
                "admission_probed": "2026-07-30",
                "admission_detail": "held 4572s (76min) with 4 detections until 03:40 UTC",
            },
        },
        None,
        "",
        leaks,
    )
    assert sorted(leaks) == [
        "LEAK.admission_detail: 18002160480",
        "LEAK.evidence: 3937444",
        "LEAK.evidence: 888-317-6003",
    ], leaks

    nodes = {"alpha": {}, "beta": {}, "gamma": {}}
    contracts = {
        "alpha.to_beta": {"producer": "alpha", "consumers": ["beta"]},
        "beta.to_alpha": {"producer": "beta", "consumers": ["alpha"]},
        "gamma.done": {"producer": "gamma", "consumers": []},
    }
    first = Problems()
    second = Problems()
    assert validate_dependency_cycles(nodes, contracts, first) == 1
    assert validate_dependency_cycles(nodes, contracts, second) == 1
    expected = (
        "dependency cycle (strongly connected component): nodes=[alpha, beta]; "
        "edges=[alpha -[alpha.to_beta]-> beta; beta -[beta.to_alpha]-> alpha]"
    )
    assert first.items == [expected], first.items
    assert second.items == first.items, (first.items, second.items)

    with tempfile.TemporaryDirectory(prefix="earshot-doc-lint-") as temp_dir:
        root = Path(temp_dir)
        (root / "docs").mkdir()
        (root / "docs" / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
        (root / "alpha.txt").write_text("implementation\n", encoding="utf-8")
        nodes = {
            "alpha": {
                "doc": "docs/alpha.md",
            }
        }
        governed = {"docs/alpha.md", "alpha.txt"}
        owners = {"docs/alpha.md": "alpha", "alpha.txt": "alpha"}
        claims = {
            "claim": [
                {
                    "id": "alpha.work",
                    "node": "alpha",
                    "worker": "synthetic-lane",
                    "branch": "synthetic-branch",
                    "state": "active",
                    "contract_change": True,
                    "paths": ["alpha.txt", "docs/alpha.md"],
                    "verification": ["synthetic-test"],
                    "evidence": ["synthetic-report"],
                }
            ]
        }
        claim_problems = Problems()
        assert validate_claims(
            root,
            claims,
            nodes,
            governed,
            owners,
            claim_problems,
            dirty_paths=governed,
        ) == 1
        assert claim_problems.items == [], claim_problems.items

        invalid_claims = {
            "claim": [
                {
                    "id": "alpha.invalid",
                    "node": "alpha",
                    "worker": "synthetic-lane",
                    "branch": "synthetic-branch",
                    "state": "active",
                    "contract_change": True,
                    "paths": ["alpha*.txt"],
                    "verification": ["synthetic-test"],
                    "evidence": ["synthetic-report"],
                }
            ]
        }
        invalid_problems = Problems()
        validate_claims(
            root,
            invalid_claims,
            nodes,
            governed,
            owners,
            invalid_problems,
            dirty_paths=governed,
        )
        assert any(
            "exact normalized repository file" in message
            for message in invalid_problems.items
        ), invalid_problems.items
        assert any(
            "does not claim its node contract doc" in message
            for message in invalid_problems.items
        ), invalid_problems.items
        assert any(
            "dirty governed path has no open claim" in message
            for message in invalid_problems.items
        ), invalid_problems.items


def main() -> int:
    args = parse_args()
    try:
        run_self_tests()
    except AssertionError as exc:
        print(f"doc-lint: SELF-TEST FAILED: {exc}", file=sys.stderr)
        return 1
    if args.self_test:
        print("doc-lint: self-test OK")
        return 0

    root = args.root.resolve()
    registry = args.registry if args.registry.is_absolute() else root / args.registry
    claims_path = args.claims if args.claims.is_absolute() else root / args.claims
    problems = Problems()
    data = load_registry(registry, problems)
    if data is None:
        return problems.emit()
    claims_data = load_claims(claims_path, problems)
    if claims_data is None:
        return problems.emit()

    nodes, contracts = validate_records(data, problems)
    doc_count, governed_count, governed, owners = validate_paths(
        root,
        data,
        nodes,
        problems,
    )
    claim_count = validate_claims(
        root,
        claims_data,
        nodes,
        governed,
        owners,
        problems,
    )
    validate_contracts(nodes, contracts, problems)
    cycle_count = validate_dependency_cycles(nodes, contracts, problems)
    validate_headings(root, data, nodes, problems)
    citation_count = validate_spec_citations(root, data, problems)
    validate_dial_in_digits(root, problems)
    validate_operations_surface(root, problems)
    markdown_count, link_count = validate_canonical_docs(root, problems)
    result = problems.emit()
    if result == 0:
        print(
            "doc-lint: OK "
            f"({len(nodes)} nodes, {len(contracts)} contracts, {claim_count} open claims, "
            f"{governed_count} governed files, {doc_count} docs, "
            f"{cycle_count} dependency cycles, {citation_count} spec citations, "
            f"{link_count} local links across "
            f"{markdown_count} Markdown files)"
        )
    return result


if __name__ == "__main__":
    sys.exit(main())
