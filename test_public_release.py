import json
import copy
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_usage_dashboard as dashboard


PACKAGE_ROOT = Path(__file__).resolve().parent


class PublicReleaseTests(unittest.TestCase):
    def test_default_timezone_uses_tz_env_or_utc(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(dashboard.default_timezone_name(), "UTC")

        with patch.dict(os.environ, {"TZ": "America/New_York"}, clear=True):
            self.assertEqual(dashboard.default_timezone_name(), "America/New_York")

    def test_privacy_redaction_removes_private_session_fields_but_keeps_usage(self) -> None:
        payload = {
            "meta": {
                "codexHome": "example-codex-home",
                "sessionFiles": 1,
                "sessionsWithUsage": 1,
            },
            "totals": {"usage": {"total": 123}},
            "sessions": [
                {
                    "threadId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "title": "Synthetic task title",
                    "cwd": "sample-project",
                    "source": '{"subagent": true, "private": "value"}',
                    "path": "sample-rollout.jsonl",
                    "usage": {"input": 100, "cachedInput": 10, "output": 23, "reasoningOutput": 3, "total": 123},
                }
            ],
            "dailySessions": {
                "2026-05-12": [
                    {
                        "threadId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "title": "Synthetic task title",
                        "cwd": "sample-project",
                        "model": "gpt-5.5",
                        "usage": {"input": 100, "cachedInput": 10, "output": 23, "reasoningOutput": 3, "total": 123},
                    }
                ]
            },
            "hourlySessions": {},
        }
        original_usage = copy.deepcopy(payload["sessions"][0]["usage"])

        dashboard.redact_payload(payload)

        self.assertEqual(payload["meta"]["codexHome"], "redacted")
        self.assertEqual(payload["sessions"][0]["threadId"], "session-1")
        self.assertEqual(payload["sessions"][0]["title"], "Session 1")
        self.assertEqual(payload["sessions"][0]["cwd"], "Redacted path")
        self.assertEqual(payload["sessions"][0]["path"], "Redacted path")
        self.assertEqual(payload["sessions"][0]["source"], "redacted")
        self.assertEqual(payload["sessions"][0]["usage"], original_usage)
        self.assertEqual(payload["dailySessions"]["2026-05-12"][0]["threadId"], "session-1")
        self.assertEqual(payload["dailySessions"]["2026-05-12"][0]["title"], "Session 1")
        self.assertEqual(payload["dailySessions"]["2026-05-12"][0]["cwd"], "Redacted path")

    def test_dashboard_template_has_no_data_state(self) -> None:
        self.assertIn('id="noDataState"', dashboard.HTML_TEMPLATE)
        self.assertIn("No Codex usage data found", dashboard.HTML_TEMPLATE)
        self.assertIn("npx codex-analytics-dashboard@latest", dashboard.HTML_TEMPLATE)

    def test_npm_package_uses_analytics_dashboard_name(self) -> None:
        package = json.loads((PACKAGE_ROOT / "package.json").read_text(encoding="utf-8"))

        self.assertEqual(package["name"], "codex-analytics-dashboard")
        self.assertIn("codex-analytics-dashboard", package["bin"])
        self.assertEqual(package["bin"]["codex-analytics-dashboard"], "bin/codex-analytics-dashboard.js")


if __name__ == "__main__":
    unittest.main()
