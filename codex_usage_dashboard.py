#!/usr/bin/env python3
"""Generate a local Codex analytics dashboard from ~/.codex rollout logs."""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.server
import json
import os
import platform
import re
import socket
import socketserver
import sqlite3
import subprocess
import sys
import urllib.parse
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

MODEL_PRICES = {
    # USD per 1M tokens. Sources are linked in the generated dashboard footer.
    "gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.2": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
    "gpt-5": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
}

DEFAULT_MODEL = "gpt-5.5"
ROLLOUT_ID_RE = re.compile(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<id>[0-9a-f-]{36})\.jsonl$")
REDACTED_PATH = "Redacted path"
SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_PRIVACY_LEVEL = "projects"
APP_NAME = "Codex Analytics Dashboard"
CONFIG_FILE_NAME = "config.json"
USER_WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)
TIMEZONE_NAME_RE = re.compile(r"^[A-Za-z0-9_+\-./]{1,80}$")


def default_timezone_name() -> str:
    return os.environ.get("TZ") or "UTC"


def valid_timezone_name(value: str | None) -> str | None:
    if not value:
        return None
    timezone_name = value.strip()
    if not TIMEZONE_NAME_RE.fullmatch(timezone_name):
        return None
    try:
        ZoneInfo(timezone_name)
    except Exception:
        return None
    return timezone_name


def app_data_dir() -> Path:
    override = os.environ.get("CODEX_USAGE_DASHBOARD_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / APP_NAME
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "codex-analytics-dashboard"


def config_path() -> Path:
    return app_data_dir() / CONFIG_FILE_NAME


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(config: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def parse_project_alias(value: str) -> tuple[str, str] | None:
    if "=" not in value:
        return None
    source, target = value.split("=", 1)
    source = " ".join(source.strip().split())
    target = " ".join(target.strip().split())
    if not source or not target:
        return None
    return source, target


def alias_key(value: Any) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def config_project_aliases(config: dict[str, Any]) -> dict[str, str]:
    aliases = config.get("projectAliases")
    if not isinstance(aliases, dict):
        return {}
    return {alias_key(source): str(target) for source, target in aliases.items() if alias_key(source) and str(target).strip()}


def resolve_project_aliases(args: argparse.Namespace) -> dict[str, str]:
    config = load_config()
    aliases = config_project_aliases(config)
    raw_values = args.project_alias or []
    if raw_values:
        stored = dict(config.get("projectAliases") or {})
        for value in raw_values:
            parsed = parse_project_alias(value)
            if parsed:
                source, target = parsed
                aliases[alias_key(source)] = target
                stored[source] = target
        config["projectAliases"] = dict(sorted(stored.items()))
        save_config(config)
    return aliases


def default_device_name() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macOS device"
    if system == "windows":
        return "Windows device"
    if system == "linux":
        return "Linux device"
    return "Codex device"


def slugify(value: str, fallback: str = "device") -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or fallback


def resolve_snapshot_root(path: Path) -> Path:
    clean_name = re.sub(r"[\s_-]+", "", path.name.lower())
    if clean_name in {"codexanalytics"}:
        return path
    return path / "Codex Analytics"


def resolve_snapshot_setup(args: argparse.Namespace) -> tuple[Path | None, SnapshotDevice | None]:
    if args.no_snapshot:
        return None, None

    config = load_config()
    snapshot_dir_value = args.snapshot_dir or config.get("snapshotDir") or ""
    if not snapshot_dir_value:
        return None, None
    snapshot_root = resolve_snapshot_root(Path(snapshot_dir_value).expanduser())

    device_id = str(config.get("deviceId") or uuid.uuid4())
    device_name = args.device_name or str(config.get("deviceName") or default_device_name())
    device_slug = str(config.get("deviceSlug") or slugify(args.device_name or device_name))

    config.update(
        {
            "snapshotDir": str(snapshot_root),
            "deviceId": device_id,
            "deviceName": device_name,
            "deviceSlug": device_slug,
        }
    )
    save_config(config)

    return snapshot_root.resolve(), SnapshotDevice(device_id, device_name, device_slug)


@dataclass
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "Usage":
        payload = payload or {}
        return cls(
            input_tokens=int(payload.get("input_tokens") or 0),
            cached_input_tokens=int(payload.get("cached_input_tokens") or 0),
            output_tokens=int(payload.get("output_tokens") or 0),
            reasoning_output_tokens=int(payload.get("reasoning_output_tokens") or 0),
            total_tokens=int(payload.get("total_tokens") or 0),
        )

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_output_tokens += other.reasoning_output_tokens
        self.total_tokens += other.total_tokens

    def diff(self, previous: "Usage") -> "Usage":
        return Usage(
            input_tokens=max(0, self.input_tokens - previous.input_tokens),
            cached_input_tokens=max(0, self.cached_input_tokens - previous.cached_input_tokens),
            output_tokens=max(0, self.output_tokens - previous.output_tokens),
            reasoning_output_tokens=max(0, self.reasoning_output_tokens - previous.reasoning_output_tokens),
            total_tokens=max(0, self.total_tokens - previous.total_tokens),
        )

    def has_tokens(self) -> bool:
        return self.total_tokens > 0 or self.input_tokens > 0 or self.output_tokens > 0

    def to_json(self) -> dict[str, int]:
        return {
            "input": self.input_tokens,
            "cachedInput": self.cached_input_tokens,
            "output": self.output_tokens,
            "reasoningOutput": self.reasoning_output_tokens,
            "total": self.total_tokens,
        }


@dataclass
class MessageEvents:
    user: int = 0
    agent_primary: int = 0
    agent_subagent: int = 0

    @property
    def agent(self) -> int:
        return self.agent_primary + self.agent_subagent

    @property
    def total(self) -> int:
        return self.user + self.agent

    def add_type(self, payload_type: str, is_subagent: bool = False) -> None:
        if payload_type == "user_message":
            self.user += 1
        elif payload_type == "agent_message":
            if is_subagent:
                self.agent_subagent += 1
            else:
                self.agent_primary += 1

    def add(self, other: "MessageEvents") -> None:
        self.user += other.user
        self.agent_primary += other.agent_primary
        self.agent_subagent += other.agent_subagent

    def to_json(self) -> dict[str, int]:
        return {
            "total": self.total,
            "user": self.user,
            "agent": self.agent,
            "primaryAgent": self.agent_primary,
            "subagentAgent": self.agent_subagent,
        }


@dataclass
class UserTextStats:
    messages: int = 0
    words: int = 0

    def add_message(self, text: str) -> None:
        self.messages += 1
        self.words += count_user_words(text)

    def add(self, other: "UserTextStats") -> None:
        self.messages += other.messages
        self.words += other.words

    def to_json(self) -> dict[str, int | float]:
        average = round(self.words / self.messages, 2) if self.messages else 0
        return {
            "messages": self.messages,
            "words": self.words,
            "avgWordsPerMessage": average,
        }


@dataclass
class SessionMeta:
    thread_id: str
    title: str = "Untitled Codex session"
    cwd: str = ""
    source: str = ""
    model: str = ""
    reasoning_effort: str = ""
    created_at_ms: int | None = None
    updated_at_ms: int | None = None
    rollout_path: str = ""


@dataclass
class SessionAggregate:
    thread_id: str
    path: str
    title: str
    cwd: str = ""
    source: str = ""
    model: str = ""
    reasoning_effort: str = ""
    first_seen: str = ""
    last_seen: str = ""
    usage: Usage = field(default_factory=Usage)
    by_model: dict[str, Usage] = field(default_factory=dict)
    by_model_effort: dict[str, dict[str, Usage]] = field(default_factory=dict)
    days: set[str] = field(default_factory=set)
    event_count: int = 0
    message_events: MessageEvents = field(default_factory=MessageEvents)
    user_text: UserTextStats = field(default_factory=UserTextStats)


@dataclass
class SnapshotDevice:
    device_id: str
    name: str
    slug: str

    def to_json(self) -> dict[str, str]:
        return {
            "id": self.device_id,
            "name": self.name,
            "slug": self.slug,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a local Codex analytics dashboard.")
    parser.add_argument(
        "--codex-home",
        default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
        help="Path to the Codex data directory. Defaults to ~/.codex.",
    )
    parser.add_argument(
        "--out",
        default="codex_analytics_dashboard.html",
        help="Output HTML file. Defaults to ./codex_analytics_dashboard.html.",
    )
    parser.add_argument(
        "--json-out",
        default="codex_analytics_data.json",
        help="Optional machine-readable data export. Defaults to ./codex_analytics_data.json.",
    )
    parser.add_argument(
        "--timezone",
        default=default_timezone_name(),
        help="Timezone used for daily grouping. Defaults to TZ or UTC.",
    )
    parser.add_argument(
        "--redact",
        "--privacy",
        dest="redact",
        action="store_true",
        help="Redact session titles, thread IDs, local paths, and source metadata in generated outputs.",
    )
    parser.add_argument(
        "--snapshot-dir",
        default=os.environ.get("CODEX_ANALYTICS_SNAPSHOT_DIR", ""),
        help=(
            "Synced parent directory for privacy-preserving multi-device snapshots. A Codex Analytics "
            "folder is created inside unless the path is already named Codex Analytics."
        ),
    )
    parser.add_argument(
        "--device-name",
        default="",
        help="Friendly name for this device in synced dashboards. Defaults to a generic OS label.",
    )
    parser.add_argument(
        "--project-alias",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help=(
            "Rename a project bucket in generated dashboard output, for example "
            "'New project=Thesis-DSDE'. Can be passed multiple times and is saved in user-local config."
        ),
    )
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="Disable saved snapshot sync for this run, even if a snapshot directory is configured.",
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help="Do not write the companion JSON export.",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the dashboard in your default browser after generating it.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Serve the dashboard on localhost after generating it. Press Ctrl-C to stop.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the browser automatically when used with --serve.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Preferred localhost port for --serve. Defaults to 8765.",
    )
    parser.add_argument(
        "--server-url-file",
        default="",
        help="Optional file where --serve writes the active localhost dashboard URL.",
    )
    parser.add_argument(
        "--generator-source",
        default=os.environ.get("CODEX_USAGE_GENERATOR", ""),
        help="Generator file to execute on localhost refresh so UI/template changes appear without restarting the server.",
    )
    return parser.parse_args()


def iter_rollout_files(codex_home: Path) -> list[Path]:
    roots = [codex_home / "sessions", codex_home / "archived_sessions"]
    files: list[Path] = []
    for root in roots:
        if root.exists():
            files.extend(sorted(root.rglob("rollout-*.jsonl")))
    seen: set[Path] = set()
    unique_files: list[Path] = []
    for path in files:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique_files.append(path)
    return unique_files


def rollout_id(path: Path) -> str:
    match = ROLLOUT_ID_RE.search(path.name)
    return match.group("id") if match else path.stem


def parse_ts(value: str, tz: ZoneInfo) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(tz)


def iso_date(value: datetime) -> str:
    return value.date().isoformat()


def utc_ms_to_iso(ms: int | None, tz: ZoneInfo) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz).isoformat(timespec="seconds")


def normalize_model(model: str | None) -> str:
    model = (model or "").strip()
    if model in MODEL_PRICES:
        return model
    lowered = model.lower()
    if "codex" in lowered and re.search(r"(?:gpt[- ]*)?5\.3.*spark|spark.*(?:gpt[- ]*)?5\.3", lowered):
        return "codex-gpt-5.3-spark"
    if re.search(r"gpt[- ]*5\.3.*spark|spark.*gpt[- ]*5\.3", lowered):
        return "gpt-5.3-spark"
    if "codex" in lowered and re.search(r"(?:gpt[- ]*)?5\.3", lowered):
        return "codex-gpt-5.3"
    if re.search(r"gpt[- ]*5\.3", lowered):
        return "gpt-5.3"
    if "gpt-5.4-mini" in lowered:
        return "gpt-5.4-mini"
    if "gpt-5.5" in lowered:
        return "gpt-5.5"
    if "gpt-5.4" in lowered:
        return "gpt-5.4"
    if "gpt-5.2" in lowered:
        return "gpt-5.2"
    if lowered.startswith("gpt-5"):
        return "gpt-5"
    return model or DEFAULT_MODEL


def normalize_reasoning_effort(value: str | None) -> str:
    lowered = (value or "").strip().lower().replace("_", "-")
    if lowered in {"minimal", "none"}:
        return "low"
    if lowered in {"low", "medium", "high", "xhigh"}:
        return lowered
    if lowered in {"x-high", "extra-high", "extra high"}:
        return "xhigh"
    return "unknown"


def effort_from_turn_context(payload: dict[str, Any], fallback: str) -> str:
    settings = (payload.get("collaboration_mode") or {}).get("settings") or {}
    return normalize_reasoning_effort(payload.get("effort") or settings.get("reasoning_effort") or fallback)


def is_subagent_source(source: str) -> bool:
    source = (source or "").strip()
    if not source:
        return False
    try:
        parsed = json.loads(source)
    except json.JSONDecodeError:
        return "subagent" in source.lower()
    if isinstance(parsed, dict):
        return "subagent" in parsed
    return False


def clean_session_title(title: str, source: str = "", model: str = "", max_length: int = 96) -> str:
    cleaned = " ".join((title or "").split())
    if not cleaned:
        return "Untitled Codex session"

    lower = cleaned.lower()
    if (
        "the following is the codex agent history whose request action you are assessing" in lower
        or "reviewed codex session id:" in lower
        or normalize_model(source) == "codex-auto-review"
        or normalize_model(model) == "codex-auto-review"
    ):
        return "Codex approval review"

    if len(cleaned) <= max_length:
        return cleaned
    return f"{cleaned[: max_length - 1].rstrip()}…"


def load_thread_meta(codex_home: Path, tz: ZoneInfo) -> dict[str, SessionMeta]:
    state_db = codex_home / "state_5.sqlite"
    if not state_db.exists():
        return {}

    meta: dict[str, SessionMeta] = {}
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select id, title, first_user_message, cwd, source, model, reasoning_effort, created_at_ms, updated_at_ms, rollout_path
            from threads
            """
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    for row in rows:
        thread_id = row["id"]
        title = row["title"] or row["first_user_message"]
        source = row["source"] or ""
        meta[thread_id] = SessionMeta(
            thread_id=thread_id,
            title=clean_session_title(title or "Untitled Codex session", source, row["model"] or ""),
            cwd=row["cwd"] or "",
            source=source,
            model=row["model"] or "",
            reasoning_effort=normalize_reasoning_effort(row["reasoning_effort"] or ""),
            created_at_ms=row["created_at_ms"],
            updated_at_ms=row["updated_at_ms"],
            rollout_path=row["rollout_path"] or "",
        )
    return meta


def usage_cost(usage: Usage, model: str) -> float:
    rates = MODEL_PRICES.get(normalize_model(model), MODEL_PRICES[DEFAULT_MODEL])
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    uncached_input = max(0, usage.input_tokens - cached)
    return (
        uncached_input * rates["input"]
        + cached * rates["cached_input"]
        + usage.output_tokens * rates["output"]
    ) / 1_000_000


def hour_key(local_dt: datetime) -> str:
    return local_dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00:00")


def add_usage_to_bucket(bucket: dict[str, Any], usage: Usage, model: str, effort: str = "unknown") -> None:
    bucket_usage: Usage = bucket.setdefault("usage", Usage())
    bucket_usage.add(usage)
    by_model: dict[str, Usage] = bucket.setdefault("by_model", {})
    normalized = normalize_model(model)
    by_model.setdefault(normalized, Usage()).add(usage)
    normalized_effort = normalize_reasoning_effort(effort)
    by_model_effort: dict[str, dict[str, Usage]] = bucket.setdefault("by_model_effort", {})
    effort_bucket = by_model_effort.setdefault(normalized, {})
    effort_bucket.setdefault(normalized_effort, Usage()).add(usage)
    bucket["events"] = int(bucket.get("events", 0)) + 1


def add_message_event_to_bucket(bucket: dict[str, Any], payload_type: str, is_subagent: bool) -> None:
    message_events: MessageEvents = bucket.setdefault("message_events", MessageEvents())
    message_events.add_type(payload_type, is_subagent)


def count_user_words(text: str) -> int:
    return len(USER_WORD_RE.findall(text or ""))


def user_message_text(payload: dict[str, Any]) -> str:
    message = payload.get("message")
    if isinstance(message, str):
        return message
    text_elements = payload.get("text_elements")
    if isinstance(text_elements, list):
        parts: list[str] = []
        for item in text_elements:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def add_user_text_to_bucket(bucket: dict[str, Any], user_text: UserTextStats) -> None:
    bucket_user_text: UserTextStats = bucket.setdefault("user_text", UserTextStats())
    bucket_user_text.add(user_text)


def ensure_period_session(
    period_sessions: dict[str, dict[str, dict[str, Any]]],
    period_key: str,
    thread_id: str,
    title: str,
    cwd: str,
    model: str,
    effort: str,
    local_dt: datetime,
) -> dict[str, Any]:
    event_iso = local_dt.isoformat(timespec="seconds")
    bucket_sessions = period_sessions.setdefault(period_key, {})
    period_session = bucket_sessions.setdefault(
        thread_id,
        {
            "thread_id": thread_id,
            "title": title,
            "cwd": cwd,
            "model": model,
            "effort": effort,
            "usage": Usage(),
            "by_model": {},
            "by_model_effort": {},
            "events": 0,
            "message_events": MessageEvents(),
            "user_text": UserTextStats(),
            "first_event": event_iso,
            "last_event": event_iso,
        },
    )
    if event_iso < period_session.get("first_event", event_iso):
        period_session["first_event"] = event_iso
    if event_iso > period_session.get("last_event", event_iso):
        period_session["last_event"] = event_iso
    if cwd and not period_session.get("cwd"):
        period_session["cwd"] = cwd
    if model and not period_session.get("model"):
        period_session["model"] = model
    if effort and not period_session.get("effort"):
        period_session["effort"] = effort
    return period_session


def parse_rollout_file(
    path: Path,
    meta: SessionMeta | None,
    tz: ZoneInfo,
    daily: dict[str, dict[str, Any]],
    hourly: dict[str, dict[str, Any]],
    daily_sessions: dict[str, dict[str, dict[str, Any]]],
    hourly_sessions: dict[str, dict[str, dict[str, Any]]],
) -> SessionAggregate | None:
    thread_id = meta.thread_id if meta else rollout_id(path)
    session = SessionAggregate(
        thread_id=thread_id,
        path=str(path),
        title=(meta.title if meta else "") or "Untitled Codex session",
        cwd=(meta.cwd if meta else "") or "",
        source=(meta.source if meta else "") or "",
        model=(meta.model if meta else "") or "",
        reasoning_effort=(meta.reasoning_effort if meta else "") or "",
        first_seen=utc_ms_to_iso(meta.created_at_ms if meta else None, tz),
        last_seen=utc_ms_to_iso(meta.updated_at_ms if meta else None, tz),
    )

    previous_total = Usage()
    last_total = Usage()
    current_model = normalize_model(session.model)
    current_effort = normalize_reasoning_effort(session.reasoning_effort)
    current_cwd = session.cwd
    is_subagent_session = is_subagent_source(session.source)
    saw_usage = False

    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return None

    with handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            timestamp = row.get("timestamp")
            if timestamp and not session.first_seen:
                session.first_seen = parse_ts(timestamp, tz).isoformat(timespec="seconds")
            if timestamp:
                session.last_seen = parse_ts(timestamp, tz).isoformat(timespec="seconds")

            payload = row.get("payload") or {}
            row_type = row.get("type")
            payload_type = payload.get("type")

            if row_type == "turn_context":
                turn_payload = payload
                current_model = normalize_model(turn_payload.get("model") or current_model)
                current_effort = effort_from_turn_context(turn_payload, current_effort)
                current_cwd = turn_payload.get("cwd") or current_cwd
                if not session.model:
                    session.model = current_model
                if not session.reasoning_effort or session.reasoning_effort == "unknown":
                    session.reasoning_effort = current_effort
                if not session.cwd:
                    session.cwd = current_cwd
                continue

            if row_type == "event_msg" and payload_type in {"user_message", "agent_message"}:
                if not timestamp:
                    continue
                local_dt = parse_ts(timestamp, tz)
                day = iso_date(local_dt)
                hour = hour_key(local_dt)
                model_for_event = current_model or normalize_model(session.model)
                user_text = UserTextStats()
                if payload_type == "user_message" and not is_subagent_session:
                    user_text.add_message(user_message_text(payload))
                session.days.add(day)
                session.message_events.add_type(payload_type, is_subagent_session)
                if user_text.messages:
                    session.user_text.add(user_text)
                daily_bucket = daily.setdefault(day, {})
                hourly_bucket = hourly.setdefault(hour, {})
                add_message_event_to_bucket(daily_bucket, payload_type, is_subagent_session)
                add_message_event_to_bucket(hourly_bucket, payload_type, is_subagent_session)
                if user_text.messages:
                    add_user_text_to_bucket(daily_bucket, user_text)
                    add_user_text_to_bucket(hourly_bucket, user_text)
                for period_sessions, period_key in ((daily_sessions, day), (hourly_sessions, hour)):
                    period_session = ensure_period_session(
                        period_sessions,
                        period_key,
                        thread_id,
                        session.title,
                        current_cwd or session.cwd,
                        model_for_event,
                        current_effort,
                        local_dt,
                    )
                    period_session["message_events"].add_type(payload_type, is_subagent_session)
                    if user_text.messages:
                        period_session["user_text"].add(user_text)
                continue

            if row_type == "event_msg" and payload_type == "token_count":
                info = payload.get("info") or {}
                total_payload = info.get("total_token_usage")
                if not total_payload:
                    continue

                total = Usage.from_payload(total_payload)
                delta = total.diff(previous_total)

                # Some very old or migrated logs may emit a lower cumulative total.
                # In that case, trust last_token_usage for this event if it exists.
                if not delta.has_tokens() and total.total_tokens < previous_total.total_tokens:
                    delta = Usage.from_payload(info.get("last_token_usage"))

                previous_total = total
                last_total = total

                if not delta.has_tokens() or not timestamp:
                    continue

                saw_usage = True
                session.usage.add(delta)
                model_for_event = current_model or normalize_model(session.model)
                effort_for_event = current_effort or normalize_reasoning_effort(session.reasoning_effort)
                session.by_model.setdefault(model_for_event, Usage()).add(delta)
                session.by_model_effort.setdefault(model_for_event, {}).setdefault(effort_for_event, Usage()).add(delta)
                session.event_count += 1

                local_dt = parse_ts(timestamp, tz)
                day = iso_date(local_dt)
                hour = hour_key(local_dt)
                session.days.add(day)
                add_usage_to_bucket(daily.setdefault(day, {}), delta, model_for_event, effort_for_event)
                add_usage_to_bucket(hourly.setdefault(hour, {}), delta, model_for_event, effort_for_event)

                for period_sessions, period_key in ((daily_sessions, day), (hourly_sessions, hour)):
                    period_session = ensure_period_session(
                        period_sessions,
                        period_key,
                        thread_id,
                        session.title,
                        current_cwd or session.cwd,
                        model_for_event,
                        effort_for_event,
                        local_dt,
                    )
                    period_session["usage"].add(delta)
                    period_session["by_model"].setdefault(model_for_event, Usage()).add(delta)
                    period_session["by_model_effort"].setdefault(model_for_event, {}).setdefault(effort_for_event, Usage()).add(delta)
                    period_session["events"] += 1

    if saw_usage:
        # Prefer the cumulative last total for session totals. It should match
        # summed deltas but survives odd first-event cases more gracefully.
        if last_total.has_tokens() and last_total.total_tokens >= session.usage.total_tokens:
            session.usage = last_total
        if not session.model:
            session.model = current_model
        if not session.reasoning_effort or session.reasoning_effort == "unknown":
            session.reasoning_effort = current_effort
        return session
    return None


def fill_date_range(daily: dict[str, dict[str, Any]]) -> list[str]:
    if not daily:
        return []
    start = datetime.fromisoformat(min(daily.keys())).date()
    end = datetime.fromisoformat(max(daily.keys())).date()
    dates: list[str] = []
    current = start
    while current <= end:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def usage_json_by_model(by_model: dict[str, Usage]) -> dict[str, dict[str, int]]:
    return {model: usage.to_json() for model, usage in sorted(by_model.items())}


def usage_json_by_model_effort(by_model_effort: dict[str, dict[str, Usage]]) -> dict[str, dict[str, dict[str, int]]]:
    return {
        model: {effort: usage.to_json() for effort, usage in sorted(efforts.items())}
        for model, efforts in sorted(by_model_effort.items())
    }


def usage_bucket_json(
    key_name: str,
    buckets: dict[str, dict[str, Any]],
    session_buckets: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for key in sorted(buckets):
        bucket = buckets[key]
        usage = bucket.get("usage", Usage())
        by_model = bucket.get("by_model", {})
        by_model_effort = bucket.get("by_model_effort", {})
        message_events = bucket.get("message_events", MessageEvents())
        user_text = bucket.get("user_text", UserTextStats())
        sessions_for_bucket = (session_buckets or {}).get(key, {})
        rows.append(
            {
                key_name: key,
                "usage": usage.to_json(),
                "byModel": usage_json_by_model(by_model),
                "byModelEffort": usage_json_by_model_effort(by_model_effort),
                "events": int(bucket.get("events", 0)),
                "messageEvents": message_events.to_json(),
                "userText": user_text.to_json(),
                "sessionCount": len(sessions_for_bucket),
            }
        )
    return rows


def session_buckets_json(session_buckets: dict[str, dict[str, dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for key, session_map in session_buckets.items():
        rows = []
        for item in session_map.values():
            rows.append(
                {
                    "threadId": item["thread_id"],
                    "title": item["title"],
                    "cwd": item["cwd"],
                    "model": item["model"],
                    "effort": item["effort"],
                    "usage": item["usage"].to_json(),
                    "byModel": usage_json_by_model(item["by_model"]),
                    "byModelEffort": usage_json_by_model_effort(item["by_model_effort"]),
                    "events": item["events"],
                    "messageEvents": item["message_events"].to_json(),
                    "userText": item["user_text"].to_json(),
                    "firstEvent": item["first_event"],
                    "lastEvent": item["last_event"],
                }
            )
        rows.sort(key=lambda item: item["usage"]["total"], reverse=True)
        result[key] = rows
    return result


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    id_map: dict[str, str] = {}

    def redacted_id(raw_id: Any) -> str:
        key = str(raw_id or f"missing-{len(id_map) + 1}")
        if key not in id_map:
            id_map[key] = f"session-{len(id_map) + 1}"
        return id_map[key]

    def redacted_title(session_id: str) -> str:
        suffix = session_id.rsplit("-", 1)[-1]
        return f"Session {suffix}"

    def redact_row(row: dict[str, Any]) -> None:
        session_id = redacted_id(row.get("threadId"))
        row["threadId"] = session_id
        if "title" in row:
            row["title"] = redacted_title(session_id)
        if "cwd" in row:
            row["cwd"] = REDACTED_PATH
        if "path" in row:
            row["path"] = REDACTED_PATH
        if "source" in row:
            row["source"] = "redacted"

    meta = payload.get("meta")
    if isinstance(meta, dict):
        if "codexHome" in meta:
            meta["codexHome"] = "redacted"
        meta["redacted"] = True

    for row in payload.get("sessions", []):
        if isinstance(row, dict):
            redact_row(row)

    for bucket_name in ("dailySessions", "hourlySessions"):
        buckets = payload.get(bucket_name, {})
        if not isinstance(buckets, dict):
            continue
        for rows in buckets.values():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    redact_row(row)

    return payload


def project_name_from_path(value: Any) -> str:
    clean = str(value or "").strip().replace("\\", "/").rstrip("/")
    if not clean or clean == REDACTED_PATH or clean == "No cwd captured":
        return "No project captured"
    parts = [part for part in clean.split("/") if part and not re.fullmatch(r"[A-Za-z]:", part)]
    return parts[-1] if parts else "No project captured"


def apply_project_aliases(payload: dict[str, Any], aliases: dict[str, str]) -> None:
    if not aliases:
        return

    def alias_cwd(value: Any) -> Any:
        project_name = project_name_from_path(value)
        return aliases.get(alias_key(project_name), value)

    def apply_to_rows(rows: Any) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if isinstance(row, dict) and "cwd" in row:
                row["cwd"] = alias_cwd(row.get("cwd"))

    apply_to_rows(payload.get("sessions"))
    for bucket in (payload.get("dailySessions") or {}).values():
        apply_to_rows(bucket)
    for bucket in (payload.get("hourlySessions") or {}).values():
        apply_to_rows(bucket)
    for device_payload in (payload.get("devicePayloads") or {}).values():
        if isinstance(device_payload, dict):
            apply_project_aliases(device_payload, aliases)


def snapshot_session_id(device_id: str, raw_id: Any) -> str:
    digest = hashlib.sha256(f"{device_id}:{raw_id or ''}".encode("utf-8")).hexdigest()[:12]
    return f"session-{digest}"


def create_snapshot_payload(payload: dict[str, Any], device: SnapshotDevice) -> dict[str, Any]:
    snapshot = copy.deepcopy(payload)

    def sanitize_row(row: dict[str, Any]) -> None:
        session_id = snapshot_session_id(device.device_id, row.get("threadId"))
        row["threadId"] = session_id
        row["title"] = str(row.get("title") or "Untitled Codex session")
        if "cwd" in row:
            row["cwd"] = project_name_from_path(row.get("cwd"))
        row.pop("source", None)
        row.pop("path", None)
        row["deviceId"] = device.device_id
        row["deviceName"] = device.name

    meta = snapshot.setdefault("meta", {})
    if isinstance(meta, dict):
        meta["codexHome"] = "redacted"
        meta["redacted"] = True
        meta["schemaVersion"] = SNAPSHOT_SCHEMA_VERSION
        meta["privacyLevel"] = SNAPSHOT_PRIVACY_LEVEL
        meta["deviceId"] = device.device_id
        meta["deviceName"] = device.name
        meta["deviceSlug"] = device.slug
        meta["devices"] = [device.to_json()]

    for row in snapshot.get("daily", []):
        if isinstance(row, dict):
            row["deviceId"] = device.device_id
            row["deviceName"] = device.name
    for row in snapshot.get("hourly", []):
        if isinstance(row, dict):
            row["deviceId"] = device.device_id
            row["deviceName"] = device.name
    for row in snapshot.get("sessions", []):
        if isinstance(row, dict):
            sanitize_row(row)
    for bucket_name in ("dailySessions", "hourlySessions"):
        buckets = snapshot.get(bucket_name, {})
        if not isinstance(buckets, dict):
            continue
        for rows in buckets.values():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    sanitize_row(row)

    snapshot.pop("devicePayloads", None)
    return snapshot


def device_from_snapshot(payload: dict[str, Any]) -> SnapshotDevice | None:
    meta = payload.get("meta", {})
    if not isinstance(meta, dict):
        return None
    device_id = str(meta.get("deviceId") or "")
    device_name = str(meta.get("deviceName") or "")
    device_slug = str(meta.get("deviceSlug") or slugify(device_name or device_id))
    if not device_id or not device_name:
        return None
    return SnapshotDevice(device_id, device_name, device_slug)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def write_device_snapshot(snapshot_dir: Path, snapshot: dict[str, Any]) -> Path:
    device = device_from_snapshot(snapshot)
    if device is None:
        raise ValueError("snapshot is missing device metadata")
    meta = snapshot.setdefault("meta", {})
    if isinstance(meta, dict):
        meta.setdefault("schemaVersion", SNAPSHOT_SCHEMA_VERSION)
        meta.setdefault("privacyLevel", SNAPSHOT_PRIVACY_LEVEL)
        meta.setdefault("redacted", True)
        meta.setdefault("devices", [device.to_json()])
    device_dir = snapshot_dir / device.slug
    write_json_atomic(device_dir / "device.json", device.to_json())
    write_json_atomic(device_dir / "snapshot.json", snapshot)
    return device_dir / "snapshot.json"


def load_snapshot_payloads(snapshot_dir: Path) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    if not snapshot_dir.exists():
        return snapshots
    candidate_paths = list(snapshot_dir.glob("*/snapshot.json"))
    legacy_devices_dir = snapshot_dir / "devices"
    if legacy_devices_dir.exists():
        candidate_paths.extend(legacy_devices_dir.glob("*/snapshot.json"))
    seen: set[Path] = set()
    for path in sorted(candidate_paths):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        meta = payload.get("meta", {})
        if not isinstance(meta, dict):
            continue
        if meta.get("schemaVersion") != SNAPSHOT_SCHEMA_VERSION:
            continue
        if meta.get("privacyLevel") != SNAPSHOT_PRIVACY_LEVEL:
            continue
        if device_from_snapshot(payload) is None:
            continue
        snapshots.append(payload)
    return snapshots


def add_usage_json(target: dict[str, int], source: dict[str, Any] | None) -> None:
    source = source or {}
    for key in ("input", "cachedInput", "output", "reasoningOutput", "total"):
        target[key] = int(target.get(key, 0) or 0) + int(source.get(key, 0) or 0)


def add_message_events_json(target: dict[str, int], source: dict[str, Any] | None) -> None:
    source = source or {}
    for key in ("total", "user", "agent", "primaryAgent", "subagentAgent"):
        target[key] = int(target.get(key, 0) or 0) + int(source.get(key, 0) or 0)


def add_user_text_json(target: dict[str, int | float], source: dict[str, Any] | None) -> None:
    source = source or {}
    target["messages"] = int(target.get("messages", 0) or 0) + int(source.get("messages", 0) or 0)
    target["words"] = int(target.get("words", 0) or 0) + int(source.get("words", 0) or 0)
    messages = int(target.get("messages", 0) or 0)
    words = int(target.get("words", 0) or 0)
    target["avgWordsPerMessage"] = round(words / messages, 2) if messages else 0


def usage_zero_json() -> dict[str, int]:
    return {"input": 0, "cachedInput": 0, "output": 0, "reasoningOutput": 0, "total": 0}


def message_events_zero_json() -> dict[str, int]:
    return {"total": 0, "user": 0, "agent": 0, "primaryAgent": 0, "subagentAgent": 0}


def user_text_zero_json() -> dict[str, int | float]:
    return {"messages": 0, "words": 0, "avgWordsPerMessage": 0}


def add_by_model_json(target: dict[str, dict[str, int]], source: dict[str, Any] | None) -> None:
    for model, usage in (source or {}).items():
        target.setdefault(model, usage_zero_json())
        add_usage_json(target[model], usage)


def add_by_model_effort_json(target: dict[str, dict[str, dict[str, int]]], source: dict[str, Any] | None) -> None:
    for model, efforts in (source or {}).items():
        model_bucket = target.setdefault(model, {})
        for effort, usage in (efforts or {}).items():
            model_bucket.setdefault(effort, usage_zero_json())
            add_usage_json(model_bucket[effort], usage)


def aggregate_snapshot_rows(rows: list[dict[str, Any]], key_name: str) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get(key_name) or "")
        if not key:
            continue
        bucket = buckets.setdefault(
            key,
            {
                key_name: key,
                "usage": usage_zero_json(),
                "byModel": {},
                "byModelEffort": {},
                "events": 0,
                "messageEvents": message_events_zero_json(),
                "userText": user_text_zero_json(),
                "sessionCount": 0,
            },
        )
        add_usage_json(bucket["usage"], row.get("usage"))
        add_by_model_json(bucket["byModel"], row.get("byModel"))
        add_by_model_effort_json(bucket["byModelEffort"], row.get("byModelEffort"))
        add_message_events_json(bucket["messageEvents"], row.get("messageEvents"))
        add_user_text_json(bucket["userText"], row.get("userText"))
        bucket["events"] += int(row.get("events", 0) or 0)
        bucket["sessionCount"] += int(row.get("sessionCount", 0) or 0)
    return [buckets[key] for key in sorted(buckets)]


def combine_session_buckets(snapshots: list[dict[str, Any]], bucket_name: str) -> dict[str, list[dict[str, Any]]]:
    combined: dict[str, list[dict[str, Any]]] = {}
    for snapshot in snapshots:
        buckets = snapshot.get(bucket_name, {})
        if not isinstance(buckets, dict):
            continue
        for key, rows in buckets.items():
            if isinstance(rows, list):
                combined.setdefault(key, []).extend(copy.deepcopy([row for row in rows if isinstance(row, dict)]))
    for rows in combined.values():
        rows.sort(key=lambda item: item.get("usage", {}).get("total", 0), reverse=True)
    return dict(sorted(combined.items()))


def combine_snapshot_payloads(snapshots: list[dict[str, Any]], tz_name: str) -> dict[str, Any]:
    valid_snapshots = [snapshot for snapshot in snapshots if device_from_snapshot(snapshot) is not None]
    if not valid_snapshots:
        return build_empty_payload(tz_name)

    devices = [device_from_snapshot(snapshot) for snapshot in valid_snapshots]
    devices_json = [device.to_json() for device in devices if device is not None]
    first = valid_snapshots[0]
    pricing = copy.deepcopy(first.get("pricing", {"defaultModel": DEFAULT_MODEL, "models": MODEL_PRICES}))
    price_sources = copy.deepcopy(first.get("meta", {}).get("priceSources", []))

    totals_usage = usage_zero_json()
    totals_user_text = user_text_zero_json()
    totals_by_model: dict[str, dict[str, int]] = {}
    totals_by_model_effort: dict[str, dict[str, dict[str, int]]] = {}
    all_sessions: list[dict[str, Any]] = []
    all_daily_rows: list[dict[str, Any]] = []
    all_hourly_rows: list[dict[str, Any]] = []
    session_files = 0
    sessions_with_usage = 0

    for snapshot in valid_snapshots:
        totals = snapshot.get("totals", {})
        add_usage_json(totals_usage, totals.get("usage"))
        add_user_text_json(totals_user_text, totals.get("userText"))
        add_by_model_json(totals_by_model, totals.get("byModel"))
        add_by_model_effort_json(totals_by_model_effort, totals.get("byModelEffort"))
        all_sessions.extend(copy.deepcopy([row for row in snapshot.get("sessions", []) if isinstance(row, dict)]))
        all_daily_rows.extend(copy.deepcopy([row for row in snapshot.get("daily", []) if isinstance(row, dict)]))
        all_hourly_rows.extend(copy.deepcopy([row for row in snapshot.get("hourly", []) if isinstance(row, dict)]))
        meta = snapshot.get("meta", {})
        if isinstance(meta, dict):
            session_files += int(meta.get("sessionFiles", 0) or 0)
            sessions_with_usage += int(meta.get("sessionsWithUsage", 0) or 0)

    all_sessions.sort(key=lambda item: item.get("usage", {}).get("total", 0), reverse=True)
    default_model = pricing.get("defaultModel", DEFAULT_MODEL)
    models = pricing.get("models", MODEL_PRICES)
    cost_logged_mix = 0.0
    for model, usage in totals_by_model.items():
        rate = models.get(model) or models.get(default_model) or MODEL_PRICES.get(DEFAULT_MODEL, {})
        cached = min(int(usage.get("cachedInput", 0) or 0), int(usage.get("input", 0) or 0))
        uncached = max(0, int(usage.get("input", 0) or 0) - cached)
        cost_logged_mix += (
            uncached * float(rate.get("input", 0))
            + cached * float(rate.get("cached_input", 0))
            + int(usage.get("output", 0) or 0) * float(rate.get("output", 0))
        ) / 1_000_000

    generated_at = datetime.now(ZoneInfo(tz_name)).isoformat(timespec="seconds")
    return {
        "meta": {
            "generatedAt": generated_at,
            "timezone": tz_name,
            "codexHome": "synced snapshots",
            "sessionFiles": session_files,
            "sessionsWithUsage": sessions_with_usage,
            "priceSources": price_sources,
            "redacted": True,
            "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
            "privacyLevel": SNAPSHOT_PRIVACY_LEVEL,
            "devices": devices_json,
        },
        "pricing": pricing,
        "totals": {
            "usage": totals_usage,
            "userText": totals_user_text,
            "byModel": dict(sorted(totals_by_model.items())),
            "byModelEffort": dict(sorted(totals_by_model_effort.items())),
            "costLoggedMix": cost_logged_mix,
        },
        "hourly": aggregate_snapshot_rows(all_hourly_rows, "hour"),
        "daily": aggregate_snapshot_rows(all_daily_rows, "date"),
        "dailySessions": combine_session_buckets(valid_snapshots, "dailySessions"),
        "hourlySessions": combine_session_buckets(valid_snapshots, "hourlySessions"),
        "sessions": all_sessions,
        "devicePayloads": {
            str(snapshot["meta"]["deviceId"]): copy.deepcopy(snapshot)
            for snapshot in valid_snapshots
            if isinstance(snapshot.get("meta"), dict)
        },
    }


def build_empty_payload(tz_name: str) -> dict[str, Any]:
    return {
        "meta": {
            "generatedAt": datetime.now(ZoneInfo(tz_name)).isoformat(timespec="seconds"),
            "timezone": tz_name,
            "codexHome": "synced snapshots",
            "sessionFiles": 0,
            "sessionsWithUsage": 0,
            "priceSources": [],
            "redacted": True,
            "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
            "privacyLevel": SNAPSHOT_PRIVACY_LEVEL,
            "devices": [],
        },
        "pricing": {"defaultModel": DEFAULT_MODEL, "models": MODEL_PRICES},
        "totals": {"usage": usage_zero_json(), "userText": user_text_zero_json(), "byModel": {}, "byModelEffort": {}, "costLoggedMix": 0},
        "hourly": [],
        "daily": [],
        "dailySessions": {},
        "hourlySessions": {},
        "sessions": [],
        "devicePayloads": {},
    }


def build_payload(codex_home: Path, tz_name: str) -> dict[str, Any]:
    tz = ZoneInfo(tz_name)
    thread_meta = load_thread_meta(codex_home, tz)
    daily: dict[str, dict[str, Any]] = {}
    hourly: dict[str, dict[str, Any]] = {}
    daily_sessions: dict[str, dict[str, dict[str, Any]]] = {}
    hourly_sessions: dict[str, dict[str, dict[str, Any]]] = {}
    sessions: list[SessionAggregate] = []

    for path in iter_rollout_files(codex_home):
        thread_id = rollout_id(path)
        meta = thread_meta.get(thread_id)
        parsed = parse_rollout_file(path, meta, tz, daily, hourly, daily_sessions, hourly_sessions)
        if parsed:
            sessions.append(parsed)

    all_usage = Usage()
    all_user_text = UserTextStats()
    all_by_model: dict[str, Usage] = {}
    all_by_model_effort: dict[str, dict[str, Usage]] = {}
    for session in sessions:
        all_usage.add(session.usage)
        all_user_text.add(session.user_text)
        for model, usage in session.by_model.items():
            all_by_model.setdefault(model, Usage()).add(usage)
        for model, efforts in session.by_model_effort.items():
            effort_bucket = all_by_model_effort.setdefault(model, {})
            for effort, usage in efforts.items():
                effort_bucket.setdefault(effort, Usage()).add(usage)

    dates = fill_date_range(daily)
    daily_json = []
    for date in dates:
        bucket = daily.get(date, {})
        usage = bucket.get("usage", Usage())
        by_model = bucket.get("by_model", {})
        by_model_effort = bucket.get("by_model_effort", {})
        message_events = bucket.get("message_events", MessageEvents())
        user_text = bucket.get("user_text", UserTextStats())
        sessions_for_day = daily_sessions.get(date, {})
        daily_json.append(
            {
                "date": date,
                "usage": usage.to_json(),
                "byModel": usage_json_by_model(by_model),
                "byModelEffort": usage_json_by_model_effort(by_model_effort),
                "events": int(bucket.get("events", 0)),
                "messageEvents": message_events.to_json(),
                "userText": user_text.to_json(),
                "sessionCount": len(sessions_for_day),
            }
        )

    daily_session_json = session_buckets_json(daily_sessions)
    hourly_session_json = session_buckets_json(hourly_sessions)

    session_json = []
    for session in sessions:
        session_json.append(
            {
                "threadId": session.thread_id,
                "title": session.title,
                "cwd": session.cwd,
                "source": session.source,
                "isSubagent": is_subagent_source(session.source),
                "model": normalize_model(session.model),
                "effort": normalize_reasoning_effort(session.reasoning_effort),
                "firstSeen": session.first_seen,
                "lastSeen": session.last_seen,
                "path": session.path,
                "usage": session.usage.to_json(),
                "byModel": usage_json_by_model(session.by_model),
                "byModelEffort": usage_json_by_model_effort(session.by_model_effort),
                "days": sorted(session.days),
                "events": session.event_count,
                "messageEvents": session.message_events.to_json(),
                "userText": session.user_text.to_json(),
            }
        )
    session_json.sort(key=lambda item: item["usage"]["total"], reverse=True)

    generated_at = datetime.now(tz).isoformat(timespec="seconds")
    return {
        "meta": {
            "generatedAt": generated_at,
            "timezone": tz_name,
            "codexHome": str(codex_home),
            "sessionFiles": len(iter_rollout_files(codex_home)),
            "sessionsWithUsage": len(sessions),
            "priceSources": [
                {
                    "label": "OpenAI API pricing",
                    "url": "https://openai.com/api/pricing/",
                    "note": "GPT-5.5, GPT-5.4 and GPT-5.4 mini standard text-token pricing.",
                },
                {
                    "label": "OpenAI GPT-5.2 model docs",
                    "url": "https://platform.openai.com/docs/models/gpt-5.2/",
                    "note": "GPT-5.2 text-token pricing.",
                },
                {
                    "label": "OpenAI GPT-5 model docs",
                    "url": "https://platform.openai.com/docs/models/gpt-5/",
                    "note": "GPT-5 text-token pricing.",
                },
            ],
        },
        "pricing": {
            "defaultModel": DEFAULT_MODEL,
            "models": MODEL_PRICES,
        },
        "totals": {
            "usage": all_usage.to_json(),
            "userText": all_user_text.to_json(),
            "byModel": usage_json_by_model(all_by_model),
            "byModelEffort": usage_json_by_model_effort(all_by_model_effort),
            "costLoggedMix": sum(usage_cost(usage, model) for model, usage in all_by_model.items()),
        },
        "hourly": usage_bucket_json("hour", hourly, hourly_sessions),
        "daily": daily_json,
        "dailySessions": daily_session_json,
        "hourlySessions": hourly_session_json,
        "sessions": session_json,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex Analytics Dashboard</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%231f2528'/%3E%3Crect x='16' y='30' width='8' height='18' rx='4' fill='%232f7f79'/%3E%3Crect x='28' y='18' width='8' height='30' rx='4' fill='%23b98525'/%3E%3Crect x='40' y='25' width='8' height='23' rx='4' fill='%23c65f46'/%3E%3Ccircle cx='44' cy='16' r='4' fill='%238a6fbd'/%3E%3C/svg%3E">
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f4;
      --panel: #ffffff;
      --panel-soft: #f0f3ef;
      --ink: #1f2528;
      --muted: #687174;
      --line: #d9dfd8;
      --input: #2f7f79;
      --output: #c65f46;
      --cached: #8a6fbd;
      --reasoning: #496fa8;
      --accent: #b98525;
      --good: #20865f;
      --shadow: 0 8px 24px rgba(37, 45, 40, 0.08);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 16px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
    }
    button, select, input {
      font: inherit;
    }
    .shell {
      width: min(1440px, calc(100% - 32px));
      margin: 0 auto;
      padding: 28px 0 40px;
    }
    header {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 24px;
      align-items: end;
      margin-bottom: 18px;
    }
    h1 {
      margin: 0;
      font-size: clamp(30px, 4vw, 54px);
      line-height: 1;
      letter-spacing: 0;
    }
    h2 {
      margin: 0 0 12px;
      font-size: 18px;
      line-height: 1.2;
      letter-spacing: 0;
    }
    h3 {
      margin: 0;
      font-size: 15px;
      letter-spacing: 0;
    }
    .subtitle {
      margin: 10px 0 0;
      color: var(--muted);
      max-width: 760px;
      line-height: 1.45;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      justify-content: flex-end;
      align-items: center;
    }
    .control {
      display: flex;
      gap: 8px;
      align-items: center;
      padding: 8px 10px;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      min-height: 42px;
    }
    .control label {
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }
    select {
      border: 0;
      color: var(--ink);
      background: transparent;
      min-width: 110px;
      outline: none;
    }
    input[type="date"] {
      border: 0;
      color: var(--ink);
      background: transparent;
      min-width: 128px;
      outline: none;
    }
    .period-control {
      position: relative;
      gap: 4px;
    }
    .period-arrows {
      display: inline-flex;
      gap: 2px;
    }
    .period-button {
      width: 30px;
      height: 30px;
      border: 0;
      border-radius: 6px;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      font-weight: 800;
      line-height: 1;
    }
    .period-button svg {
      width: 16px;
      min-width: 0;
      height: 16px;
      flex: none;
      display: block;
      margin: 0 auto;
      stroke: currentColor;
    }
    .period-button:hover {
      background: var(--panel-soft);
      color: var(--ink);
    }
    .period-button:disabled {
      opacity: 0.35;
      cursor: default;
    }
    .period-button:disabled:hover {
      background: transparent;
      color: var(--muted);
    }
    .period-date-input {
      position: absolute;
      width: 1px;
      min-width: 0;
      height: 1px;
      opacity: 0;
      pointer-events: none;
    }
    .segmented {
      display: inline-flex;
      gap: 2px;
      padding: 3px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
      flex-shrink: 0;
    }
    .segmented button {
      border: 0;
      border-radius: 6px;
      background: transparent;
      color: var(--muted);
      min-height: 32px;
      padding: 0 10px;
      cursor: pointer;
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .segmented button.active {
      background: var(--panel);
      color: var(--ink);
      box-shadow: 0 1px 3px rgba(31, 37, 40, 0.12);
    }
    .grid {
      display: grid;
      gap: 14px;
    }
    .stat-grid {
      grid-template-columns: repeat(6, minmax(0, 1fr));
      margin-bottom: 14px;
    }
    .stat {
      min-height: 118px;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 10px;
    }
    .stat .label {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.25;
    }
    .stat .value {
      font-size: clamp(22px, 2.6vw, 34px);
      line-height: 1;
      font-weight: 780;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }
    .stat .hint {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.25;
    }
    .main-grid {
      grid-template-columns: minmax(0, 1.35fr) minmax(360px, 0.65fr);
      align-items: start;
    }
    .top-stack,
    .details,
    .wide-panel {
      min-width: 0;
    }
    .wide-panel {
      grid-column: 1 / -1;
    }
    .bottom-insights {
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: minmax(320px, 0.43fr) minmax(0, 1fr);
      gap: 14px;
      align-items: start;
      min-width: 0;
    }
    .bottom-insights .panel {
      min-width: 0;
      min-height: 0;
    }
    .top-sessions-panel {
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .details {
      align-self: stretch;
      display: grid;
      gap: 14px;
      grid-template-rows: auto minmax(0, 1fr);
      min-height: 0;
      overflow: hidden;
    }
    .details .panel {
      min-height: 0;
    }
    .details .panel:last-child {
      display: flex;
      flex-direction: column;
      min-height: 0;
      overflow: hidden;
    }
    .details .panel:last-child .table-wrap {
      flex: 1;
      max-height: none;
      min-height: 0;
      overflow: auto;
    }
    .panel {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
      padding: 16px;
      overflow: hidden;
    }
    .panel-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 8px;
    }
    .chart-controls {
      display: flex;
      justify-content: flex-end;
      align-items: stretch;
      gap: 8px;
      flex-wrap: nowrap;
      overflow-x: auto;
      padding-bottom: 1px;
    }
    .chart-controls .control,
    .chart-controls .segmented {
      min-height: 38px;
      flex: 0 0 auto;
    }
    .chart-controls .control {
      padding: 3px 7px;
    }
    .chart-controls .segmented button {
      min-height: 30px;
    }
    .legend {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
      align-items: center;
    }
    .dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      display: inline-block;
      margin-right: 5px;
      vertical-align: -1px;
    }
    .chart-wrap {
      width: 100%;
      min-height: 330px;
      overflow-x: auto;
      padding-bottom: 4px;
    }
    svg {
      display: block;
      width: 100%;
      min-width: 720px;
      height: auto;
    }
    .bar-input { fill: var(--input); cursor: pointer; }
    .bar-output { fill: var(--output); cursor: pointer; }
    .bar-total {
      fill: var(--accent);
      cursor: pointer;
      transition: filter 140ms ease, opacity 140ms ease;
    }
    .bar-total.selected {
      filter: drop-shadow(0 0 7px rgba(184, 139, 54, 0.72)) drop-shadow(0 0 16px rgba(184, 139, 54, 0.32));
      opacity: 1;
    }
    .cost-bar {
      cursor: pointer;
      transition: filter 140ms ease, opacity 140ms ease;
    }
    .cost-bar.input { fill: var(--input); }
    .cost-bar.cached { fill: var(--cached); }
    .cost-bar.output { fill: var(--output); }
    .cost-bar.selected {
      filter: drop-shadow(0 0 7px rgba(47, 127, 121, 0.42));
    }
    .chart-hitbox {
      fill: transparent;
      cursor: pointer;
    }
    .chart-hitbox:hover + .chart-hover-band {
      opacity: 1;
    }
    .chart-hover-band {
      fill: rgba(31, 37, 40, 0.05);
      opacity: 0;
      pointer-events: none;
    }
    .ratio-line {
      fill: none;
      stroke: var(--output);
      stroke-width: 3;
      stroke-linejoin: round;
      stroke-linecap: round;
    }
    .ratio-area {
      fill: rgba(198, 95, 70, 0.12);
    }
    .ratio-dot {
      fill: var(--panel);
      stroke: var(--output);
      stroke-width: 2;
      cursor: pointer;
    }
    .bar-muted { fill: #e6ebe5; }
    .axis { stroke: var(--line); stroke-width: 1; }
    .axis-label { fill: var(--muted); font-size: 12px; }
    .axis-label.input-axis { fill: var(--input); font-weight: 700; }
    .axis-label.output-axis { fill: var(--output); font-weight: 700; }
    .chart-value-label {
      fill: var(--ink);
      font-size: 11px;
      font-weight: 800;
      pointer-events: none;
      paint-order: stroke;
      stroke: rgba(255, 255, 255, 0.88);
      stroke-width: 3px;
      stroke-linejoin: round;
    }
    .chart-value-label.total {
      font-size: 10.5px;
    }
    .chart-value-label.in-bar {
      fill: #ffffff;
      stroke: rgba(31, 37, 40, 0.28);
    }
    .chart-value-label.vertical {
      font-size: 11px;
      letter-spacing: 0;
    }
    .chart-value-label.ratio {
      fill: var(--output);
    }
    .axis-title {
      font-size: 12px;
      font-weight: 750;
      letter-spacing: 0;
    }
    .axis-title.input-axis { fill: var(--input); }
    .axis-title.output-axis { fill: var(--output); }
    .selected-marker {
      fill: rgba(184, 139, 54, 0.13);
      stroke: rgba(184, 139, 54, 0.36);
      stroke-width: 1;
      pointer-events: none;
    }
    .summary-strip {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 12px;
    }
    .summary-strip.three-up {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
    .mini {
      position: relative;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
      padding: 10px;
      min-height: 70px;
      display: flex;
      flex-direction: column;
    }
    .mini.has-help {
      cursor: help;
    }
    .mini.has-help:focus-visible {
      outline: 2px solid var(--ink);
      outline-offset: 2px;
    }
    .mini.has-help::after {
      content: "?";
      position: absolute;
      top: 8px;
      right: 8px;
      width: 17px;
      height: 17px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      opacity: 0;
      transition: opacity 120ms ease;
    }
    .mini.has-help:hover::after,
    .mini.has-help:focus-visible::after {
      opacity: 1;
    }
    .mini span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
      padding-right: 20px;
    }
    .mini strong {
      display: block;
      font-size: clamp(16px, 1.8vw, 18px);
      line-height: 1.12;
      margin-top: auto;
      white-space: nowrap;
    }
    .heatmap-tools {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    .heatmap-scroll {
      overflow-x: auto;
      padding: 6px 0 8px;
      scrollbar-width: none;
      -ms-overflow-style: none;
    }
    .heatmap-scroll::-webkit-scrollbar {
      display: none;
    }
    .heatmap-shell {
      position: relative;
    }
    .heatmap {
      display: grid;
      grid-template-columns: 34px repeat(var(--heat-weeks, 52), 18px);
      grid-template-rows: 14px 20px repeat(7, 18px);
      gap: 5px;
      width: max-content;
      min-height: 200px;
      padding-right: 4px;
      align-items: center;
    }
    .heatmap-corner,
    .heatmap-week-heading,
    .heatmap-week,
    .heatmap-day-label {
      color: var(--muted);
      font-size: 11px;
      line-height: 1;
      white-space: nowrap;
      user-select: none;
    }
    .heatmap-corner {
      height: 14px;
    }
    .heatmap-week-heading {
      grid-column: 2 / -1;
      text-align: center;
      font-weight: 800;
      align-self: end;
    }
    .heatmap-week-row-spacer {
      height: 20px;
    }
    .heatmap-day-label {
      text-align: right;
      padding-right: 4px;
      font-weight: 650;
    }
    .heatmap-week {
      text-align: center;
      width: 18px;
      height: 20px;
      justify-self: center;
      align-self: end;
      font-size: 10px;
      font-weight: 700;
    }
    .heatmap-week.current {
      color: var(--ink);
      background: var(--panel-soft);
      border: 1px solid var(--line);
      border-radius: 4px;
      display: grid;
      place-items: center;
    }
    .heat-cell {
      width: 18px;
      height: 18px;
      border: 1px solid rgba(31, 37, 40, 0.08);
      border-radius: 3px;
      cursor: pointer;
      background: #e7ebe5;
      padding: 0;
    }
    .heat-cell.active {
      outline: 2px solid var(--ink);
      outline-offset: 1px;
    }
    .heat-cell.empty {
      opacity: 0.55;
    }
    .heat-cell.out-year {
      opacity: 0.22;
      cursor: default;
      pointer-events: none;
    }
    .heatmap-pan {
      position: relative;
      width: 100%;
      height: 18px;
      margin-top: 10px;
      border: 1px solid rgba(31, 37, 40, 0.06);
      border-radius: 999px;
      background: #d8ded8;
      cursor: pointer;
      touch-action: none;
    }
    .heatmap-pan[hidden] {
      display: none;
    }
    .heatmap-pan:focus-visible {
      outline: 2px solid var(--input);
      outline-offset: 4px;
      border-radius: 999px;
    }
    .heatmap-pan-thumb {
      position: absolute;
      top: 2px;
      left: 0;
      width: var(--heat-pan-thumb-width, 100%);
      height: 12px;
      transform: translateX(var(--heat-pan-thumb-left, 0px));
      border: 2px solid var(--panel);
      border-radius: 999px;
      background: #6f837b;
      box-shadow: 0 1px 4px rgba(31, 37, 40, 0.18);
      pointer-events: none;
    }
    .heatmap-pan.dragging .heatmap-pan-thumb {
      background: #566d64;
      box-shadow: 0 2px 8px rgba(31, 37, 40, 0.22);
    }
    .heat-tooltip {
      position: fixed;
      z-index: 20;
      min-width: 190px;
      max-width: 260px;
      pointer-events: none;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.98);
      box-shadow: 0 12px 30px rgba(31, 37, 40, 0.18);
      padding: 10px;
      color: var(--ink);
      opacity: 0;
      transform: translate(-50%, -112%);
      transition: opacity 120ms ease;
    }
    .heat-tooltip.visible {
      opacity: 1;
    }
    .kpi-tooltip {
      position: fixed;
      z-index: 24;
      min-width: 220px;
      max-width: 300px;
      pointer-events: none;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.98);
      box-shadow: 0 12px 30px rgba(31, 37, 40, 0.18);
      padding: 10px;
      color: var(--ink);
      opacity: 0;
      transform: translate(-50%, -112%);
      transition: opacity 120ms ease;
    }
    .kpi-tooltip.visible {
      opacity: 1;
    }
    .kpi-tooltip .tooltip-title {
      font-weight: 780;
      margin-bottom: 5px;
    }
    .kpi-tooltip .tooltip-body {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .kpi-tooltip .tooltip-detail {
      margin-top: 10px;
      padding-top: 9px;
      border-top: 1px solid var(--line);
      color: var(--ink);
      font-size: 12px;
      line-height: 1.45;
    }
    .heat-tooltip .tooltip-date {
      font-weight: 780;
      margin-bottom: 8px;
    }
    .heat-tooltip dl {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 6px 12px;
      margin: 0;
      font-size: 12px;
    }
    .heat-tooltip dt {
      color: var(--muted);
    }
    .heat-tooltip dd {
      margin: 0;
      font-weight: 700;
      text-align: right;
    }
    .heat-legend {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      color: var(--muted);
      font-size: 12px;
      margin-top: 10px;
    }
    .scale {
      display: flex;
      gap: 4px;
      align-items: center;
    }
    .scale i {
      display: block;
      width: 16px;
      height: 10px;
      border-radius: 2px;
      border: 1px solid rgba(31, 37, 40, 0.08);
    }
    .details {
      display: grid;
      gap: 14px;
    }
    .day-title {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      margin-bottom: 12px;
    }
    .day-title .date {
      font-size: 26px;
      font-weight: 780;
      letter-spacing: 0;
    }
    .split-meter {
      display: grid;
      grid-template-columns: minmax(10px, var(--input-share, 1fr)) minmax(10px, var(--output-share, 1fr));
      height: 14px;
      border-radius: 99px;
      overflow: hidden;
      background: var(--panel-soft);
      border: 1px solid var(--line);
      margin: 12px 0;
    }
    .split-meter .input { background: var(--input); }
    .split-meter .output { background: var(--output); }
    .table-wrap {
      overflow: auto;
      max-height: 520px;
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 680px;
      background: var(--panel);
    }
    .model-table {
      min-width: 1180px;
    }
    .model-toggle {
      border: 0;
      background: transparent;
      color: var(--ink);
      cursor: pointer;
      font: inherit;
      font-weight: 700;
      padding: 0;
      text-align: left;
    }
    .model-toggle:hover {
      text-decoration: underline;
      text-underline-offset: 3px;
    }
    .model-toggle::before {
      content: "+";
      display: inline-block;
      width: 14px;
      color: var(--muted);
      font-weight: 800;
    }
    .model-toggle[aria-expanded="true"]::before {
      content: "-";
    }
    .model-effort-row td {
      background: rgba(240, 243, 239, 0.55);
    }
    .model-effort-name {
      padding-left: 18px;
    }
    th, td {
      padding: 10px 12px;
      text-align: right;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
      vertical-align: top;
    }
    th:first-child, td:first-child {
      text-align: left;
      width: 34%;
    }
    .model-table th:first-child,
    .model-table td:first-child {
      width: 24%;
    }
    th {
      position: sticky;
      top: 0;
      background: var(--panel-soft);
      z-index: 1;
      color: var(--muted);
      font-weight: 650;
    }
    tr:last-child td { border-bottom: 0; }
    .session-title {
      font-weight: 700;
      color: var(--ink);
      line-height: 1.25;
      max-width: 460px;
      overflow-wrap: anywhere;
    }
    .session-title-toggle {
      width: 100%;
      min-width: 0;
      border: 0;
      background: transparent;
      color: inherit;
      cursor: pointer;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 18px;
      gap: 6px;
      align-items: start;
      padding: 0;
      text-align: left;
      font: inherit;
      font-weight: inherit;
    }
    .session-title-toggle::after {
      content: "v";
      display: grid;
      place-items: center;
      width: 18px;
      height: 18px;
      border: 1px solid var(--line);
      border-radius: 50%;
      color: var(--muted);
      font-size: 10px;
      font-weight: 800;
      line-height: 1;
      transition: transform 140ms ease;
    }
    .session-title-toggle:hover .session-title-text {
      text-decoration: underline;
      text-underline-offset: 3px;
    }
    .session-title-toggle:focus-visible {
      outline: 2px solid var(--ink);
      outline-offset: 2px;
      border-radius: 4px;
    }
    .session-title-toggle.session-title-expanded::after {
      transform: rotate(180deg);
    }
    .session-title-text {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .session-title-toggle.session-title-expanded .session-title-text {
      overflow: visible;
      overflow-wrap: anywhere;
      text-overflow: clip;
      white-space: normal;
    }
    .session-sub {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      margin-top: 4px;
      max-width: 460px;
      overflow-wrap: anywhere;
    }
    .top-list {
      display: grid;
      gap: 10px;
      max-height: 520px;
      overflow: auto;
      padding-right: 4px;
    }
    .top-sessions-panel .top-list {
      flex: 1;
      max-height: none;
      min-height: 0;
    }
    .top-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
      padding: 12px;
    }
    .top-sessions-panel .top-item {
      padding: 10px;
    }
    .top-item h3 {
      line-height: 1.25;
      overflow-wrap: anywhere;
    }
    .top-sessions-panel .top-item h3 {
      font-size: 13px;
    }
    .top-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      margin-top: 8px;
    }
    .top-sessions-panel .top-meta {
      gap: 6px;
    }
    .progress {
      height: 8px;
      border-radius: 99px;
      overflow: hidden;
      background: #e0e6df;
      margin-top: 10px;
      display: flex;
    }
    .progress-fill {
      display: flex;
      height: 100%;
    }
    .progress .input { background: var(--input); }
    .progress .output { background: var(--output); }
    .project-bars {
      display: grid;
      gap: 10px;
      min-height: 0;
      align-content: start;
    }
    .project-bar-row {
      display: grid;
      grid-template-columns: minmax(150px, 240px) minmax(0, 1fr) minmax(110px, auto);
      gap: 12px;
      align-items: center;
      min-width: 0;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
    }
    .project-name {
      font-weight: 750;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }
    .project-path {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.25;
      margin-top: 4px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .project-track {
      position: relative;
      height: 18px;
      border-radius: 99px;
      overflow: hidden;
      background: #e0e6df;
    }
    .project-fill {
      height: 100%;
      min-width: 2px;
      border-radius: 99px;
      background: linear-gradient(90deg, var(--input), var(--accent));
    }
    .project-metrics {
      text-align: right;
      white-space: nowrap;
    }
    .project-metrics strong {
      display: block;
      line-height: 1.1;
    }
    .project-metrics span {
      display: block;
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
    }
    footer {
      margin-top: 20px;
      color: var(--muted);
      line-height: 1.45;
      font-size: 13px;
    }
    footer a {
      color: var(--ink);
      text-decoration: underline;
      text-decoration-thickness: 1px;
      text-underline-offset: 3px;
    }
    .empty-state {
      padding: 24px;
      color: var(--muted);
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
    }
    .no-data-state {
      margin-bottom: 14px;
    }
    .no-data-state[hidden] {
      display: none;
    }
    .no-data-state h2 {
      margin-bottom: 8px;
      color: var(--ink);
    }
    .no-data-state p {
      max-width: 820px;
      margin: 0 0 10px;
      line-height: 1.45;
    }
    .no-data-state code {
      color: var(--ink);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    }
    @media (max-width: 1120px) {
      .stat-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .main-grid { grid-template-columns: 1fr; }
      .wide-panel { grid-column: auto; }
      .bottom-insights {
        grid-column: auto;
        grid-template-columns: 1fr;
      }
      .details { height: auto !important; }
      header { grid-template-columns: 1fr; }
      .toolbar { justify-content: flex-start; }
    }
    @media (max-width: 720px) {
      .shell {
        width: min(100% - 20px, 1440px);
        padding-top: 18px;
      }
      .stat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .summary-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .summary-strip.three-up { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .stat { min-height: 104px; }
      .control {
        width: 100%;
        justify-content: space-between;
      }
      .panel-head {
        align-items: stretch;
        flex-direction: column;
      }
      .chart-controls { width: 100%; }
      .segmented {
        width: auto;
      }
      .segmented button {
        flex: 1;
      }
      .period-button {
        flex: 0 0 30px;
      }
      .project-bar-row {
        grid-template-columns: 1fr;
        gap: 8px;
      }
      .project-metrics {
        text-align: left;
      }
      select { min-width: 150px; }
      svg { min-width: 620px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div>
        <h1>Codex Analytics Dashboard</h1>
        <p class="subtitle">Daily token momentum from local Codex logs. Cost is a what-if API estimate, not your subscription billing.</p>
      </div>
      <div class="toolbar">
        <div class="control">
          <label for="deviceSelect">Device</label>
          <select id="deviceSelect"></select>
        </div>
        <div class="control">
          <label for="timezoneSelect">Timezone</label>
          <select id="timezoneSelect"></select>
        </div>
        <div class="control">
          <label for="rangeSelect">Range</label>
          <select id="rangeSelect">
            <option value="30">Last 30 days</option>
            <option value="90">Last 90 days</option>
            <option value="all">All time</option>
          </select>
        </div>
        <div class="control">
          <label for="priceSelect">Cost basis</label>
          <select id="priceSelect"></select>
        </div>
      </div>
    </header>

    <section class="empty-state no-data-state" id="noDataState" hidden>
      <h2>No Codex usage data found</h2>
      <p>This dashboard reads local Codex logs from <code id="noDataCodexHome"></code>. Start a Codex session, then refresh this localhost page to regenerate the dashboard.</p>
      <p>Quick start: <code>npx codex-analytics-dashboard@latest</code></p>
    </section>

    <section class="grid stat-grid" id="statGrid"></section>

    <section class="grid main-grid">
      <div class="grid top-stack">
        <section class="panel">
          <div class="panel-head">
            <div>
              <h2 id="mainChartTitle">Daily Total Tokens</h2>
              <div class="legend" id="mainChartLegend">
                <span><i class="dot" style="background: var(--accent)"></i>Total Tokens</span>
                <span><i class="dot" style="background: var(--cached)"></i>Total Tokens = Real Input + Cached Input + Output</span>
              </div>
            </div>
            <div class="chart-controls">
              <div class="control">
                <label for="chartResolution">View</label>
                <select id="chartResolution">
                  <option value="hour">24 Hours</option>
                  <option value="day" selected>7 Days</option>
                  <option value="week">14 Weeks</option>
                  <option value="month">12 Months</option>
                </select>
              </div>
              <div class="control period-control">
                <label for="chartWindowDate">Start</label>
                <span class="period-arrows">
                  <button type="button" class="period-button" id="chartPeriodPrev" aria-label="Previous period" title="Previous period">&lt;</button>
                  <button type="button" class="period-button" id="chartPeriodNext" aria-label="Next period" title="Next period">&gt;</button>
                </span>
                <button type="button" class="period-button period-calendar-button" id="chartCalendarButton" aria-label="Choose start date" title="Choose start date">
                  <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M8 2v4"></path>
                    <path d="M16 2v4"></path>
                    <rect x="3" y="4" width="18" height="18" rx="2"></rect>
                    <path d="M3 10h18"></path>
                  </svg>
                </button>
                <input id="chartWindowDate" class="period-date-input" type="date" aria-label="Start date for visible period" tabindex="-1">
              </div>
              <div class="control">
                <label for="chartMetric">Metric</label>
                <select id="chartMetric">
                  <option value="total">Total Tokens</option>
                  <option value="sessions">Sessions</option>
                  <option value="cost">Total Cost</option>
                  <option value="outputRatio">Output Ratio</option>
                  <option value="input">Real Input Tokens</option>
                  <option value="cachedInput">Cached Input Tokens</option>
                  <option value="totalInput">Total Input Tokens</option>
                  <option value="cacheShare">Cache Share</option>
                  <option value="output">Output Tokens</option>
                  <option value="reasoning">Reasoning Tokens</option>
                  <option value="messages">Total Messages</option>
                  <option value="events">Events</option>
                  <option value="inputCost">Real Input Cost</option>
                  <option value="cachedInputCost">Cached Input Cost</option>
                  <option value="outputCost">Output Cost</option>
                  <option value="costBreakdown">Cost Split</option>
                </select>
              </div>
            </div>
          </div>
          <div class="chart-wrap" id="barChart"></div>
          <div class="summary-strip" id="rangeSummary"></div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <h2>Daily Heatmap</h2>
            <div class="heatmap-tools">
              <div class="control">
                <label for="heatYear">Year</label>
                <select id="heatYear"></select>
              </div>
              <div class="control">
                <label for="heatMetric">Metric</label>
                <select id="heatMetric">
                  <option value="total">Total Tokens</option>
                  <option value="input">Real Input Tokens</option>
                  <option value="totalInput">Total Input Tokens</option>
                  <option value="output">Output Tokens</option>
                  <option value="cost">API cost estimate</option>
                  <option value="sessions">Sessions</option>
                  <option value="messages">Messages</option>
                  <option value="events">Events</option>
                </select>
              </div>
            </div>
          </div>
          <div class="heatmap-scroll">
            <div class="heatmap-shell">
              <div class="heatmap" id="heatmap"></div>
              <div class="heat-tooltip" id="heatTooltip" role="tooltip"></div>
              <div class="kpi-tooltip" id="kpiTooltip" role="tooltip"></div>
            </div>
          </div>
          <div class="heatmap-pan" id="heatmapPan" role="scrollbar" aria-controls="heatmap" aria-label="Heatmap horizontal position" aria-orientation="horizontal" aria-valuemin="0" aria-valuemax="0" aria-valuenow="0" tabindex="0" hidden>
            <div class="heatmap-pan-thumb" id="heatmapPanThumb"></div>
          </div>
          <div class="heat-legend">
            <span id="heatmapCaption"></span>
            <span class="scale" id="heatmapScale"></span>
          </div>
        </section>
      </div>

      <aside class="details">
        <section class="panel" id="dayDetails"></section>
        <section class="panel">
          <div class="panel-head">
            <h2 id="sessionTableTitle">Sessions In Selected Day</h2>
          </div>
          <div class="table-wrap" id="sessionTable"></div>
        </section>
      </aside>

      <section class="panel wide-panel">
        <div class="panel-head">
          <div>
            <h2>Usage By Model</h2>
            <div class="legend">Token counts are assigned from Codex turn/session metadata.</div>
          </div>
          <div class="legend"><span id="modelBreakdownCaption"></span></div>
        </div>
        <div class="table-wrap" id="modelBreakdown"></div>
      </section>

      <div class="bottom-insights">
        <section class="panel top-sessions-panel">
          <div class="panel-head">
            <h2>Top Sessions</h2>
            <div class="legend"><span id="topSessionsCaption"></span></div>
          </div>
          <div class="top-list" id="topSessions"></div>
        </section>

        <section class="panel top-projects-panel">
          <div class="panel-head">
            <div>
              <h2>Top Projects</h2>
            </div>
            <div class="legend"><span id="topProjectsCaption"></span></div>
          </div>
          <div class="project-bars" id="topProjects"></div>
        </section>
      </div>
    </section>

    <footer id="footer"></footer>
  </div>

  <script>
    const ROOT_DATA = __DATA_JSON__;
    let DATA = ROOT_DATA;
    const state = {
      device: "all",
      range: "30",
      price: "logged",
      chartMode: "total",
      chartResolution: "day",
      chartWindowStart: {
        hour: null,
        day: null,
        week: null,
        month: null
      },
      heatMetric: "total",
      heatYear: null,
      heatScrollLeft: 0,
      selectedChartKey: null,
      selectedDate: null
    };

    let byDate = new Map();
    let byHour = new Map();
    let allDates = [];
    const expandedModelRows = new Set();
    const HEATMAP_TOKEN_FULL_SCALE = 250_000_000;
    const HEATMAP_TOKEN_METRICS = new Set(["total", "input", "totalInput", "output"]);
    const HEATMAP_EMPTY_COLOR = "#e7ebe5";
    const HEATMAP_COLOR_STEPS = 7;
    const HEATMAP_SCALE_STEPS = [0, 1/7, 2/7, 3/7, 4/7, 5/7, 6/7, 1];
    const HEATMAP_PROJECT_GRADIENT = [
      [0.00, [47, 127, 121]],
      [1.00, [185, 133, 37]],
    ];
    const TIMEZONE_OPTIONS = [
      ["Pacific/Pago_Pago", "UTC-11 - Pago Pago"],
      ["Pacific/Honolulu", "UTC-10 - Honolulu"],
      ["America/Anchorage", "UTC-09 - Anchorage"],
      ["America/Los_Angeles", "UTC-08 - Los Angeles"],
      ["America/Denver", "UTC-07 - Denver"],
      ["America/Chicago", "UTC-06 - Chicago"],
      ["America/New_York", "UTC-05 - New York"],
      ["America/Halifax", "UTC-04 - Halifax"],
      ["America/Sao_Paulo", "UTC-03 - Sao Paulo"],
      ["Atlantic/South_Georgia", "UTC-02 - South Georgia"],
      ["Atlantic/Azores", "UTC-01 - Azores"],
      ["UTC", "UTC - Coordinated Universal Time"],
      ["Europe/London", "UTC+00 - London"],
      ["Europe/Berlin", "UTC+01 - Berlin"],
      ["Europe/Athens", "UTC+02 - Athens"],
      ["Europe/Moscow", "UTC+03 - Moscow"],
      ["Asia/Dubai", "UTC+04 - Dubai"],
      ["Asia/Karachi", "UTC+05 - Karachi"],
      ["Asia/Dhaka", "UTC+06 - Dhaka"],
      ["Asia/Bangkok", "UTC+07 - Bangkok"],
      ["Asia/Shanghai", "UTC+08 - Shanghai"],
      ["Asia/Tokyo", "UTC+09 - Tokyo"],
      ["Australia/Sydney", "UTC+10 - Sydney"],
      ["Pacific/Noumea", "UTC+11 - Noumea"],
      ["Pacific/Auckland", "UTC+12 - Auckland"],
      ["Pacific/Apia", "UTC+13 - Apia"],
      ["Pacific/Kiritimati", "UTC+14 - Kiritimati"],
    ];
    let heatmapPanDrag = null;

    function refreshDataIndexes() {
      byDate = new Map((DATA.daily || []).map(day => [day.date, day]));
      byHour = new Map((DATA.hourly || []).map(hour => [hour.hour, hour]));
      allDates = (DATA.daily || []).map(day => day.date);
    }

    function usageZero() {
      return { input: 0, cachedInput: 0, output: 0, reasoningOutput: 0, total: 0 };
    }

    function messageEventsZero() {
      return { total: 0, user: 0, agent: 0, primaryAgent: 0, subagentAgent: 0 };
    }

    function userTextZero() {
      return { messages: 0, words: 0, avgWordsPerMessage: 0 };
    }

    function addUsage(target, source) {
      target.input += source.input || 0;
      target.cachedInput += source.cachedInput || 0;
      target.output += source.output || 0;
      target.reasoningOutput += source.reasoningOutput || 0;
      target.total += source.total || 0;
      return target;
    }

    function addMessageEvents(target, source) {
      target.total += source?.total || 0;
      target.user += source?.user || 0;
      target.agent += source?.agent || 0;
      target.primaryAgent += source?.primaryAgent || 0;
      target.subagentAgent += source?.subagentAgent || 0;
      return target;
    }

    function addUserText(target, source) {
      target.messages += source?.messages || 0;
      target.words += source?.words || 0;
      target.avgWordsPerMessage = target.messages ? target.words / target.messages : 0;
      return target;
    }

    function hasUsageData() {
      return Boolean(
        (DATA.meta?.sessionFiles || 0) > 0 ||
        (DATA.meta?.sessionsWithUsage || 0) > 0 ||
        (DATA.daily || []).some(day => (day.usage?.total || 0) > 0 || (day.sessionCount || 0) > 0)
      );
    }

    function renderNoDataState() {
      const node = document.getElementById("noDataState");
      const codexHome = document.getElementById("noDataCodexHome");
      if (!node || !codexHome) return;
      codexHome.textContent = DATA.meta?.codexHome || "~/.codex";
      node.hidden = hasUsageData();
    }

    function compact(value) {
      const abs = Math.abs(value || 0);
      if (abs >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(2)}B`;
      if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
      if (abs >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
      return String(Math.round(value || 0));
    }

    function compactAxis(value) {
      const abs = Math.abs(value || 0);
      const format = (scaled, suffix) => {
        const rounded = Math.round(scaled * 100) / 100;
        if (Math.abs(rounded - Math.round(rounded)) < 0.001) return `${Math.round(rounded)}${suffix}`;
        if (Math.abs(rounded * 10 - Math.round(rounded * 10)) < 0.001) return `${rounded.toFixed(1)}${suffix}`;
        return `${rounded.toFixed(2)}${suffix}`;
      };
      if (abs >= 1_000_000_000) return format(value / 1_000_000_000, "B");
      if (abs >= 1_000_000) return format(value / 1_000_000, "M");
      if (abs >= 1_000) return format(value / 1_000, "K");
      return String(Math.round(value || 0));
    }

    function tokenAxisStep(maxValue) {
      const max = Math.max(0, maxValue || 0);
      if (max >= 2_000_000_000) return 500_000_000;
      if (max >= 750_000_000) return 250_000_000;
      if (max >= 500_000_000) return 100_000_000;
      if (max >= 100_000_000) return 50_000_000;
      if (max >= 50_000_000) return 20_000_000;
      if (max >= 10_000_000) return 10_000_000;
      if (max >= 1_000_000) return 1_000_000;
      if (max >= 500_000) return 100_000;
      if (max >= 100_000) return 50_000;
      if (max >= 50_000) return 10_000;
      if (max >= 10_000) return 5_000;
      if (max >= 1_000) return 1_000;
      if (max >= 100) return 100;
      if (max >= 10) return 10;
      return 1;
    }

    function niceTokenAxis(maxValue) {
      const max = Math.max(0, maxValue || 0);
      if (max <= 0) return { max: 1, ticks: [0, 1] };
      const step = tokenAxisStep(max);
      const axisMax = Math.max(step, Math.ceil((max + step * 0.65) / step) * step);
      const ticks = [];
      for (let value = 0; value <= axisMax + step * 0.5; value += step) {
        ticks.push(Math.round(value));
      }
      return { max: axisMax, ticks };
    }

    function number(value) {
      return new Intl.NumberFormat(undefined).format(Math.round(value || 0));
    }

    function money(value) {
      return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value || 0);
    }

    function moneyAxis(value) {
      const abs = Math.abs(value || 0);
      if (abs >= 1000) return `$${(value / 1000).toFixed(abs >= 10_000 ? 0 : 1)}K`;
      if (abs >= 100) return `$${Math.round(value)}`;
      if (abs >= 10) return `$${value.toFixed(0)}`;
      if (abs >= 1) return `$${value.toFixed(1)}`;
      return `$${value.toFixed(2)}`;
    }

    function niceMoneyAxis(maxValue) {
      const max = Math.max(0, maxValue || 0);
      if (max <= 0) return { max: 1, ticks: [0, 0.25, 0.5, 0.75, 1] };
      const roughStep = max / 4;
      const power = Math.pow(10, Math.floor(Math.log10(roughStep)));
      const fraction = roughStep / power;
      const niceFraction = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
      const step = niceFraction * power;
      const axisMax = Math.max(step, Math.ceil((max + step * 0.3) / step) * step);
      const ticks = [];
      for (let value = 0; value <= axisMax + step * 0.5; value += step) ticks.push(value);
      return { max: axisMax, ticks };
    }

    function pct(value) {
      if (!Number.isFinite(value)) return "0%";
      return `${Math.round(value)}%`;
    }

    function precisePct(value) {
      if (!Number.isFinite(value)) return "0.00%";
      return `${value.toFixed(2)}%`;
    }

    function nicePercentAxis(maxValue) {
      const max = Math.max(0, maxValue || 0);
      const rawMax = Math.min(100, Math.max(1, Math.ceil(max * 1.2)));
      const step = rawMax <= 4 ? 1 : rawMax <= 10 ? 2 : rawMax <= 25 ? 5 : rawMax <= 50 ? 10 : 25;
      const axisMax = Math.min(100, Math.ceil(rawMax / step) * step);
      const ticks = [];
      for (let value = 0; value <= axisMax; value += step) ticks.push(value);
      return { max: axisMax, ticks };
    }

    function outputRatio(day) {
      return day?.usage?.total ? (day.usage.output / day.usage.total) * 100 : 0;
    }

    function uncachedInput(usage) {
      return Math.max(0, (usage?.input || 0) - (usage?.cachedInput || 0));
    }

    function dateLabel(date) {
      return new Intl.DateTimeFormat("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" }).format(new Date(`${date}T12:00:00`));
    }

    function monthLabel(date) {
      return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(new Date(`${date}T12:00:00`));
    }

    function dateKey(date) {
      const y = date.getFullYear();
      const m = String(date.getMonth() + 1).padStart(2, "0");
      const d = String(date.getDate()).padStart(2, "0");
      return `${y}-${m}-${d}`;
    }

    function hourKeyFromDate(date) {
      const h = String(date.getHours()).padStart(2, "0");
      return `${dateKey(date)}T${h}:00:00`;
    }

    function datetimeLocalValue(date) {
      const h = String(date.getHours()).padStart(2, "0");
      return `${dateKey(date)}T${h}:00`;
    }

    function monthKeyFromDate(date) {
      const y = date.getFullYear();
      const m = String(date.getMonth() + 1).padStart(2, "0");
      return `${y}-${m}`;
    }

    function addDays(date, days) {
      const next = new Date(date);
      next.setDate(next.getDate() + days);
      return next;
    }

    function parseLocalDay(value, hour = 12) {
      const [year, month, day] = String(value || "").split("-").map(Number);
      if (!year || !month || !day) return null;
      return new Date(year, month - 1, day, hour, 0, 0, 0);
    }

    function parseLocalHour(value) {
      const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2})(?::\d{2})?/);
      if (!match) return null;
      const [, year, month, day, hour] = match.map(Number);
      if (!year || !month || !day || !Number.isFinite(hour)) return null;
      return new Date(year, month - 1, day, hour, 0, 0, 0);
    }

    function mondayOfWeek(date) {
      const monday = new Date(date.getFullYear(), date.getMonth(), date.getDate(), 12, 0, 0, 0);
      const offset = (monday.getDay() + 6) % 7;
      monday.setDate(monday.getDate() - offset);
      return monday;
    }

    function isoWeekNumber(date) {
      const working = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
      const day = working.getUTCDay() || 7;
      working.setUTCDate(working.getUTCDate() + 4 - day);
      const yearStart = new Date(Date.UTC(working.getUTCFullYear(), 0, 1));
      return Math.ceil((((working - yearStart) / 86400000) + 1) / 7);
    }

    function isoWeekYear(date) {
      const working = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
      const day = working.getUTCDay() || 7;
      working.setUTCDate(working.getUTCDate() + 4 - day);
      return working.getUTCFullYear();
    }

    function isoWeekStart(year, weekNumber) {
      const jan4 = new Date(year, 0, 4, 12, 0, 0, 0);
      const weekOne = mondayOfWeek(jan4);
      const start = new Date(weekOne);
      start.setDate(start.getDate() + (weekNumber - 1) * 7);
      return start;
    }

    function isoWeeksInYear(year) {
      return isoWeekNumber(new Date(year, 11, 28, 12, 0, 0, 0));
    }

    function heatmapYears() {
      const generatedAt = new Date(DATA.meta.generatedAt);
      const generatedYear = Number.isNaN(generatedAt.getTime()) ? new Date().getFullYear() : generatedAt.getFullYear();
      const dataYears = DATA.daily.map(day => new Date(`${day.date}T12:00:00`).getFullYear()).filter(Number.isFinite);
      const maxYear = Math.max(2026, generatedYear, ...dataYears);
      const years = [];
      for (let year = 2026; year <= maxYear; year++) years.push(year);
      return years;
    }

    function renderHeatYearOptions() {
      const select = document.getElementById("heatYear");
      const years = heatmapYears();
      if (!state.heatYear || !years.includes(Number(state.heatYear))) {
        state.heatYear = years.at(-1);
      }
      select.innerHTML = years.map(year => `<option value="${year}">${year}</option>`).join("");
      select.value = String(state.heatYear);
    }

    function heatmapWeekCapacity() {
      const scroll = document.querySelector(".heatmap-scroll");
      const width = scroll?.clientWidth || window.innerWidth || 0;
      const labelColumn = 34;
      const weekColumn = 18;
      const gap = 5;
      const rightOutlineReserve = 4;
      const available = Math.max(0, width - labelColumn - rightOutlineReserve);
      const weeks = Math.floor(available / (weekColumn + gap));
      return Math.max(20, Math.min(52, weeks || 20));
    }

    function modelRate(model) {
      const normalized = DATA.pricing.models[model] ? model : DATA.pricing.defaultModel;
      return DATA.pricing.models[normalized];
    }

    function costForUsage(usage, model) {
      return costPartsForUsage(usage, model).totalCost;
    }

    function costPartsForUsage(usage, model) {
      const rate = modelRate(model);
      const cached = Math.min(usage.cachedInput || 0, usage.input || 0);
      const uncached = Math.max(0, (usage.input || 0) - cached);
      const inputCost = (uncached * rate.input) / 1_000_000;
      const cachedInputCost = (cached * rate.cached_input) / 1_000_000;
      const outputCost = ((usage.output || 0) * rate.output) / 1_000_000;
      return { inputCost, cachedInputCost, outputCost, totalCost: inputCost + cachedInputCost + outputCost };
    }

    function costForByModel(byModel, fallbackUsage) {
      if (state.price !== "logged") return costForUsage(fallbackUsage, state.price);
      return Object.entries(byModel || {}).reduce((sum, [model, usage]) => sum + costForUsage(usage, model), 0);
    }

    function costPartsForByModel(byModel, fallbackUsage) {
      const total = { inputCost: 0, cachedInputCost: 0, outputCost: 0, totalCost: 0 };
      const add = parts => {
        total.inputCost += parts.inputCost;
        total.cachedInputCost += parts.cachedInputCost;
        total.outputCost += parts.outputCost;
        total.totalCost += parts.totalCost;
      };
      if (state.price !== "logged") {
        add(costPartsForUsage(fallbackUsage, state.price));
      } else {
        for (const [model, usage] of Object.entries(byModel || {})) add(costPartsForUsage(usage, model));
      }
      return total;
    }

    const effortOrder = ["low", "medium", "high", "xhigh", "unknown"];

    function effortLabel(effort) {
      return {
        low: "Low",
        medium: "Medium",
        high: "High",
        xhigh: "Extra high",
        unknown: "Unknown"
      }[effort] || effort;
    }

    function effortRank(effort) {
      const index = effortOrder.indexOf(effort);
      return index === -1 ? effortOrder.length : index;
    }

    function sessionCountForBucket(bucket) {
      if (!bucket) return 0;
      if (state.chartResolution === "hour") {
        return mergeSessionRows((DATA.hourlySessions || {})[bucket.key] || []).length;
      }
      if (state.chartResolution === "day") {
        return mergeSessionRows((DATA.dailySessions || {})[bucket.date] || []).length;
      }
      return selectedSessionRows({
        scope: state.chartResolution,
        key: bucket.key,
        date: bucket.date,
        bucket
      }).length;
    }

    function costPartsForBucket(bucket) {
      return costPartsForByModel(bucket?.byModel || {}, bucket?.usage || usageZero());
    }

    const CHART_METRICS = {
      total: {
        label: "Total Tokens",
        axis: "Total Tokens",
        color: "var(--accent)",
        value: bucket => bucket.usage.total || 0,
        format: compact
      },
      input: {
        label: "Real Input Tokens",
        axis: "Real Input Tokens",
        color: "var(--input)",
        value: bucket => uncachedInput(bucket.usage),
        format: compact
      },
      totalInput: {
        label: "Total Input Tokens",
        axis: "Total Input Tokens",
        color: "var(--good)",
        value: bucket => bucket.usage.input || 0,
        format: compact
      },
      cachedInput: {
        label: "Cached Input Tokens",
        axis: "Cached Input Tokens",
        color: "var(--cached)",
        value: bucket => bucket.usage.cachedInput || 0,
        format: compact
      },
      output: {
        label: "Output Tokens",
        axis: "Output Tokens",
        color: "var(--output)",
        value: bucket => bucket.usage.output || 0,
        format: compact
      },
      reasoning: {
        label: "Reasoning Tokens",
        axis: "Reasoning Tokens",
        color: "var(--reasoning)",
        value: bucket => bucket.usage.reasoningOutput || 0,
        format: compact
      },
      sessions: {
        label: "Sessions",
        axis: "Sessions",
        color: "var(--good)",
        value: sessionCountForBucket,
        format: compact
      },
      cost: {
        label: "Total Cost",
        axis: "Total Cost",
        color: "var(--ink)",
        kind: "money",
        value: bucket => costForByModel(bucket.byModel || {}, bucket.usage || usageZero()),
        format: money
      },
      messages: {
        label: "Total Messages",
        axis: "Total Messages",
        color: "var(--reasoning)",
        value: bucket => bucket.messageEvents?.total || 0,
        format: compact
      },
      events: {
        label: "Events",
        axis: "Events",
        color: "var(--muted)",
        value: bucket => bucket.events || 0,
        format: compact
      },
      cacheShare: {
        label: "Cache Share",
        axis: "Cache Share",
        color: "var(--cached)",
        kind: "percent",
        value: bucket => bucket.usage.input ? (bucket.usage.cachedInput / bucket.usage.input) * 100 : 0,
        rangeValue: agg => agg.usage.input ? (agg.usage.cachedInput / agg.usage.input) * 100 : 0,
        isActive: bucket => (bucket.usage.input || 0) > 0,
        format: precisePct
      },
      inputCost: {
        label: "Real Input Cost",
        axis: "Real Input Cost",
        color: "var(--input)",
        kind: "money",
        value: bucket => costPartsForBucket(bucket).inputCost,
        format: money
      },
      cachedInputCost: {
        label: "Cached Input Cost",
        axis: "Cached Input Cost",
        color: "var(--cached)",
        kind: "money",
        value: bucket => costPartsForBucket(bucket).cachedInputCost,
        format: money
      },
      outputCost: {
        label: "Output Cost",
        axis: "Output Cost",
        color: "var(--output)",
        kind: "money",
        value: bucket => costPartsForBucket(bucket).outputCost,
        format: money
      }
    };

    function selectedChartMetric() {
      return CHART_METRICS[state.chartMode] || CHART_METRICS.total;
    }

    function filteredDays() {
      if (state.range === "all" || allDates.length === 0) return DATA.daily;
      const count = Number(state.range);
      return DATA.daily.slice(Math.max(0, DATA.daily.length - count));
    }

    function aggregate(days) {
      const usage = usageZero();
      const messageEvents = messageEventsZero();
      const userText = userTextZero();
      const byModel = {};
      const byModelEffort = {};
      let events = 0;
      let activeDays = 0;
      let sessions = 0;
      for (const day of days) {
        addUsage(usage, day.usage);
        addMessageEvents(messageEvents, day.messageEvents);
        addUserText(userText, day.userText);
        events += day.events || 0;
        sessions += day.sessionCount || 0;
        if ((day.usage.total || 0) > 0) activeDays += 1;
        for (const [model, modelUsage] of Object.entries(day.byModel || {})) {
          byModel[model] ||= usageZero();
          addUsage(byModel[model], modelUsage);
        }
        addByModelEffortUsage(byModelEffort, day.byModelEffort || {});
      }
      return { usage, byModel, byModelEffort, messageEvents, userText, events, activeDays, sessions };
    }

    function addByModelUsage(target, byModel) {
      for (const [model, usage] of Object.entries(byModel || {})) {
        target[model] ||= usageZero();
        addUsage(target[model], usage);
      }
    }

    function addByModelEffortUsage(target, byModelEffort) {
      for (const [model, efforts] of Object.entries(byModelEffort || {})) {
        target[model] ||= {};
        for (const [effort, usage] of Object.entries(efforts || {})) {
          target[model][effort] ||= usageZero();
          addUsage(target[model][effort], usage);
        }
      }
    }

    function makeSeriesBucket(key, label, title, date) {
      return {
        key,
        label,
        title,
        date,
        usage: usageZero(),
        byModel: {},
        byModelEffort: {},
        messageEvents: messageEventsZero(),
        userText: userTextZero(),
        events: 0,
        sessionCount: 0
      };
    }

    function addSourceToSeriesBucket(bucket, source) {
      if (!bucket || !source) return bucket;
      addUsage(bucket.usage, source.usage || usageZero());
      addByModelUsage(bucket.byModel, source.byModel || {});
      addByModelEffortUsage(bucket.byModelEffort, source.byModelEffort || {});
      addMessageEvents(bucket.messageEvents, source.messageEvents || messageEventsZero());
      addUserText(bucket.userText, source.userText || userTextZero());
      bucket.events += source.events || 0;
      bucket.sessionCount += source.sessionCount || 0;
      return bucket;
    }

    function generatedLocalDate() {
      const generated = new Date(DATA.meta.generatedAt);
      return Number.isNaN(generated.getTime()) ? new Date() : generated;
    }

    function parseWindowStart(value, resolution) {
      if (!value) return null;
      if (resolution === "hour") return parseLocalHour(value);
      return parseLocalDay(value);
    }

    function normalizeWindowStart(date, resolution) {
      const normalized = new Date(date);
      if (resolution === "hour") {
        normalized.setMinutes(0, 0, 0);
        return normalized;
      }
      if (resolution === "week") return mondayOfWeek(normalized);
      if (resolution === "month") return new Date(normalized.getFullYear(), normalized.getMonth(), 1, 12, 0, 0, 0);
      return new Date(normalized.getFullYear(), normalized.getMonth(), normalized.getDate(), 12, 0, 0, 0);
    }

    function formatWindowStart(date, resolution) {
      const normalized = normalizeWindowStart(date, resolution);
      return resolution === "hour" ? hourKeyFromDate(normalized) : dateKey(normalized);
    }

    function latestWindowStart(resolution = state.chartResolution) {
      const now = generatedLocalDate();
      if (resolution === "hour") {
        const currentHour = new Date(now);
        currentHour.setMinutes(0, 0, 0);
        currentHour.setHours(currentHour.getHours() - 23);
        return currentHour;
      }
      if (resolution === "week") {
        return addDays(mondayOfWeek(now), -13 * 7);
      }
      if (resolution === "month") {
        return new Date(now.getFullYear(), now.getMonth() - 11, 1, 12, 0, 0, 0);
      }
      const currentDay = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 12, 0, 0, 0);
      return addDays(currentDay, -6);
    }

    function chartWindowStart(resolution = state.chartResolution) {
      const stored = parseWindowStart(state.chartWindowStart?.[resolution], resolution);
      return normalizeWindowStart(stored || latestWindowStart(resolution), resolution);
    }

    function setChartWindowStart(resolution, date) {
      const latest = latestWindowStart(resolution);
      let next = normalizeWindowStart(date, resolution);
      if (next > latest) next = latest;
      state.chartWindowStart[resolution] = formatWindowStart(next, resolution);
    }

    function chartWindowEnd(start, resolution = state.chartResolution) {
      if (resolution === "hour") {
        const end = new Date(start);
        end.setHours(end.getHours() + 23);
        return end;
      }
      if (resolution === "week") return addDays(start, 13 * 7 + 6);
      if (resolution === "month") return new Date(start.getFullYear(), start.getMonth() + 12, 0, 12, 0, 0, 0);
      return addDays(start, 6);
    }

    function dateTimeLabel(date) {
      return new Intl.DateTimeFormat("en-US", {
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit"
      }).format(date);
    }

    function periodTitle() {
      const start = chartWindowStart();
      const end = chartWindowEnd(start);
      if (state.chartResolution === "hour") return `${dateTimeLabel(start)} - ${dateTimeLabel(end)}`;
      return `${dateLabel(dateKey(start))} - ${dateLabel(dateKey(end))}`;
    }

    function renderPeriodNavigator() {
      const input = document.getElementById("chartWindowDate");
      const prev = document.getElementById("chartPeriodPrev");
      const next = document.getElementById("chartPeriodNext");
      const calendar = document.getElementById("chartCalendarButton");
      if (!input || !prev || !next || !calendar) return;

      const start = chartWindowStart();
      const latest = latestWindowStart();
      const title = periodTitle();
      if (state.chartResolution === "hour") {
        input.type = "datetime-local";
        input.step = "3600";
        input.value = datetimeLocalValue(start);
        input.max = datetimeLocalValue(latest);
      } else {
        input.type = "date";
        input.removeAttribute("step");
        input.value = dateKey(start);
        input.max = dateKey(latest);
      }
      input.title = title;
      prev.title = "Previous period";
      const atLatest = start.getTime() >= latest.getTime();
      next.disabled = atLatest;
      next.title = atLatest ? "Current period" : "Next period";
      next.setAttribute("aria-disabled", atLatest ? "true" : "false");
      const pickerLabel = state.chartResolution === "hour" ? "Choose start date and hour" : "Choose start date";
      calendar.title = `${pickerLabel} (${title})`;
      calendar.setAttribute("aria-label", `${pickerLabel}. Current window: ${title}`);
    }

    function shiftChartWindow(direction) {
      const resolution = state.chartResolution;
      const start = chartWindowStart(resolution);
      let next = new Date(start);
      if (resolution === "hour") next.setHours(next.getHours() + direction * 24);
      else if (resolution === "day") next = addDays(start, direction * 7);
      else if (resolution === "week") next = addDays(start, direction * 14 * 7);
      else if (resolution === "month") next = new Date(start.getFullYear(), start.getMonth() + direction * 12, 1, 12, 0, 0, 0);
      setChartWindowStart(resolution, next);
      state.selectedChartKey = null;
      selectVisibleBucket();
      renderAll();
    }

    function setChartWindowDate(value) {
      const date = state.chartResolution === "hour" ? parseLocalHour(value) : parseLocalDay(value);
      if (!date) return;
      setChartWindowStart(state.chartResolution, date);
      state.selectedChartKey = null;
      selectVisibleBucket();
      renderAll();
    }

    function chartSeriesBuckets() {
      if (state.chartResolution === "hour") {
        const firstHour = chartWindowStart("hour");
        const buckets = [];
        for (let offset = 0; offset < 24; offset++) {
          const cursor = new Date(firstHour);
          cursor.setHours(cursor.getHours() + offset);
          const key = hourKeyFromDate(cursor);
          const label = cursor.getHours() === 0 ? monthLabel(dateKey(cursor)) : `${String(cursor.getHours()).padStart(2, "0")}:00`;
          const title = new Intl.DateTimeFormat("en-US", { weekday: "long", month: "long", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(cursor);
          buckets.push(addSourceToSeriesBucket(makeSeriesBucket(key, label, title, dateKey(cursor)), byHour.get(key)));
        }
        return buckets;
      }

      if (state.chartResolution === "week") {
        const firstWeekStart = chartWindowStart("week");
        const buckets = [];
        const byWeek = new Map();
        for (let offset = 0; offset < 14; offset++) {
          const start = addDays(firstWeekStart, offset * 7);
          const key = dateKey(start);
          const end = addDays(start, 6);
          const label = `Wk ${isoWeekNumber(start)}`;
          const title = `${dateLabel(key)} - ${dateLabel(dateKey(end))}`;
          const bucket = makeSeriesBucket(key, label, title, key);
          buckets.push(bucket);
          byWeek.set(key, bucket);
        }
        for (const day of DATA.daily) {
          const weekKey = dateKey(mondayOfWeek(new Date(`${day.date}T12:00:00`)));
          addSourceToSeriesBucket(byWeek.get(weekKey), day);
        }
        return buckets;
      }

      if (state.chartResolution === "month") {
        const firstMonth = chartWindowStart("month");
        const buckets = [];
        const byMonth = new Map();
        for (let offset = 0; offset < 12; offset++) {
          const start = new Date(firstMonth.getFullYear(), firstMonth.getMonth() + offset, 1, 12, 0, 0, 0);
          const key = monthKeyFromDate(start);
          const label = new Intl.DateTimeFormat("en-US", { month: "short", year: "2-digit" }).format(start);
          const title = new Intl.DateTimeFormat("en-US", { month: "long", year: "numeric" }).format(start);
          const bucket = makeSeriesBucket(key, label, title, dateKey(start));
          buckets.push(bucket);
          byMonth.set(key, bucket);
        }
        for (const day of DATA.daily) {
          addSourceToSeriesBucket(byMonth.get(day.date.slice(0, 7)), day);
        }
        return buckets;
      }

      const startDate = chartWindowStart("day");
      const buckets = [];
      for (let offset = 0; offset < 7; offset++) {
        const cursor = addDays(startDate, offset);
        const key = dateKey(cursor);
        const label = monthLabel(key);
        buckets.push(addSourceToSeriesBucket(makeSeriesBucket(key, label, dateLabel(key), key), byDate.get(key)));
      }
      return buckets;
    }

    function chartResolutionName() {
      return {
        hour: "Hourly",
        day: "Daily",
        week: "Weekly",
        month: "Monthly"
      }[state.chartResolution] || "Daily";
    }

    function scopeLabel(scope) {
      return {
        hour: "Hour",
        day: "Day",
        week: "Week",
        month: "Month"
      }[scope] || "Day";
    }

    function selectedBucketContext() {
      if (state.selectedChartKey) {
        const bucket = chartSeriesBuckets().find(item => item.key === state.selectedChartKey);
        if (bucket) {
          return {
            scope: state.chartResolution,
            key: bucket.key,
            date: bucket.date,
            title: bucket.title,
            bucket
          };
        }
      }

      const date = state.selectedDate || (DATA.daily.at(-1)?.date || "");
      const bucket = byDate.get(date) || {
        date,
        usage: usageZero(),
        byModel: {},
        byModelEffort: {},
        messageEvents: messageEventsZero(),
        userText: userTextZero(),
        events: 0,
        sessionCount: 0
      };
      return {
        scope: "day",
        key: date,
        date,
        title: date ? dateLabel(date) : "No date selected",
        bucket
      };
    }

    function isBucketSelected(bucket) {
      if (state.selectedChartKey) return bucket.key === state.selectedChartKey;
      return state.chartResolution === "day" && bucket.date === state.selectedDate;
    }

    function datesBetween(startDate, endDate) {
      const dates = [];
      const cursor = new Date(`${startDate}T12:00:00`);
      const end = new Date(`${endDate}T12:00:00`);
      while (cursor <= end) {
        const key = dateKey(cursor);
        if (byDate.has(key)) dates.push(key);
        cursor.setDate(cursor.getDate() + 1);
      }
      return dates;
    }

    function selectedContextDates(context) {
      if (context.scope === "week") {
        const end = dateKey(addDays(new Date(`${context.key}T12:00:00`), 6));
        return datesBetween(context.key, end);
      }
      if (context.scope === "month") {
        return allDates.filter(date => date.slice(0, 7) === context.key);
      }
      return context.date ? [context.date] : [];
    }

    function mergeSessionRows(rows) {
      const merged = new Map();
      for (const row of rows) {
        const id = row.threadId || `${row.title || "session"}-${row.firstEvent || ""}`;
        if (!merged.has(id)) {
          merged.set(id, {
            threadId: row.threadId,
            title: row.title || "Untitled Codex session",
            cwd: row.cwd || "",
            model: row.model || "",
            effort: row.effort || "",
            usage: usageZero(),
            byModel: {},
            messageEvents: messageEventsZero(),
            userText: userTextZero(),
            events: 0,
            firstEvent: row.firstEvent || "",
            lastEvent: row.lastEvent || ""
          });
        }
        const target = merged.get(id);
        addUsage(target.usage, row.usage || usageZero());
        addByModelUsage(target.byModel, row.byModel || {});
        addMessageEvents(target.messageEvents, row.messageEvents || messageEventsZero());
        addUserText(target.userText, row.userText || userTextZero());
        target.events += row.events || 0;
        if (row.firstEvent && (!target.firstEvent || row.firstEvent < target.firstEvent)) target.firstEvent = row.firstEvent;
        if (row.lastEvent && (!target.lastEvent || row.lastEvent > target.lastEvent)) target.lastEvent = row.lastEvent;
        if (!target.cwd && row.cwd) target.cwd = row.cwd;
        if (!target.model && row.model) target.model = row.model;
        if (!target.effort && row.effort) target.effort = row.effort;
      }
      return Array.from(merged.values()).sort((a, b) => (b.usage.total || 0) - (a.usage.total || 0));
    }

    function selectedSessionRows(context) {
      if (context.scope === "hour") {
        return mergeSessionRows((DATA.hourlySessions || {})[context.key] || []);
      }
      if (context.scope === "day") {
        return mergeSessionRows((DATA.dailySessions || {})[context.date] || []);
      }

      const rows = [];
      for (const date of selectedContextDates(context)) {
        rows.push(...((DATA.dailySessions || {})[date] || []));
      }
      return mergeSessionRows(rows);
    }

    function uniqueSessionsForDays(days) {
      const threadIds = new Set();
      for (const day of days) {
        for (const session of DATA.dailySessions[day.date] || []) {
          threadIds.add(session.threadId);
        }
      }
      return threadIds.size;
    }

    function currentStreak() {
      let streak = 0;
      for (let i = DATA.daily.length - 1; i >= 0; i--) {
        if ((DATA.daily[i].usage.total || 0) > 0) streak += 1;
        else if (streak > 0) break;
      }
      return streak;
    }

    function bestDay() {
      return DATA.daily.reduce((best, day) => (day.usage.total || 0) > (best.usage.total || 0) ? day : best, { date: "", usage: usageZero() });
    }

    function renderPriceOptions() {
      const select = document.getElementById("priceSelect");
      select.innerHTML = "";
      const options = [["logged", "Total: detected model mix"]];
      for (const model of Object.keys(DATA.pricing.models)) options.push([model, `What-if: all as ${model}`]);
      select.innerHTML = options.map(([value, label]) => `<option value="${value}">${label}</option>`).join("");
      select.value = state.price;
    }

    function currentTimezone() {
      return DATA.meta?.timezone || ROOT_DATA.meta?.timezone || "UTC";
    }

    function renderTimezoneOptions() {
      const select = document.getElementById("timezoneSelect");
      if (!select) return;
      const timezoneName = currentTimezone();
      const options = TIMEZONE_OPTIONS.some(([value]) => value === timezoneName)
        ? TIMEZONE_OPTIONS
        : [[timezoneName, `${timezoneName} - current`], ...TIMEZONE_OPTIONS];
      select.innerHTML = options.map(([value, label]) => `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`).join("");
      select.value = timezoneName;
    }

    function setTimezone(timezoneName) {
      if (!timezoneName || timezoneName === currentTimezone()) return;
      const select = document.getElementById("timezoneSelect");
      if (window.location.protocol === "file:") {
        if (select) select.value = currentTimezone();
        return;
      }
      const url = new URL(window.location.href);
      url.search = `?timezone=${encodeURIComponent(timezoneName)}`;
      window.location.assign(url.toString());
    }

    function availableDevices() {
      return Array.isArray(ROOT_DATA.meta?.devices) ? ROOT_DATA.meta.devices : [];
    }

    function renderDeviceOptions() {
      const select = document.getElementById("deviceSelect");
      if (!select) return;
      const devices = availableDevices();
      const options = devices.length
        ? [["all", "All devices"], ...devices.map(device => [device.id, device.name || device.slug || "Device"])]
        : [["all", "Local device"]];
      select.innerHTML = options.map(([value, label]) => `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`).join("");
      select.value = options.some(([value]) => value === state.device) ? state.device : "all";
    }

    function setActiveDevice(deviceId) {
      state.device = deviceId || "all";
      DATA = state.device === "all" ? ROOT_DATA : (ROOT_DATA.devicePayloads?.[state.device] || ROOT_DATA);
      refreshDataIndexes();
      state.selectedChartKey = null;
      state.selectedDate = null;
      renderPriceOptions();
      renderDeviceOptions();
      renderTimezoneOptions();
      selectVisibleBucket();
      renderAll();
    }

    function renderStats() {
      const days = filteredDays();
      const agg = aggregate(days);
      const selectedCost = costForByModel(agg.byModel, agg.usage);
      const outputShare = agg.usage.total ? (agg.usage.output / agg.usage.total) * 100 : 0;
      const busiest = bestDay();
      const avgPerActiveDay = agg.activeDays ? agg.usage.total / agg.activeDays : 0;
      const stats = [
        ["Total Tokens", compact(agg.usage.total), `${number(uncachedInput(agg.usage))} Real Input / ${number(agg.usage.cachedInput)} Cached Input / ${number(agg.usage.output)} Output`],
        ["What-if API Cost", money(selectedCost), state.price === "logged" ? "Each detected model priced with its own API rate" : `All usage priced as ${state.price}`],
        ["Cached Input Tokens", compact(agg.usage.cachedInput), `${pct(agg.usage.input ? agg.usage.cachedInput / agg.usage.input * 100 : 0)} of Total Input Tokens`],
        ["Reasoning Tokens", compact(agg.usage.reasoningOutput), "Included inside Output Tokens"],
        ["Active days", String(agg.activeDays), `${currentStreak()} day current streak`],
        ["Busiest day", busiest.date ? compact(busiest.usage.total) : "0", busiest.date ? dateLabel(busiest.date) : "No usage yet"],
      ];
      document.getElementById("statGrid").innerHTML = stats.map(([label, value, hint]) => `
        <article class="stat">
          <div class="label">${label}</div>
          <div class="value">${value}</div>
          <div class="hint">${hint}</div>
        </article>
      `).join("");
    }

    function renderRangeSummary() {
      const buckets = chartSeriesBuckets();
      const agg = aggregate(buckets);
      const selectedCost = costForByModel(agg.byModel, agg.usage);
      const costParts = costPartsForByModel(agg.byModel, agg.usage);
      const activeBuckets = buckets.filter(bucket => (bucket.usage.total || 0) > 0);
      const avgBucketOutputRatio = activeBuckets.length
        ? activeBuckets.reduce((sum, bucket) => sum + outputRatio(bucket), 0) / activeBuckets.length
        : 0;
      const bestOutputRatio = activeBuckets.reduce((best, bucket) => outputRatio(bucket) > outputRatio(best) ? bucket : best, { usage: usageZero() });
      const items = state.chartMode === "costBreakdown"
        ? [
            ["Total Cost", money(costParts.totalCost)],
            ["Real Input Cost", money(costParts.inputCost)],
            ["Cached Input Cost", money(costParts.cachedInputCost)],
            ["Output Cost", money(costParts.outputCost)],
          ]
        : state.chartMode === "outputRatio"
        ? [
            ["Range Output Ratio", precisePct(agg.usage.total ? agg.usage.output / agg.usage.total * 100 : 0)],
            [`Avg ${chartResolutionName().toLowerCase()} Ratio`, precisePct(avgBucketOutputRatio)],
            ["Highest Ratio", precisePct(outputRatio(bestOutputRatio))],
            ["Output Tokens", compact(agg.usage.output)],
          ]
        : (() => {
            const metric = selectedChartMetric();
            const bucketValues = buckets.map(bucket => metric.value(bucket));
            const rangeValue = typeof metric.rangeValue === "function"
              ? metric.rangeValue(agg, buckets)
              : bucketValues.reduce((sum, value) => sum + (value || 0), 0);
            const activeMetricValues = buckets
              .map((bucket, index) => ({ bucket, value: bucketValues[index] || 0 }))
              .filter(item => typeof metric.isActive === "function" ? metric.isActive(item.bucket) : item.value > 0)
              .map(item => item.value);
            const activeMetricTotal = activeMetricValues.reduce((sum, value) => sum + value, 0);
            return [
              [`Range ${metric.label}`, metric.format(rangeValue)],
              [`Avg / active ${chartResolutionName().toLowerCase()}`, metric.format(activeMetricValues.length ? activeMetricTotal / activeMetricValues.length : 0)],
              ["Active Buckets", number(activeMetricValues.length)],
              metric.kind === "money"
                ? ["Total Tokens", compact(agg.usage.total)]
                : metric.kind === "percent"
                ? ["Total Input Tokens", compact(agg.usage.input)]
                : ["API Estimate", money(selectedCost)],
            ];
          })();
      document.getElementById("rangeSummary").innerHTML = items.map(([label, value]) => `
        <div class="mini"><span>${label}</span><strong>${value}</strong></div>
      `).join("");
    }

    function setSelectedDate(date, chartKey = null) {
      if (!date) return;
      state.selectedDate = date;
      state.selectedChartKey = chartKey;
      renderAll();
    }

    function selectVisibleBucket() {
      const buckets = chartSeriesBuckets();
      const bucket = [...buckets].reverse().find(item => (item.usage.total || 0) > 0) || buckets.at(-1);
      if (!bucket) return;
      state.selectedDate = bucket.date;
      state.selectedChartKey = bucket.key;
    }

    function renderBars() {
      const buckets = chartSeriesBuckets();
      renderChartModeHeader();
      if (state.chartMode === "outputRatio") {
        renderOutputRatioChart(buckets);
      } else if (state.chartMode === "costBreakdown") {
        renderCostBreakdownChart(buckets);
      } else {
        renderMetricBarChart(buckets);
      }
    }

    function renderChartModeHeader() {
      const title = document.getElementById("mainChartTitle");
      const legend = document.getElementById("mainChartLegend");
      document.getElementById("chartResolution").value = state.chartResolution;
      document.getElementById("chartMetric").value = state.chartMode;
      renderPeriodNavigator();
      const prefix = chartResolutionName();
      if (state.chartMode === "costBreakdown") {
        title.textContent = `${prefix} API Cost Split`;
        legend.innerHTML = `
          <span><i class="dot" style="background: var(--input)"></i>Real Input Cost</span>
          <span><i class="dot" style="background: var(--cached)"></i>Cached Input Cost</span>
          <span><i class="dot" style="background: var(--output)"></i>Output Cost</span>
        `;
      } else if (state.chartMode === "outputRatio") {
        title.textContent = `${prefix} Output Ratio`;
        legend.innerHTML = `
          <span><i class="dot" style="background: var(--output)"></i>Output / Total Tokens</span>
          <span><i class="dot" style="background: var(--input)"></i>Lower ratio often means heavier context/input</span>
        `;
      } else {
        const metric = selectedChartMetric();
        title.textContent = `${prefix} ${metric.label}`;
        legend.innerHTML = `
          <span><i class="dot" style="background: ${metric.color}"></i>${metric.label}</span>
        `;
      }
    }

    function renderMetricBarChart(buckets) {
      const metric = selectedChartMetric();
      const width = Math.max(760, buckets.length * 40 + 96);
      const height = 340;
      const pad = { top: 24, right: 24, bottom: 48, left: 64 };
      const chartW = width - pad.left - pad.right;
      const chartH = height - pad.top - pad.bottom;
      const values = buckets.map(bucket => metric.value(bucket));
      const maxValue = Math.max(1, ...values);
      const yAxis = metric.kind === "money" ? niceMoneyAxis(maxValue) : metric.kind === "percent" ? nicePercentAxis(maxValue) : niceTokenAxis(maxValue);
      const slot = chartW / Math.max(1, buckets.length);
      const barW = Math.max(14, Math.min(42, slot * 0.88));
      const y = value => pad.top + chartH - (value / yAxis.max) * chartH;
      const x = index => pad.left + index * slot + slot / 2;
      const ticks = yAxis.ticks.map(value => {
        const yy = y(value);
        const axisLabel = metric.kind === "money" ? moneyAxis(value) : metric.kind === "percent" ? precisePct(value) : compactAxis(value);
        return `<line class="axis" x1="${pad.left}" x2="${width - pad.right}" y1="${yy}" y2="${yy}"></line>
          <line class="axis" x1="${pad.left - 5}" x2="${pad.left}" y1="${yy}" y2="${yy}"></line>
          <text class="axis-label" x="${pad.left - 8}" y="${yy + 4}" text-anchor="end">${axisLabel}</text>`;
      }).join("");
      const labelEvery = Math.max(1, Math.ceil(buckets.length / 8));
      const bars = buckets.map((bucket, index) => {
        const cx = x(index);
        const value = values[index] || 0;
        const valueY = y(value);
        const valueH = pad.top + chartH - valueY;
        const label = index % labelEvery === 0 ? `<text class="axis-label" x="${cx}" y="${height - 16}" text-anchor="middle">${escapeHtml(bucket.label)}</text>` : "";
        const isSelected = isBucketSelected(bucket);
        const selected = isSelected ? `<rect class="selected-marker" x="${cx - slot / 2 + 2}" y="${pad.top - 6}" width="${Math.max(8, slot - 4)}" height="${chartH + 12}" rx="4"></rect>` : "";
        const hitX = cx - slot / 2 + 2;
        const hitW = Math.max(8, slot - 4);
        const formattedValue = metric.format(value);
        const valueLabel = `<text class="chart-value-label total" x="${cx}" y="${valueY - 8}" text-anchor="middle">${formattedValue}</text>`;
        const selectedClass = isSelected ? "selected" : "";
        return `
          <rect class="chart-hitbox" data-date="${bucket.date}" data-key="${bucket.key}" x="${hitX}" y="${pad.top - 6}" width="${hitW}" height="${chartH + 12}" rx="4">
            <title>${escapeHtml(bucket.title)} ${metric.label}: ${formattedValue}</title>
          </rect>
          <rect class="chart-hover-band" x="${hitX}" y="${pad.top - 6}" width="${hitW}" height="${chartH + 12}" rx="4"></rect>
          <rect class="bar-total ${selectedClass}" data-date="${bucket.date}" data-key="${bucket.key}" x="${cx - barW / 2}" y="${valueY}" width="${barW}" height="${Math.max(1, valueH)}" rx="4" style="fill:${metric.color}">
            <title>${escapeHtml(bucket.title)} ${metric.label}: ${formattedValue}</title>
          </rect>
          ${selected}
          ${valueLabel}
          ${label}`;
      }).join("");
      document.getElementById("barChart").innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(metric.label)} bars">
          ${ticks}
          <line class="axis" x1="${pad.left}" x2="${pad.left}" y1="${pad.top}" y2="${pad.top + chartH}"></line>
          <line class="axis" x1="${pad.left}" x2="${width - pad.right}" y1="${pad.top + chartH}" y2="${pad.top + chartH}"></line>
          <text class="axis-title" x="${pad.left - 54}" y="${pad.top + chartH / 2}" text-anchor="middle" dominant-baseline="middle" transform="rotate(-90 ${pad.left - 54} ${pad.top + chartH / 2})" style="fill:${metric.color}">${escapeHtml(metric.axis)}</text>
          ${bars}
        </svg>`;
      document.querySelectorAll("#barChart [data-date]").forEach(node => {
        node.addEventListener("click", event => setSelectedDate(event.currentTarget.dataset.date, event.currentTarget.dataset.key));
      });
    }

    function renderOutputRatioChart(buckets) {
      const width = Math.max(760, buckets.length * 34 + 104);
      const height = 340;
      const pad = { top: 24, right: 26, bottom: 48, left: 70 };
      const chartW = width - pad.left - pad.right;
      const chartH = height - pad.top - pad.bottom;
      const ratios = buckets.map(outputRatio);
      const maxRatio = Math.max(0.01, ...ratios);
      const yMax = Math.min(100, Math.max(1, Math.ceil(maxRatio * 1.2 * 100) / 100));
      const slot = chartW / Math.max(1, buckets.length);
      const x = index => pad.left + index * slot + slot / 2;
      const y = value => pad.top + chartH - (value / yMax) * chartH;
      const ticks = [0, 0.25, 0.5, 0.75, 1].map(ratio => {
        const yy = pad.top + chartH - ratio * chartH;
        const value = yMax * ratio;
        return `<line class="axis" x1="${pad.left}" x2="${width - pad.right}" y1="${yy}" y2="${yy}"></line>
          <line class="axis" x1="${pad.left - 5}" x2="${pad.left}" y1="${yy}" y2="${yy}"></line>
          <text class="axis-label output-axis" x="${pad.left - 8}" y="${yy + 4}" text-anchor="end">${precisePct(value)}</text>`;
      }).join("");
      const labelEvery = Math.max(1, Math.ceil(buckets.length / 8));
      const points = buckets.map((bucket, index) => `${x(index)},${y(outputRatio(bucket))}`);
      const line = points.length > 0 ? `<polyline class="ratio-line" points="${points.join(" ")}"></polyline>` : "";
      const area = points.length > 0 ? `<polygon class="ratio-area" points="${pad.left + slot / 2},${pad.top + chartH} ${points.join(" ")} ${x(buckets.length - 1)},${pad.top + chartH}"></polygon>` : "";
      const dots = buckets.map((bucket, index) => {
        const cx = x(index);
        const ratio = outputRatio(bucket);
        const label = index % labelEvery === 0 ? `<text class="axis-label" x="${cx}" y="${height - 16}" text-anchor="middle">${escapeHtml(bucket.label)}</text>` : "";
        const isSelected = isBucketSelected(bucket);
        const selected = isSelected ? `<rect class="selected-marker" x="${cx - slot / 2 + 2}" y="${pad.top - 6}" width="${Math.max(8, slot - 4)}" height="${chartH + 12}" rx="4"></rect>` : "";
        const hitX = cx - slot / 2 + 2;
        const hitW = Math.max(8, slot - 4);
        const ratioY = y(ratio);
        const ratioLabelY = Math.max(pad.top + 12, ratioY - 10);
        const ratioValueLabel = ratio > 0 || bucket.usage.total > 0
          ? `<text class="chart-value-label ratio" x="${cx}" y="${ratioLabelY}" text-anchor="middle">${precisePct(ratio)}</text>`
          : "";
        return `
          <rect class="chart-hitbox" data-date="${bucket.date}" data-key="${bucket.key}" x="${hitX}" y="${pad.top - 6}" width="${hitW}" height="${chartH + 12}" rx="4">
            <title>${escapeHtml(bucket.title)} Output Ratio: ${precisePct(ratio)}; Output Tokens: ${number(bucket.usage.output)}; Total Tokens: ${number(bucket.usage.total)}</title>
          </rect>
          <rect class="chart-hover-band" x="${hitX}" y="${pad.top - 6}" width="${hitW}" height="${chartH + 12}" rx="4"></rect>
          <circle class="ratio-dot" data-date="${bucket.date}" data-key="${bucket.key}" cx="${cx}" cy="${ratioY}" r="4">
            <title>${escapeHtml(bucket.title)} Output Ratio: ${precisePct(ratio)}; Output Tokens: ${number(bucket.usage.output)}; Total Tokens: ${number(bucket.usage.total)}</title>
          </circle>
          ${ratioValueLabel}
          ${selected}
          ${label}`;
      }).join("");
      document.getElementById("barChart").innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Daily output token ratio line chart">
          ${ticks}
          <line class="axis" x1="${pad.left}" x2="${pad.left}" y1="${pad.top}" y2="${pad.top + chartH}"></line>
          <line class="axis" x1="${pad.left}" x2="${width - pad.right}" y1="${pad.top + chartH}" y2="${pad.top + chartH}"></line>
          <text class="axis-title output-axis" x="${pad.left - 54}" y="${pad.top + chartH / 2}" text-anchor="middle" dominant-baseline="middle" transform="rotate(-90 ${pad.left - 54} ${pad.top + chartH / 2})">Output Ratio</text>
          ${area}
          ${line}
          ${dots}
        </svg>`;
      document.querySelectorAll("#barChart [data-date]").forEach(node => {
        node.addEventListener("click", event => setSelectedDate(event.currentTarget.dataset.date, event.currentTarget.dataset.key));
      });
    }

    function renderCostBreakdownChart(buckets) {
      const rows = buckets.map(bucket => ({ bucket, costs: costPartsForByModel(bucket.byModel, bucket.usage) }));
      const width = Math.max(760, buckets.length * 54 + 112);
      const height = 340;
      const pad = { top: 24, right: 26, bottom: 48, left: 70 };
      const chartW = width - pad.left - pad.right;
      const chartH = height - pad.top - pad.bottom;
      const maxCost = Math.max(1, ...rows.flatMap(row => [row.costs.inputCost, row.costs.cachedInputCost, row.costs.outputCost]));
      const yAxis = niceMoneyAxis(maxCost);
      const slot = chartW / Math.max(1, buckets.length);
      const groupW = Math.min(52, slot * 0.82);
      const barW = Math.max(4, Math.min(15, (groupW - 8) / 3));
      const y = value => pad.top + chartH - (value / yAxis.max) * chartH;
      const x = index => pad.left + index * slot + slot / 2;
      const ticks = yAxis.ticks.map(value => {
        const yy = y(value);
        return `<line class="axis" x1="${pad.left}" x2="${width - pad.right}" y1="${yy}" y2="${yy}"></line>
          <line class="axis" x1="${pad.left - 5}" x2="${pad.left}" y1="${yy}" y2="${yy}"></line>
          <text class="axis-label" x="${pad.left - 8}" y="${yy + 4}" text-anchor="end">${moneyAxis(value)}</text>`;
      }).join("");
      const labelEvery = Math.max(1, Math.ceil(buckets.length / 8));
      const bars = rows.map((row, index) => {
        const { bucket, costs } = row;
        const cx = x(index);
        const isSelected = isBucketSelected(bucket);
        const selected = isSelected ? `<rect class="selected-marker" x="${cx - slot / 2 + 2}" y="${pad.top - 6}" width="${Math.max(8, slot - 4)}" height="${chartH + 12}" rx="4"></rect>` : "";
        const hitX = cx - slot / 2 + 2;
        const hitW = Math.max(8, slot - 4);
        const label = index % labelEvery === 0 ? `<text class="axis-label" x="${cx}" y="${height - 16}" text-anchor="middle">${escapeHtml(bucket.label)}</text>` : "";
        const selectedClass = isSelected ? "selected" : "";
        const specs = [
          ["input", costs.inputCost, -barW - 3, "Real Input Cost"],
          ["cached", costs.cachedInputCost, 0, "Cached Input Cost"],
          ["output", costs.outputCost, barW + 3, "Output Cost"],
        ];
        const costBars = specs.map(([kind, value, dx, title]) => {
          const barH = value > 0 ? Math.max(1, pad.top + chartH - y(value)) : 0;
          return `<rect class="cost-bar ${kind} ${selectedClass}" data-date="${bucket.date}" data-key="${bucket.key}" x="${cx + dx - barW / 2}" y="${y(value)}" width="${barW}" height="${barH}" rx="3">
            <title>${escapeHtml(bucket.title)} ${title}: ${money(value)}</title>
          </rect>`;
        }).join("");
        return `
          <rect class="chart-hitbox" data-date="${bucket.date}" data-key="${bucket.key}" x="${hitX}" y="${pad.top - 6}" width="${hitW}" height="${chartH + 12}" rx="4">
            <title>${escapeHtml(bucket.title)} Total Cost: ${money(costs.totalCost)}; Real Input Cost: ${money(costs.inputCost)}; Cached Input Cost: ${money(costs.cachedInputCost)}; Output Cost: ${money(costs.outputCost)}</title>
          </rect>
          <rect class="chart-hover-band" x="${hitX}" y="${pad.top - 6}" width="${hitW}" height="${chartH + 12}" rx="4"></rect>
          ${costBars}
          ${selected}
          ${label}`;
      }).join("");
      document.getElementById("barChart").innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="API cost split bars">
          ${ticks}
          <line class="axis" x1="${pad.left}" x2="${pad.left}" y1="${pad.top}" y2="${pad.top + chartH}"></line>
          <line class="axis" x1="${pad.left}" x2="${width - pad.right}" y1="${pad.top + chartH}" y2="${pad.top + chartH}"></line>
          <text class="axis-title" x="${pad.left - 54}" y="${pad.top + chartH / 2}" text-anchor="middle" dominant-baseline="middle" transform="rotate(-90 ${pad.left - 54} ${pad.top + chartH / 2})" fill="var(--ink)">API cost</text>
          ${bars}
        </svg>`;
      document.querySelectorAll("#barChart [data-date]").forEach(node => {
        node.addEventListener("click", event => setSelectedDate(event.currentTarget.dataset.date, event.currentTarget.dataset.key));
      });
    }

    function heatValue(day) {
      if (!day) return 0;
      if (state.heatMetric === "cost") return costForByModel(day.byModel, day.usage);
      if (state.heatMetric === "sessions") return day.sessionCount || 0;
      if (state.heatMetric === "messages") return day.messageEvents?.total || 0;
      if (state.heatMetric === "events") return day.events || 0;
      if (state.heatMetric === "input") return uncachedInput(day.usage);
      if (state.heatMetric === "totalInput") return day.usage.input || 0;
      return day.usage[state.heatMetric] || 0;
    }

    function heatScaleMax(metric, observedMax) {
      if (HEATMAP_TOKEN_METRICS.has(metric)) return HEATMAP_TOKEN_FULL_SCALE;
      return Math.max(1, observedMax || 0);
    }

    function heatGradientColor(t) {
      const stops = HEATMAP_PROJECT_GRADIENT;
      let lo = stops[0], hi = stops[stops.length - 1];
      for (let i = 1; i < stops.length; i++) {
        if (t <= stops[i][0]) {
          lo = stops[i - 1];
          hi = stops[i];
          break;
        }
      }
      const local = (t - lo[0]) / Math.max(0.001, hi[0] - lo[0]);
      const rgb = lo[1].map((v, i) => Math.round(v + (hi[1][i] - v) * local));
      return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
    }

    function heatColor(value, maxValue) {
      if (!value || maxValue <= 0) return HEATMAP_EMPTY_COLOR;
      const t = Math.min(1, Math.max(0, value / maxValue));
      const step = Math.max(1, Math.ceil(t * HEATMAP_COLOR_STEPS));
      return heatGradientColor(step / HEATMAP_COLOR_STEPS);
    }

    function renderHeatmap() {
      const map = document.getElementById("heatmap");
      renderHeatYearOptions();
      const selectedYear = Number(state.heatYear);
      const weekCount = isoWeeksInYear(selectedYear);
      map.style.setProperty("--heat-weeks", weekCount);
      const generatedAt = new Date(DATA.meta.generatedAt);
      const currentWeekStart = mondayOfWeek(Number.isNaN(generatedAt.getTime()) ? new Date() : generatedAt);
      const weekStarts = [];
      for (let weekNumber = 1; weekNumber <= weekCount; weekNumber++) {
        weekStarts.push(isoWeekStart(selectedYear, weekNumber));
      }
      const horizonDays = [];
      for (const weekStart of weekStarts) {
        for (let weekdayIndex = 0; weekdayIndex < 7; weekdayIndex++) {
          const dayDate = new Date(weekStart);
          dayDate.setDate(dayDate.getDate() + weekdayIndex);
          if (dayDate.getFullYear() === selectedYear) {
            horizonDays.push(byDate.get(dateKey(dayDate)));
          }
        }
      }
      const observedMaxValue = Math.max(1, ...horizonDays.map(heatValue));
      const maxValue = heatScaleMax(state.heatMetric, observedMaxValue);
      const cells = [];
      cells.push('<div class="heatmap-corner" aria-hidden="true"></div>');
      cells.push(`<div class="heatmap-week-heading">${selectedYear}</div>`);
      cells.push('<div class="heatmap-week-row-spacer" aria-hidden="true"></div>');
      for (let weekIndex = 0; weekIndex < weekCount; weekIndex++) {
        const weekNumber = weekIndex + 1;
        const current = selectedYear === isoWeekYear(currentWeekStart) && weekNumber === isoWeekNumber(currentWeekStart);
        cells.push(`<div class="heatmap-week ${current ? "current" : ""}" title="Calendar week ${weekNumber}">${weekNumber}</div>`);
      }
      const dayLabels = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
      for (let weekdayIndex = 0; weekdayIndex < 7; weekdayIndex++) {
        cells.push(`<div class="heatmap-day-label">${dayLabels[weekdayIndex]}</div>`);
        for (const weekStart of weekStarts) {
          const cursor = new Date(weekStart);
          cursor.setDate(cursor.getDate() + weekdayIndex);
          const date = dateKey(cursor);
          const inYear = cursor.getFullYear() === selectedYear;
          if (!inYear) {
            cells.push('<span class="heat-cell empty out-year" aria-hidden="true"></span>');
            continue;
          }
          const day = byDate.get(date);
          const value = heatValue(day);
          const empty = !day || value <= 0;
          const active = date === state.selectedDate;
          const labelValue = state.heatMetric === "cost" ? money(value) : number(value);
          const usage = day?.usage || usageZero();
          const messageEvents = day?.messageEvents || messageEventsZero();
          cells.push(`<button class="heat-cell ${empty ? "empty" : ""} ${active ? "active" : ""}" data-date="${date}" data-real-input="${uncachedInput(usage)}" data-cached-input="${usage.cachedInput}" data-total-input="${usage.input}" data-output="${usage.output}" data-total="${usage.total}" data-sessions="${day?.sessionCount || 0}" data-messages="${messageEvents.total}" data-user-messages="${messageEvents.user}" data-agent-messages="${messageEvents.agent}" data-primary-agent-messages="${messageEvents.primaryAgent}" data-subagent-agent-messages="${messageEvents.subagentAgent}" data-events="${day?.events || 0}" data-cost="${costForByModel(day?.byModel || {}, usage)}" style="background:${heatColor(value, maxValue)}" aria-label="${dateLabel(date)} ${labelValue}"></button>`);
        }
      }
      map.innerHTML = cells.join("");
      map.querySelectorAll("[data-date]").forEach(node => {
        node.addEventListener("click", event => setSelectedDate(event.currentTarget.dataset.date));
        node.addEventListener("mouseenter", event => showHeatTooltip(event.currentTarget));
        node.addEventListener("mousemove", event => moveHeatTooltip(event));
        node.addEventListener("mouseleave", hideHeatTooltip);
      });
      const metricLabel = document.querySelector(`#heatMetric option[value="${state.heatMetric}"]`).textContent;
      const scaleNote = HEATMAP_TOKEN_METRICS.has(state.heatMetric)
        ? `warmer means more use; full color at ${compactAxis(HEATMAP_TOKEN_FULL_SCALE)} tokens.`
        : "warmer means more use.";
      document.getElementById("heatmapCaption").textContent = `${selectedYear} · ${weekCount} calendar weeks · ${metricLabel}; ${scaleNote}`;
      document.getElementById("heatmapScale").innerHTML = HEATMAP_SCALE_STEPS.map(v => `<i style="background:${heatColor(maxValue * v, maxValue)}"></i>`).join("") + "<span>More</span>";
      requestAnimationFrame(syncHeatmapPan);
    }

    function syncHeatmapPan() {
      const scroll = document.querySelector(".heatmap-scroll");
      const pan = document.getElementById("heatmapPan");
      const thumb = document.getElementById("heatmapPanThumb");
      if (!scroll || !pan || !thumb) return;

      const maxScroll = Math.max(0, scroll.scrollWidth - scroll.clientWidth);
      const visibleRatio = scroll.scrollWidth > 0 ? Math.min(1, scroll.clientWidth / scroll.scrollWidth) : 1;
      const hidden = maxScroll <= 1 || visibleRatio >= 0.999;
      pan.hidden = hidden;
      pan.setAttribute("aria-hidden", hidden ? "true" : "false");
      if (hidden) {
        state.heatScrollLeft = 0;
        scroll.scrollLeft = 0;
        return;
      }

      const nextLeft = Math.min(maxScroll, Math.max(0, state.heatScrollLeft || 0));
      if (Math.abs(scroll.scrollLeft - nextLeft) > 1) scroll.scrollLeft = nextLeft;

      const trackWidth = pan.clientWidth || scroll.clientWidth;
      const thumbWidth = Math.max(36, Math.round(trackWidth * visibleRatio));
      const maxThumbLeft = Math.max(0, trackWidth - thumbWidth);
      const thumbLeft = maxScroll > 0 && maxThumbLeft > 0 ? (scroll.scrollLeft / maxScroll) * maxThumbLeft : 0;

      pan.style.setProperty("--heat-pan-thumb-width", `${Math.round(thumbWidth)}px`);
      pan.style.setProperty("--heat-pan-thumb-left", `${Math.round(thumbLeft)}px`);
      pan.setAttribute("aria-valuemin", "0");
      pan.setAttribute("aria-valuemax", String(Math.round(maxScroll)));
      pan.setAttribute("aria-valuenow", String(Math.round(scroll.scrollLeft)));
      pan.setAttribute("aria-valuetext", `${Math.round(visibleRatio * 100)}% visible`);
    }

    function setHeatmapScrollLeft(value) {
      const scroll = document.querySelector(".heatmap-scroll");
      if (!scroll) return;
      const maxScroll = Math.max(0, scroll.scrollWidth - scroll.clientWidth);
      const nextLeft = Math.min(maxScroll, Math.max(0, value));
      state.heatScrollLeft = nextLeft;
      scroll.scrollLeft = nextLeft;
      syncHeatmapPan();
    }

    function setHeatmapPanFromClientX(clientX, pointerOffset = null) {
      const scroll = document.querySelector(".heatmap-scroll");
      const pan = document.getElementById("heatmapPan");
      const thumb = document.getElementById("heatmapPanThumb");
      if (!scroll || !pan || !thumb || pan.hidden) return;

      const maxScroll = Math.max(0, scroll.scrollWidth - scroll.clientWidth);
      const trackRect = pan.getBoundingClientRect();
      const thumbWidth = thumb.getBoundingClientRect().width || trackRect.width;
      const maxThumbLeft = Math.max(0, trackRect.width - thumbWidth);
      if (maxScroll <= 1 || maxThumbLeft <= 0) return;

      const offset = pointerOffset === null ? thumbWidth / 2 : pointerOffset;
      const thumbLeft = Math.min(maxThumbLeft, Math.max(0, clientX - trackRect.left - offset));
      setHeatmapScrollLeft((thumbLeft / maxThumbLeft) * maxScroll);
    }

    function showHeatTooltip(node) {
      const tooltip = document.getElementById("heatTooltip");
      const date = node.dataset.date;
      const sessions = Number(node.dataset.sessions || 0);
      const messages = Number(node.dataset.messages || 0);
      const userMessages = Number(node.dataset.userMessages || 0);
      const agentMessages = Number(node.dataset.agentMessages || 0);
      const primaryAgentMessages = Number(node.dataset.primaryAgentMessages || 0);
      const subagentAgentMessages = Number(node.dataset.subagentAgentMessages || 0);
      tooltip.innerHTML = `
        <div class="tooltip-date">${dateLabel(date)}</div>
        <dl>
          <dt>Real Input Tokens</dt><dd>${number(Number(node.dataset.realInput || 0))}</dd>
          <dt>Cached Input Tokens</dt><dd>${number(Number(node.dataset.cachedInput || 0))}</dd>
          <dt>Total Input Tokens</dt><dd>${number(Number(node.dataset.totalInput || 0))}</dd>
          <dt>Output Tokens</dt><dd>${number(Number(node.dataset.output || 0))}</dd>
          <dt>Total Tokens</dt><dd>${number(Number(node.dataset.total || 0))}</dd>
          <dt>Sessions</dt><dd>${number(sessions)}</dd>
          <dt>Total messages</dt><dd>${number(messages)}</dd>
          <dt>User messages</dt><dd>${number(userMessages)}</dd>
          <dt>Agent messages</dt><dd>${number(agentMessages)}</dd>
          <dt>Primary agent</dt><dd>${number(primaryAgentMessages)}</dd>
          <dt>Sub-agents</dt><dd>${number(subagentAgentMessages)}</dd>
          <dt>Events</dt><dd>${number(Number(node.dataset.events || 0))}</dd>
        </dl>
      `;
      tooltip.classList.add("visible");
      const rect = node.getBoundingClientRect();
      positionHeatTooltip(rect.left + rect.width / 2, rect.top);
    }

    function moveHeatTooltip(event) {
      positionHeatTooltip(event.clientX, event.clientY);
    }

    function positionHeatTooltip(x, y) {
      const tooltip = document.getElementById("heatTooltip");
      const margin = 12;
      const width = tooltip.offsetWidth || 220;
      const height = tooltip.offsetHeight || 128;
      const clampedX = Math.max(width / 2 + margin, Math.min(window.innerWidth - width / 2 - margin, x));
      const aboveY = y - 14;
      const finalY = aboveY - height < margin ? y + height + 24 : aboveY;
      tooltip.style.left = `${clampedX}px`;
      tooltip.style.top = `${finalY}px`;
      tooltip.style.transform = aboveY - height < margin ? "translate(-50%, 8px)" : "translate(-50%, -112%)";
    }

    function hideHeatTooltip() {
      document.getElementById("heatTooltip").classList.remove("visible");
    }

    const KPI_HELP = {
      total: "Sum of all tokens captured in the selected window. This is Real Input Tokens plus Cached Input Tokens plus Output Tokens; Reasoning Tokens are already included in Output Tokens.",
      sessions: "Unique Codex sessions or threads with activity in the selected window.",
      input: "Real Input Tokens are the non-cached input tokens in this dashboard: Total Input Tokens minus Cached Input Tokens. They can include fresh user text, tool results, file reads, and other new context.",
      totalInput: "Technical total input reported by Codex. This is Real Input Tokens plus Cached Input Tokens.",
      output: "Tokens generated by the model. This includes visible replies and internal Reasoning Tokens where Codex reports them.",
      cost: "What-if estimate of roughly what this usage would cost with the selected OpenAI API token prices. This is not your subscription bill.",
      inputCost: "Estimated cost for Real Input Tokens in the selected window.",
      cachedInputCost: "Estimated cost for Cached Input Tokens in the selected window.",
      outputCost: "Estimated cost for Output Tokens in the selected window. Reasoning Tokens are part of Output Tokens and are included here.",
      cachedInput: "Input tokens served from prompt cache. Cached Input Tokens can include reused conversation context and other repeated prompt material that the API cache can reuse.",
      cacheShare: "Share of Total Input Tokens that were served from prompt cache in the selected window.",
      reasoning: "Subset of Output Tokens reported as Reasoning Tokens. These tokens are already included in Output Tokens and Total Tokens.",
      agentsSpawned: "Unique subagent sessions whose first captured timestamp falls inside the selected window. This counts spawned agents, not sub-agent messages.",
      messages: "Reconstructed Codex message events: user_message plus agent_message. Agent messages are also split into primary-agent and sub-agent sessions when metadata exposes that distinction. This is not the same as only manually typed chat messages.",
      userWords: "Words counted from primary-session user_message text. This excludes tool results and file reads, and does not store the message text in synced snapshots.",
      avgUserWords: "Average words per counted primary user message in the selected window.",
      events: "Number of token_count measurement points in the local Codex logs for the selected window. An event is a usage update, not necessarily a single message or session.",
      outputShare: "Share of Output Tokens within the selected window's Total Tokens. Lower values usually mean heavier context/input compared with generated replies."
    };

    function inputBreakdownText(usage) {
      return [
        `Real Input Tokens: ${number(uncachedInput(usage))}`,
        `Cached Input Tokens: ${number(usage.cachedInput || 0)}`,
        `Total Input Tokens: ${number(usage.input || 0)}`,
        "Real Input Tokens can include new user text, tool results, file reads, and other fresh context.",
        "Cached Input Tokens are reused prompt material served from cache."
      ].join("\n");
    }

    function costBreakdownText(parts) {
      return [
        `Real Input Cost: ${money(parts.inputCost)}`,
        `Cached Input Cost: ${money(parts.cachedInputCost)}`,
        `Output Cost: ${money(parts.outputCost)}`,
        "Reasoning Tokens are included in Output Cost; there is no separate reasoning price here."
      ].join("\n");
    }

    function messageBreakdownText(messageEvents) {
      return [
        `User messages: ${number(messageEvents.user)}`,
        `Agent messages: ${number(messageEvents.agent)}`,
        `Primary agent: ${number(messageEvents.primaryAgent)}`,
        `Sub-agents: ${number(messageEvents.subagentAgent)}`
      ].join("\n");
    }

    function userTextBreakdownText(userText) {
      const stats = userText || userTextZero();
      return [
        `Counted user messages: ${number(stats.messages || 0)}`,
        `User words: ${number(stats.words || 0)}`,
        "Based on stored user_message text only; prompts, responses, tool output, file reads, and local paths are not stored in synced snapshots."
      ].join("\n");
    }

    function agentsSpawnedForContext(context) {
      if (!context) return 0;
      const subagents = (DATA.sessions || []).filter(session => session.isSubagent);
      if (context.scope === "hour") {
        return subagents.filter(session => {
          const seen = new Date(session.firstSeen || session.lastSeen || "");
          return !Number.isNaN(seen.getTime()) && hourKeyFromDate(seen) === context.key;
        }).length;
      }
      const dates = new Set(selectedContextDates(context));
      return subagents.filter(session => {
        const seen = new Date(session.firstSeen || session.lastSeen || "");
        return !Number.isNaN(seen.getTime()) && dates.has(dateKey(seen));
      }).length;
    }

    function agentsSpawnedText(count) {
      return [
        `${number(count)} unique subagent session${count === 1 ? "" : "s"} started in this selected window.`,
        "This is different from Sub-agent messages; it counts agent sessions, not their replies."
      ].join("\n");
    }

    function kpiCard(key, label, value, detail = "") {
      const description = KPI_HELP[key] || "";
      return `<div class="mini has-help" tabindex="0" data-kpi="${key}" data-title="${escapeHtml(label)}" data-description="${escapeHtml(description)}" data-detail="${escapeHtml(detail)}"><span>${label}</span><strong>${value}</strong></div>`;
    }

    function sessionTitleMarkup(title) {
      const safeTitle = escapeHtml(title || "Untitled Codex session");
      return `
        <button class="session-title-toggle" type="button" aria-expanded="false" aria-label="Expand session title">
          <span class="session-title-text">${safeTitle}</span>
        </button>
      `;
    }

    function toggleSessionTitle(button) {
      const expanded = button.getAttribute("aria-expanded") === "true";
      button.setAttribute("aria-expanded", expanded ? "false" : "true");
      button.setAttribute("aria-label", expanded ? "Expand session title" : "Collapse session title");
      button.classList.toggle("session-title-expanded", !expanded);
      requestAnimationFrame(() => {
        syncDetailsHeight();
        syncBottomInsightsHeight();
      });
    }

    function attachKpiTooltips() {
      document.querySelectorAll("[data-kpi]").forEach(node => {
        node.addEventListener("mouseenter", event => showKpiTooltip(event.currentTarget));
        node.addEventListener("mousemove", event => moveKpiTooltip(event));
        node.addEventListener("mouseleave", hideKpiTooltip);
        node.addEventListener("focus", event => showKpiTooltip(event.currentTarget));
        node.addEventListener("blur", hideKpiTooltip);
      });
    }

    function showKpiTooltip(node) {
      const tooltip = document.getElementById("kpiTooltip");
      const detail = node.dataset.detail
        ? `<div class="tooltip-detail">${escapeHtml(node.dataset.detail).replaceAll("\n", "<br>")}</div>`
        : "";
      tooltip.innerHTML = `
        <div class="tooltip-title">${escapeHtml(node.dataset.title || "")}</div>
        <div class="tooltip-body">${escapeHtml(node.dataset.description || "")}</div>
        ${detail}
      `;
      tooltip.classList.add("visible");
      const rect = node.getBoundingClientRect();
      positionKpiTooltip(rect.left + rect.width / 2, rect.top);
    }

    function moveKpiTooltip(event) {
      positionKpiTooltip(event.clientX, event.clientY);
    }

    function positionKpiTooltip(x, y) {
      const tooltip = document.getElementById("kpiTooltip");
      const margin = 12;
      const width = tooltip.offsetWidth || 260;
      const height = tooltip.offsetHeight || 118;
      const clampedX = Math.max(width / 2 + margin, Math.min(window.innerWidth - width / 2 - margin, x));
      const aboveY = y - 14;
      const showBelow = aboveY - height < margin;
      tooltip.style.left = `${clampedX}px`;
      tooltip.style.top = `${showBelow ? y + 12 : aboveY}px`;
      tooltip.style.transform = showBelow ? "translate(-50%, 8px)" : "translate(-50%, -112%)";
    }

    function hideKpiTooltip() {
      document.getElementById("kpiTooltip").classList.remove("visible");
    }

    function renderDayDetails() {
      const context = selectedBucketContext();
      const bucket = context.bucket;
      const rows = selectedSessionRows(context);
      const usage = bucket.usage || usageZero();
      const messageEvents = bucket.messageEvents || messageEventsZero();
      const userText = bucket.userText || userTextZero();
      const avgUserWords = userText.messages ? userText.words / userText.messages : 0;
      const totalIO = Math.max(1, (usage.input || 0) + (usage.output || 0));
      const inputShare = Math.max(1, Math.round((usage.input || 0) / totalIO * 100));
      const outputShare = Math.max(1, Math.round((usage.output || 0) / totalIO * 100));
      const cost = costForByModel(bucket.byModel || {}, usage);
      const costParts = costPartsForByModel(bucket.byModel || {}, usage);
      const costDetail = costBreakdownText(costParts);
      const agentsSpawned = agentsSpawnedForContext(context);
      const label = scopeLabel(context.scope);
      document.getElementById("dayDetails").innerHTML = `
        <div class="day-title">
          <div>
            <h2>Selected ${label}</h2>
            <div class="date">${escapeHtml(context.title)}</div>
          </div>
        </div>
        <div class="split-meter" style="--input-share:${inputShare}fr; --output-share:${outputShare}fr">
          <div class="input" title="Input share"></div>
          <div class="output" title="Output share"></div>
        </div>
        <div class="summary-strip">
          ${kpiCard("total", "Total Tokens", compact(usage.total))}
          ${kpiCard("input", "Real Input Tokens", compact(uncachedInput(usage)), inputBreakdownText(usage))}
          ${kpiCard("cachedInput", "Cached Input Tokens", compact(usage.cachedInput), inputBreakdownText(usage))}
          ${kpiCard("output", "Output Tokens", compact(usage.output))}
        </div>
        <div class="summary-strip">
          ${kpiCard("cost", "Total Cost", money(cost), costDetail)}
          ${kpiCard("inputCost", "Real Input Cost", money(costParts.inputCost), costDetail)}
          ${kpiCard("cachedInputCost", "Cached Input Cost", money(costParts.cachedInputCost), costDetail)}
          ${kpiCard("outputCost", "Output Cost", money(costParts.outputCost), costDetail)}
        </div>
        <div class="summary-strip">
          ${kpiCard("sessions", "Sessions", number(rows.length))}
          ${kpiCard("messages", "Total Messages", number(messageEvents.total), messageBreakdownText(messageEvents))}
          ${kpiCard("userWords", "User Words", number(userText.words || 0), userTextBreakdownText(userText))}
          ${kpiCard("avgUserWords", "Avg Words / Message", avgUserWords.toFixed(1), userTextBreakdownText(userText))}
        </div>
        <div class="summary-strip">
          ${kpiCard("totalInput", "Total Input Tokens", compact(usage.input), inputBreakdownText(usage))}
          ${kpiCard("cacheShare", "Cache Share", precisePct(usage.input ? usage.cachedInput / usage.input * 100 : 0), inputBreakdownText(usage))}
          ${kpiCard("outputShare", "Output Share", precisePct(usage.total ? usage.output / usage.total * 100 : 0))}
          ${kpiCard("agentsSpawned", "Agents Spawned", number(agentsSpawned), agentsSpawnedText(agentsSpawned))}
        </div>
        <div class="summary-strip">
          ${kpiCard("events", "Events", number(bucket.events || 0))}
          ${kpiCard("reasoning", "Reasoning Tokens", compact(usage.reasoningOutput))}
        </div>
      `;
      attachKpiTooltips();
    }

    function renderSessionTable() {
      const context = selectedBucketContext();
      const label = scopeLabel(context.scope);
      const rows = selectedSessionRows(context);
      document.getElementById("sessionTableTitle").textContent = `Sessions In Selected ${label}`;
      if (rows.length === 0) {
        document.getElementById("sessionTable").innerHTML = `<div class="empty-state">No session activity in this selected ${label.toLowerCase()}.</div>`;
        return;
      }
      document.getElementById("sessionTable").innerHTML = `
        <table>
          <thead>
            <tr>
              <th>Session</th>
              <th>Total Tokens</th>
              <th>Real Input Tokens</th>
              <th>Output Tokens</th>
              <th>Cached Input Tokens</th>
              <th>Reasoning Tokens</th>
              <th>Total Messages</th>
              <th>Total Cost</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(row => `
              <tr>
                <td>
                  <div class="session-title">${sessionTitleMarkup(row.title)}</div>
                  <div class="session-sub">${escapeHtml([row.model, row.cwd || "No cwd captured", row.deviceName].filter(Boolean).join(" · "))}</div>
                </td>
                <td>${compact(row.usage.total)}</td>
                <td>${compact(uncachedInput(row.usage))}</td>
                <td>${compact(row.usage.output)}</td>
                <td>${compact(row.usage.cachedInput)}</td>
                <td>${compact(row.usage.reasoningOutput)}</td>
                <td>${number(row.messageEvents?.total || 0)}</td>
                <td>${money(costForByModel(row.byModel, row.usage))}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>`;
    }

    function escapeHtml(value) {
      return String(value || "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function projectNameFromPath(path) {
      const clean = String(path || "").replace(/\/+$/, "");
      if (!clean || clean === "No cwd captured") return "Unknown Project";
      const parts = clean.split("/").filter(Boolean);
      return parts.at(-1) || clean;
    }

    function compactProjectPath(path) {
      const clean = String(path || "");
      if (!clean || clean === "No cwd captured") return "No cwd captured";
      return clean.replace(/^\/Users\/[^/]+/, "~");
    }

    function aggregateProjects() {
      const projects = new Map();
      for (const session of DATA.sessions || []) {
        const key = session.cwd || "No cwd captured";
        if (!projects.has(key)) {
          projects.set(key, {
            cwd: key,
            name: projectNameFromPath(key),
            usage: usageZero(),
            byModel: {},
            sessions: 0,
            events: 0,
            lastSeen: ""
          });
        }
        const target = projects.get(key);
        addUsage(target.usage, session.usage || usageZero());
        addByModelUsage(target.byModel, session.byModel || {});
        target.sessions += 1;
        target.events += session.events || 0;
        if (session.lastSeen && (!target.lastSeen || session.lastSeen > target.lastSeen)) target.lastSeen = session.lastSeen;
      }
      return Array.from(projects.values()).sort((a, b) => (b.usage.total || 0) - (a.usage.total || 0));
    }

    function renderTopSessions() {
      const top = DATA.sessions.slice(0, 10);
      const max = Math.max(1, ...top.map(item => item.usage.total || 0));
      document.getElementById("topSessionsCaption").textContent = `Top ${top.length} of ${DATA.sessions.length}`;
      document.getElementById("topSessions").innerHTML = top.map(item => {
        const relativeShare = Math.max(0, Math.min(100, (item.usage.total || 0) / max * 100));
        const inputShare = Math.max(0, Math.min(100, (item.usage.input || 0) / Math.max(1, item.usage.total || 0) * 100));
        const outputShare = Math.max(0, Math.min(100, (item.usage.output || 0) / Math.max(1, item.usage.total || 0) * 100));
        return `
          <article class="top-item">
            <h3>${sessionTitleMarkup(item.title)}</h3>
            <div class="top-meta">
              <span>${compact(item.usage.total)} Tokens</span>
              <span>${money(costForByModel(item.byModel, item.usage))}</span>
              <span>${escapeHtml(item.model)}</span>
              ${item.deviceName ? `<span>${escapeHtml(item.deviceName)}</span>` : ""}
            </div>
            <div class="progress" title="${Math.round(relativeShare)}% of top session, split by Total Input Tokens and Output Tokens">
              <div class="progress-fill" style="width:${relativeShare}%">
                <div class="input" style="width:${inputShare}%"></div>
                <div class="output" style="width:${outputShare}%"></div>
              </div>
            </div>
            <div class="top-meta">
              <span>${item.lastSeen ? new Date(item.lastSeen).toLocaleString() : ""}</span>
              <span>${Math.round(relativeShare)}% of top session</span>
            </div>
          </article>
        `;
      }).join("");
    }

    function renderTopProjects() {
      const projects = aggregateProjects();
      const top = projects.slice(0, 10);
      const max = Math.max(1, ...top.map(item => item.usage.total || 0));
      document.getElementById("topProjectsCaption").textContent = `Top ${top.length} Projects by Total Tokens out of ${projects.length} Projects`;
      if (top.length === 0) {
        document.getElementById("topProjects").innerHTML = '<div class="empty-state">No project usage captured.</div>';
        return;
      }
      document.getElementById("topProjects").innerHTML = top.map((item, index) => {
        const share = Math.max(0.5, Math.min(100, (item.usage.total || 0) / max * 100));
        const cost = costForByModel(item.byModel, item.usage);
        const sessionWord = item.sessions === 1 ? "session" : "sessions";
        return `
          <article class="project-bar-row" title="${escapeHtml(compactProjectPath(item.cwd))}">
            <div>
              <div class="project-name">${index + 1}. ${escapeHtml(item.name)}</div>
              <div class="project-path">${escapeHtml(compactProjectPath(item.cwd))}</div>
            </div>
            <div class="project-track" aria-label="${escapeHtml(item.name)} Total Tokens">
              <div class="project-fill" style="width:${share}%"></div>
            </div>
            <div class="project-metrics">
              <strong>${compact(item.usage.total)}</strong>
              <span>${money(cost)} · ${number(item.sessions)} ${sessionWord}</span>
            </div>
          </article>
        `;
      }).join("");
    }

    function renderModelBreakdown() {
      const days = filteredDays();
      const agg = aggregate(days);
      const buildRow = (model, usage, effort = null, childCount = 0) => {
          const pricedModel = state.price === "logged" ? model : state.price;
          const costs = costPartsForUsage(usage, pricedModel);
          return {
            model,
            effort,
            childCount,
            usage,
            inputCost: costs.inputCost,
            cachedInputCost: costs.cachedInputCost,
            outputCost: costs.outputCost,
            cost: costs.totalCost,
            share: agg.usage.total ? usage.total / agg.usage.total * 100 : 0,
            ratio: usage.total ? usage.output / usage.total * 100 : 0,
          };
        };
      const rows = Object.entries(agg.byModel || {})
        .map(([model, usage]) => buildRow(model, usage, null, Object.keys(agg.byModelEffort?.[model] || {}).length))
        .sort((a, b) => (b.usage.total || 0) - (a.usage.total || 0));

      document.getElementById("modelBreakdownCaption").textContent =
        state.price === "logged" ? "click a model to split by reasoning effort" : `cost shown as if all usage were ${state.price}`;

      if (rows.length === 0) {
        document.getElementById("modelBreakdown").innerHTML = '<div class="empty-state">No model usage in this range.</div>';
        return;
      }

      const renderUsageCells = row => `
        <td>${compact(row.usage.total)}</td>
        <td>${precisePct(row.share)}</td>
        <td>${compact(uncachedInput(row.usage))}</td>
        <td>${compact(row.usage.output)}</td>
        <td>${precisePct(row.ratio)}</td>
        <td>${compact(row.usage.cachedInput)}</td>
        <td>${compact(row.usage.reasoningOutput)}</td>
        <td>${money(row.inputCost)}</td>
        <td>${money(row.cachedInputCost)}</td>
        <td>${money(row.outputCost)}</td>
        <td>${money(row.cost)}</td>
      `;

      const renderModelRow = row => {
        const effortEntries = Object.entries(agg.byModelEffort?.[row.model] || {})
          .sort(([a], [b]) => effortRank(a) - effortRank(b))
          .map(([effort, usage]) => buildRow(row.model, usage, effort));
        const canExpand = effortEntries.length > 1 || (effortEntries.length === 1 && effortEntries[0].effort !== "unknown");
        const expanded = expandedModelRows.has(row.model);
        const mainTitle = canExpand
          ? `<button class="model-toggle" type="button" data-model="${escapeHtml(row.model)}" aria-expanded="${expanded ? "true" : "false"}">${escapeHtml(row.model)}</button>`
          : `<div class="session-title">${escapeHtml(row.model)}</div>`;
        const mainRow = `
          <tr>
            <td>
              ${mainTitle}
              <div class="session-sub">${canExpand ? `${effortEntries.length} reasoning effort${effortEntries.length === 1 ? "" : "s"}` : "No effort split captured"}</div>
            </td>
            ${renderUsageCells(row)}
          </tr>
        `;
        const childRows = !expanded ? "" : effortEntries.map(child => `
          <tr class="model-effort-row">
            <td>
              <div class="session-title model-effort-name">${escapeHtml(effortLabel(child.effort))}</div>
              <div class="session-sub model-effort-name">Reasoning effort for ${escapeHtml(child.model)}</div>
            </td>
            ${renderUsageCells(child)}
          </tr>
        `).join("");
        return mainRow + childRows;
      };

      document.getElementById("modelBreakdown").innerHTML = `
        <table class="model-table">
          <thead>
            <tr>
              <th>Model</th>
              <th>Total Tokens</th>
              <th>Share</th>
              <th>Real Input Tokens</th>
              <th>Output Tokens</th>
              <th>Output Ratio</th>
              <th>Cached Input Tokens</th>
              <th>Reasoning Tokens</th>
              <th>Real Input Cost</th>
              <th>Cached Input Cost</th>
              <th>Output Cost</th>
              <th>Total Cost</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(renderModelRow).join("")}
          </tbody>
        </table>`;
      document.querySelectorAll(".model-toggle").forEach(button => {
        button.addEventListener("click", event => {
          const model = event.currentTarget.dataset.model;
          if (expandedModelRows.has(model)) expandedModelRows.delete(model);
          else expandedModelRows.add(model);
          renderModelBreakdown();
        });
      });
    }

    function renderFooter() {
      const sources = DATA.meta.priceSources.map(source => `<a href="${source.url}">${escapeHtml(source.label)}</a>`).join(" · ");
      document.getElementById("footer").innerHTML = `
        Generated ${new Date(DATA.meta.generatedAt).toLocaleString()} from <code>${escapeHtml(DATA.meta.codexHome)}</code>.
        Total Tokens are Real Input Tokens plus Cached Input Tokens plus Output Tokens. Reasoning Tokens are included in Output Tokens.
        "Total: detected model mix" means each model row is priced with that model's API rate, then summed. Model attribution comes from Codex turn/session metadata; token_count events do not carry a model field directly.
        Messages are reconstructed from user_message and agent_message log events; sessions in the range summary are unique thread IDs active in the selected range.
        Price sources: ${sources}.
      `;
    }

    function ensureInitialDate() {
      if (state.selectedDate && byDate.has(state.selectedDate)) return;
      const active = DATA.daily.filter(day => (day.usage.total || 0) > 0);
      state.selectedDate = (active.at(-1) || DATA.daily.at(-1) || { date: null }).date;
    }

    function syncDetailsHeight() {
      const details = document.querySelector(".details");
      const topStack = document.querySelector(".top-stack");
      if (!details || !topStack) return;
      if (window.matchMedia("(max-width: 1120px)").matches) {
        details.style.height = "";
        return;
      }
      const height = topStack.getBoundingClientRect().height;
      if (height > 0) details.style.height = `${Math.round(height)}px`;
    }

    function syncBottomInsightsHeight() {
      const sessions = document.querySelector(".top-sessions-panel");
      const projects = document.querySelector(".top-projects-panel");
      if (!sessions || !projects) return;
      sessions.style.height = "";
      if (window.matchMedia("(max-width: 1120px)").matches) return;
      const height = projects.getBoundingClientRect().height;
      if (height > 0) sessions.style.height = `${Math.round(height)}px`;
    }

    function renderAll() {
      renderNoDataState();
      ensureInitialDate();
      renderStats();
      renderBars();
      renderRangeSummary();
      renderHeatmap();
      renderDayDetails();
      renderSessionTable();
      renderModelBreakdown();
      renderTopSessions();
      renderTopProjects();
      renderFooter();
      requestAnimationFrame(() => {
        syncDetailsHeight();
        syncBottomInsightsHeight();
      });
    }

    document.getElementById("rangeSelect").addEventListener("change", event => {
      state.range = event.target.value;
      renderAll();
    });
    document.getElementById("deviceSelect").addEventListener("change", event => {
      setActiveDevice(event.target.value);
    });
    document.getElementById("timezoneSelect").addEventListener("change", event => {
      setTimezone(event.target.value);
    });
    document.getElementById("heatMetric").addEventListener("change", event => {
      state.heatMetric = event.target.value;
      renderAll();
    });
    document.getElementById("heatYear").addEventListener("change", event => {
      state.heatYear = Number(event.target.value);
      state.heatScrollLeft = 0;
      renderAll();
    });
    document.addEventListener("click", event => {
      const button = event.target.closest(".session-title-toggle");
      if (!button) return;
      toggleSessionTitle(button);
    });
    document.getElementById("heatmapPan").addEventListener("pointerdown", event => {
      const pan = event.currentTarget;
      const thumb = document.getElementById("heatmapPanThumb");
      if (!thumb || pan.hidden) return;
      const thumbRect = thumb.getBoundingClientRect();
      const clickedThumb = event.clientX >= thumbRect.left && event.clientX <= thumbRect.right;
      const pointerOffset = clickedThumb ? event.clientX - thumbRect.left : thumbRect.width / 2;
      pan.setPointerCapture(event.pointerId);
      pan.classList.add("dragging");
      heatmapPanDrag = { pointerId: event.pointerId, pointerOffset };
      setHeatmapPanFromClientX(event.clientX, pointerOffset);
      event.preventDefault();
    });
    document.getElementById("heatmapPan").addEventListener("pointermove", event => {
      if (!heatmapPanDrag || heatmapPanDrag.pointerId !== event.pointerId) return;
      setHeatmapPanFromClientX(event.clientX, heatmapPanDrag.pointerOffset);
      event.preventDefault();
    });
    document.getElementById("heatmapPan").addEventListener("pointerup", event => {
      if (!heatmapPanDrag || heatmapPanDrag.pointerId !== event.pointerId) return;
      heatmapPanDrag = null;
      event.currentTarget.classList.remove("dragging");
      event.currentTarget.releasePointerCapture(event.pointerId);
    });
    document.getElementById("heatmapPan").addEventListener("pointercancel", event => {
      heatmapPanDrag = null;
      event.currentTarget.classList.remove("dragging");
    });
    document.getElementById("heatmapPan").addEventListener("keydown", event => {
      const scroll = document.querySelector(".heatmap-scroll");
      if (!scroll) return;
      const weekStep = 23;
      const pageStep = Math.max(weekStep, scroll.clientWidth * 0.8);
      const current = scroll.scrollLeft;
      let next = null;
      if (event.key === "ArrowLeft") next = current - weekStep;
      else if (event.key === "ArrowRight") next = current + weekStep;
      else if (event.key === "PageUp") next = current - pageStep;
      else if (event.key === "PageDown") next = current + pageStep;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = scroll.scrollWidth;
      if (next === null) return;
      setHeatmapScrollLeft(next);
      event.preventDefault();
    });
    document.querySelector(".heatmap-scroll").addEventListener("scroll", event => {
      state.heatScrollLeft = event.currentTarget.scrollLeft;
      syncHeatmapPan();
    });
    document.getElementById("chartResolution").addEventListener("change", event => {
      state.chartResolution = event.target.value;
      state.selectedChartKey = null;
      selectVisibleBucket();
      renderAll();
    });
    document.getElementById("chartPeriodPrev").addEventListener("click", () => shiftChartWindow(-1));
    document.getElementById("chartPeriodNext").addEventListener("click", () => shiftChartWindow(1));
    document.getElementById("chartCalendarButton").addEventListener("click", () => {
      const input = document.getElementById("chartWindowDate");
      if (!input) return;
      if (typeof input.showPicker === "function") {
        try {
          input.showPicker();
          return;
        } catch (error) {
          // Fall back for browsers that expose showPicker but reject hidden inputs.
        }
      }
      {
        input.focus();
        input.click();
      }
    });
    document.getElementById("chartWindowDate").addEventListener("change", event => setChartWindowDate(event.target.value));
    document.getElementById("priceSelect").addEventListener("change", event => {
      state.price = event.target.value;
      renderAll();
    });
    document.getElementById("chartMetric").addEventListener("change", event => {
      state.chartMode = event.target.value;
      state.selectedChartKey = null;
      renderAll();
    });
    window.addEventListener("resize", () => {
      renderHeatmap();
      requestAnimationFrame(() => {
        syncDetailsHeight();
        syncBottomInsightsHeight();
      });
    });

    refreshDataIndexes();
    renderPriceOptions();
    renderDeviceOptions();
    renderTimezoneOptions();
    renderAll();
  </script>
</body>
</html>
"""


def write_outputs(payload: dict[str, Any], out: Path, json_out: Path | None) -> None:
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html = HTML_TEMPLATE.replace("__DATA_JSON__", data_json)
    out.write_text(html, encoding="utf-8")
    if json_out:
        json_out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def build_dashboard_payload(
    codex_home: Path,
    timezone_name: str,
    redact: bool = False,
    snapshot_dir: Path | None = None,
    device: SnapshotDevice | None = None,
    project_aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    aliases = project_aliases or {}
    raw_payload = build_payload(codex_home, timezone_name)
    apply_project_aliases(raw_payload, aliases)
    if snapshot_dir and device:
        current_snapshot = create_snapshot_payload(raw_payload, device)
        write_device_snapshot(snapshot_dir, current_snapshot)
        snapshots = load_snapshot_payloads(snapshot_dir)
        combined_payload = combine_snapshot_payloads(snapshots, timezone_name)
        apply_project_aliases(combined_payload, aliases)
        return combined_payload
    if redact:
        redact_payload(raw_payload)
    return raw_payload


def resolve_generator_source(value: str) -> Path | None:
    if value:
        source = Path(value).expanduser().resolve()
        return source if source.exists() else None
    try:
        source = Path(__file__).expanduser().resolve()
    except NameError:
        return None
    return source if source.exists() else None


def generate_with_current_source(
    generator_source: Path,
    codex_home: Path,
    timezone_name: str,
    out: Path,
    json_out: Path | None,
    redact: bool = False,
    snapshot_dir: Path | None = None,
    device_name: str = "",
    no_snapshot: bool = False,
    project_aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(generator_source),
        "--codex-home",
        str(codex_home),
        "--out",
        str(out),
        "--timezone",
        timezone_name,
        "--generator-source",
        str(generator_source),
    ]
    if json_out:
        command.extend(["--json-out", str(json_out)])
    else:
        command.append("--no-json")
    if redact:
        command.append("--redact")
    if snapshot_dir:
        command.extend(["--snapshot-dir", str(snapshot_dir)])
    if device_name:
        command.extend(["--device-name", device_name])
    if no_snapshot:
        command.append("--no-snapshot")
    for source_key, target in sorted((project_aliases or {}).items()):
        command.extend(["--project-alias", f"{source_key}={target}"])

    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"latest generator failed: {detail}")

    if json_out and json_out.exists():
        return json.loads(json_out.read_text(encoding="utf-8"))

    snapshot_setup = None if no_snapshot else resolve_snapshot_setup(argparse.Namespace(snapshot_dir=str(snapshot_dir or ""), device_name=device_name, no_snapshot=no_snapshot))
    local_snapshot_dir, local_device = snapshot_setup if snapshot_setup else (None, None)
    return build_dashboard_payload(codex_home, timezone_name, redact, local_snapshot_dir, local_device, project_aliases)


def find_available_port(start_port: int) -> int:
    for port in range(start_port, start_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No available localhost port found from {start_port} to {start_port + 49}")


class QuietHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return


def make_refreshing_handler(
    directory: Path,
    dashboard_name: str,
    json_name: str | None,
    refresh_outputs: Any,
) -> type[QuietHTTPRequestHandler]:
    refresh_names = {dashboard_name}
    if json_name:
        refresh_names.add(json_name)

    class RefreshingDashboardHandler(QuietHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(directory), **kwargs)

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            super().end_headers()

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query)
            requested_timezone = (query.get("timezone") or [""])[0]
            timezone_override = valid_timezone_name(requested_timezone)
            if requested_timezone and not timezone_override:
                self.send_error(400, "Unsupported timezone")
                return
            if path in ("", "/"):
                path = f"/{dashboard_name}"
                self.path = f"{path}?{parsed.query}" if parsed.query else path
            if Path(path).name in refresh_names:
                try:
                    refresh_outputs(timezone_override)
                except Exception as exc:
                    self.send_error(500, f"Could not refresh dashboard: {exc}")
                    return
            super().do_GET()

    return RefreshingDashboardHandler


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def serve_dashboard(
    out: Path,
    json_out: Path | None,
    preferred_port: int,
    open_browser: bool,
    refresh_outputs: Any,
    url_file: Path | None,
) -> None:
    port = find_available_port(preferred_port)
    handler = make_refreshing_handler(
        out.parent,
        out.name,
        json_out.name if json_out else None,
        refresh_outputs,
    )
    with ReusableTCPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{port}/{out.name}"
        if url_file:
            url_file.parent.mkdir(parents=True, exist_ok=True)
            url_file.write_text(url, encoding="utf-8")
        print(f"Serving dashboard at {url}")
        if open_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped local dashboard server")
        finally:
            if url_file and url_file.exists():
                try:
                    if url_file.read_text(encoding="utf-8").strip() == url:
                        url_file.unlink()
                except OSError:
                    pass


def main() -> None:
    args = parse_args()
    codex_home = Path(args.codex_home).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    json_out = None if args.no_json else Path(args.json_out).expanduser().resolve()
    url_file = Path(args.server_url_file).expanduser().resolve() if args.server_url_file else None
    generator_source = resolve_generator_source(args.generator_source)
    snapshot_dir, snapshot_device = resolve_snapshot_setup(args)
    project_aliases = resolve_project_aliases(args)

    def refresh_outputs(timezone_override: str | None = None) -> dict[str, Any]:
        timezone_name = timezone_override or args.timezone
        if args.serve and generator_source:
            return generate_with_current_source(
                generator_source,
                codex_home,
                timezone_name,
                out,
                json_out,
                args.redact,
                snapshot_dir,
                args.device_name,
                args.no_snapshot,
                project_aliases,
            )
        refreshed_payload = build_dashboard_payload(codex_home, timezone_name, args.redact, snapshot_dir, snapshot_device, project_aliases)
        write_outputs(refreshed_payload, out, json_out)
        return refreshed_payload

    payload = refresh_outputs()

    total = payload["totals"]["usage"]["total"]
    sessions = payload["meta"]["sessionsWithUsage"]
    print(f"Wrote {out}")
    if json_out:
        print(f"Wrote {json_out}")
    print(f"Parsed {sessions} sessions with {total:,} total tokens")
    if args.serve:
        serve_dashboard(
            out,
            json_out,
            args.port,
            open_browser=not args.no_open,
            refresh_outputs=refresh_outputs,
            url_file=url_file,
        )
    elif args.open:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
