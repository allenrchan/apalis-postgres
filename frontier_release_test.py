#!/usr/bin/env python3

from __future__ import annotations

import copy
import unittest

import frontier_release


class FrontierReleaseTest(unittest.TestCase):
    def test_current_release_is_valid(self) -> None:
        self.assertEqual(frontier_release.validation_errors(), [])

    def test_registry_publication_claim_is_rejected(self) -> None:
        manifest = frontier_release.build_manifest()
        manifest["package"]["registryPublishAllowed"] = True
        errors = frontier_release.validation_errors(manifest)
        self.assertIn("the Frontier fork must not claim a registry release", errors)

    def test_runtime_patch_claim_is_rejected(self) -> None:
        manifest = frontier_release.build_manifest()
        manifest["fork"]["carriedRuntimePatch"] = True
        errors = frontier_release.validation_errors(manifest)
        self.assertIn("CT-11 must not claim a hidden carried runtime patch", errors)

    def test_partial_or_mixed_release_identity_is_rejected(self) -> None:
        manifest = frontier_release.build_manifest()
        manifest["fork"]["carriedPatchCommits"] = copy.deepcopy(
            manifest["fork"]["carriedPatchCommits"][:1]
        )
        self.assertIn(
            "frontier-release.json does not match current governed content",
            frontier_release.validation_errors(manifest),
        )

    def test_release_identity_is_content_bound(self) -> None:
        manifest = frontier_release.build_manifest()
        manifest["content"]["runtime"]["sha256"] = "0" * 64
        self.assertIn(
            "frontier-release.json does not match current governed content",
            frontier_release.validation_errors(manifest),
        )

    def test_rollback_is_whole_revision(self) -> None:
        rollback = frontier_release.build_manifest()["rollback"]
        self.assertTrue(rollback["partialRollbackForbidden"])
        self.assertEqual(
            rollback["previousKnownGoodRevision"],
            frontier_release.PREVIOUS_FRONTIER_REVISION,
        )

    def test_frontier_acceptance_names_real_bounded_owners(self) -> None:
        required = frontier_release.build_manifest()["frontierAcceptance"]["required"]
        self.assertEqual(
            required,
            [
                "bash scripts/verify.sh test frontier-jobs",
                "just it-target frontier-jobs job_queue_storage_integration_test",
                "just it-target frontier-jobs worker_task_integration_test",
            ],
        )


if __name__ == "__main__":
    unittest.main()
