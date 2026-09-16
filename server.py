#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZCode 用量看板 —— 把 ZCode 本地记录的模型请求折算成 API token 用量与费用。

数据源（全部只读，不写入 ZCode 任何文件）：
  ~/.zcode/cli/db/db.sqlite  ->  model_usage / turn_usage / session

视图：概览 / 分析 / 请求事件，单价表见 prices.json。

用法：
  python server.py                 # 默认 http://127.0.0.1:8787
  python server.py --port 9000 --open
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
PRICES_PATH = os.path.join(BASE_DIR, "prices.json")
RUNTIME_PATH = os.path.join(BASE_DIR, "runtime.json")

# 心跳看护：浏览器标签关掉后就没人再 ping，服务自行退出，不留孤儿进程
VIEWERS = {}
VIEWERS_LOCK = threading.Lock()
EVER_SEEN = False
START_TIME = time.monotonic()
AUTO_SHUTDOWN = False
BYE_GRACE = 5.0        # 收到 bye 后再等这么久（页面刷新会重新 ping，避免误杀）
IDLE_TIMEOUT = 12.0    # 超过这么久没 ping 视为标签已关闭（页面每 4 秒一次心跳）
STARTUP_GRACE = 600.0  # 启动后一直没人打开页面的兜底退出时间

HOME = os.path.expanduser("~")
DEFAULT_DB = os.path.join(HOME, ".zcode", "cli", "db", "db.sqlite")
DB_PATH = os.environ.get("ZCODE_DB") or DEFAULT_DB

DISPLAY_TZ = datetime.now().astimezone().tzinfo


# --------------------------------------------------------------------------- #
# 单价表
# --------------------------------------------------------------------------- #
class Pricing:
    """按 prices.json 给每条请求算钱。规则按顺序首次命中，match 支持 * 通配。"""

    def __init__(self, path: str):
        self.path = path
        self._mtime = None
        self._lock = threading.Lock()
        self.cfg = {"display_currency": "CNY", "usd_to_cny": 7.1, "defaults": {}, "rules": []}
        self.reload(force=True)

    def reload(self, force: bool = False) -> None:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        with self._lock:
            if not force and mtime == self._mtime:
                return
            try:
                with open(self.path, encoding="utf-8") as fh:
                    self.cfg = json.load(fh)
            except Exception as exc:  # 单价表坏了不该让看板挂掉
                print(f"[prices] 读取失败，沿用上一版：{exc}", file=sys.stderr)
                return
            self._mtime = mtime
            self._rules = [
                (r.get("match", "*").lower(), r) for r in self.cfg.get("rules", [])
            ]
            self.display_currency = self.cfg.get("display_currency") or "CNY"
            self.usd_to_cny = float(self.cfg.get("usd_to_cny") or 7.1)
            print(f"[prices] 载入 {len(self._rules)} 条规则 -> {self.display_currency}")

    # -- 换算 ------------------------------------------------------------- #
    def to_display(self, amount: float, currency: str) -> float:
        if not amount:
            return 0.0
        if currency == self.display_currency:
            return amount
        if self.display_currency == "CNY" and currency == "USD":
            return amount * self.usd_to_cny
        if self.display_currency == "USD" and currency == "CNY":
            return amount / self.usd_to_cny
        return amount

    def rule_for(self, model_id: str):
        name = (model_id or "").lower()
        for pattern, rule in self._rules:
            if fnmatchcase(name, pattern):
                return rule
        return None

    def _unit_prices(self, rule: dict, started_ms: int) -> dict:
        prices = {
            "input": rule.get("input"),
            "cache_read": rule.get("cache_read"),
            "cache_write": rule.get("cache_write"),
            "output": rule.get("output"),
        }
        defaults = self.cfg.get("defaults") or {}
        base = prices["input"] or 0.0
        if prices["cache_read"] is None:
            prices["cache_read"] = base * float(defaults.get("cache_read_ratio", 0.1))
        if prices["cache_write"] is None:
            prices["cache_write"] = base * float(defaults.get("cache_write_ratio", 1.0))

        peak = rule.get("peak")
        if peak and started_ms:
            ratio = 1.0
            moment = datetime.fromtimestamp(started_ms / 1000, tz=timezone.utc)
            weekdays_only = peak.get("weekdays_only", True)
            in_window = any(
                start <= moment.hour < end for start, end in peak.get("windows_utc", [])
            )
            if not (weekdays_only and moment.weekday() >= 5) and in_window:
                ratio = 1.0
            else:
                ratio = float(peak.get("offpeak_ratio", 1.0))
            prices = {k: (v * ratio if v is not None else None) for k, v in prices.items()}
        return prices

    def cost(self, model_id: str, started_ms: int, input_tokens: int,
             cache_read: int, cache_write: int, output_tokens: int) -> dict:
        rule = self.rule_for(model_id)
        if rule is None:
            return {
                "priced": False, "total": 0.0, "currency": self.display_currency,
                "components": {"input": 0.0, "cache_read": 0.0, "cache_write": 0.0, "output": 0.0},
                "rule": None, "label": None, "source": None,
            }
        unit = self._unit_prices(rule, started_ms)
        currency = rule.get("currency", "USD")
        # input_tokens 是归一化值，已经把缓存命中算进去了，扣掉才是按原价计费的增量部分
        fresh_input = max((input_tokens or 0) - (cache_read or 0), 0)
        parts = {
            "input": fresh_input / 1e6 * (unit["input"] or 0),
            "cache_read": (cache_read or 0) / 1e6 * (unit["cache_read"] or 0),
            "cache_write": (cache_write or 0) / 1e6 * (unit["cache_write"] or 0),
            "output": (output_tokens or 0) / 1e6 * (unit["output"] or 0),
        }
        total = sum(parts.values())
        return {
            "priced": True,
            "total": round(self.to_display(total, currency), 8),
            "currency": self.display_currency,
            "components": {k: round(self.to_display(v, currency), 8) for k, v in parts.items()},
            "rule": rule.get("match"),
            "label": rule.get("label") or rule.get("match"),
            "source": rule.get("source", "assumed"),
        }

    def public_view(self) -> dict:
        return {
            "display_currency": self.display_currency,
            "usd_to_cny": self.usd_to_cny,
            "rules": [
                {
                    "match": r.get("match"),
                    "label": r.get("label") or r.get("match"),
                    "currency": r.get("currency", "USD"),
                    "input": r.get("input"),
                    "cache_read": r.get("cache_read"),
                    "cache_write": r.get("cache_write"),
                    "output": r.get("output"),
                    "source": r.get("source", "assumed"),
                    "note": r.get("note", ""),
                }
                for r in self.cfg.get("rules", [])
            ],
        }


PRICING = Pricing(PRICES_PATH)


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #
SELECT_FIELDS = """
  mu.id, mu.attempt_index, mu.started_at, mu.completed_at, mu.duration_ms,
  mu.time_to_first_token_ms, mu.provider_id, mu.model_id, mu.variant, mu.agent,
  mu.mode, mu.task_type, mu.query_source, mu.status, mu.finish_reason,
  mu.tool_call_count, mu.input_tokens, mu.output_tokens, mu.reasoning_tokens,
  mu.cache_creation_input_tokens, mu.cache_read_input_tokens, mu.computed_total_tokens,
  mu.retry_count, mu.context_exceeded, mu.cancelled_by_user,
  mu.error_type, mu.error_code, mu.error_message, mu.session_id, mu.turn_id, mu.trace_id,
  s.title AS session_title, s.directory AS project_dir, s.project_id AS project_id
"""


def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=8.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=1")
    return con


def parse_time(value: str, end_of_day: bool = False):
    """'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM' / epoch 毫秒 -> 毫秒时间戳。"""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d" and end_of_day:
            dt = dt + timedelta(days=1) - timedelta(milliseconds=1)
        return int(dt.timestamp() * 1000)
    return None


def parse_filters(qs: dict) -> dict:
    def one(key, default=None):
        vals = qs.get(key) or []
        return vals[0] if vals else default

    filters = {
        "from": parse_time(one("from")),
        "to": parse_time(one("to"), end_of_day=True),
        "q": (one("q") or "").strip(),
        "priced": one("priced") or "",
    }
    for key in ("model", "provider", "agent", "source", "status", "project"):
        vals = [v for v in (one(key) or "").split(",") if v]
        if vals:
            filters[key] = vals
    return filters


def build_where(f: dict):
    clauses, params = [], []
    if f.get("from"):
        clauses.append("mu.started_at >= ?")
        params.append(f["from"])
    if f.get("to"):
        clauses.append("mu.started_at <= ?")
        params.append(f["to"])
    columns = {
        "model": "mu.model_id",
        "provider": "mu.provider_id",
        "agent": "mu.agent",
        "source": "mu.query_source",
        "status": "mu.status",
        "project": "s.project_id",
    }
    for key, column in columns.items():
        values = f.get(key)
        if values:
            clauses.append(f"{column} IN ({','.join('?' * len(values))})")
            params.extend(values)
    if f.get("q"):
        like = f"%{f['q']}%"
        clauses.append(
            "(mu.error_message LIKE ? OR mu.error_code LIKE ? OR mu.model_id LIKE ?"
            " OR mu.provider_id LIKE ? OR s.title LIKE ? OR s.directory LIKE ?)"
        )
        params.extend([like] * 6)
    return (" AND ".join(clauses) if clauses else "1=1"), params


def row_to_request(row: sqlite3.Row) -> dict:
    input_tokens = row["input_tokens"] or 0
    cache_read = row["cache_read_input_tokens"] or 0
    cache_write = row["cache_creation_input_tokens"] or 0
    output_tokens = row["output_tokens"] or 0
    cost = PRICING.cost(
        row["model_id"], row["started_at"], input_tokens, cache_read, cache_write, output_tokens
    )
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "duration_ms": row["duration_ms"],
        "ttft_ms": row["time_to_first_token_ms"],
        "provider_id": row["provider_id"],
        "model_id": row["model_id"],
        "variant": row["variant"],
        "agent": row["agent"],
        "mode": row["mode"],
        "task_type": row["task_type"],
        "query_source": row["query_source"],
        "status": row["status"],
        "finish_reason": row["finish_reason"],
        "tool_call_count": row["tool_call_count"],
        "attempt_index": row["attempt_index"],
        "retry_count": row["retry_count"],
        "context_exceeded": row["context_exceeded"],
        "cancelled_by_user": row["cancelled_by_user"],
        "error_type": row["error_type"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "session_id": row["session_id"],
        "session_title": row["session_title"],
        "project_id": row["project_id"],
        "project_dir": row["project_dir"],
        "turn_id": row["turn_id"],
        "trace_id": row["trace_id"],
        "tokens": {
            "input": input_tokens,
            "input_fresh": max(input_tokens - cache_read, 0),
            "cache_read": cache_read,
            "cache_write": cache_write,
            "output": output_tokens,
            "reasoning": row["reasoning_tokens"] or 0,
            "total": (input_tokens or 0) + output_tokens,
        },
        "cost": cost,
    }


def load_requests(f: dict):
    where, params = build_where(f)
    con = db_connect()
    try:
        sql = (
            f"SELECT {SELECT_FIELDS} FROM model_usage mu"
            " LEFT JOIN session s ON s.id = mu.session_id"
            f" WHERE {where} ORDER BY mu.started_at DESC"
        )
        rows = [row_to_request(r) for r in con.execute(sql, params)]
    finally:
        con.close()

    want = f.get("priced")
    if want == "priced":
        rows = [r for r in rows if r["cost"]["priced"]]
    elif want == "unpriced":
        rows = [r for r in rows if not r["cost"]["priced"]]
    return rows


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #
def blank_bucket():
    return {
        "requests": 0, "errors": 0, "cancelled": 0, "running": 0, "retries": 0,
        "input": 0, "input_fresh": 0, "cache_read": 0, "cache_write": 0,
        "output": 0, "reasoning": 0, "tokens": 0,
        "cost": 0.0, "cost_input": 0.0, "cost_cache_read": 0.0,
        "cost_cache_write": 0.0, "cost_output": 0.0,
        "_ms": 0, "_ms_n": 0, "_ttft": 0, "_ttft_n": 0, "_unpriced": 0,
        "_first": None, "_last": None,
    }


def add_to(bucket: dict, req: dict) -> None:
    tk, cost = req["tokens"], req["cost"]
    bucket["requests"] += 1
    if req["status"] == "error":
        bucket["errors"] += 1
    elif req["status"] == "cancelled":
        bucket["cancelled"] += 1
    elif req["status"] == "running":
        bucket["running"] += 1
    if req["retry_count"]:
        bucket["retries"] += 1
    for key in ("input", "input_fresh", "cache_read", "cache_write", "output", "reasoning", "total"):
        bucket[key if key != "total" else "tokens"] += tk[key]
    bucket["cost"] += cost["total"]
    if cost["priced"]:
        for key in ("input", "cache_read", "cache_write", "output"):
            bucket[f"cost_{key}"] += cost["components"][key]
    else:
        bucket["_unpriced"] += 1
    if req["duration_ms"]:
        bucket["_ms"] += req["duration_ms"]
        bucket["_ms_n"] += 1
    if req["ttft_ms"]:
        bucket["_ttft"] += req["ttft_ms"]
        bucket["_ttft_n"] += 1
    ts = req["started_at"]
    bucket["_first"] = ts if bucket["_first"] is None else min(bucket["_first"], ts)
    bucket["_last"] = ts if bucket["_last"] is None else max(bucket["_last"], ts)


def finalize(bucket: dict) -> dict:
    out = {k: v for k, v in bucket.items() if not k.startswith("_")}
    out["avg_ms"] = round(bucket["_ms"] / bucket["_ms_n"], 1) if bucket["_ms_n"] else None
    out["avg_ttft_ms"] = round(bucket["_ttft"] / bucket["_ttft_n"], 1) if bucket["_ttft_n"] else None
    out["error_rate"] = round(bucket["errors"] / bucket["requests"], 4) if bucket["requests"] else 0.0
    billed_input = out["input_fresh"] + out["cache_read"]
    out["cache_hit_rate"] = round(out["cache_read"] / billed_input, 4) if billed_input else 0.0
    out["cost"] = round(out["cost"], 6)
    for key in ("cost_input", "cost_cache_read", "cost_cache_write", "cost_output"):
        out[key] = round(out[key], 6)
    out["first_seen"] = bucket["_first"]
    out["last_seen"] = bucket["_last"]
    return out


def percentile(values, ratio):
    if not values:
        return None
    values = sorted(values)
    idx = min(int(len(values) * ratio), len(values) - 1)
    return values[idx]


def aggregate(rows) -> dict:
    totals = blank_bucket()
    by_model, by_provider, by_project, by_agent, by_source = {}, {}, {}, {}, {}
    daily, hourly = {}, {}
    durations = []

    for req in rows:
        add_to(totals, req)
        model_key = req["model_id"] or "(未知)"
        bucket = by_model.setdefault(model_key, blank_bucket())
        add_to(bucket, req)
        bucket["provider_id"] = req["provider_id"]
        bucket["cost_rule"] = req["cost"].get("label")
        bucket["price_source"] = req["cost"].get("source")

        add_to(by_provider.setdefault(req["provider_id"] or "(未知)", blank_bucket()), req)
        project_key = req["project_id"] or "(未关联项目)"
        pbucket = by_project.setdefault(project_key, blank_bucket())
        add_to(pbucket, req)
        pbucket["directory"] = req["project_dir"]
        add_to(by_agent.setdefault(req["agent"] or "(未知)", blank_bucket()), req)
        add_to(by_source.setdefault(req["query_source"] or "(未知)", blank_bucket()), req)

        moment = datetime.fromtimestamp(req["started_at"] / 1000, tz=DISPLAY_TZ)
        day = moment.strftime("%Y-%m-%d")
        add_to(daily.setdefault(day, blank_bucket()), req)
        hkey = (moment.weekday(), moment.hour)
        hbucket = hourly.setdefault(hkey, {"requests": 0, "cost": 0.0, "tokens": 0})
        hbucket["requests"] += 1
        hbucket["cost"] += req["cost"]["total"]
        hbucket["tokens"] += req["tokens"]["total"]
        if req["duration_ms"]:
            durations.append(req["duration_ms"])

    total_cost = totals["cost"] or 0.0

    def with_share(items: dict, extra_keys=()):
        result = []
        for key, bucket in items.items():
            item = finalize(bucket)
            item["key"] = key
            for k in extra_keys:
                item[k] = bucket.get(k)
            item["cost_share"] = round(item["cost"] / total_cost, 6) if total_cost else 0.0
            result.append(item)
        result.sort(key=lambda x: x["cost"], reverse=True)
        return result

    unpriced_models = sorted(
        {r["model_id"] for r in rows if not r["cost"]["priced"]}
    )
    unpriced_tokens = sum(r["tokens"]["total"] for r in rows if not r["cost"]["priced"])

    return {
        "kpi": {
            **finalize(totals),
            "p50_ms": percentile(durations, 0.5),
            "p95_ms": percentile(durations, 0.95),
            "unpriced_requests": totals["_unpriced"],
            "unpriced_tokens": unpriced_tokens,
            "unpriced_models": unpriced_models,
            "sessions": len({r["session_id"] for r in rows if r["session_id"]}),
            "turns": len({(r["session_id"], r["turn_id"]) for r in rows if r["turn_id"]}),
            "models": len(by_model),
            "currency": PRICING.display_currency,
        },
        "series_daily": [
            {"date": day, **finalize(bucket)} for day, bucket in sorted(daily.items())
        ],
        "heatmap": [
            {"dow": dow, "hour": hour, **{k: round(v, 6) if isinstance(v, float) else v
                                          for k, v in bucket.items()}}
            for (dow, hour), bucket in sorted(hourly.items())
        ],
        "by_model": with_share(by_model, ("provider_id", "cost_rule", "price_source")),
        "by_provider": with_share(by_provider),
        "by_project": with_share(by_project, ("directory",)),
        "by_agent": with_share(by_agent),
        "by_source": with_share(by_source),
        "top_requests": [
            {
                "id": r["id"], "started_at": r["started_at"], "model_id": r["model_id"],
                "project_id": r["project_id"], "tokens": r["tokens"],
                "cost": r["cost"]["total"], "duration_ms": r["duration_ms"],
                "session_title": r["session_title"],
            }
            for r in sorted(rows, key=lambda r: r["cost"]["total"], reverse=True)[:15]
        ],
        "generated_at": int(time.time() * 1000),
    }


# --------------------------------------------------------------------------- #
# 会话心跳与看护
# --------------------------------------------------------------------------- #
def viewer_ping(sid: str) -> None:
    global EVER_SEEN
    if not sid:
        return
    with VIEWERS_LOCK:
        VIEWERS[sid] = time.monotonic()
        EVER_SEEN = True


def viewer_bye(sid: str) -> None:
    """标签页卸载时调用：不立刻注销，而是让这个会话在 BYE_GRACE 秒后过期。

    这样刷新页面（先 pagehide 再重新 ping）不会被误判成关闭。
    """
    with VIEWERS_LOCK:
        if sid:
            VIEWERS[sid] = time.monotonic() - IDLE_TIMEOUT + BYE_GRACE
            EVER_SEEN = True


def live_viewers() -> int:
    now = time.monotonic()
    with VIEWERS_LOCK:
        for sid in [s for s, t in VIEWERS.items() if now - t > IDLE_TIMEOUT]:
            VIEWERS.pop(sid, None)
        return len(VIEWERS)


def watchdog(server: HTTPServer) -> None:
    """没有任何标签在看这个页面时退出进程。"""
    while True:
        time.sleep(2)
        if live_viewers():
            continue
        if EVER_SEEN or (time.monotonic() - START_TIME) > STARTUP_GRACE:
            print("[watchdog] 已无活跃查看者，退出", file=sys.stderr)
            remove_runtime()
            threading.Thread(target=server.shutdown, daemon=True).start()
            return


def write_runtime(host: str, port: int) -> None:
    payload = {
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}/",
        "db": DB_PATH,
        "auto_shutdown": AUTO_SHUTDOWN,
        "started_at": int(time.time() * 1000),
    }
    try:
        with open(RUNTIME_PATH, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"[runtime] 写状态文件失败：{exc}", file=sys.stderr)


def remove_runtime() -> None:
    try:
        os.remove(RUNTIME_PATH)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "zcode-usage-dashboard"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    # -- helpers ---------------------------------------------------------- #
    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, status=200, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve_static(self, rel_path: str):
        rel_path = rel_path.lstrip("/") or "index.html"
        target = os.path.normpath(os.path.join(STATIC_DIR, rel_path))
        if not target.startswith(STATIC_DIR) or not os.path.isfile(target):
            self.send_text("404", 404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(os.path.splitext(target)[1], "application/octet-stream")
        with open(target, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- routes ----------------------------------------------------------- #
    def do_GET(self):
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        qs = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            return self.serve_static("index.html")
        if route.startswith("/static/"):
            return self.serve_static(route[len("/static/"):])

        try:
            if route == "/api/bootstrap":
                return self.send_json(self.api_bootstrap())
            if route == "/api/summary":
                filters = parse_filters(qs)
                rows = load_requests(filters)
                return self.send_json({"filters": filters, **aggregate(rows)})
            if route == "/api/events":
                return self.send_json(self.api_events(qs))
            if route == "/api/event":
                return self.send_json(self.api_event(qs))
            if route == "/api/live":
                return self.send_json(self.api_live())
            if route == "/api/ping":
                viewer_ping((qs.get("sid") or [""])[0])
                return self.send_json({"ok": True, "viewers": live_viewers()})
            if route == "/api/viewers":
                # 只查数量、不注册会话：给 zsage 判断"页面是不是已经开着"用
                return self.send_json({
                    "viewers": live_viewers(),
                    "auto_shutdown": AUTO_SHUTDOWN,
                    "port": self.server.server_address[1],
                })
            if route == "/api/bye":
                viewer_bye((qs.get("sid") or [""])[0])
                return self.send_json({"ok": True})
            if route == "/api/prices":
                PRICING.reload()
                return self.send_json(PRICING.public_view())
            if route == "/api/export.csv":
                return self.export_csv(qs)
        except Exception as exc:  # 让前端看到错误而不是空白
            import traceback
            traceback.print_exc()
            return self.send_json({"error": str(exc)}, 500)

        self.send_text("404", 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        # sendBeacon 走 POST，心跳和告别都要在这里也认一遍
        if parsed.path == "/api/bye":
            viewer_bye((qs.get("sid") or [""])[0])
            return self.send_json({"ok": True})
        if parsed.path == "/api/ping":
            viewer_ping((qs.get("sid") or [""])[0])
            return self.send_json({"ok": True, "viewers": live_viewers()})
        if parsed.path != "/api/prices":
            return self.send_text("404", 404)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(raw)
        except Exception as exc:
            return self.send_json({"error": f"不是合法 JSON：{exc}"}, 400)
        with open(PRICES_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        PRICING.reload(force=True)
        return self.send_json({"ok": True, "rules": len(PRICING.cfg.get("rules", []))})

    # -- api -------------------------------------------------------------- #
    def api_bootstrap(self):
        PRICING.reload()
        con = db_connect()
        try:
            meta = con.execute(
                "SELECT MIN(started_at) AS min_ts, MAX(started_at) AS max_ts,"
                " COUNT(*) AS total FROM model_usage"
            ).fetchone()
            groups = {}
            for row in con.execute(
                "SELECT provider_id, model_id, COUNT(*) AS n, MAX(started_at) AS last_ts"
                " FROM model_usage GROUP BY provider_id, model_id ORDER BY n DESC"
            ):
                groups.setdefault(row["model_id"], []).append(
                    {"provider_id": row["provider_id"], "requests": row["n"], "last_ts": row["last_ts"]}
                )
            facet_sql = {
                "providers": "SELECT DISTINCT provider_id AS v FROM model_usage ORDER BY v",
                "agents": "SELECT DISTINCT agent AS v FROM model_usage WHERE agent IS NOT NULL ORDER BY v",
                "sources": "SELECT DISTINCT query_source AS v FROM model_usage ORDER BY v",
                "statuses": "SELECT DISTINCT status AS v FROM model_usage ORDER BY v",
            }
            facets = {k: [r["v"] for r in con.execute(sql)] for k, sql in facet_sql.items()}
            facets["projects"] = [
                {"value": r["project_id"], "label": r["directory"], "requests": r["n"]}
                for r in con.execute(
                    "SELECT s.project_id, s.directory, COUNT(*) AS n FROM model_usage mu"
                    " JOIN session s ON s.id = mu.session_id GROUP BY s.project_id ORDER BY n DESC"
                )
            ]
            turns = con.execute(
                "SELECT COUNT(*) AS n, SUM(computed_total_tokens) AS t FROM turn_usage"
            ).fetchone()
        finally:
            con.close()

        models = []
        for model_id, entries in groups.items():
            rule = PRICING.rule_for(model_id)
            models.append(
                {
                    "model_id": model_id,
                    "requests": sum(e["requests"] for e in entries),
                    "providers": [e["provider_id"] for e in entries],
                    "last_ts": max(e["last_ts"] or 0 for e in entries),
                    "pricing": None if rule is None else {
                        "label": rule.get("label") or rule.get("match"),
                        "currency": rule.get("currency", "USD"),
                        "input": rule.get("input"),
                        "cache_read": rule.get("cache_read"),
                        "cache_write": rule.get("cache_write"),
                        "output": rule.get("output"),
                        "source": rule.get("source", "assumed"),
                        "note": rule.get("note", ""),
                    },
                }
            )
        models.sort(key=lambda m: m["requests"], reverse=True)

        return {
            "db_path": DB_PATH,
            "db_ok": True,
            "range": {"min": meta["min_ts"], "max": meta["max_ts"]},
            "total_rows": meta["total"],
            "models": models,
            "turns": {"count": turns["n"], "tokens": turns["t"]},
            "facets": facets,
            "pricing": PRICING.public_view(),
            "server_time": int(time.time() * 1000),
        }

    def api_events(self, qs):
        filters = parse_filters(qs)
        rows = load_requests(filters)

        sort_key = (qs.get("sort") or ["started_at"])[0]
        order = (qs.get("order") or ["desc"])[0]

        def key(row):
            if sort_key == "cost":
                return row["cost"]["total"]
            if sort_key == "tokens":
                return row["tokens"]["total"]
            if sort_key == "duration":
                return row["duration_ms"] or 0
            if sort_key == "ttft":
                return row["ttft_ms"] or 0
            return row["started_at"]

        rows.sort(key=key, reverse=(order != "asc"))

        try:
            page = max(int((qs.get("page") or ["1"])[0]), 1)
            size = min(max(int((qs.get("size") or ["50"])[0]), 1), 500)
        except ValueError:
            page, size = 1, 50
        start = (page - 1) * size
        items = rows[start:start + size]
        return {
            "total": len(rows),
            "page": page,
            "size": size,
            "cost_total": round(sum(r["cost"]["total"] for r in rows), 6),
            "tokens_total": sum(r["tokens"]["total"] for r in rows),
            "items": items,
            "currency": PRICING.display_currency,
        }

    def api_event(self, qs):
        event_id = (qs.get("id") or [""])[0]
        if not event_id:
            return {"error": "缺少 id"}
        con = db_connect()
        try:
            row = con.execute(
                f"SELECT {SELECT_FIELDS}, mu.raw_usage_json, mu.provider_metadata_json,"
                " mu.logical_request_id, mu.span_id, mu.parent_user_message_id,"
                " mu.assistant_message_id, mu.provider_total_tokens"
                " FROM model_usage mu LEFT JOIN session s ON s.id = mu.session_id WHERE mu.id = ?",
                (event_id,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return {"error": "没有这条记录"}

        request = row_to_request(row)
        return {
            "request": request,
            "logical_request_id": row["logical_request_id"],
            "span_id": row["span_id"],
            "parent_user_message_id": row["parent_user_message_id"],
            "assistant_message_id": row["assistant_message_id"],
            "provider_total_tokens": row["provider_total_tokens"],
            "raw_usage": _maybe_json(row["raw_usage_json"]),
            "provider_metadata": _maybe_json(row["provider_metadata_json"]),
        }

    def api_live(self):
        con = db_connect()
        try:
            rows = [
                row_to_request(r)
                for r in con.execute(
                    f"SELECT {SELECT_FIELDS} FROM model_usage mu"
                    " LEFT JOIN session s ON s.id = mu.session_id"
                    " WHERE mu.status = 'running' ORDER BY mu.started_at DESC LIMIT 20"
                )
            ]
            recent = [
                row_to_request(r)
                for r in con.execute(
                    f"SELECT {SELECT_FIELDS} FROM model_usage mu"
                    " LEFT JOIN session s ON s.id = mu.session_id"
                    " ORDER BY mu.started_at DESC LIMIT 12"
                )
            ]
        finally:
            con.close()
        now = int(time.time() * 1000)
        for row in rows:
            row["elapsed_ms"] = now - row["started_at"]
        return {"now": now, "running": rows, "recent": recent}

    def export_csv(self, qs):
        filters = parse_filters(qs)
        rows = load_requests(filters)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "started_at_local", "model_id", "provider_id", "project", "session_title", "agent",
            "query_source", "status", "attempt_index", "retry_count",
            "input_tokens", "input_fresh", "cache_read", "cache_write", "output_tokens",
            "reasoning_tokens", "total_tokens", "duration_ms", "ttft_ms",
            f"cost_{PRICING.display_currency}", "cost_input", "cost_cache_read",
            "cost_cache_write", "cost_output", "priced", "price_rule", "error",
        ])
        for row in rows:
            tk, cost = row["tokens"], row["cost"]
            writer.writerow([
                datetime.fromtimestamp(row["started_at"] / 1000, tz=DISPLAY_TZ)
                .strftime("%Y-%m-%d %H:%M:%S"),
                row["model_id"], row["provider_id"], row["project_dir"], row["session_title"],
                row["agent"], row["query_source"], row["status"], row["attempt_index"] + 1,
                row["retry_count"], tk["input"], tk["input_fresh"], tk["cache_read"],
                tk["cache_write"], tk["output"], tk["reasoning"], tk["total"],
                row["duration_ms"], row["ttft_ms"], round(cost["total"], 6),
                round(cost["components"]["input"], 6), round(cost["components"]["cache_read"], 6),
                round(cost["components"]["cache_write"], 6), round(cost["components"]["output"], 6),
                "yes" if cost["priced"] else "no", cost.get("rule") or "",
                row["error_message"] or "",
            ])
        body = "\ufeff" + buffer.getvalue()  # BOM 让 Excel 认出 UTF-8
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header(
            "Content-Disposition", f'attachment; filename="zcode-usage-{stamp}.csv"'
        )
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _maybe_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return text


def main():
    global DB_PATH, IDLE_TIMEOUT, STARTUP_GRACE, AUTO_SHUTDOWN
    parser = argparse.ArgumentParser(description="ZCode 用量看板")
    parser.add_argument("--port", type=int, default=int(os.environ.get("ZCODE_DASH_PORT", 8787)),
                        help="监听端口，0 表示自动挑一个空闲端口")
    parser.add_argument("--host", default=os.environ.get("ZCODE_DASH_HOST", "127.0.0.1"))
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--open", action="store_true", help="启动后自动用系统浏览器打开")
    parser.add_argument("--auto-shutdown", action="store_true",
                        help="没有浏览器标签在查看时自动退出（配合 --port 0 用于一次性查看）")
    parser.add_argument("--idle-timeout", type=float, default=IDLE_TIMEOUT,
                        help="默认 20 秒：超过这么久收不到页面心跳就认为标签已关闭")
    parser.add_argument("--startup-grace", type=float, default=STARTUP_GRACE,
                        help="默认 600 秒：启动后一直没人打开页面时的兜底退出时间")
    args = parser.parse_args()

    IDLE_TIMEOUT = args.idle_timeout
    STARTUP_GRACE = args.startup_grace
    AUTO_SHUTDOWN = args.auto_shutdown

    DB_PATH = args.db
    if not os.path.isfile(DB_PATH):
        print(f"找不到数据库：{DB_PATH}", file=sys.stderr)
        print("用 --db 指定 ZCode 的 db.sqlite 路径。", file=sys.stderr)
        return 1

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    host, port = server.server_address[0], server.server_address[1]
    url = f"http://{host}:{port}/"
    write_runtime(host, port)
    print(f"ZCode 用量看板：{url}")
    print(f"数据库：{DB_PATH}")
    print(f"单价表：{PRICES_PATH}（改完刷新页面即生效）")
    if args.auto_shutdown:
        print(f"看护已开启：页面关闭约 {BYE_GRACE:.0f} 秒后自动退出")
        threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        remove_runtime()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
