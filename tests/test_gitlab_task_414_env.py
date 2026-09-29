from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_ROOT = REPO_ROOT / "benchmarks/cua_world/environments/gitlab_task_414_env"


class GitLabTask414EnvironmentTests(unittest.TestCase):
    def test_source_bundle_validation_works_outside_a_git_repository(self) -> None:
        script = ENV_ROOT / "scripts/validate_task_414.py"
        spec = importlib.util.spec_from_file_location("validate_task_414", script)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        original_cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                result = module.validate_source(ENV_ROOT / "assets")
        finally:
            os.chdir(original_cwd)

        self.assertEqual(
            result["bundle_sha256"],
            "4cfe0cd21f791325289455ec6b8d03ebf394458e1312d399275e46127348f1a6",
        )
        self.assertTrue(result["checks"]["license_absent"])

    def test_firefox_snap_parents_are_created_for_desktop_user(self) -> None:
        helper = (ENV_ROOT / "scripts/task_utils.sh").read_text()

        self.assertIn('local snap_root="/home/ga/snap/firefox"', helper)
        self.assertIn(
            '/home/ga/snap "$snap_root" "${snap_root}/common" "$profile"',
            helper,
        )
        self.assertIn("install -d -m 0700 -o ga -g ga", helper)


if __name__ == "__main__":
    unittest.main()
