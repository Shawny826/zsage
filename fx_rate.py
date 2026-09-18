#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""USD→CNY 汇率：保证用的是「当天」的价，而不是写死在配置里的那个数字。

为什么单独成模块：server.py（折算展示币种）和 zsage.py（每日同步 / 手动刷新）
都要用，和 model_catalog.py 一样单独放，避免两处各写一份。

取值优先级（``current()``）：

1. 内存里今天的值
2. 落盘缓存（``fx_rate_cache.json``，已 gitignore）里今天的值
3. 都不是今天的 → **后台**拉一次，本次调用先用旧值顶上
4. 彻底没有 → ``prices.json`` 的 ``usd_to_cny``（离线兜底）

几个刻意的取舍：

* **只认「当天」**，缓存里记 ``date``，跨天自动重拉 —— 这样"每次计算"拿到的
  都是当日汇率，而不必真的每次去联网。
* **拉取永远在后台线程里**，绝不让 ``/api/*`` 等网络：就绪探测要求 10 秒内返回
  200，而外网在这台机器上时快时慢（models.dev 能拖到几分钟）。
* **不写回 prices.json**。那是用户手改的配置文件，自动改写只会制造无意义的 diff；
  拉到的值落在独立缓存文件里，``usd_to_cny`` 降级为离线兜底。
* 两个数据源都免 key：``open.er-api.com``（每日更新）优先，
  ``frankfurter.dev``（ECB 参考汇率）兜底；都不通就沿用旧值。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import datetime

CACHE_FILENAME = "fx_rate_cache.json"

# (名称, URL, 取值函数)。按顺序试，第一个成功就用。
SOURCES = (
    ("open.er-api.com", "https://open.er-api.com/v6/latest/USD",
     lambda d: (d.get("rates") or {}).get("CNY")),
    ("frankfurter.dev (ECB)", "https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY",
     lambda d: (d.get("rates") or {}).get("CNY")),
)

# 汇率落在这个区间之外就当拉到了脏数据，宁可不用
MIN_RATE, MAX_RATE = 3.0, 15.0

_LOCK = threading.Lock()
_STATE: dict | None = None      # {"rate","source","fetched_at","date"}
_FETCHING = False


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def cache_file(base_dir: str) -> str:
    return os.path.join(base_dir, CACHE_FILENAME)


def read_cache(base_dir: str) -> dict | None:
    """读磁盘缓存；没有或坏了返回 None。"""
    try:
        with open(cache_file(base_dir), encoding="utf-8") as fh:
            payload = json.load(fh)
        rate = float(payload["rate"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not (MIN_RATE <= rate <= MAX_RATE):
        return None
    return {
        "rate": rate,
        "source": payload.get("source") or "cache",
        "fetched_at": float(payload.get("fetched_at") or 0),
        "date": payload.get("date") or "",
    }


def write_cache(base_dir: str, rate: float, source: str) -> None:
    try:
        with open(cache_file(base_dir), "w", encoding="utf-8") as fh:
            json.dump({"rate": rate, "source": source,
                       "fetched_at": time.time(), "date": today()}, fh)
    except OSError:
        pass


def fetch_live(timeout: float = 20.0) -> tuple[float, str] | None:
    """联网取一次。返回 (rate, 来源名)；全都不通返回 None。"""
    for name, url, pick in SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "zsage"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            rate = float(pick(data))
        except Exception:
            continue
        if MIN_RATE <= rate <= MAX_RATE:
            return rate, name
    return None


def _refresh(base_dir: str) -> None:
    """后台线程体：拉一次并落盘 + 更新内存。"""
    global _STATE, _FETCHING
    try:
        got = fetch_live()
        if got:
            rate, source = got
            write_cache(base_dir, rate, source)
            with _LOCK:
                _STATE = {"rate": rate, "source": source,
                          "fetched_at": time.time(), "date": today()}
    finally:
        with _LOCK:
            _FETCHING = False


def refresh_now(base_dir: str) -> tuple[float, str] | None:
    """阻塞式刷新，给 CLI 用（每日同步 / 手动 sync-fx）。"""
    global _STATE
    got = fetch_live()
    if not got:
        return None
    rate, source = got
    write_cache(base_dir, rate, source)
    with _LOCK:
        _STATE = {"rate": rate, "source": source,
                  "fetched_at": time.time(), "date": today()}
    return got


def current(base_dir: str, fallback: float) -> dict:
    """当前该用的汇率。不是今天的就在后台刷，本次先用手上有的。

    返回 {"rate", "source", "date", "stale"}；stale=True 表示这不是今天的值
    （网络不通或还没拉回来），界面据此说明折算依据。
    """
    global _STATE, _FETCHING
    with _LOCK:
        state = _STATE
        fetching = _FETCHING
    if state is None:
        state = read_cache(base_dir)
        with _LOCK:
            _STATE = state
    if state and state.get("date") == today():
        return {**state, "stale": False}
    # 手上没有今天的值 —— 丢后台拉，别让调用方等
    if not fetching:
        with _LOCK:
            _FETCHING = True
        threading.Thread(target=_refresh, args=(base_dir,), daemon=True).start()
    if state:
        return {**state, "stale": True}
    return {"rate": float(fallback), "source": "prices.json", "date": "", "stale": True}
