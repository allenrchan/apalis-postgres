#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

import frontier_release


def current_manifest():
    """Validate the explicitly retained producer descriptor, never infer approval."""
    manifest = json.loads(frontier_release.MANIFEST.read_text())
    return frontier_release.build_manifest(manifest.get("fork", {}).get("runtimePatch"))


class FrontierReleaseTest(unittest.TestCase):
    def test_current_release_is_valid(self) -> None:
        self.assertEqual(frontier_release.validation_errors(), [])

    def test_registry_publication_claim_is_rejected(self) -> None:
        manifest = current_manifest()
        manifest["package"]["registryPublishAllowed"] = True
        errors = frontier_release.validation_errors(manifest)
        self.assertIn("the Frontier fork must not claim a registry release", errors)

    def test_runtime_patch_claim_is_rejected(self) -> None:
        manifest = current_manifest()
        manifest["fork"]["carriedRuntimePatch"] = True
        manifest["fork"].pop("runtimePatch", None)
        errors = frontier_release.validation_errors(manifest)
        self.assertIn("CT-11 must not claim a hidden carried runtime patch", errors)

    def test_partial_or_mixed_release_identity_is_rejected(self) -> None:
        manifest = current_manifest()
        manifest["fork"]["carriedPatchCommits"] = copy.deepcopy(
            manifest["fork"]["carriedPatchCommits"][:1]
        )
        self.assertIn(
            "frontier-release.json does not match current governed content",
            frontier_release.validation_errors(manifest),
        )

    def test_release_identity_is_content_bound(self) -> None:
        manifest = current_manifest()
        manifest["content"]["runtime"]["sha256"] = "0" * 64
        self.assertIn(
            "frontier-release.json does not match current governed content",
            frontier_release.validation_errors(manifest),
        )

    def test_rollback_is_whole_revision(self) -> None:
        rollback = current_manifest()["rollback"]
        self.assertTrue(rollback["partialRollbackForbidden"])
        self.assertEqual(
            rollback["previousKnownGoodRevision"],
            frontier_release.PREVIOUS_FRONTIER_REVISION,
        )

    def test_frontier_acceptance_names_real_bounded_owners(self) -> None:
        required = current_manifest()["frontierAcceptance"]["required"]
        self.assertEqual(
            required,
            [
                "bash scripts/verify.sh test frontier-jobs",
                "just it-target frontier-jobs job_queue_storage_integration_test",
                "just it-target frontier-jobs worker_task_integration_test",
            ],
        )

    def test_producer_acceptance_names_the_governance_pull_request(self) -> None:
        acceptance = current_manifest()["producerAcceptance"]

        self.assertEqual(
            acceptance["mergedPullRequest"],
            "https://github.com/allenrchan/apalis-postgres/pull/2",
        )


class RuntimePatchCliTest(unittest.TestCase):
    """The release owner sees the real Git delta and actual public CLI."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="apalis-runtime-patch-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "fork"
        subprocess.run(["git", "clone", "--quiet", "--shared", str(frontier_release.ROOT), str(self.root)],
                       check=True, capture_output=True, text=True, timeout=10)
        subprocess.run(["git", "checkout", "--quiet", frontier_release.RUNTIME_PATCH_BASE],
                       cwd=self.root, check=True, capture_output=True, timeout=10)
        shutil.copyfile(frontier_release.ROOT / "frontier_release.py", self.root / "frontier_release.py")

    def change_observation_files(self):
        for name in ("src/lib.rs", "src/fetcher.rs"):
            with (self.root / name).open("a") as output:
                output.write("\n// Fictional fixture-only lifecycle observation delta.\n")
        (self.root / "src/consumer_event.rs").write_text("// Fixture-only typed event surface; not compiled or deployed.\n")
        subprocess.run(["git", "add", "--", "src"], cwd=self.root, check=True, capture_output=True, timeout=10)

    def cli(self, *arguments):
        return subprocess.run([sys.executable, "frontier_release.py", *arguments], cwd=self.root,
                              capture_output=True, text=True, timeout=15)

    def test_render_refuses_undeclared_runtime_delta_instead_of_claiming_upstream_equivalence(self):
        previous = (self.root / "frontier-release.json").read_bytes()
        self.change_observation_files()
        result = self.cli("render")
        self.assertNotEqual(result.returncode, 0,
            "render must not label changed runtime as upstream-equivalent without an exact patch declaration")
        self.assertIn("explicit reviewed lifecycle patch declaration", result.stdout)
        self.assertEqual((self.root / "frontier-release.json").read_bytes(), previous)

    def declaration(self):
        result = self.cli("describe-runtime-patch")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def render_declaration(self, declaration):
        path = self.root / "reviewed-runtime-patch.json"
        path.write_text(json.dumps(declaration))
        return self.cli("render", "--runtime-patch", str(path))

    def test_explicit_exact_patch_is_honest_and_validates_through_public_cli(self):
        self.change_observation_files()
        declaration = self.declaration()
        result = self.render_declaration(declaration)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = json.loads((self.root / "frontier-release.json").read_text())
        self.assertFalse(manifest["upstream"]["runtimeEquivalent"])
        self.assertTrue(manifest["fork"]["carriedRuntimePatch"])
        self.assertEqual(manifest["fork"]["runtimePatch"], declaration)
        self.assertEqual(tuple(declaration["files"]), frontier_release.RUNTIME_PATCH_FILES)
        self.assertNotEqual(manifest["upstream"]["runtimeContentSha256"],
                            manifest["content"]["runtime"]["sha256"])
        self.assertFalse(manifest["package"]["registryPublishAllowed"])
        self.assertTrue(manifest["rollback"]["partialRollbackForbidden"])
        validated = self.cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)

    def test_source_edit_after_review_rejects_stale_declaration(self):
        self.change_observation_files()
        declaration = self.declaration()
        with (self.root / "src/fetcher.rs").open("a") as output:
            output.write("// Changed after source review.\n")
        result = self.render_declaration(declaration)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match the exact reviewed source delta", result.stdout)

    def test_descriptor_cannot_expand_or_relabel_its_scope(self):
        self.change_observation_files()
        original = self.declaration()
        for field, value in (("baseRevision", "0" * 40), ("kind", "any-runtime-change"),
                             ("schemaVersion", True), ("extra", "approval")):
            with self.subTest(field=field):
                declaration = copy.deepcopy(original)
                declaration[field] = value
                result = self.render_declaration(declaration)
                self.assertNotEqual(result.returncode, 0)

    def test_non_observation_runtime_path_is_rejected_even_if_declared(self):
        self.change_observation_files()
        declaration = self.declaration()
        with (self.root / "Cargo.toml").open("a") as output:
            output.write("\n# Unexpected dependency/runtime contract delta.\n")
        result = self.render_declaration(declaration)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly the three reviewed source paths", result.stdout)

    def test_untracked_runtime_file_is_not_hidden_by_git_diff(self):
        self.change_observation_files()
        declaration = self.declaration()
        (self.root / "src/unreviewed.rs").write_text("// Not staged or reviewed.\n")
        result = self.render_declaration(declaration)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly the three reviewed source paths", result.stdout)

    def test_untracked_symlink_directory_cannot_hide_runtime_files(self):
        self.change_observation_files()
        declaration = self.declaration()
        outside = Path(self.temporary.name) / "unreviewed"
        outside.mkdir()
        (outside / "hidden.rs").write_text("// Not part of the source declaration.\n")
        (self.root / "src/hidden").symlink_to(outside, target_is_directory=True)
        result = self.render_declaration(declaration)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runtime symlinks are not permitted", result.stdout)

    def test_executable_mode_change_is_rejected(self):
        self.change_observation_files()
        declaration = self.declaration()
        (self.root / "src/fetcher.rs").chmod(0o755)
        result = self.render_declaration(declaration)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("may not delete files or change executable modes", result.stdout)

    def test_unchanged_runtime_cannot_claim_patch(self):
        result = self.render_declaration({"kind": frontier_release.RUNTIME_PATCH_KIND})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unchanged runtime may not claim", result.stdout)

    def test_malformed_declaration_fails_without_overwriting_manifest(self):
        self.change_observation_files()
        previous = (self.root / "frontier-release.json").read_bytes()
        path = self.root / "reviewed-runtime-patch.json"
        path.write_text("{ malformed")
        result = self.cli("render", "--runtime-patch", str(path))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual((self.root / "frontier-release.json").read_bytes(), previous)

    def test_post_render_delta_or_equivalence_claim_is_rejected(self):
        self.change_observation_files()
        self.assertEqual(self.render_declaration(self.declaration()).returncode, 0)
        path = self.root / "frontier-release.json"
        manifest = json.loads(path.read_text())
        manifest["upstream"]["runtimeEquivalent"] = True
        path.write_text(json.dumps(manifest))
        result = self.cli("validate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match current governed content", result.stdout)


def feature_matrix(workflow):
    lines = workflow.read_text().splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "feature_args:":
            continue
        indentation = len(line) - len(line.lstrip())
        values = []
        for item in lines[index + 1:]:
            if not item.strip():
                continue
            if len(item) - len(item.lstrip()) <= indentation:
                break
            if not item.strip().startswith("- "):
                raise AssertionError("feature_args must remain an explicit scalar matrix")
            values.append(json.loads(item.strip()[2:]))
        return values
    return [""]


class CoverageWorkflowTest(unittest.TestCase):
    """Evaluate the actual rendered coverage command against the supported CI inputs."""
    def commands(self):
        workflow = frontier_release.ROOT / ".github/workflows/coverage.yml"
        commands = re.findall(r"^\s+run: (cargo tarpaulin[^\n]+)$", workflow.read_text(), re.MULTILINE)
        self.assertEqual(len(commands), 1, "coverage must retain one explicit execution command")
        return [shlex.split(commands[0].replace("${{ matrix.feature_args }}", arguments))
                for arguments in feature_matrix(workflow)]

    def selectors(self, command):
        selected = []
        for index, value in enumerate(command):
            if value in ("--all-features", "--no-default-features"):
                selected.append(value)
            if value == "--features":
                selected.extend([value, command[index + 1]])
        return selected

    def enabled_features(self, command):
        features = tomllib.loads((frontier_release.ROOT / "Cargo.toml").read_text())["features"]
        selected = self.selectors(command)
        enabled = set(features if "--all-features" in selected else
                      [] if "--no-default-features" in selected else features["default"])
        if "--features" in selected:
            enabled.update(selected[selected.index("--features") + 1].split(","))
        return enabled

    def test_coverage_never_combines_separately_qualified_datetime_configurations(self):
        for command in self.commands():
            enabled = self.enabled_features(command)
            self.assertFalse({"chrono", "time"} <= enabled,
                             "coverage must use the separately qualified SQLx datetime configurations")

    def test_coverage_sqlx_native_tests_select_tokio_without_async_std(self):
        for command in self.commands():
            enabled = self.enabled_features(command)
            self.assertIn("tokio-comp", enabled)
            self.assertFalse(any(value.startswith("async-std-comp") for value in enabled),
                             "mixed runtime selection runs SQLx tests outside their required Tokio reactor")

    def test_coverage_uses_the_existing_supported_ci_feature_matrix(self):
        ci = frontier_release.ROOT / ".github/workflows/ci.yml"
        expected = [shlex.split(arguments) for arguments in feature_matrix(ci)]
        self.assertCountEqual([self.selectors(command) for command in self.commands()], expected)

    def test_coverage_retains_real_workspace_native_tests_and_existing_timeout(self):
        for command in self.commands():
            self.assertIn("--workspace", command)
            self.assertEqual(command[command.index("--timeout") + 1], "120")
            self.assertEqual(command[command.index("--") + 1:], ["--test-threads=1"])
            self.assertFalse({"--lib", "--skip", "--ignore-tests", "--exclude"} & set(command))


if __name__ == "__main__":
    unittest.main()
