"""Request usage accounting for the local dashboard."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config.repository import _get_db_path

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]|\S")
_MODEL_ALIASES = {
    ("tongji", "gpt-5.5"): "glm-5.1",
    ("tongji", "gpt5.5"): "glm-5.1",
}


@dataclass
class UsageRecord:
    provider: str
    protocol: str
    model: str
    status: str
    started_at: int
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    account_id: str = ""
    error: str = ""


def estimate_tokens(text: str) -> int:
    return len(_TOKEN_PATTERN.findall(text or ""))


def init_usage_schema(db_path: Path | None = None) -> None:
    conn = sqlite3.connect(db_path or _get_db_path())
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_record (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                protocol TEXT NOT NULL,
                model TEXT NOT NULL,
                account_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_usage_started_at ON usage_record(started_at)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_usage_model ON usage_record(model)")
        conn.commit()
    finally:
        conn.close()


def record_usage(record: UsageRecord, db_path: Path | None = None) -> None:
    conn = sqlite3.connect(db_path or _get_db_path())
    try:
        init_usage_schema(db_path)
        conn.execute(
            """
            INSERT INTO usage_record (
                provider, protocol, model, account_id, status, started_at, latency_ms,
                input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.provider,
                record.protocol,
                record.model,
                record.account_id,
                record.status,
                record.started_at,
                record.latency_ms,
                record.input_tokens,
                record.output_tokens,
                record.cache_creation_tokens,
                record.cache_read_tokens,
                record.error,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def usage_summary(
    *,
    days: int = 7,
    granularity: str = "day",
    db_path: Path | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    init_usage_schema(db_path)
    current = int(time.time() if now is None else now)
    safe_days = min(max(int(days or 7), 1), 90)
    bucket_mode = "hour" if granularity == "hour" else "day"
    since = current - safe_days * 86400
    rows = _usage_rows(since, db_path)
    all_rows = _usage_rows(None, db_path)
    totals = _usage_totals(rows)
    all_time_totals = _usage_totals(all_rows)
    models = _usage_models(rows)
    buckets = _usage_buckets(rows, since=since, now=current, granularity=bucket_mode)
    recent = [
        {
            "model": _usage_model_name(row),
            "provider": row["provider"],
            "protocol": row["protocol"],
            "status": row["status"],
            "started_at": row["started_at"],
            "latency_ms": row["latency_ms"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "total_tokens": (
                row["input_tokens"]
                + row["output_tokens"]
                + row["cache_creation_tokens"]
                + row["cache_read_tokens"]
            ),
            "account_id": row["account_id"],
            "error": row["error"],
        }
        for row in sorted(rows, key=lambda item: int(item["started_at"]), reverse=True)[:30]
    ]
    return {
        "window_days": safe_days,
        "granularity": bucket_mode,
        "generated_at": current,
        "totals": totals,
        "totals_all_time": all_time_totals,
        "models": models,
        "buckets": buckets,
        "recent": recent,
    }


def _usage_rows(since: int | None, db_path: Path | None) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path or _get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        where = "WHERE started_at >= ?" if since is not None else ""
        params = (since,) if since is not None else ()
        return [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT provider, protocol, model, account_id, status, started_at, latency_ms,
                       input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, error
                FROM usage_record
                {where}
                ORDER BY started_at ASC
                """,
                params,
            ).fetchall()
        ]
    finally:
        conn.close()


def _usage_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    requests = len(rows)
    input_tokens = sum(int(row["input_tokens"]) for row in rows)
    output_tokens = sum(int(row["output_tokens"]) for row in rows)
    cache_creation_tokens = sum(int(row["cache_creation_tokens"]) for row in rows)
    cache_read_tokens = sum(int(row["cache_read_tokens"]) for row in rows)
    tokens = input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
    return {
        "requests": requests,
        "errors": sum(1 for row in rows if row["status"] != "ok"),
        "tokens": tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "avg_latency_seconds": (
            round(sum(int(row["latency_ms"]) for row in rows) / requests / 1000, 2)
            if requests
            else 0
        ),
    }


def _usage_models(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        model = _usage_model_name(row)
        item = grouped.setdefault(
            model,
            {
                "model": model,
                "requests": 0,
                "tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "avg_latency_seconds": 0,
                "_latency_ms": 0,
            },
        )
        item["requests"] += 1
        item["input_tokens"] += int(row["input_tokens"])
        item["output_tokens"] += int(row["output_tokens"])
        item["tokens"] += (
            int(row["input_tokens"])
            + int(row["output_tokens"])
            + int(row["cache_creation_tokens"])
            + int(row["cache_read_tokens"])
        )
        item["_latency_ms"] += int(row["latency_ms"])
    out = []
    for item in grouped.values():
        item["avg_latency_seconds"] = round(
            item["_latency_ms"] / max(int(item["requests"]), 1) / 1000, 2
        )
        item.pop("_latency_ms", None)
        out.append(item)
    return sorted(out, key=lambda item: int(item["tokens"]), reverse=True)


def _usage_model_name(row: dict[str, Any]) -> str:
    provider = str(row.get("provider") or "").lower().strip()
    model = str(row.get("model") or "").strip() or "unknown"
    return _MODEL_ALIASES.get((provider, model.lower()), model)


def _usage_buckets(
    rows: list[dict[str, Any]],
    *,
    since: int,
    now: int,
    granularity: str,
) -> list[dict[str, Any]]:
    step = 3600 if granularity == "hour" else 86400
    start = since - (since % step)
    end = now - (now % step)
    buckets: dict[int, dict[str, Any]] = {}
    ts = start
    while ts <= end:
        buckets[ts] = {
            "ts": ts,
            "label": _bucket_label(ts, granularity),
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "latency_ms": 0,
        }
        ts += step
    for row in rows:
        bucket_ts = int(row["started_at"]) - (int(row["started_at"]) % step)
        bucket = buckets.setdefault(
            bucket_ts,
            {
                "ts": bucket_ts,
                "label": _bucket_label(bucket_ts, granularity),
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "latency_ms": 0,
            },
        )
        bucket["requests"] += 1
        bucket["input_tokens"] += int(row["input_tokens"])
        bucket["output_tokens"] += int(row["output_tokens"])
        bucket["cache_creation_tokens"] += int(row["cache_creation_tokens"])
        bucket["cache_read_tokens"] += int(row["cache_read_tokens"])
        bucket["latency_ms"] += int(row["latency_ms"])
    result = []
    for bucket in sorted(buckets.values(), key=lambda item: int(item["ts"])):
        avg_latency = (
            round(int(bucket["latency_ms"]) / int(bucket["requests"]) / 1000, 2)
            if bucket["requests"]
            else 0
        )
        result.append(
            {
                "ts": bucket["ts"],
                "label": bucket["label"],
                "requests": bucket["requests"],
                "input_tokens": bucket["input_tokens"],
                "output_tokens": bucket["output_tokens"],
                "cache_creation_tokens": bucket["cache_creation_tokens"],
                "cache_read_tokens": bucket["cache_read_tokens"],
                "avg_latency_seconds": avg_latency,
            }
        )
    return result


def _bucket_label(ts: int, granularity: str) -> str:
    fmt = "%Y-%m-%d %H:00" if granularity == "hour" else "%Y-%m-%d"
    return time.strftime(fmt, time.localtime(ts))


def usage_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)
