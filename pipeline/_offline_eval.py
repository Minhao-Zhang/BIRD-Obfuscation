"""Shared, dependency-free helpers for split-machine offline evaluation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

REQUESTS_NAME = "requests.jsonl"
PRIVATE_GRADING_NAME = "grading_manifest.private.jsonl"
MANIFEST_NAME = "manifest.json"
GENERATIONS_NAME = "generations.jsonl"
OFFLINE_BUNDLE_SCHEMA_VERSION = 1


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_sha256(value: dict) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(encoded)


def read_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON in {path} at line {line_number}: {exc}"
                ) from exc
    return records


def load_manifest(bundle_dir: Path) -> dict:
    path = bundle_dir / MANIFEST_NAME
    if not path.exists():
        raise FileNotFoundError(f"Offline bundle manifest not found: {path}")
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    if manifest.get("offline_bundle_schema_version") != OFFLINE_BUNDLE_SCHEMA_VERSION:
        raise ValueError(
            "Unsupported offline bundle schema version: "
            f"{manifest.get('offline_bundle_schema_version')!r}"
        )
    return manifest


def index_unique(records: list[dict], key: str, source: Path) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for record in records:
        value = record.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Missing {key!r} in {source}")
        if value in indexed:
            raise ValueError(f"Duplicate {key} {value!r} in {source}")
        indexed[value] = record
    return indexed


def verify_public_bundle(bundle_dir: Path) -> tuple[dict, list[dict]]:
    manifest = load_manifest(bundle_dir)
    requests_path = bundle_dir / REQUESTS_NAME
    if not requests_path.exists():
        raise FileNotFoundError(f"Offline requests file not found: {requests_path}")
    actual_sha256 = sha256_file(requests_path)
    if actual_sha256 != manifest.get("requests_sha256"):
        raise ValueError(
            f"{requests_path} hash mismatch: expected "
            f"{manifest.get('requests_sha256')}, got {actual_sha256}"
        )
    requests = read_jsonl(requests_path)
    if len(requests) != manifest.get("request_count"):
        raise ValueError(
            f"Request count mismatch: manifest={manifest.get('request_count')}, "
            f"file={len(requests)}"
        )
    index_unique(requests, "request_id", requests_path)
    for request in requests:
        if request.get("eval_name") != manifest.get("eval_name"):
            raise ValueError(
                f"Request eval_name mismatch for {request.get('request_id')}"
            )
        if request.get("prompt_version") != manifest.get("prompt_version"):
            raise ValueError(
                f"Request prompt_version mismatch for {request.get('request_id')}"
            )
        identity = {
            key: request[key]
            for key in (
                "eval_name",
                "condition",
                "question_id",
                "db_id",
                "prompt_version",
                "instructions",
                "input",
                "schema_sha256",
            )
        }
        expected = canonical_sha256(identity)
        if request.get("request_sha256") != expected:
            raise ValueError(
                f"Request payload hash mismatch for {request['request_id']}"
            )
    return manifest, requests


def verify_private_bundle(
    bundle_dir: Path,
) -> tuple[dict, list[dict], dict[str, dict]]:
    manifest, requests = verify_public_bundle(bundle_dir)
    grading_path = bundle_dir / PRIVATE_GRADING_NAME
    if not grading_path.exists():
        raise FileNotFoundError(
            f"Private grading manifest not found: {grading_path}"
        )
    expected_grading_sha = manifest.get("grading_manifest_sha256")
    if expected_grading_sha:
        actual_grading_sha = sha256_file(grading_path)
        if actual_grading_sha != expected_grading_sha:
            raise ValueError(
                f"{grading_path} hash mismatch: expected "
                f"{expected_grading_sha}, got {actual_grading_sha}"
            )
    grading = read_jsonl(grading_path)
    grading_by_id = index_unique(grading, "request_id", grading_path)
    request_by_id = index_unique(
        requests,
        "request_id",
        bundle_dir / REQUESTS_NAME,
    )
    if set(grading_by_id) != set(request_by_id):
        raise ValueError("Public requests and private grading manifest IDs differ")
    for request_id, private in grading_by_id.items():
        if private.get("request_sha256") != request_by_id[request_id].get(
            "request_sha256"
        ):
            raise ValueError(
                f"Public/private request hash mismatch for {request_id}"
            )
    return manifest, requests, grading_by_id


def eval_metadata_from_manifest(
    manifest: dict,
    *,
    model: str,
    effort: str,
) -> dict:
    """Build result metadata anchored to the prepare-time bundle snapshot."""
    return {
        "result_schema_version": 1,
        "eval_name": manifest["eval_name"],
        "model": model,
        "effort": effort,
        "prompt_version": manifest["prompt_version"],
        "git_commit": manifest.get("git_commit"),
        "git_dirty": manifest.get("git_dirty", False),
        "dataset_files": manifest["dataset_files"],
        "offline_bundle": {
            "schema_version": manifest["offline_bundle_schema_version"],
            "split": manifest.get("split", "test"),
            "requests_sha256": manifest["requests_sha256"],
        },
    }


def default_bundle_dir(eval_name: str, split: str) -> Path:
    suffix = "" if split == "test" else f"-{split}"
    return Path("eval") / "offline" / f"{eval_name}{suffix}"
