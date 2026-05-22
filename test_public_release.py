import json
import copy
import os
import tempfile
import threading
import urllib.error
import urllib.request
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

    def test_valid_timezone_name_accepts_iana_and_rejects_invalid(self) -> None:
        self.assertEqual(dashboard.valid_timezone_name("Europe/Berlin"), "Europe/Berlin")
        self.assertEqual(dashboard.valid_timezone_name(" America/New_York "), "America/New_York")
        self.assertIsNone(dashboard.valid_timezone_name("../Europe/Berlin"))
        self.assertIsNone(dashboard.valid_timezone_name("Not/AZone"))

    def test_normalize_model_keeps_codex_53_and_spark_distinct(self) -> None:
        self.assertEqual(dashboard.normalize_model("Codex GPT 5.3 Spark"), "codex-gpt-5.3-spark")
        self.assertEqual(dashboard.normalize_model("Codex 5.3 Spark"), "codex-gpt-5.3-spark")
        self.assertEqual(dashboard.normalize_model("codex-gpt-5.3-spark"), "codex-gpt-5.3-spark")
        self.assertEqual(dashboard.normalize_model("Codex GPT 5.3"), "codex-gpt-5.3")
        self.assertEqual(dashboard.normalize_model("Codex 5.3"), "codex-gpt-5.3")
        self.assertEqual(dashboard.normalize_model("gpt-5.3"), "gpt-5.3")

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

    def test_snapshot_payload_keeps_project_and_session_names_without_private_fields(self) -> None:
        payload = {
            "meta": {
                "generatedAt": "2026-05-17T12:00:00+02:00",
                "timezone": "Europe/Berlin",
                "codexHome": r"X:\SourceLogs\.codex",
                "sessionFiles": 1,
                "sessionsWithUsage": 1,
                "priceSources": [],
            },
            "pricing": {"defaultModel": "gpt-5.5", "models": {}},
            "totals": {"usage": {"total": 123}, "byModel": {}, "byModelEffort": {}, "costLoggedMix": 0},
            "daily": [],
            "hourly": [],
            "sessions": [
                {
                    "threadId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "title": "Implement dashboard sync feature",
                    "cwd": r"X:\SourceLogs\Client Matter\codex-analytics-dashboard",
                    "source": '{"private": "value"}',
                    "path": r"X:\SourceLogs\.codex\sessions\rollout-local.jsonl",
                    "model": "gpt-5.5",
                    "usage": {"input": 100, "cachedInput": 10, "output": 23, "reasoningOutput": 3, "total": 123},
                    "byModel": {"gpt-5.5": {"input": 100, "cachedInput": 10, "output": 23, "reasoningOutput": 3, "total": 123}},
                    "byModelEffort": {},
                }
            ],
            "dailySessions": {
                "2026-05-17": [
                    {
                        "threadId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "title": "Implement dashboard sync feature",
                        "cwd": r"X:\SourceLogs\Client Matter\codex-analytics-dashboard",
                        "model": "gpt-5.5",
                        "usage": {"input": 100, "cachedInput": 10, "output": 23, "reasoningOutput": 3, "total": 123},
                        "byModel": {"gpt-5.5": {"input": 100, "cachedInput": 10, "output": 23, "reasoningOutput": 3, "total": 123}},
                        "byModelEffort": {},
                    }
                ]
            },
            "hourlySessions": {},
        }
        device = dashboard.SnapshotDevice(
            device_id="device-1234567890abcdef",
            name="Work Windows",
            slug="work-windows",
        )

        snapshot = dashboard.create_snapshot_payload(payload, device)
        encoded = json.dumps(snapshot, ensure_ascii=False)

        self.assertEqual(snapshot["meta"]["codexHome"], "redacted")
        self.assertEqual(snapshot["meta"]["privacyLevel"], "projects")
        self.assertEqual(snapshot["meta"]["deviceName"], "Work Windows")
        self.assertEqual(snapshot["sessions"][0]["cwd"], "codex-analytics-dashboard")
        self.assertEqual(snapshot["sessions"][0]["title"], "Implement dashboard sync feature")
        self.assertEqual(snapshot["dailySessions"]["2026-05-17"][0]["title"], "Implement dashboard sync feature")
        self.assertNotIn(r"X:\SourceLogs", encoded)
        self.assertNotIn("Client Matter", encoded)
        self.assertNotIn("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", encoded)
        self.assertNotIn("rollout-local.jsonl", encoded)
        self.assertNotIn('"private": "value"', encoded)

    def test_project_aliases_rename_historical_project_buckets(self) -> None:
        payload = {
            "sessions": [
                {"cwd": r"X:\Work\New project", "title": "Local thesis work"},
                {"cwd": "New project", "title": "Synced thesis work"},
                {"cwd": "Other project", "title": "Other work"},
            ],
            "dailySessions": {
                "2026-05-22": [
                    {"cwd": "New project", "title": "Daily thesis work"},
                ]
            },
            "hourlySessions": {},
            "devicePayloads": {
                "device-a": {
                    "sessions": [{"cwd": "New project", "title": "Device thesis work"}],
                    "dailySessions": {},
                    "hourlySessions": {},
                    "devicePayloads": {},
                }
            },
        }

        dashboard.apply_project_aliases(payload, {dashboard.alias_key("New project"): "Thesis-DSDE"})

        self.assertEqual(payload["sessions"][0]["cwd"], "Thesis-DSDE")
        self.assertEqual(payload["sessions"][1]["cwd"], "Thesis-DSDE")
        self.assertEqual(payload["sessions"][2]["cwd"], "Other project")
        self.assertEqual(payload["dailySessions"]["2026-05-22"][0]["cwd"], "Thesis-DSDE")
        self.assertEqual(payload["devicePayloads"]["device-a"]["sessions"][0]["cwd"], "Thesis-DSDE")

    def test_user_text_word_count_handles_basic_multilingual_messages(self) -> None:
        self.assertEqual(dashboard.count_user_words("Hallo Welt, schreibe 10 Fragen."), 5)
        self.assertEqual(dashboard.count_user_words("  Zwei\nZeilen, ein Test!  "), 4)
        self.assertEqual(dashboard.count_user_words(""), 0)

    def test_snapshot_folder_combines_device_subdirectories(self) -> None:
        base_payload = {
            "meta": {
                "generatedAt": "2026-05-17T12:00:00+02:00",
                "timezone": "Europe/Berlin",
                "codexHome": "redacted",
                "sessionFiles": 1,
                "sessionsWithUsage": 1,
                "priceSources": [],
            },
            "pricing": {"defaultModel": "gpt-5.5", "models": {"gpt-5.5": {"input": 5, "cached_input": 0.5, "output": 30}}},
            "totals": {
                "usage": {"input": 10, "cachedInput": 0, "output": 5, "reasoningOutput": 1, "total": 15},
                "byModel": {"gpt-5.5": {"input": 10, "cachedInput": 0, "output": 5, "reasoningOutput": 1, "total": 15}},
                "byModelEffort": {},
                "costLoggedMix": 0,
                "userText": {"messages": 1, "words": 4},
            },
            "daily": [
                {
                    "date": "2026-05-17",
                    "usage": {"input": 10, "cachedInput": 0, "output": 5, "reasoningOutput": 1, "total": 15},
                    "byModel": {"gpt-5.5": {"input": 10, "cachedInput": 0, "output": 5, "reasoningOutput": 1, "total": 15}},
                    "byModelEffort": {},
                    "events": 1,
                    "messageEvents": {"total": 2, "user": 1, "agent": 1, "primaryAgent": 1, "subagentAgent": 0},
                    "userText": {"messages": 1, "words": 4},
                    "sessionCount": 1,
                }
            ],
            "hourly": [],
            "sessions": [
                {
                    "threadId": "session-1",
                    "title": "Session 1",
                    "cwd": "codex-analytics-dashboard",
                    "model": "gpt-5.5",
                    "usage": {"input": 10, "cachedInput": 0, "output": 5, "reasoningOutput": 1, "total": 15},
                    "byModel": {"gpt-5.5": {"input": 10, "cachedInput": 0, "output": 5, "reasoningOutput": 1, "total": 15}},
                    "byModelEffort": {},
                    "events": 1,
                    "messageEvents": {"total": 2, "user": 1, "agent": 1, "primaryAgent": 1, "subagentAgent": 0},
                    "userText": {"messages": 1, "words": 4},
                }
            ],
            "dailySessions": {},
            "hourlySessions": {},
        }
        first = copy.deepcopy(base_payload)
        first["meta"]["deviceId"] = "device-a"
        first["meta"]["deviceName"] = "Work Windows"
        first["meta"]["deviceSlug"] = "work-windows"
        second = copy.deepcopy(base_payload)
        second["meta"]["deviceId"] = "device-b"
        second["meta"]["deviceName"] = "Personal MacBook"
        second["meta"]["deviceSlug"] = "personal-macbook"
        second["sessions"][0]["threadId"] = "session-2"

        with tempfile.TemporaryDirectory() as tmp:
            snapshot_dir = Path(tmp)
            dashboard.write_device_snapshot(snapshot_dir, first)
            dashboard.write_device_snapshot(snapshot_dir, second)

            snapshots = dashboard.load_snapshot_payloads(snapshot_dir)
            combined = dashboard.combine_snapshot_payloads(snapshots, "Europe/Berlin")
            snapshot_exists = (snapshot_dir / "work-windows" / "snapshot.json").exists()

        self.assertTrue(snapshot_exists)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(combined["totals"]["usage"]["total"], 30)
        self.assertEqual(combined["totals"]["userText"]["words"], 8)
        self.assertEqual(combined["daily"][0]["sessionCount"], 2)
        self.assertEqual(combined["daily"][0]["userText"]["messages"], 2)
        self.assertEqual({device["name"] for device in combined["meta"]["devices"]}, {"Work Windows", "Personal MacBook"})
        self.assertIn("device-a", combined["devicePayloads"])
        self.assertIn("device-b", combined["devicePayloads"])

    def test_snapshot_root_creates_codex_analytics_folder_under_parent(self) -> None:
        self.assertEqual(
            dashboard.resolve_snapshot_root(Path(r"X:\Cloud Drive")),
            Path(r"X:\Cloud Drive") / "Codex Analytics",
        )
        self.assertEqual(
            dashboard.resolve_snapshot_root(Path(r"X:\Cloud Drive\Codex Analytics")),
            Path(r"X:\Cloud Drive\Codex Analytics"),
        )
        self.assertEqual(
            dashboard.resolve_snapshot_root(Path(r"X:\Cloud Drive\CodexAnalytics")),
            Path(r"X:\Cloud Drive\CodexAnalytics"),
        )

    def test_dashboard_template_has_no_data_state(self) -> None:
        self.assertIn('id="noDataState"', dashboard.HTML_TEMPLATE)
        self.assertIn("No Codex usage data found", dashboard.HTML_TEMPLATE)
        self.assertIn("npx codex-analytics-dashboard@latest", dashboard.HTML_TEMPLATE)

    def test_dashboard_template_has_device_filter(self) -> None:
        self.assertIn('id="deviceSelect"', dashboard.HTML_TEMPLATE)
        self.assertIn("renderDeviceOptions", dashboard.HTML_TEMPLATE)
        self.assertIn("All devices", dashboard.HTML_TEMPLATE)

    def test_dashboard_template_has_timezone_filter(self) -> None:
        self.assertIn('id="timezoneSelect"', dashboard.HTML_TEMPLATE)
        self.assertIn("renderTimezoneOptions", dashboard.HTML_TEMPLATE)
        self.assertIn("Europe/Berlin", dashboard.HTML_TEMPLATE)
        self.assertIn("?timezone=", dashboard.HTML_TEMPLATE)

    def test_refreshing_handler_passes_timezone_query_to_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            (directory / "index.html").write_text("ok", encoding="utf-8")
            seen: list[str | None] = []
            handler = dashboard.make_refreshing_handler(
                directory,
                "index.html",
                None,
                lambda timezone_name=None: seen.append(timezone_name) or {},
            )
            server = dashboard.ReusableTCPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html?timezone=America/New_York", timeout=5) as response:
                    self.assertEqual(response.read(), b"ok")
                self.assertEqual(seen[-1], "America/New_York")

                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html?timezone=../Europe/Berlin", timeout=5)
                self.assertEqual(raised.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()

    def test_dashboard_template_has_collapsible_session_titles_and_bottom_aligned_kpis(self) -> None:
        self.assertIn("session-title-toggle", dashboard.HTML_TEMPLATE)
        self.assertIn("toggleSessionTitle", dashboard.HTML_TEMPLATE)
        self.assertIn("session-title-expanded", dashboard.HTML_TEMPLATE)
        self.assertIn("margin-top: auto", dashboard.HTML_TEMPLATE)

    def test_dashboard_template_has_user_word_kpis(self) -> None:
        self.assertIn("User Words", dashboard.HTML_TEMPLATE)
        self.assertIn("Avg Words / Message", dashboard.HTML_TEMPLATE)
        self.assertIn("userText", dashboard.HTML_TEMPLATE)

    def test_dashboard_template_has_expanded_heatmap_scale_and_relative_session_bars(self) -> None:
        self.assertIn("HEATMAP_COLOR_STEPS = 7", dashboard.HTML_TEMPLATE)
        self.assertIn("HEATMAP_SCALE_STEPS", dashboard.HTML_TEMPLATE)
        self.assertIn("progress-fill", dashboard.HTML_TEMPLATE)
        self.assertIn("% of top session, split", dashboard.HTML_TEMPLATE)

    def test_npm_package_uses_analytics_dashboard_name(self) -> None:
        package = json.loads((PACKAGE_ROOT / "package.json").read_text(encoding="utf-8"))

        self.assertEqual(package["name"], "codex-analytics-dashboard")
        self.assertIn("codex-analytics-dashboard", package["bin"])
        self.assertEqual(package["bin"]["codex-analytics-dashboard"], "bin/codex-analytics-dashboard.js")


if __name__ == "__main__":
    unittest.main()
