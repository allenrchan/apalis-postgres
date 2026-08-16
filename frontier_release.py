#!/usr/bin/env python3
"""Render and validate the Frontier CT-11 fork release identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "frontier-release.json"
UPSTREAM_BASE = "b6983a1822abb61937e313d0f0013c15662d1f93"
PREVIOUS_FRONTIER_REVISION = "00bc2d691ab0eefe7df1cca44cf7d6aaaba0c68b"
RUNTIME_ROOTS = (
    ".sqlx",
    "Cargo.lock",
    "Cargo.toml",
    "build.rs",
    "deny.toml",
    "migrations",
    "queries",
    "sqlx.toml",
    "src",
    "supply-chain",
)
CARRIED_PATCH_FILES = (
    ".github/workflows/ci.yml",
    "CHANGELOG.md",
    "examples/basic.rs",
    "examples/unique_jobs.rs",
)
CARRIED_PATCH_COMMITS = (
    {
        "revision": "5238bfacb64998290b12a114052f878748caf805",
        "purpose": "Test mutually exclusive chrono and time SQLx feature configurations independently.",
    },
    {
        "revision": PREVIOUS_FRONTIER_REVISION,
        "purpose": "Record the verified SQLx feature matrix in the changelog.",
    },
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def files_under(roots: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for relative in roots:
        path = ROOT / relative
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(candidate for candidate in path.rglob("*") if candidate.is_file())
    return sorted(files)


def content_group(roots: tuple[str, ...]) -> dict[str, Any]:
    files = files_under(roots)
    entries = {
        str(path.relative_to(ROOT)): sha256(path.read_bytes())
        for path in files
    }
    return {
        "fileCount": len(entries),
        "sha256": canonical_sha(entries),
    }


def package_version() -> str:
    manifest = tomllib.loads((ROOT / "Cargo.toml").read_text())
    return str(manifest["package"]["version"])


def release_policy() -> dict[str, Any]:
    return {
        "owner": "Frontier Jobs dependency owner",
        "kind": "third-party-infrastructure-fork",
        "updateCadence": "review upstream main and security advisories at least monthly and before every pin change",
        "securityResponse": "classify exposure immediately; pin away from a compromised revision or publish a reviewed fork repair under the incident SLA",
        "rebaseRule": "rebase only onto a reviewed immutable upstream revision and re-run producer plus Frontier consumer acceptance",
        "retirementTrigger": "upstream publishes the SQLx 0.9 adapter and the complete Frontier queue migration/storage/worker acceptance passes",
        "runtimeExpansionProhibited": True,
    }


def build_manifest() -> dict[str, Any]:
    runtime = content_group(RUNTIME_ROOTS)
    tooling = content_group(("frontier_release.py",))
    policy = release_policy()
    identity = canonical_sha(
        {
            "runtime": runtime["sha256"],
            "tooling": tooling["sha256"],
            "policy": policy,
            "upstreamBase": UPSTREAM_BASE,
            "carriedPatches": CARRIED_PATCH_COMMITS,
        }
    )
    return {
        "schemaVersion": 1,
        "contractId": "CT-11",
        "status": "governed-retain",
        "releaseId": f"ct11-{identity[:16]}",
        "package": {
            "name": "apalis-postgres",
            "version": package_version(),
            "registryPublishAllowed": False,
            "distribution": "immutable-git-revision-only",
        },
        "upstream": {
            "repository": "https://github.com/apalis-dev/apalis-postgres.git",
            "baseRevision": UPSTREAM_BASE,
            "runtimeContentSha256": runtime["sha256"],
            "runtimeEquivalent": True,
        },
        "fork": {
            "repository": "https://github.com/allenrchan/apalis-postgres.git",
            "previousFrontierRevision": PREVIOUS_FRONTIER_REVISION,
            "carriedPatchCommits": list(CARRIED_PATCH_COMMITS),
            "carriedPatchFiles": list(CARRIED_PATCH_FILES),
            "carriedRuntimePatch": False,
        },
        "content": {
            "runtime": runtime,
            "releaseTooling": tooling,
        },
        "policy": policy,
        "producerAcceptance": {
            "mergedPullRequest": "https://github.com/allenrchan/apalis-postgres/pull/1",
            "required": [
                "stable, beta, and pinned nightly test suites",
                "default chrono and no-default time feature matrices",
                "Rustfmt, Clippy, documentation, coverage, and unused-dependency checks",
                "cargo deny, cargo audit, and cargo vet security review before a changed runtime pin",
            ],
        },
        "frontierAcceptance": {
            "owner": "Frontier Jobs dependency owner",
            "required": [
                "just it-target frontier-jobs job_queue_storage_integration_test",
                "just it-target frontier-jobs job_queue_integration_test",
                "just it-target frontier-jobs worker_task_integration_test",
            ],
        },
        "rollback": {
            "previousKnownGoodRevision": PREVIOUS_FRONTIER_REVISION,
            "partialRollbackForbidden": True,
            "migrationRule": "forward repair is required after an incompatible queue-schema or migration-ledger change",
        },
    }


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=False
    )


def validation_errors(manifest: dict[str, Any] | None = None) -> list[str]:
    errors: list[str] = []
    if manifest is None:
        if not MANIFEST.is_file():
            return ["frontier-release.json is missing"]
        manifest = json.loads(MANIFEST.read_text())
    expected = build_manifest()
    if manifest != expected:
        errors.append("frontier-release.json does not match current governed content")

    if manifest.get("package", {}).get("registryPublishAllowed") is not False:
        errors.append("the Frontier fork must not claim a registry release")
    if manifest.get("fork", {}).get("carriedRuntimePatch") is not False:
        errors.append("CT-11 must not claim a hidden carried runtime patch")

    ancestor = git("merge-base", "--is-ancestor", UPSTREAM_BASE, "HEAD")
    if ancestor.returncode != 0:
        errors.append("the declared upstream base is not an ancestor of HEAD")
    runtime_diff = git("diff", "--quiet", UPSTREAM_BASE, "--", *RUNTIME_ROOTS)
    if runtime_diff.returncode != 0:
        errors.append("fork runtime content differs from the declared upstream base")
    changed = git(
        "diff", "--name-only", f"{UPSTREAM_BASE}..{PREVIOUS_FRONTIER_REVISION}"
    )
    if changed.returncode != 0:
        errors.append("cannot resolve the carried patch range")
    elif tuple(filter(None, changed.stdout.splitlines())) != CARRIED_PATCH_FILES:
        errors.append("carried patch files differ from the reviewed manifest")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("render", "validate"))
    args = parser.parse_args()
    if args.command == "render":
        MANIFEST.write_text(json.dumps(build_manifest(), indent=2, sort_keys=True) + "\n")
        print(f"Rendered {MANIFEST.name}")
        return 0
    errors = validation_errors()
    if errors:
        for error in errors:
            print(f"CT-11 release error: {error}")
        return 1
    print("CT-11 Frontier fork release passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
