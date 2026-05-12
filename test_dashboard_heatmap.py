import re
import unittest

from codex_usage_dashboard import HTML_TEMPLATE


class HeatmapScaleTests(unittest.TestCase):
    def test_heatmap_uses_top_projects_palette_and_fixed_token_ceiling(self) -> None:
        script_match = re.search(r"<script>([\s\S]*)</script>", HTML_TEMPLATE)
        self.assertIsNotNone(script_match, "dashboard script not found")
        script = script_match.group(1)

        self.assertIn("const HEATMAP_TOKEN_FULL_SCALE = 250_000_000;", script)
        self.assertIn('const HEATMAP_TOKEN_METRICS = new Set(["total", "input", "totalInput", "output"]);', script)
        self.assertIn("const HEATMAP_PROJECT_GRADIENT = [", script)
        self.assertIn("[0.00, [47, 127, 121]]", script)
        self.assertIn("[1.00, [185, 133, 37]]", script)
        self.assertIn("return HEATMAP_TOKEN_FULL_SCALE;", script)
        self.assertIn("heatScaleMax(state.heatMetric, observedMaxValue)", script)
        self.assertIn("warmer means more use", script)
        self.assertIn("full color at ${compactAxis(HEATMAP_TOKEN_FULL_SCALE)} tokens", script)


if __name__ == "__main__":
    unittest.main()
