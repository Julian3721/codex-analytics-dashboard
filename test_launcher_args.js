"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { buildArgs, normalizeUserArgs } = require("./bin/codex-analytics-dashboard.js");

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "codex-analytics-dashboard-"));
process.env.CODEX_USAGE_DASHBOARD_DIR = tmp;

try {
  assert.deepEqual(normalizeUserArgs(["--", "--snapshot-dir", "X:\\Cloud Drive"]), [
    "--snapshot-dir",
    "X:\\Cloud Drive",
  ]);

  const args = buildArgs(["--", "--snapshot-dir", "X:\\Cloud Drive", "--device-name", "Windows PC", "--no-open"]);
  assert.equal(args.includes("--"), false);
  assert.equal(args.includes("--snapshot-dir"), true);
  assert.equal(args[args.indexOf("--snapshot-dir") + 1], "X:\\Cloud Drive");
  assert.equal(args.includes("--device-name"), true);
  assert.equal(args[args.indexOf("--device-name") + 1], "Windows PC");
  assert.equal(args.includes("--serve"), true);
  assert.equal(args.includes("--out"), true);
  assert.equal(args[args.indexOf("--out") + 1], path.join(tmp, "codex_analytics_dashboard.html"));
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}
