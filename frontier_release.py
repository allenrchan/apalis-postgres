#!/usr/bin/env python3
"""Render and validate the Frontier CT-11 fork release identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import subprocess
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "frontier-release.json"
UPSTREAM_BASE = "b6983a1822abb61937e313d0f0013c15662d1f93"
PREVIOUS_FRONTIER_REVISION = "00bc2d691ab0eefe7df1cca44cf7d6aaaba0c68b"
RUNTIME_PATCH_BASE = "1f28de216b7da33f1389a941c0e08b17c76c5cbd"
RUNTIME_PATCH_KIND = "process-local-consumer-lifecycle-observation"
RUNTIME_PATCH_FILES = ("src/consumer_event.rs", "src/fetcher.rs", "src/lib.rs")
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


def runtime_snapshot(revision: str | None = None) -> dict[str, dict[str, str]]:
    """Include mode and untracked runtime files; staged Git alone is insufficient."""
    if revision is None:
        for relative in RUNTIME_ROOTS:
            root = ROOT / relative
            candidates = (root, *root.rglob("*")) if root.is_dir() else (root,)
            if any(path.is_symlink() for path in candidates):
                raise ValueError("runtime symlinks are not permitted")
        result = {}
        for path in files_under(RUNTIME_ROOTS):
            result[str(path.relative_to(ROOT))] = {
                "sha256": sha256(path.read_bytes()),
                "mode": "100755" if path.stat().st_mode & stat.S_IXUSR else "100644",
            }
        return result
    tree = git("ls-tree", "-rz", revision, "--", *RUNTIME_ROOTS)
    if tree.returncode:
        raise ValueError("cannot resolve the declared runtime baseline")
    result = {}
    for record in filter(None, tree.stdout.split("\0")):
        metadata, name = record.split("\t", 1)
        mode, kind, object_id = metadata.split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ValueError("runtime baseline contains an unsupported file type")
        blob = subprocess.run(["git", "cat-file", "blob", object_id], cwd=ROOT,
                              capture_output=True, check=False, timeout=10)
        if blob.returncode:
            raise ValueError("cannot read the declared runtime baseline")
        result[name] = {"sha256": sha256(blob.stdout), "mode": mode}
    return result


def snapshot_content(snapshot: dict[str, dict[str, str]]) -> dict[str, Any]:
    entries = {name: item["sha256"] for name, item in snapshot.items()}
    return {"fileCount": len(entries), "sha256": canonical_sha(entries)}


def describe_runtime_patch() -> dict[str, Any]:
    """Produce a source-bound draft for review, never an acceptance receipt."""
    if git("merge-base", "--is-ancestor", RUNTIME_PATCH_BASE, "HEAD").returncode:
        raise ValueError("the reviewed runtime patch base is not an ancestor of HEAD")
    base = runtime_snapshot(RUNTIME_PATCH_BASE)
    if base != runtime_snapshot(UPSTREAM_BASE):
        raise ValueError("the reviewed patch base is not upstream-runtime-equivalent")
    current = runtime_snapshot()
    changed = sorted(name for name in base.keys() | current.keys()
                     if base.get(name) != current.get(name))
    if tuple(changed) != RUNTIME_PATCH_FILES:
        raise ValueError("lifecycle patch must change exactly the three reviewed source paths")
    entries = {name: {"before": base.get(name), "after": current.get(name)}
               for name in changed}
    if any(item["after"] is None or item["after"]["mode"] != "100644"
           for item in entries.values()):
        raise ValueError("lifecycle patch may not delete files or change executable modes")
    declaration = {
        "schemaVersion": 1,
        "kind": RUNTIME_PATCH_KIND,
        "baseRevision": RUNTIME_PATCH_BASE,
        "files": entries,
    }
    return {**declaration, "contentSha256": canonical_sha(declaration)}


def verified_runtime_patch(declaration: dict[str, Any] | None) -> dict[str, Any] | None:
    changed = runtime_snapshot() != runtime_snapshot(UPSTREAM_BASE)
    if not changed:
        if declaration is not None:
            raise ValueError("an unchanged runtime may not claim a carried runtime patch")
        return None
    if declaration is None:
        raise ValueError("changed runtime requires an explicit reviewed lifecycle patch declaration")
    expected = describe_runtime_patch()
    # Canonical bytes also reject bool-as-int and extra/missing declaration fields.
    if canonical_sha(declaration) != canonical_sha(expected):
        raise ValueError("runtime patch declaration does not match the exact reviewed source delta")
    return expected


def release_policy() -> dict[str, Any]:
    return {
        "owner": "Frontier Jobs dependency owner",
        "kind": "third-party-infrastructure-fork",
        "updateCadence": "review upstream main and security advisories at least monthly and before every pin change",
        "securityResponse": "classify exposure immediately; pin away from a compromised revision or publish a reviewed fork repair under the incident SLA",
        "rebaseRule": "rebase only onto a reviewed immutable upstream revision and re-run producer plus Frontier consumer acceptance",
        "retirementTrigger": "upstream publishes the SQLx 0.9 adapter and the complete Frontier queue migration/storage/worker acceptance passes",
        "runtimeExpansionProhibited": True,
        "runtimePatchRule": "Only an explicit exact-source lifecycle-observation declaration is permitted; queue SQL, schema, claim, acknowledgement, credential and handler semantics remain outside its scope.",
    }


def build_manifest(runtime_patch: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime_patch = verified_runtime_patch(runtime_patch)
    runtime = content_group(RUNTIME_ROOTS)
    upstream_runtime = snapshot_content(runtime_snapshot(UPSTREAM_BASE))
    tooling = content_group(("frontier_release.py",))
    policy = release_policy()
    identity = canonical_sha(
        {
            "runtime": runtime["sha256"],
            "tooling": tooling["sha256"],
            "policy": policy,
            "upstreamBase": UPSTREAM_BASE,
            "carriedPatches": CARRIED_PATCH_COMMITS,
            "runtimePatch": runtime_patch,
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
            "runtimeContentSha256": upstream_runtime["sha256"],
            "runtimeEquivalent": runtime_patch is None,
        },
        "fork": {
            "repository": "https://github.com/allenrchan/apalis-postgres.git",
            "previousFrontierRevision": PREVIOUS_FRONTIER_REVISION,
            "carriedPatchCommits": list(CARRIED_PATCH_COMMITS),
            "carriedPatchFiles": list(CARRIED_PATCH_FILES),
            "carriedRuntimePatch": runtime_patch is not None,
            **({"runtimePatch": runtime_patch} if runtime_patch is not None else {}),
        },
        "content": {
            "runtime": runtime,
            "releaseTooling": tooling,
        },
        "policy": policy,
        "producerAcceptance": {
            "mergedPullRequest": "https://github.com/allenrchan/apalis-postgres/pull/2",
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
                "bash scripts/verify.sh test frontier-jobs",
                "just it-target frontier-jobs job_queue_storage_integration_test",
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
    try:
        expected = build_manifest(manifest.get("fork", {}).get("runtimePatch"))
        if manifest != expected:
            errors.append("frontier-release.json does not match current governed content")
    except ValueError as error:
        errors.append(str(error))

    if manifest.get("package", {}).get("registryPublishAllowed") is not False:
        errors.append("the Frontier fork must not claim a registry release")
    if (manifest.get("fork", {}).get("carriedRuntimePatch") is True
            and not manifest.get("fork", {}).get("runtimePatch")):
        errors.append("CT-11 must not claim a hidden carried runtime patch")

    ancestor = git("merge-base", "--is-ancestor", UPSTREAM_BASE, "HEAD")
    if ancestor.returncode != 0:
        errors.append("the declared upstream base is not an ancestor of HEAD")
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
    parser.add_argument("command", choices=("render", "validate", "describe-runtime-patch"))
    parser.add_argument("--runtime-patch", type=Path,
                        help="explicit reviewed declaration; only valid with render")
    args = parser.parse_args()
    if args.runtime_patch is not None and args.command != "render":
        parser.error("--runtime-patch is only valid with render")
    try:
        if args.command == "describe-runtime-patch":
            print(json.dumps(describe_runtime_patch(), indent=2, sort_keys=True))
            return 0
        if args.command == "render":
            declaration = (json.loads(args.runtime_patch.read_text())
                           if args.runtime_patch is not None else None)
            manifest = build_manifest(declaration)
            MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            print(f"Rendered {MANIFEST.name}")
            return 0
    except (ValueError, OSError) as error:
        print(f"CT-11 release error: {error}")
        return 1
    errors = validation_errors()
    if errors:
        for error in errors:
            print(f"CT-11 release error: {error}")
        return 1
    print("CT-11 Frontier fork release passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
