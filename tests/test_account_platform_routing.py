import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from typer.testing import CliRunner

from naukri_agent.cli import app
from naukri_agent.config import ConfigError, validate_account_platform
from naukri_agent.core.orchestrator import Orchestrator


class TestAccountPlatformRouting(unittest.TestCase):
    def test_secondary_cannot_select_single_login_platform(self):
        for platform in ("linkedin", "cutshort", "wellfound", "instahyre", "hiringcafe"):
            with self.subTest(platform=platform):
                with self.assertRaisesRegex(ConfigError, "Select primary"):
                    validate_account_platform("secondary", platform)
        validate_account_platform("secondary", "naukri")
        validate_account_platform("secondary", None)
        validate_account_platform("primary", "linkedin")

    def test_cli_rejects_secondary_platform_before_database_work(self):
        settings = MagicMock(default_account="primary")
        migrate = AsyncMock()
        with (
            patch("naukri_agent.cli._bootstrap", return_value=(settings, MagicMock())),
            patch("naukri_agent.cli.run_migrations", migrate),
        ):
            result = CliRunner().invoke(
                app, ["run", "--account", "secondary", "--platform", "linkedin"]
            )
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("Select primary", result.output)
        migrate.assert_not_awaited()

    def test_direct_orchestrator_cannot_bypass_routing(self):
        settings = MagicMock(default_account="primary", dry_run=False, side_effects_enabled=True)
        with self.assertRaisesRegex(ConfigError, "Select primary"):
            Orchestrator(MagicMock(), settings, account="secondary", only_platform="cutshort")

    def test_manual_workflow_defaults_to_live(self):
        workflow = yaml.load(
            Path(".github/workflows/naukri_agent.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["dry_run"]["default"], "false")
        steps = workflow["jobs"]["run-agent"]["steps"]
        self.assertEqual(steps[0]["name"], "Validate account and platform selection")
