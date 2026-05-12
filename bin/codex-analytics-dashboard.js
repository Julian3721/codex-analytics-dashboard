#!/usr/bin/env node
"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const GENERATOR = path.join(ROOT, "codex_usage_dashboard.py");
const APP_NAME = "Codex Analytics Dashboard";

function hasOption(args, names) {
  return args.some((arg) => names.some((name) => arg === name || arg.startsWith(`${name}=`)));
}

function defaultOutputDir() {
  if (process.env.CODEX_USAGE_DASHBOARD_DIR) {
    return path.resolve(process.env.CODEX_USAGE_DASHBOARD_DIR);
  }

  if (process.platform === "darwin") {
    return path.join(os.homedir(), "Library", "Application Support", APP_NAME);
  }

  if (process.platform === "win32") {
    return path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"), APP_NAME);
  }

  return path.join(process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state"), "codex-analytics-dashboard");
}

function localTimezone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || process.env.TZ || "UTC";
}

function buildArgs(userArgs) {
  if (hasOption(userArgs, ["--help", "-h"])) {
    return [GENERATOR, ...userArgs];
  }

  const outputDir = defaultOutputDir();
  fs.mkdirSync(outputDir, { recursive: true });

  const args = [GENERATOR, ...userArgs];
  if (!hasOption(userArgs, ["--out"])) {
    args.push("--out", path.join(outputDir, "codex_analytics_dashboard.html"));
  }
  if (!hasOption(userArgs, ["--json-out", "--no-json"])) {
    args.push("--json-out", path.join(outputDir, "codex_analytics_data.json"));
  }
  if (!hasOption(userArgs, ["--timezone"])) {
    args.push("--timezone", localTimezone());
  }
  if (!hasOption(userArgs, ["--server-url-file"])) {
    args.push("--server-url-file", path.join(outputDir, "codex_analytics_dashboard_server.url"));
  }
  if (!hasOption(userArgs, ["--generator-source"])) {
    args.push("--generator-source", GENERATOR);
  }
  if (!hasOption(userArgs, ["--serve"])) {
    args.push("--serve");
  }
  return args;
}

function main() {
  if (!fs.existsSync(GENERATOR)) {
    console.error(`Cannot find dashboard generator at ${GENERATOR}`);
    process.exit(1);
  }

  const python = process.env.PYTHON || "python3";
  const child = spawn(python, buildArgs(process.argv.slice(2)), { stdio: "inherit" });

  child.on("error", (error) => {
    if (error.code === "ENOENT") {
      console.error("python3 was not found. Install Python 3 or set PYTHON=/path/to/python.");
    } else {
      console.error(error.message);
    }
    process.exit(1);
  });

  child.on("exit", (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code || 0);
  });
}

main();
