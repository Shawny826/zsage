#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""models.dev 目录的抓取与匹配。

zsage.py（CLI 的 sync-prices）和 server.py（设置页的「重新匹配」按钮）都要用，
所以单独成模块，避免两处各写一份。

要点：
* 端点是 ``https://models.dev/api.json``。``/api/models`` 返回的是 HTML 页面，不能当 JSON 用。
* ZCode 记录的 model_id 是裸名（``glm-5.3``、``claude-opus-5``），与 models.dev 的
  model id 同构；OpenRouter 那种带 provider 前缀的 id（``openai/gpt-4``）匹配不上，别用。
* 同名模型常被多家 provider 收录，需优先取官方，免得拿到转售商价格。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import urllib.request

MODELS_DEV_URL = "https://models.dev/api.json"

# 目录 4.6MB、拉一次 8~10 秒，落盘缓存让重启与断网都不至于没有参考价。
# server 与 CLI 共用同一个文件（放在仓库根目录，已被 .gitignore 排除）。
CACHE_FILENAME = "models_dev_cache.json"

# 归一化名冲突时优先取这些官方 provider
FIRST_PARTY_PROVIDERS = {
    "zhipuai", "zai", "zai-coding-plan", "zhipuai-coding-plan",
    "anthropic", "openai", "google", "google-vertex", "google-vertex-anthropic",
    "deepseek", "moonshotai", "moonshotai-cn", "xai", "meta", "mistral",
    "alibaba", "qwen", "cohere", "amazon-bedrock", "azure", "perplexity",
    "minimax", "baidu", "bytedance", "stepfun",
}

# 低于这个相似度就不认，免得把不相干的模型价格套上去
FUZZY_MIN_RATIO = 0.82


def normalize(name: str) -> str:
    """只留小写字母数字，便于跨厂商比对：glm-5.3 / GLM_5.3 -> glm53。"""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def glob_safe(model_id: str) -> str:
    """模型名含通配符元字符时别包 * ，改用精确串，免得误伤别的模型。"""
    return model_id if any(ch in model_id for ch in "*?[") else f"*{model_id}*"


def fetch_catalog(timeout: float = 30.0, user_agent: str = "zsage"):
    """拉 models.dev 全量目录，返回 {归一化名: (provider_id, provider_name, model, rank)}。

    只收录带报价的模型 —— coding plan 一类的 provider 报价是空的，收进来没意义。
    """
    req = urllib.request.Request(MODELS_DEV_URL, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    catalog: dict = {}
    for pid, provider in raw.items():
        rank = 0 if pid in FIRST_PARTY_PROVIDERS else 1
        for model in (provider.get("models") or {}).values():
            cost = model.get("cost") or {}
            if not cost.get("input") and not cost.get("output"):
                continue
            key = normalize(model.get("id"))
            if not key:
                continue
            prev = catalog.get(key)
            if prev is None or rank < prev[3]:
                catalog[key] = (pid, provider.get("name") or pid, model, rank)
    return catalog


def match(model_id: str, catalog: dict):
    """给一个模型名在目录里找价格。返回 (命中项, 匹配方式) 或 (None, "")。

    三级策略：精确 -> 包含 -> 模糊。模糊这一级就是"未识别模型的近似匹配"。
    """
    key = normalize(model_id)
    if not key:
        return None, ""
    if key in catalog:
        return catalog[key], "精确"
    subs = [k for k in catalog if k in key or key in k]
    if subs:
        return catalog[max(subs, key=len)], "包含"
    best, score = None, 0.0
    for k in catalog:
        ratio = difflib.SequenceMatcher(None, key, k).ratio()
        if ratio > score:
            best, score = k, ratio
    if best and score >= FUZZY_MIN_RATIO:
        return catalog[best], f"近似({score:.2f})"
    return None, ""


def price_of(model: dict) -> dict:
    """从目录条目里抽出 zsage 用的价格四元组（单位：每百万 token）。"""
    cost = model.get("cost") or {}
    return {
        "currency": "USD",
        "input": cost.get("input"),
        "cache_read": cost.get("cache_read"),
        "cache_write": cost.get("cache_write"),
        "output": cost.get("output"),
    }


def cache_file(base_dir: str) -> str:
    return os.path.join(base_dir, CACHE_FILENAME)


def read_cache(base_dir: str):
    """读磁盘缓存。返回 (catalog, fetched_at)；没有或坏了就 (None, 0.0)。"""
    try:
        with open(cache_file(base_dir), encoding="utf-8") as fh:
            payload = json.load(fh)
        data = payload.get("catalog") or {}
        return (data if data else None), float(payload.get("fetched_at") or 0)
    except (OSError, ValueError):
        return None, 0.0


def write_cache(base_dir: str, data: dict, ts: float) -> None:
    try:
        with open(cache_file(base_dir), "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": ts, "catalog": data}, fh)
    except OSError:
        pass
