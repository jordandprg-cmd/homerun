#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smart_money_bridge.py — PA Beacon (Telegram) -> homerun wallet tracker
======================================================================

Bridges the Chinese-language "PA Beacon" Telegram forum group into homerun's
existing smart-money wallet tracker.

    Telegram forum  -1003761522274  "PA Beacon"
      +- 聪明钱追踪   smart-money trade alerts   (full address in Profile: URL)
      +- 大额买入     whole-market large fills   (truncated addr + display name)
      +- 世界杯专区   World Cup flow             (账户: name / truncated addr)
      +- 每日榜单     daily leaderboards         (rank, pnl, ROI, win rate)
      +- 讨论组       human chat                 (ignored)
            |
            v
    [1] TRANSLATE   zh -> en (pluggable; default = offline glossary)
            |
            v
    [2] EXTRACT     per-topic parsers -> wallet / profile name / side /
                    amount / market, PLUS the stats the bot already
                    publishes (15天盈利, 胜率, 收益率, 平均仓位, board rank).
                    Truncated 0xABC...DEF addresses are reconciled against a
                    persistent prefix/suffix index built from Profile: URLs
                    and leaderboard hyperlinks.
            |
            v
    [3] SCORE       Tier 1 — free, from the feed's own numbers. Cheap gate.
                    Tier 2 — only for Tier-1 survivors: pull positions and
                    trades through homerun's API, compute the real score.
                    Everything failing the gates is ELIMINATED.
            |
            v
    [4] INJECT      POST /api/wallets?address=<0x.. or profilename>&label=..
                    homerun's resolve_wallet_identifier() takes either form,
                    so we send the full address when we have it and the
                    profile name when the address is only ever truncated.

CPU-only throughout. No GPU, no local model unless you opt into `argos`.

Install
-------
    pip install telethon httpx
    # optional translator backends:
    #   pip install deep-translator     # cloud, no API key
    #   pip install argostranslate      # fully offline CPU neural

Quick start
-----------
    python smart_money_bridge.py login          # one-time Telegram login
    python smart_money_bridge.py list-topics    # confirm the 5 topic ids
    python smart_money_bridge.py test-parse --file samples.txt
    python smart_money_bridge.py backfill       # sweep history
    python smart_money_bridge.py score          # evaluate candidates
    python smart_money_bridge.py review         # look before you leap
    python smart_money_bridge.py inject --dry-run
    python smart_money_bridge.py inject         # push into homerun
    python smart_money_bridge.py run            # live daemon

Config: bridge_config.json (auto-created) + .env. Env wins over JSON.
State:  smart_money_bridge.db (SQLite, stdlib only).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path
from typing import Any, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    print("FATAL: pip install httpx", file=sys.stderr)
    raise

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "bridge_config.json"
DB_PATH = HERE / "smart_money_bridge.db"
SESSION_PATH = HERE / "smart_money_bridge"  # Telethon appends .session

log = logging.getLogger("bridge")


# =============================================================================
# 0.  ENV / CONFIG
# =============================================================================

def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — avoids a python-dotenv dependency."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# The PA Beacon forum. `t.me/c/3761522274/<topic>` -> peer id -1003761522274.
PA_BEACON_CHAT_ID = -1003761522274

DEFAULT_CONFIG: dict[str, Any] = {
    "_comment": (
        "Topic ids are best confirmed with `python smart_money_bridge.py list-topics`. "
        "`kind` selects the parser: smart_money | large_buy | worldcup | leaderboard | "
        "generic | ignore."
    ),

    # ---- Telegram -------------------------------------------------------
    "telegram": {
        "chat": PA_BEACON_CHAT_ID,     # numeric id, @handle, or exact title
        # Per-topic config. `id: null` means "not confirmed yet" — run
        # list-topics, paste the real ids, or leave null to match by title.
        "topics": [
            {"id": None, "title": "聪明钱追踪", "kind": "smart_money",
             "enabled": True, "backfill_limit": 4000},
            {"id": None, "title": "大额买入", "kind": "large_buy",
             "enabled": True, "backfill_limit": 4000},
            {"id": None, "title": "每日榜单", "kind": "leaderboard",
             "enabled": True, "backfill_limit": 600},
            # 107k messages and mostly one market — high volume, low unique
            # wallet yield. Off by default; flip enabled when you want it.
            {"id": None, "title": "世界杯专区", "kind": "worldcup",
             "enabled": False, "backfill_limit": 2000},
            {"id": None, "title": "讨论组", "kind": "ignore",
             "enabled": False, "backfill_limit": 0},
        ],
    },

    # ---- Translation ----------------------------------------------------
    "translate": {
        # glossary | argos | deep | none
        #   glossary = built-in term map. Instant, offline, zero deps, and
        #              exact on PA Beacon's templated fields.
        #   argos    = offline neural CPU (pip install argostranslate, ~1GB)
        #   deep     = deep-translator/Google (internet, no API key)
        "backend": "glossary",
        "source_lang": "zh",
        "target_lang": "en",
        "fallback_chain": ["glossary", "none"],
    },

    # ---- Scoring --------------------------------------------------------
    "scoring": {
        # ---- Tier 1: free, straight from what the bot already prints ----
        # Only wallets passing this get an API round-trip.
        "tier1": {
            "min_win_rate_pct": 55.0,       # 胜率
            "min_profit_usd": 20_000.0,     # 盈利 / 15天盈利
            "min_roi_pct": 0.0,             # 收益率
            "min_mentions": 1,
            # Floor for wallets with NO published stats, judged on their
            # biggest reported 金额 alone. 世界杯专区 only reports trades
            # over $5K by its own stated criteria, so a $5K floor there
            # admits literally everyone — hence $25K.
            "min_single_trade_usd": 25_000.0,
            # A wallet the bot itself put on 稳健盈利榜/高胜率榜 skips the
            # profit floor — being ranked is already the filter.
            "board_bypasses_profit_floor": True,
            # Win rate is a poor standalone gate in prediction markets: a
            # trader buying underdogs at 20c is *supposed* to lose most
            # bets and still print money. Measured on real PA Beacon data,
            # a 55% floor killed Supah9ga ($492k profit, 46.7% win) and
            # NO-GOD-PLEASE-NO ($312k, 46.3%). Profit above this overrides
            # the win-rate floor. 0 disables the bypass.
            "profit_bypasses_win_rate_usd": 100_000.0,
        },

        # ---- Tier 2: on-chain, via homerun -> Polymarket ----------------
        "weights": {
            "pnl": 0.26,           # realized + unrealized profit
            "win_rate": 0.19,      # fraction of positions in profit
            "lump_size": 0.17,     # "large lump sums" — p90 position size
            "volume": 0.11,        # total capital deployed
            "breadth": 0.08,       # distinct markets (anti one-hit-wonder)
            "recency": 0.07,       # days since last trade
            "group_signal": 0.06,  # how loudly PA Beacon flags them
            "feed_quality": 0.06,  # the bot's own pnl/winrate/board rank
        },
        "saturation": {
            "pnl_usd": 250_000.0,
            "lump_usd": 50_000.0,
            "volume_usd": 1_000_000.0,
            "breadth_markets": 40,
            "recency_days": 30,
            "group_mentions": 8,
        },
        "large_trade_usd": 10_000.0,

        # ---- HARD GATES — everything failing these is ELIMINATED --------
        "gates": {
            "min_score": 55.0,
            "min_total_pnl_usd": 5_000.0,
            "min_positions": 5,
            "min_volume_usd": 25_000.0,
            "min_win_rate": 0.45,
            "max_days_since_last_trade": 60,
        },

        # Relative cut applied AFTER the absolute gates: keep only the best
        # N% of everything that qualified, by score. null disables it.
        # Absolute thresholds are guesses until you've seen real data; a
        # percentile is self-calibrating — "the best 10% of what this group
        # actually produces" needs no tuning.
        "top_percent": None,
        # Never let the percentile cut leave fewer than this many wallets
        # (a 10% cut of 12 qualifiers is one wallet, which is useless).
        "top_percent_floor": 10,
    },

    # ---- homerun injection ---------------------------------------------
    "homerun": {
        "base_url": "http://localhost:8000",
        "api_prefix": "/api",
        "timeout_seconds": 45.0,
        "auto_inject": False,          # false => queue for `review`
        "prefer": "address",           # address | profile
        "label_template": "pabeacon{suffix}",
        "rescore_interval_hours": 12,
        "max_scores_per_cycle": 60,    # bound the API burst per cycle
    },

    "log_level": "INFO",
}


class Config:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw

    @classmethod
    def load(cls) -> "Config":
        _load_dotenv(HERE / ".env")
        if not CONFIG_PATH.exists():
            CONFIG_PATH.write_text(
                json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            log.warning("Created default config at %s", CONFIG_PATH)
            data = json.loads(json.dumps(DEFAULT_CONFIG))
        else:
            data = _deep_merge(
                json.loads(json.dumps(DEFAULT_CONFIG)),
                json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")),
            )

        hr = data["homerun"]
        hr["base_url"] = os.environ.get("HOMERUN_BASE_URL", hr["base_url"]).rstrip("/")
        hr["auto_inject"] = _env_bool("AUTO_INJECT", hr["auto_inject"])
        data["scoring"]["gates"]["min_score"] = _env_float(
            "MIN_SCORE", data["scoring"]["gates"]["min_score"]
        )
        data["translate"]["backend"] = os.environ.get(
            "TRANSLATE_BACKEND", data["translate"]["backend"]
        )
        if os.environ.get("TELEGRAM_CHAT"):
            raw_chat = os.environ["TELEGRAM_CHAT"].strip()
            data["telegram"]["chat"] = int(raw_chat) if re.fullmatch(r"-?\d+", raw_chat) else raw_chat
        data["log_level"] = os.environ.get("LOG_LEVEL", data["log_level"])
        return cls(data)

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def save(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps(self.raw, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def _deep_merge(base: dict, override: dict) -> dict:
    """override wins; nested dicts merged, lists replaced wholesale."""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# =============================================================================
# 1.  TRANSLATION
# =============================================================================

# Longest-first at build time so multi-char terms beat their substrings
# ("买入金额" must win over "买入" and "金额").
_GLOSSARY: dict[str, str] = {
    # --- PA Beacon field labels -------------------------------------
    "聪明钱交易提醒": "SMART MONEY TRADE ALERT",
    "全市场大额成交": "WHOLE-MARKET LARGE FILL",
    "大额买入信号": "LARGE BUY SIGNAL",
    "世界杯资金观察": "World Cup flow watch",
    "本市场当前持仓": "current position in this market",
    "其他持仓": "other positions",
    "成交数量": "filled quantity",
    "买入方向": "buy direction",
    "市场链接": "market link",
    "平均仓位": "average position",
    "稳健盈利榜": "steady-profit board",
    "高胜率榜": "high-win-rate board",
    "监测日报": "daily monitoring report",
    "北京时间": "Beijing time",
    "估值": "valued at",
    "合计约": "totalling approx",
    "介绍": "profile",
    "观察": "observation",
    "背景": "background",
    "口径": "criteria",
    "分类": "category",
    "盘口": "line",
    "比赛": "match",
    "账户": "account",
    "份额": "shares",
    "均价": "avg price",
    "动作": "action",
    "方向": "direction",
    "交易": "trade",
    "地址": "address",
    "时间": "time",
    "金额": "amount",
    # --- actions -----------------------------------------------------
    "买入": "BUY", "买进": "BUY", "做多": "LONG", "建仓": "OPEN", "加仓": "ADD",
    "卖出": "SELL", "卖掉": "SELL", "做空": "SHORT", "平仓": "CLOSE",
    "减仓": "REDUCE", "清仓": "EXIT", "开仓": "OPEN", "追加": "added",
    "买": "buy", "卖": "sell",
    # --- actors ------------------------------------------------------
    "聪明钱": "smart money", "大户": "whale", "巨鲸": "whale", "鲸鱼": "whale",
    "交易员": "trader", "玩家": "player", "用户名": "username", "用户": "user",
    "昵称": "nickname", "内幕": "insider",
    # --- wallets -----------------------------------------------------
    "钱包地址": "wallet address", "交易地址": "trading address",
    "钱包": "wallet", "主页": "profile",
    # --- money -------------------------------------------------------
    "买入金额": "buy amount", "卖出金额": "sell amount", "成交额": "notional",
    "交易额": "volume", "下注": "bet", "投入": "staked", "持仓": "position",
    "仓位": "position size", "规模": "size", "总额": "total",
    "余额": "balance", "本金": "principal", "累计": "cumulative",
    "美元": "USD", "美金": "USD", "美分": "cents",
    "盈利": "profit", "亏损": "loss", "收益率": "ROI", "收益": "return",
    "胜率": "win rate", "回报": "payout", "浮盈": "unrealized profit",
    "已实现": "realized", "未实现": "unrealized",
    # --- market ------------------------------------------------------
    "预测市场": "prediction market", "市场": "market", "事件": "event",
    "合约": "contract", "标的": "underlying", "问题": "question",
    "赔率": "odds", "价格": "price", "概率": "probability",
    "结果": "outcome", "结算": "resolution", "到期": "expiry",
    "流动性": "liquidity", "深度": "depth", "集中度": "concentration",
    # --- outcomes ----------------------------------------------------
    "是": "YES", "否": "NO", "会": "YES", "不会": "NO",
    # --- misc --------------------------------------------------------
    "刚刚": "just now", "分钟前": "minutes ago", "小时前": "hours ago",
    "天前": "days ago", "天": "d",
    "警报": "ALERT", "提醒": "alert", "监控": "monitor", "追踪": "tracking",
    "信号": "signal", "新增": "new", "首次": "first-time", "重复": "repeat",
    "排行榜": "leaderboard", "排名": "rank", "第": "#",
    "推高": "pushed up", "情绪": "sentiment", "可能影响": "may affect",
    "显示": "shows", "信心": "confidence", "增强": "strengthened",
    "个": "", "约": "approx", "无": "none", "其他": "other",
}

_GLOSSARY_RE = re.compile(
    "|".join(re.escape(t) for t in sorted(_GLOSSARY, key=len, reverse=True))
)
_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")


def has_chinese(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


class Translator:
    """Pluggable zh->en translation with a graceful fallback chain."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.backend = cfg.get("backend", "glossary")
        self.src = cfg.get("source_lang", "zh")
        self.dst = cfg.get("target_lang", "en")
        self._chain = [self.backend] + [
            b for b in cfg.get("fallback_chain", []) if b != self.backend
        ]
        self._argos = None
        self._deep = None
        self._cache: dict[str, str] = {}

    @staticmethod
    def _glossary(text: str) -> str:
        """Term substitution. Deterministic, offline, instant.

        Deliberately not a real MT system. PA Beacon emits fixed templates;
        substituting the template vocabulary yields a fully readable English
        line. Free-form Chinese prose (the 背景/观察 paragraphs) comes out
        pidgin — acceptable, because extraction runs against the raw text
        too, so nothing downstream depends on translation quality.
        """
        return _GLOSSARY_RE.sub(lambda m: f" {_GLOSSARY[m.group(0)]} ", text)

    def _argos_translate(self, text: str) -> str:
        if self._argos is None:
            import argostranslate.translate  # type: ignore

            installed = argostranslate.translate.get_installed_languages()
            src = next((l for l in installed if l.code == self.src), None)
            dst = next((l for l in installed if l.code == self.dst), None)
            if not src or not dst:
                raise RuntimeError(
                    f"argos {self.src}->{self.dst} model not installed. Run:\n"
                    "  python -c \"import argostranslate.package as p; p.update_package_index();"
                    " [p.install_from_path(x.download()) for x in p.get_available_packages()"
                    f" if x.from_code=='{self.src}' and x.to_code=='{self.dst}']\""
                )
            self._argos = src.get_translation(dst)
        return self._argos.translate(text)

    def _deep_translate(self, text: str) -> str:
        if self._deep is None:
            from deep_translator import GoogleTranslator  # type: ignore

            self._deep = GoogleTranslator(source=self.src, target=self.dst)
        return self._deep.translate(text)

    def translate(self, text: str) -> str:
        text = (text or "").strip()
        if not text or not has_chinese(text):
            return text
        if text in self._cache:
            return self._cache[text]

        out = text
        for backend in self._chain:
            try:
                if backend == "none":
                    out = text
                elif backend == "glossary":
                    out = self._glossary(text)
                elif backend == "argos":
                    out = self._argos_translate(text)
                elif backend == "deep":
                    out = self._deep_translate(text)
                else:
                    continue
                break
            except Exception as exc:  # noqa: BLE001 — falling through is the point
                log.debug("translator %s failed: %s", backend, exc)

        out = re.sub(r"[ \t]{2,}", " ", out).strip()
        if len(self._cache) < 20_000:
            self._cache[text] = out
        return out


# =============================================================================
# 2.  EXTRACTION
# =============================================================================

# Full 40-hex address.
ETH_RE = re.compile(r"0x[a-fA-F0-9]{40}")
# PA Beacon's elided form: 0x448861...99e319  /  0xacb206…7eb989
TRUNC_RE = re.compile(r"(0x[a-fA-F0-9]{2,12})(?:\.{2,}|…)([a-fA-F0-9]{2,12})")
PROFILE_URL_RE = re.compile(
    r"polymarket\.com/(?:profile/|@)(0x[a-fA-F0-9]{40}|[A-Za-z0-9_\-\.]{2,40})",
    re.IGNORECASE,
)
EVENT_URL_RE = re.compile(r"polymarket\.com/event/([^\s\)\]<>]+)", re.IGNORECASE)

# Display name in （full-width） or (half-width) parens right after an address.
NAME_PAREN_RE = re.compile(r"[（(]\s*([^）)]{1,48}?)\s*[）)]")

_NAME_STOPLIST = {
    "polymarket", "kalshi", "yes", "no", "buy", "sell", "none", "无", "其他",
    "pa beacon", "pabeacon", "admin", "bot", "profile", "link", "链接",
}


def _num(raw: Optional[str]) -> Optional[float]:
    """'+84,335.89' -> 84335.89 ; '78.7' -> 78.7"""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("+", "").replace("$", "").replace(" ", "")
    if not s or s in {"-", "--"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_amount(raw: Optional[str]) -> Optional[float]:
    """'$278.11K' -> 278110 ; '45万' -> 450000 ; '359,831.32' -> 359831.32"""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("$", "").replace(" ", "").replace("+", "")
    mult = 1.0
    for suffix, factor in (
        ("亿", 1e8), ("億", 1e8), ("百万", 1e6), ("万", 1e4), ("千", 1e3),
        ("b", 1e9), ("B", 1e9), ("m", 1e6), ("M", 1e6), ("k", 1e3), ("K", 1e3),
    ):
        if s.endswith(suffix):
            mult, s = factor, s[: -len(suffix)]
            break
    try:
        return float(s) * mult
    except ValueError:
        return None


def _clean_name(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    name = raw.strip().strip("（）()[]{}<>").strip()
    if not name or name.lower() in _NAME_STOPLIST:
        return None
    # A "name" that is itself a full address is an address, not a name.
    if ETH_RE.fullmatch(name):
        return None
    if len(name) > 48 or has_chinese(name):
        return None
    return name


@dataclass
class FeedStats:
    """Numbers PA Beacon publishes itself — free, no API call needed."""
    profit_usd: Optional[float] = None        # 盈利 (leaderboard)
    profit_15d_usd: Optional[float] = None    # 15天盈利 (smart-money alerts)
    roi_pct: Optional[float] = None           # 收益率
    win_rate_pct: Optional[float] = None      # 胜率
    avg_position_usd: Optional[float] = None  # 平均仓位
    board: Optional[str] = None               # 稳健盈利榜 / 高胜率榜
    board_rank: Optional[int] = None

    def merged_profit(self) -> Optional[float]:
        vals = [v for v in (self.profit_usd, self.profit_15d_usd) if v is not None]
        return max(vals) if vals else None

    def is_empty(self) -> bool:
        return all(
            getattr(self, f) is None
            for f in ("profit_usd", "profit_15d_usd", "roi_pct",
                      "win_rate_pct", "avg_position_usd", "board")
        )


@dataclass
class Candidate:
    """One wallet extracted from one message."""
    msg_id: int = 0
    topic: str = ""
    kind: str = ""
    ts: float = 0.0
    wallet: Optional[str] = None          # full 0x…, lowercase
    wallet_trunc: Optional[str] = None    # "0xabc…def" as printed
    profile: Optional[str] = None         # Polymarket display name
    side: Optional[str] = None            # BUY / SELL
    amount_usd: Optional[float] = None
    market: Optional[str] = None
    outcome: Optional[str] = None
    feed: FeedStats = field(default_factory=FeedStats)
    text_zh: str = ""
    text_en: str = ""

    @property
    def identifier(self) -> Optional[str]:
        """What we hand homerun: full address if known, else profile name."""
        return self.wallet or self.profile

    @property
    def key(self) -> Optional[str]:
        """Stable dedup key. Prefers the address; profile name otherwise."""
        if self.wallet:
            return self.wallet
        if self.profile:
            return f"@{self.profile}"
        if self.wallet_trunc:
            return f"~{self.wallet_trunc}"
        return None


def trunc_key(text: str) -> Optional[str]:
    """Normalize '0x448861...99e319' -> '0x448861~99e319' (lowercased)."""
    m = TRUNC_RE.search(text or "")
    if not m:
        return None
    return f"{m.group(1).lower()}~{m.group(2).lower()}"


def trunc_matches(tkey: str, address: str) -> bool:
    """Does full `address` satisfy the elided form `tkey`?"""
    try:
        prefix, suffix = tkey.split("~", 1)
    except ValueError:
        return False
    a = address.lower()
    return a.startswith(prefix.lower()) and a.endswith(suffix.lower())


class Extractor:
    """Per-topic parsers for the PA Beacon templates.

    Each parser gets the *augmented* text: the message body plus every URL
    harvested from Telegram's message entities, appended on their own lines.
    That matters because PA Beacon hides the only full-length address behind
    a hyperlink ("Polymarket 链接") in the leaderboard topic.
    """

    # 介绍：15天盈利 101647 美元；平均仓位 225 美元
    RE_INTRO = re.compile(
        r"介绍\s*[:：]\s*(\d+)\s*天?\s*盈利\s*([\-+]?[\d,\.]+)\s*美元"
        r"(?:\s*[;；,，]\s*平均仓位\s*([\d,\.]+)\s*美元)?"
    )
    # 地址：0x448861...99e319（Flipadelphia）
    RE_ADDR_LINE = re.compile(
        r"(?:地址|账户|交易地址|钱包地址)\s*[:：]\s*(.{0,120})"
    )
    # 动作：买入 | 方向：否 | 均价：57.7¢ | 金额：$359,831.32
    RE_ACTION = re.compile(r"动作\s*[:：]\s*(买入|卖出|BUY|SELL)", re.IGNORECASE)
    RE_AMOUNT = re.compile(r"金额\s*[:：]\s*\$?\s*([\d,\.]+\s*[KkMmBb万亿]?)")
    RE_DIRECTION = re.compile(r"(?:方向|买入方向)\s*[:：]\s*([^\|\n]{1,60})")
    RE_TRADE = re.compile(r"(?:交易|盘口|比赛)\s*[:：]\s*([^\|\n]{2,160})")
    RE_TIME = re.compile(r"时间\s*[:：]\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")

    # Leaderboard row. Whitespace-flexible: PA Beacon renders these both
    # multi-line (in-channel) and run-together (in the pinned preview).
    #   4. AppleTime67
    #   0xacb206...7eb989
    #   盈利 +84,335.89 美元 | 收益率 +9.76% | 胜率 78.7%
    #   平均仓位 140 美元
    RE_BOARD_ROW = re.compile(
        r"(\d{1,3})\.\s*(\S[^\n]{0,80}?)\s*"
        r"(0x[a-fA-F0-9]{2,12}(?:\.{2,}|…)[a-fA-F0-9]{2,12})\s*"
        r"盈利\s*([\-+]?[\d,\.]+)\s*美元\s*\|\s*"
        r"收益率\s*([\-+]?[\d\.]+)\s*%\s*\|\s*"
        r"胜率\s*([\d\.]+)\s*%"
        r"(?:\s*平均仓位\s*([\d,\.]+)\s*美元)?"
    )
    RE_BOARD_TITLE = re.compile(r"(稳健盈利榜|高胜率榜|大额榜|活跃榜)")

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _addresses(text: str) -> list[str]:
        seen, out = set(), []
        for m in ETH_RE.finditer(text):
            a = m.group(0).lower()
            if a not in seen:
                seen.add(a)
                out.append(a)
        return out

    @staticmethod
    def _profile_urls(text: str) -> tuple[list[str], list[str]]:
        """-> (full addresses, usernames) referenced by profile URLs."""
        addrs, names = [], []
        for m in PROFILE_URL_RE.finditer(text):
            v = m.group(1)
            if ETH_RE.fullmatch(v):
                addrs.append(v.lower())
            else:
                cleaned = _clean_name(v)
                if cleaned:
                    names.append(cleaned)
        return addrs, names

    def _common(self, text: str) -> dict[str, Any]:
        """Fields shared by the three alert templates."""
        side = None
        m = self.RE_ACTION.search(text)
        if m:
            tok = m.group(1).lower()
            side = "SELL" if tok in {"卖出", "sell"} else "BUY"
        elif "买入方向" in text or "大额买入" in text:
            side = "BUY"

        amount = None
        m = self.RE_AMOUNT.search(text)
        if m:
            amount = _parse_amount(m.group(1))
        if amount is None:
            m = re.search(r"金额\s*[:：]\s*\$?\s*([\d,\.]+\s*[KkMmBb万亿]?)", text)
            amount = _parse_amount(m.group(1)) if m else None

        market = None
        m = self.RE_TRADE.search(text)
        if m:
            market = re.sub(r"\s+", " ", m.group(1)).strip()[:200]
        if not market:
            m = EVENT_URL_RE.search(text)
            if m:
                market = m.group(1)[:200]

        outcome = None
        m = self.RE_DIRECTION.search(text)
        if m:
            outcome = m.group(1).strip()[:80]

        ts = None
        m = self.RE_TIME.search(text)
        if m:
            # PA Beacon stamps 北京时间 (UTC+8) unless it says UTC.
            try:
                from datetime import datetime, timedelta
                naive = datetime.strptime(m.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S")
                offset = timedelta(0) if "UTC" in text[m.start():m.start() + 60] else timedelta(hours=8)
                ts = (naive - offset).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                ts = None

        return {"side": side, "amount_usd": amount, "market": market,
                "outcome": outcome, "stamp": ts}

    def _feed_intro(self, text: str) -> FeedStats:
        fs = FeedStats()
        m = self.RE_INTRO.search(text)
        if m:
            fs.profit_15d_usd = _num(m.group(2))
            fs.avg_position_usd = _num(m.group(3))
        m = self.RE_BOARD_TITLE.search(text)
        if m:
            fs.board = m.group(1)
        return fs

    # ---- per-topic parsers ---------------------------------------------

    def parse_alert(self, text: str, kind: str) -> list[Candidate]:
        """聪明钱追踪 / 大额买入 / 世界杯专区 — one wallet per message."""
        common = self._common(text)
        feed = self._feed_intro(text)

        url_addrs, url_names = self._profile_urls(text)
        body_addrs = self._addresses(text)
        full = (url_addrs + body_addrs) or []

        # The address line carries the elided form and the display name.
        tkey, name = None, None
        m = self.RE_ADDR_LINE.search(text)
        if m:
            seg = m.group(1)
            tkey = trunc_key(seg)
            nm = NAME_PAREN_RE.search(seg)
            if nm:
                name = _clean_name(nm.group(1))
            if name is None:
                # 世界杯: 账户：LBmlb / 0xf79c...0bcb96
                slash = re.match(r"\s*([A-Za-z0-9_\-\.]{2,40})\s*/\s*0x", seg)
                if slash:
                    name = _clean_name(slash.group(1))
        if tkey is None:
            tkey = trunc_key(text)
        if name is None and url_names:
            name = url_names[0]

        # A message can carry several addresses (其他持仓 lines, market URLs).
        # The elided form on the 地址 line is the ground truth for *which*
        # wallet this alert is about, so prefer the address that satisfies it
        # and only fall back to first-seen when nothing matches.
        chosen = None
        if full:
            if tkey:
                chosen = next((a for a in full if trunc_matches(tkey, a)), None)
            if chosen is None and not tkey:
                chosen = full[0]

        if not chosen and not name and not tkey:
            return []

        return [Candidate(
            kind=kind,
            wallet=chosen,
            wallet_trunc=tkey,
            profile=name,
            side=common["side"],
            amount_usd=common["amount_usd"],
            market=common["market"],
            outcome=common["outcome"],
            feed=feed,
            text_zh=text,
        )]

    def parse_leaderboard(self, text: str) -> list[Candidate]:
        """每日榜单 — many ranked wallets per message.

        The visible text only holds elided addresses; the full ones live in
        the `Polymarket 链接` hyperlinks, which arrive appended to the text
        as harvested entity URLs. Match them up by prefix/suffix.
        """
        board = None
        m = self.RE_BOARD_TITLE.search(text)
        if m:
            board = m.group(1)

        url_addrs, _ = self._profile_urls(text)
        url_addrs += self._addresses(text)

        out: list[Candidate] = []
        for row in self.RE_BOARD_ROW.finditer(text):
            rank = int(row.group(1))
            raw_name = row.group(2).strip()
            tkey = trunc_key(row.group(3))

            # Entries 5/8/9 in PA Beacon's boards use a bare address (plus a
            # numeric suffix) as the "name" — that is an address, not a name.
            name_addr = ETH_RE.search(raw_name)
            name = _clean_name(raw_name)

            full = None
            if name_addr:
                full = name_addr.group(0).lower()
            elif tkey:
                full = next((a for a in url_addrs if trunc_matches(tkey, a)), None)

            fs = FeedStats(
                profit_usd=_num(row.group(4)),
                roi_pct=_num(row.group(5)),
                win_rate_pct=_num(row.group(6)),
                avg_position_usd=_num(row.group(7)),
                board=board,
                board_rank=rank,
            )
            if not full and not name and not tkey:
                continue
            out.append(Candidate(
                kind="leaderboard",
                wallet=full,
                wallet_trunc=tkey,
                profile=name,
                feed=fs,
                text_zh=row.group(0),
            ))
        return out

    def parse_generic(self, text: str) -> list[Candidate]:
        """Fallback for message shapes we haven't templated."""
        common = self._common(text)
        url_addrs, url_names = self._profile_urls(text)
        addrs = url_addrs + self._addresses(text)
        tkey = trunc_key(text)
        name = url_names[0] if url_names else None
        if name is None:
            nm = NAME_PAREN_RE.search(text)
            if nm:
                name = _clean_name(nm.group(1))
        if not addrs and not name and not tkey:
            return []
        return [Candidate(
            kind="generic",
            wallet=addrs[0] if addrs else None,
            wallet_trunc=tkey,
            profile=name,
            side=common["side"],
            amount_usd=common["amount_usd"],
            market=common["market"],
            outcome=common["outcome"],
            feed=self._feed_intro(text),
            text_zh=text,
        )]

    def extract(self, text: str, kind: str) -> list[Candidate]:
        if kind == "ignore" or not text.strip():
            return []
        if kind == "leaderboard":
            cands = self.parse_leaderboard(text)
            # Some leaderboard posts are prose summaries with no rows.
            return cands or self.parse_generic(text)
        if kind in {"smart_money", "large_buy", "worldcup"}:
            return self.parse_alert(text, kind)
        return self.parse_generic(text)


# =============================================================================
# 3.  STORAGE
# =============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id       INTEGER PRIMARY KEY,
    topic    TEXT NOT NULL,
    msg_id   INTEGER NOT NULL,
    ts       REAL NOT NULL,
    text_zh  TEXT NOT NULL,
    text_en  TEXT NOT NULL,
    parsed   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(topic, msg_id)
);

CREATE TABLE IF NOT EXISTS observations (
    id         INTEGER PRIMARY KEY,
    wallet_key TEXT NOT NULL,
    topic      TEXT NOT NULL,
    msg_id     INTEGER NOT NULL,
    ts         REAL NOT NULL,
    kind       TEXT,
    side       TEXT,
    amount_usd REAL,
    market     TEXT,
    outcome    TEXT,
    UNIQUE(topic, msg_id, wallet_key)
);

-- Reconciles PA Beacon's elided "0xabc...def" against full addresses
-- learned from Profile: URLs and leaderboard hyperlinks.
CREATE TABLE IF NOT EXISTS address_index (
    trunc_key TEXT PRIMARY KEY,
    address   TEXT NOT NULL,
    profile   TEXT,
    learned_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    wallet_key      TEXT PRIMARY KEY,
    address         TEXT,
    trunc_key       TEXT,
    profile         TEXT,
    first_seen      REAL NOT NULL,
    last_seen       REAL NOT NULL,
    mentions        INTEGER NOT NULL DEFAULT 0,
    reported_volume REAL NOT NULL DEFAULT 0,
    max_reported    REAL NOT NULL DEFAULT 0,
    feed_profit     REAL,
    feed_profit_15d REAL,
    feed_roi        REAL,
    feed_win_rate   REAL,
    feed_avg_pos    REAL,
    feed_board      TEXT,
    feed_best_rank  INTEGER,
    tier1           TEXT,           -- pending | pass | fail
    tier1_reason    TEXT,
    score           REAL,
    score_detail    TEXT,
    verdict         TEXT,           -- pending | qualified | eliminated
                                    -- | unresolvable | error
    reason          TEXT,
    scored_at       REAL,
    injected_at     REAL,
    injected_as     TEXT
);

CREATE INDEX IF NOT EXISTS idx_cand_verdict ON candidates(verdict);
CREATE INDEX IF NOT EXISTS idx_cand_tier1   ON candidates(tier1);
CREATE INDEX IF NOT EXISTS idx_cand_trunc   ON candidates(trunc_key);
CREATE INDEX IF NOT EXISTS idx_obs_wallet   ON observations(wallet_key);
"""


class Store:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    # -- messages ---------------------------------------------------------

    def have_message(self, topic: str, msg_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM messages WHERE topic=? AND msg_id=?", (topic, msg_id)
        ).fetchone() is not None

    def save_message(self, topic: str, msg_id: int, ts: float,
                     zh: str, en: str, parsed: bool) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO messages(topic,msg_id,ts,text_zh,text_en,parsed) "
            "VALUES(?,?,?,?,?,?)",
            (topic, msg_id, ts, zh, en, 1 if parsed else 0),
        )

    def unparsed_messages(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM messages WHERE parsed=0 ORDER BY ts DESC LIMIT ?", (limit,)
        ))

    def message_counts(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT topic, COUNT(*) n, SUM(parsed) parsed FROM messages GROUP BY topic"
        ))

    # -- address reconciliation -------------------------------------------

    def learn_address(self, tkey: Optional[str], address: Optional[str],
                      profile: Optional[str]) -> None:
        if not tkey or not address or not trunc_matches(tkey, address):
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO address_index(trunc_key,address,profile,learned_at) "
            "VALUES(?,?,?,?)",
            (tkey, address.lower(), profile, time.time()),
        )

    def resolve_trunc(self, tkey: Optional[str]) -> Optional[sqlite3.Row]:
        if not tkey:
            return None
        return self.conn.execute(
            "SELECT * FROM address_index WHERE trunc_key=?", (tkey,)
        ).fetchone()

    def backfill_truncated(self) -> int:
        """Second pass: upgrade '~0xabc~def' candidates now that we know more.

        Merges the placeholder row into the real address row, carrying its
        mentions and feed stats across, then drops the placeholder.
        """
        merged = 0
        rows = list(self.conn.execute(
            "SELECT * FROM candidates WHERE address IS NULL AND trunc_key IS NOT NULL"
        ))
        for row in rows:
            hit = self.resolve_trunc(row["trunc_key"])
            if not hit:
                continue
            addr = hit["address"]
            target = self.conn.execute(
                "SELECT * FROM candidates WHERE wallet_key=?", (addr,)
            ).fetchone()
            if target is None:
                self.conn.execute(
                    "UPDATE candidates SET wallet_key=?, address=?, "
                    "profile=COALESCE(profile,?) WHERE wallet_key=?",
                    (addr, addr, hit["profile"], row["wallet_key"]),
                )
            else:
                # SQLite's scalar MAX(a,b) yields NULL if either side is
                # NULL, so every numeric merge is written null-safely:
                # keep whichever side has a value, take the larger when both
                # do. Getting this wrong silently poisoned feed_profit with
                # a sentinel and failed the wallet at Tier 1.
                self.conn.execute(
                    "UPDATE candidates SET "
                    "  mentions        = mentions + ?,"
                    "  reported_volume = reported_volume + ?,"
                    "  max_reported    = MAX(max_reported, ?),"
                    "  profile         = COALESCE(profile, ?),"
                    "  feed_profit     = CASE"
                    "     WHEN feed_profit IS NULL THEN ?"
                    "     WHEN ? IS NULL THEN feed_profit"
                    "     ELSE MAX(feed_profit, ?) END,"
                    "  feed_profit_15d = CASE"
                    "     WHEN feed_profit_15d IS NULL THEN ?"
                    "     WHEN ? IS NULL THEN feed_profit_15d"
                    "     ELSE MAX(feed_profit_15d, ?) END,"
                    "  feed_roi        = COALESCE(feed_roi, ?),"
                    "  feed_win_rate   = COALESCE(feed_win_rate, ?),"
                    "  feed_avg_pos    = COALESCE(feed_avg_pos, ?),"
                    "  feed_board      = COALESCE(feed_board, ?),"
                    "  feed_best_rank  = MIN(COALESCE(feed_best_rank, 9999),"
                    "                        COALESCE(?, 9999)),"
                    "  first_seen      = MIN(first_seen, ?),"
                    "  last_seen       = MAX(last_seen, ?) "
                    "WHERE wallet_key=?",
                    (row["mentions"], row["reported_volume"], row["max_reported"],
                     row["profile"],
                     row["feed_profit"], row["feed_profit"], row["feed_profit"],
                     row["feed_profit_15d"], row["feed_profit_15d"], row["feed_profit_15d"],
                     row["feed_roi"], row["feed_win_rate"], row["feed_avg_pos"],
                     row["feed_board"], row["feed_best_rank"],
                     row["first_seen"], row["last_seen"], addr),
                )
                self.conn.execute(
                    "DELETE FROM candidates WHERE wallet_key=?", (row["wallet_key"],)
                )
            self.conn.execute(
                "UPDATE OR IGNORE observations SET wallet_key=? WHERE wallet_key=?",
                (addr, row["wallet_key"]),
            )
            merged += 1
        self.conn.commit()
        return merged

    # -- candidates -------------------------------------------------------

    def record(self, c: Candidate) -> None:
        key = c.key
        if not key:
            return
        self.learn_address(c.wallet_trunc, c.wallet, c.profile)

        self.conn.execute(
            "INSERT OR IGNORE INTO observations"
            "(wallet_key,topic,msg_id,ts,kind,side,amount_usd,market,outcome) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (key, c.topic, c.msg_id, c.ts, c.kind, c.side,
             c.amount_usd, c.market, c.outcome),
        )

        f = c.feed
        amt = float(c.amount_usd or 0.0)
        row = self.conn.execute(
            "SELECT 1 FROM candidates WHERE wallet_key=?", (key,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO candidates("
                " wallet_key,address,trunc_key,profile,first_seen,last_seen,mentions,"
                " reported_volume,max_reported,feed_profit,feed_profit_15d,feed_roi,"
                " feed_win_rate,feed_avg_pos,feed_board,feed_best_rank,tier1,verdict) "
                "VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,'pending','pending')",
                (key, c.wallet, c.wallet_trunc, c.profile, c.ts, c.ts, amt, amt,
                 f.profit_usd, f.profit_15d_usd, f.roi_pct, f.win_rate_pct,
                 f.avg_position_usd, f.board, f.board_rank),
            )
        else:
            self.conn.execute(
                "UPDATE candidates SET "
                "  address         = COALESCE(address, ?),"
                "  trunc_key       = COALESCE(trunc_key, ?),"
                "  profile         = COALESCE(profile, ?),"
                "  first_seen      = MIN(first_seen, ?),"
                "  last_seen       = MAX(last_seen, ?),"
                "  mentions        = mentions + 1,"
                "  reported_volume = reported_volume + ?,"
                "  max_reported    = MAX(max_reported, ?),"
                "  feed_profit     = COALESCE(?, feed_profit),"
                "  feed_profit_15d = COALESCE(?, feed_profit_15d),"
                "  feed_roi        = COALESCE(?, feed_roi),"
                "  feed_win_rate   = COALESCE(?, feed_win_rate),"
                "  feed_avg_pos    = COALESCE(?, feed_avg_pos),"
                "  feed_board      = COALESCE(?, feed_board),"
                "  feed_best_rank  = MIN(COALESCE(feed_best_rank, 9999), COALESCE(?, 9999)) "
                "WHERE wallet_key=?",
                (c.wallet, c.wallet_trunc, c.profile, c.ts, c.ts, amt, amt,
                 f.profit_usd, f.profit_15d_usd, f.roi_pct, f.win_rate_pct,
                 f.avg_position_usd, f.board, f.board_rank, key),
            )

    def candidates(self, *, verdict: Optional[str] = None,
                   tier1: Optional[str] = None,
                   limit: int = 0) -> list[sqlite3.Row]:
        sql = "SELECT * FROM candidates WHERE 1=1"
        args: list[Any] = []
        if verdict:
            sql += " AND verdict = ?"
            args.append(verdict)
        if tier1:
            sql += " AND tier1 = ?"
            args.append(tier1)
        sql += (" ORDER BY COALESCE(score,-1) DESC,"
                " COALESCE(feed_profit, feed_profit_15d, 0) DESC, mentions DESC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self.conn.execute(sql, args))

    def set_tier1(self, key: str, passed: bool, reason: str) -> None:
        self.conn.execute(
            "UPDATE candidates SET tier1=?, tier1_reason=?, "
            "verdict=CASE WHEN ? THEN verdict ELSE 'eliminated' END, "
            "reason=CASE WHEN ? THEN reason ELSE ? END "
            "WHERE wallet_key=?",
            ("pass" if passed else "fail", reason, passed, passed,
             f"tier1: {reason}", key),
        )

    def set_score(self, key: str, score: Optional[float], detail: dict,
                  verdict: str, reason: str) -> None:
        self.conn.execute(
            "UPDATE candidates SET score=?, score_detail=?, verdict=?, reason=?, "
            "scored_at=? WHERE wallet_key=?",
            (score, json.dumps(detail, ensure_ascii=False), verdict, reason,
             time.time(), key),
        )
        self.conn.commit()

    def mark_injected(self, key: str, injected_as: str) -> None:
        self.conn.execute(
            "UPDATE candidates SET injected_at=?, injected_as=? WHERE wallet_key=?",
            (time.time(), injected_as, key),
        )
        self.conn.commit()


# =============================================================================
# 4.  HOMERUN CLIENT  (data source AND injection target)
# =============================================================================

class HomerunClient:
    """Talks to the running homerun backend.

    Polymarket data is pulled *through* homerun rather than direct from
    data-api.polymarket.com, so we inherit its rate limiter, username cache
    and retry behaviour instead of running a second uncoordinated scraper
    against the same endpoints.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.base = cfg["base_url"].rstrip("/") + cfg["api_prefix"]
        self.timeout = float(cfg.get("timeout_seconds", 45.0))
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "HomerunClient":
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        assert self._client is not None, "use HomerunClient as an async context manager"
        return self._client

    async def health(self) -> bool:
        try:
            r = await self.client.get(f"{self.base}/wallets")
            return r.status_code < 500
        except Exception:  # noqa: BLE001
            return False

    async def tracked_wallets(self) -> list[dict]:
        r = await self.client.get(f"{self.base}/wallets")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            data = data.get("wallets") or data.get("data") or []
        return [d for d in data if isinstance(d, dict)]

    async def positions(self, identifier: str) -> list[dict]:
        r = await self.client.get(
            f"{self.base}/wallets/{identifier}/positions",
            params={"include_prices": "true"},
        )
        if r.status_code != 200:
            return []
        payload = r.json()
        rows = payload.get("positions") if isinstance(payload, dict) else payload
        return [p for p in (rows or []) if isinstance(p, dict)]

    async def trades(self, identifier: str, limit: int = 300) -> list[dict]:
        r = await self.client.get(
            f"{self.base}/wallets/{identifier}/trades", params={"limit": limit}
        )
        if r.status_code != 200:
            return []
        payload = r.json()
        rows = payload.get("trades") if isinstance(payload, dict) else payload
        return [t for t in (rows or []) if isinstance(t, dict)]

    async def profile(self, identifier: str) -> Optional[dict]:
        try:
            r = await self.client.get(f"{self.base}/wallets/{identifier}/profile")
            return r.json() if r.status_code == 200 else None
        except Exception:  # noqa: BLE001
            return None

    async def add_wallet(self, identifier: str, label: str) -> dict:
        """POST /api/wallets — query params, matching routes.py::add_wallet."""
        r = await self.client.post(
            f"{self.base}/wallets", params={"address": identifier, "label": label}
        )
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()


# =============================================================================
# 5.  SCORING
# =============================================================================

def _f(row: dict, *names: str, default: float = 0.0) -> float:
    for n in names:
        v = row.get(n)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return default


def _saturating(value: float, ceiling: float) -> float:
    """0..1, ~0.86 at the ceiling, asymptotic after.

    Log-shaped on purpose: a $10M wallet should not score 40x a $250k one.
    Past a point, more money stops being more signal.
    """
    if ceiling <= 0:
        return 0.0
    return min(1.0, math.log1p(max(0.0, value) / ceiling * 1.718) / 1.0)


@dataclass
class ScoreCard:
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    verdict: str = "pending"
    reason: str = ""


class Tier1:
    """Free pre-filter over the numbers PA Beacon already prints.

    Exists purely to keep the Polymarket API bill sane: the group names
    thousands of distinct wallets and most of them are not interesting.
    A wallet with no feed stats at all passes — absence of evidence isn't
    evidence, and Tier 2 will judge it properly.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg

    def check(self, row: sqlite3.Row) -> tuple[bool, str]:
        c = self.cfg
        mentions = int(row["mentions"] or 0)
        if mentions < c["min_mentions"]:
            return False, f"mentions {mentions} < {c['min_mentions']}"

        win = row["feed_win_rate"]
        profit = row["feed_profit"] if row["feed_profit"] is not None else row["feed_profit_15d"]
        roi = row["feed_roi"]
        biggest = float(row["max_reported"] or 0.0)
        boarded = bool(row["feed_board"])

        bypass = float(c.get("profit_bypasses_win_rate_usd") or 0)
        rich = bypass > 0 and profit is not None and float(profit) >= bypass
        if win is not None and float(win) < c["min_win_rate_pct"] and not rich:
            return False, f"feed win rate {float(win):.1f}% < {c['min_win_rate_pct']}%"
        if roi is not None and float(roi) < c["min_roi_pct"]:
            return False, f"feed ROI {float(roi):.2f}% < {c['min_roi_pct']}%"
        if profit is not None:
            if float(profit) < c["min_profit_usd"] and not (
                boarded and c["board_bypasses_profit_floor"]
            ):
                return False, f"feed profit ${float(profit):,.0f} < ${c['min_profit_usd']:,.0f}"

        # No published stats at all: fall back to trade size, which is the
        # only quality signal the 大额买入 topic carries.
        if profit is None and win is None and not boarded:
            if biggest < c["min_single_trade_usd"]:
                return False, (f"no feed stats and biggest reported trade "
                               f"${biggest:,.0f} < ${c['min_single_trade_usd']:,.0f}")

        bits = []
        if boarded:
            bits.append(f"{row['feed_board']}#{row['feed_best_rank']}")
        if profit is not None:
            bits.append(f"profit ${float(profit):,.0f}")
        if win is not None:
            bits.append(f"win {float(win):.1f}%")
        if biggest:
            bits.append(f"max trade ${biggest:,.0f}")
        return True, "; ".join(bits) or f"{mentions} mention(s)"


class Scorer:
    """Tier 2 — the real evaluation, from on-chain Polymarket data."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.w = cfg["weights"]
        self.sat = cfg["saturation"]
        self.gates = cfg["gates"]
        self.large_usd = float(cfg["large_trade_usd"])

    def evaluate(self, positions: list[dict], trades: list[dict],
                 row: sqlite3.Row) -> ScoreCard:
        card = ScoreCard()

        # ---- from positions --------------------------------------------
        total_pnl = total_cost = 0.0
        wins = counted = 0
        sizes: list[float] = []
        markets: set[str] = set()

        for p in positions:
            cost = _f(p, "initialValue", "initial_value")
            pnl = _f(p, "cashPnl", "cash_pnl", "realizedPnl")
            if cost <= 0 and pnl == 0:
                continue
            total_cost += cost
            total_pnl += pnl
            if cost > 0:
                sizes.append(cost)
                counted += 1
                if pnl > 0:
                    wins += 1
            mkt = str(p.get("conditionId") or p.get("condition_id")
                      or p.get("title") or p.get("market") or "").strip()
            if mkt:
                markets.add(mkt)

        # ---- from trades ------------------------------------------------
        last_ts = trade_notional = 0.0
        large_trades = 0
        for t in trades:
            size = _f(t, "usdcSize", "usdc_size")
            if size <= 0:
                size = _f(t, "size") * _f(t, "price")
            if size > 0:
                trade_notional += size
                sizes.append(size)
                if size >= self.large_usd:
                    large_trades += 1
            ts = _f(t, "timestamp", "time", "createdAt")
            if ts > 1e12:  # milliseconds
                ts /= 1000.0
            last_ts = max(last_ts, ts)

        sizes.sort()
        p90 = sizes[min(len(sizes) - 1, int(len(sizes) * 0.9))] if sizes else 0.0
        win_rate = (wins / counted) if counted else 0.0
        volume = max(total_cost, trade_notional)
        days_idle = ((time.time() - last_ts) / 86400.0) if last_ts else 999.0

        mentions = int(row["mentions"] or 0)
        feed_profit = row["feed_profit"] if row["feed_profit"] is not None else row["feed_profit_15d"]
        feed_win = row["feed_win_rate"]
        rank = row["feed_best_rank"]

        card.stats = {
            "total_pnl_usd": round(total_pnl, 2),
            "volume_usd": round(volume, 2),
            "positions": counted,
            "open_positions": len(positions),
            "trades_sampled": len(trades),
            "win_rate": round(win_rate, 4),
            "p90_position_usd": round(p90, 2),
            "large_trades": large_trades,
            "distinct_markets": len(markets),
            "days_since_last_trade": round(days_idle, 1),
            "group_mentions": mentions,
            "group_reported_volume_usd": round(float(row["reported_volume"] or 0), 2),
            "group_max_trade_usd": round(float(row["max_reported"] or 0), 2),
            "feed_profit_usd": feed_profit,
            "feed_win_rate_pct": feed_win,
            "feed_board": row["feed_board"],
            "feed_best_rank": None if rank in (None, 9999) else rank,
        }

        # ---- components (each 0..1) -------------------------------------
        feed_quality = 0.0
        parts = 0
        if feed_profit is not None:
            feed_quality += _saturating(max(0.0, float(feed_profit)), self.sat["pnl_usd"])
            parts += 1
        if feed_win is not None:
            feed_quality += max(0.0, min(1.0, (float(feed_win) - 50.0) / 30.0))
            parts += 1
        if rank not in (None, 9999):
            feed_quality += max(0.0, 1.0 - (int(rank) - 1) / 20.0)
            parts += 1
        feed_quality = (feed_quality / parts) if parts else 0.0

        comp = {
            "pnl": _saturating(max(0.0, total_pnl), self.sat["pnl_usd"]),
            "win_rate": max(0.0, min(1.0, (win_rate - 0.4) / 0.4)),  # 40%->0, 80%->1
            "lump_size": _saturating(p90, self.sat["lump_usd"]),
            "volume": _saturating(volume, self.sat["volume_usd"]),
            "breadth": min(1.0, len(markets) / max(1, self.sat["breadth_markets"])),
            "recency": max(0.0, 1.0 - days_idle / max(1.0, self.sat["recency_days"])),
            "group_signal": min(1.0, mentions / max(1, self.sat["group_mentions"])),
            "feed_quality": feed_quality,
        }
        card.components = {k: round(v, 4) for k, v in comp.items()}

        wsum = sum(self.w.values()) or 1.0
        card.score = round(100.0 * sum(comp[k] * self.w.get(k, 0.0) for k in comp) / wsum, 2)

        # ---- hard gates -------------------------------------------------
        g = self.gates
        fails: list[str] = []
        if total_pnl < g["min_total_pnl_usd"]:
            fails.append(f"pnl ${total_pnl:,.0f} < ${g['min_total_pnl_usd']:,.0f}")
        if counted < g["min_positions"]:
            fails.append(f"positions {counted} < {g['min_positions']}")
        if volume < g["min_volume_usd"]:
            fails.append(f"volume ${volume:,.0f} < ${g['min_volume_usd']:,.0f}")
        if win_rate < g["min_win_rate"]:
            fails.append(f"win rate {win_rate:.0%} < {g['min_win_rate']:.0%}")
        if days_idle > g["max_days_since_last_trade"]:
            fails.append(f"idle {days_idle:.0f}d > {g['max_days_since_last_trade']}d")
        if card.score < g["min_score"]:
            fails.append(f"score {card.score} < {g['min_score']}")

        if fails:
            card.verdict, card.reason = "eliminated", "; ".join(fails)
        else:
            card.verdict, card.reason = "qualified", f"score {card.score} — passed all gates"
        return card


# =============================================================================
# 6.  THE BRIDGE
# =============================================================================

class Bridge:
    def __init__(self, cfg: Config, db_path: Path = DB_PATH) -> None:
        self.cfg = cfg
        self.store = Store(db_path)
        self.translator = Translator(cfg["translate"])
        self.extractor = Extractor()
        self.tier1 = Tier1(cfg["scoring"]["tier1"])
        self.scorer = Scorer(cfg["scoring"])

    def close(self) -> None:
        self.store.close()

    # -- ingest -----------------------------------------------------------

    def ingest(self, topic: str, kind: str, msg_id: int, ts: float,
               text: str, urls: Optional[list[str]] = None) -> list[Candidate]:
        """Translate + extract + persist a single message."""
        text = (text or "").strip()
        if not text:
            return []
        # Entity URLs carry the only full-length addresses in the leaderboard
        # topic, so they must be visible to the extractor.
        augmented = text if not urls else text + "\n" + "\n".join(urls)

        cands = self.extractor.extract(augmented, kind)
        for c in cands:
            c.topic, c.msg_id = topic, (c.msg_id or msg_id)
            c.ts = c.ts or ts
            self.store.record(c)
        text_en = self.translator.translate(text)
        self.store.save_message(topic, msg_id, ts, text, text_en, parsed=bool(cands))
        return cands

    # -- score ------------------------------------------------------------

    def run_tier1(self) -> tuple[int, int]:
        passed = failed = 0
        for row in self.store.candidates(tier1="pending"):
            ok, reason = self.tier1.check(row)
            self.store.set_tier1(row["wallet_key"], ok, reason)
            passed += ok
            failed += (not ok)
        self.store.commit()
        return passed, failed

    async def score_tier2(self, hr: HomerunClient, *, limit: int = 0,
                          force: bool = False) -> int:
        """limit=0 means every Tier-1 survivor.

        Candidates come back ordered by published profit, so a --limit is
        "the best N by what PA Beacon says about them" rather than an
        arbitrary slice — which is what you want when spot-checking
        thresholds before committing to a multi-hour full pass.
        """
        rows = self.store.candidates(
            tier1="pass", verdict=None if force else "pending", limit=limit,
        )
        done = 0
        for i, row in enumerate(rows, 1):
            ident = row["address"] or row["profile"]
            if not ident:
                self.store.set_score(
                    row["wallet_key"], None, {}, "unresolvable",
                    "only an elided address was ever published — "
                    "no full address and no profile name to resolve",
                )
                continue

            log.info("[%d/%d] scoring %s", i, len(rows), ident)
            try:
                positions, trades = await asyncio.gather(
                    hr.positions(ident), hr.trades(ident, limit=300)
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("  data fetch failed: %s", exc)
                self.store.set_score(row["wallet_key"], None, {}, "error", str(exc)[:300])
                continue

            if not positions and not trades:
                self.store.set_score(
                    row["wallet_key"], 0.0, {}, "eliminated",
                    "Polymarket returned no positions or trades for this identifier",
                )
                continue

            card = self.scorer.evaluate(positions, trades, row)
            self.store.set_score(
                row["wallet_key"], card.score,
                {"components": card.components, "stats": card.stats},
                card.verdict, card.reason,
            )
            done += 1
            log.info("  -> %-10s score=%5.1f  %s", card.verdict.upper(),
                     card.score, card.reason)
            await asyncio.sleep(0.25)  # be polite to the shared rate limiter
        return done

    def apply_top_percent(self) -> int:
        """Relative cut: keep only the best N% of qualified wallets.

        Runs after the absolute gates, so this narrows an already-clean
        list rather than rescuing anything. Demoted wallets become
        'eliminated' with the cutoff recorded, and are restored if a later
        rescore moves them back above it.
        """
        pct = self.cfg["scoring"].get("top_percent")
        if not pct:
            return 0
        floor = int(self.cfg["scoring"].get("top_percent_floor") or 0)

        # Reconsider previously-demoted wallets so the cut can move both ways.
        self.store.conn.execute(
            "UPDATE candidates SET verdict='qualified' "
            "WHERE verdict='eliminated' AND reason LIKE 'outside top %' "
            "AND injected_at IS NULL"
        )
        rows = [r for r in self.store.candidates(verdict="qualified")
                if r["score"] is not None]
        if not rows:
            self.store.commit()
            return 0

        keep = max(floor, math.ceil(len(rows) * float(pct) / 100.0))
        if keep >= len(rows):
            self.store.commit()
            return 0

        cutoff = rows[keep - 1]["score"]  # candidates() sorts by score desc
        demoted = 0
        for row in rows[keep:]:
            if row["injected_at"]:
                continue  # already in homerun; removing it is not our call
            self.store.conn.execute(
                "UPDATE candidates SET verdict='eliminated', reason=? WHERE wallet_key=?",
                (f"outside top {pct}% (score {row['score']} < cutoff {cutoff})",
                 row["wallet_key"]),
            )
            demoted += 1
        self.store.commit()
        log.info("top-%s%% cut: kept %d of %d qualified (cutoff score %.1f)",
                 pct, keep, len(rows), cutoff)
        return demoted

    # -- inject -----------------------------------------------------------

    async def inject_qualified(self, hr: HomerunClient, *, dry_run: bool = False) -> int:
        hcfg = self.cfg["homerun"]
        try:
            existing = await hr.tracked_wallets()
        except Exception as exc:  # noqa: BLE001
            log.error("cannot read homerun's existing tracked wallets: %s", exc)
            return 0

        already: set[str] = set()
        for w in existing:
            for k in ("address", "wallet", "wallet_address", "proxyWallet",
                      "label", "username", "name"):
                v = w.get(k)
                if isinstance(v, str) and v:
                    already.add(v.lower())

        injected = 0
        for row in self.store.candidates(verdict="qualified"):
            if row["injected_at"]:
                continue
            addr = (row["address"] or "").lower()
            prof = row["profile"] or ""
            if (addr and addr in already) or (prof and prof.lower() in already):
                log.info("skip %s — homerun already tracks it", addr or prof)
                self.store.mark_injected(row["wallet_key"], addr or prof)
                continue

            # "profile name or wallet id, whichever is shown in full"
            identifier = (addr or prof) if hcfg["prefer"] == "address" else (prof or addr)
            if not identifier:
                continue

            suffix = f"-{int(row['score'])}" if row["score"] is not None else ""
            label = hcfg["label_template"].format(suffix=suffix)

            if dry_run:
                log.info("DRY RUN would inject %-44s label=%s score=%s",
                         identifier, label, row["score"])
                injected += 1
                continue

            try:
                resp = await hr.add_wallet(identifier, label)
                self.store.mark_injected(row["wallet_key"], identifier)
                injected += 1
                log.info("INJECTED %s -> %s", identifier, resp.get("address", "?"))
            except Exception as exc:  # noqa: BLE001
                log.error("inject failed for %s: %s", identifier, exc)
            await asyncio.sleep(0.4)
        return injected


# =============================================================================
# 7a. TELEGRAM DESKTOP EXPORT IMPORTER
# =============================================================================

def detect_kind(text: str) -> str:
    """Guess which PA Beacon template a message uses, from its content.

    Used when the topic is unknown — the desktop export doesn't always
    carry forum-topic structure, and a message's own header is a more
    reliable signal than its container anyway.
    """
    if "聪明钱交易提醒" in text:
        return "smart_money"
    if "大额成交" in text:
        return "large_buy"
    if re.search(r"(稳健盈利榜|高胜率榜|大额榜|活跃榜)", text) and re.search(r"盈利\s*[\-+]", text):
        return "leaderboard"
    if "世界杯" in text or re.search(r"账户\s*[:：]", text):
        return "worldcup"
    return "generic"


def _flatten_export_text(node: Any) -> tuple[str, list[str]]:
    """Telegram's export stores text as a string OR a list of runs.

    Returns (plain text, hrefs). The hrefs matter more than the text: PA
    Beacon's "Polymarket 链接" is a text_link whose visible label contains
    no address at all, and that link is the only place the leaderboard's
    full addresses appear.
    """
    if isinstance(node, str):
        return node, []
    parts: list[str] = []
    urls: list[str] = []
    for item in node or []:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            txt = str(item.get("text") or "")
            parts.append(txt)
            href = item.get("href")
            if href:
                urls.append(str(href))
            elif item.get("type") in {"link", "url"} and txt.startswith("http"):
                urls.append(txt)
    return "".join(parts), urls


class ExportImporter:
    """Ingests a Telegram Desktop `result.json` export.

    Settings -> Advanced -> Export Telegram data -> JSON. No API calls, no
    rate limits, no partial sweep: the complete history in one pass, and no
    Telegram session required.
    """

    def __init__(self, cfg: Config, bridge: Bridge) -> None:
        self.cfg = cfg
        self.bridge = bridge
        # title -> kind, from the configured topic list
        self.kind_by_title = {
            (t.get("title") or "").strip(): t.get("kind", "generic")
            for t in cfg["telegram"]["topics"]
        }
        self.enabled_titles = {
            (t.get("title") or "").strip()
            for t in cfg["telegram"]["topics"] if t.get("enabled")
        }

    @staticmethod
    def _find_exports(path: Path) -> list[Path]:
        if path.is_file():
            return [path]
        found = sorted(path.rglob("result.json"))
        if not found:
            found = sorted(path.glob("*.json"))
        return found

    # A forum's "General" topic has no topic_created record and its
    # messages carry no reply_to_message_id. Its id is always 1.
    GENERAL_TOPIC_ID = 1

    def _topic_map(self, messages: list[dict]) -> dict[int, str]:
        """topic id -> title, from the forum's service messages.

        Two shapes, and they disagree about where the title lives:
          topic_created -> the topic's id IS the message id, title in `title`
          topic_edit    -> the topic is what the message replies to (or
                           General when it replies to nothing), and the new
                           name is in `new_title`
        PA Beacon renamed its General topic to 讨论组 that second way, so
        reading only `title` loses it entirely.
        """
        topics: dict[int, str] = {}
        for m in messages:
            action = m.get("action")
            if action == "topic_created":
                title, mid = m.get("title"), m.get("id")
                if title and mid is not None:
                    topics[int(mid)] = str(title).strip()
            elif action == "topic_edit":
                title = m.get("new_title") or m.get("title")
                tid = m.get("reply_to_message_id") or self.GENERAL_TOPIC_ID
                if title:
                    topics[int(tid)] = str(title).strip()
        return topics

    def ingest_file(self, path: Path, *, all_topics: bool = False,
                    limit: int = 0) -> tuple[int, int, int]:
        """-> (messages read, messages ingested, candidate rows)"""
        log.info("reading %s", path)
        data = json.loads(path.read_text(encoding="utf-8-sig"))

        messages = data.get("messages")
        if messages is None and isinstance(data.get("chats"), dict):
            # Full-account export: {"chats": {"list": [ {name, messages}, ...]}}
            read = ingested = hits = 0
            for chat in data["chats"].get("list", []):
                name = str(chat.get("name") or "")
                if not all_topics and name and self.enabled_titles and \
                        name not in self.enabled_titles and \
                        str(self.cfg["telegram"]["chat"]) not in {str(chat.get("id")), name}:
                    continue
                r, i, h = self._ingest_messages(
                    chat.get("messages") or [], fallback_topic=name,
                    all_topics=all_topics, limit=limit,
                )
                read, ingested, hits = read + r, ingested + i, hits + h
            return read, ingested, hits

        if messages is None:
            raise SystemExit(
                f"{path} has no `messages` array — is this a Telegram JSON export? "
                "(Settings -> Advanced -> Export Telegram data, format JSON)"
            )
        return self._ingest_messages(
            messages, fallback_topic=str(data.get("name") or path.parent.name),
            all_topics=all_topics, limit=limit,
        )

    def _ingest_messages(self, messages: list[dict], *, fallback_topic: str,
                         all_topics: bool, limit: int) -> tuple[int, int, int]:
        topic_titles = self._topic_map(messages)
        read = ingested = hits = 0

        for m in messages:
            if limit and ingested >= limit:
                break
            if m.get("type") == "service":
                continue
            read += 1

            raw = m.get("text_entities")
            if raw is None:
                raw = m.get("text")
            text, urls = _flatten_export_text(raw)
            text = (text or "").strip()
            if not text:
                continue

            # Topic: prefer the forum thread this message belongs to. No
            # reply_to_message_id means the General topic (id 1), not
            # "unknown" — that's where PA Beacon's 讨论组 chatter lives.
            # A reply_to pointing at an ordinary message (a genuine reply,
            # not a thread root) misses the map and falls through to the
            # chat name, where detect_kind() sorts it out from the header.
            tid = m.get("reply_to_message_id") or self.GENERAL_TOPIC_ID
            topic = topic_titles.get(int(tid)) or fallback_topic

            if not all_topics and self.enabled_titles and topic in self.kind_by_title \
                    and topic not in self.enabled_titles:
                continue

            kind = self.kind_by_title.get(topic) or detect_kind(text)
            if kind == "ignore" and not all_topics:
                continue
            if kind == "generic":
                kind = detect_kind(text)

            mid = int(m.get("id") or 0)
            if not mid or self.bridge.store.have_message(topic, mid):
                continue

            ts = m.get("date_unixtime")
            if ts is not None:
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    ts = None
            if ts is None:
                try:
                    from datetime import datetime
                    ts = datetime.fromisoformat(
                        str(m.get("date"))).replace(tzinfo=timezone.utc).timestamp()
                except Exception:  # noqa: BLE001
                    ts = time.time()

            cands = self.bridge.ingest(topic, kind, mid, ts, text, urls)
            ingested += 1
            hits += len(cands)
            if ingested % 500 == 0:
                self.bridge.store.commit()
                log.info("  ...%d ingested, %d candidate rows", ingested, hits)

        self.bridge.store.commit()
        return read, ingested, hits

    def run(self, path: Path, *, all_topics: bool = False,
            limit: int = 0) -> tuple[int, int, int]:
        files = self._find_exports(path)
        if not files:
            raise SystemExit(f"no result.json found under {path}")
        totals = [0, 0, 0]
        for f in files:
            r, i, h = self.ingest_file(f, all_topics=all_topics, limit=limit)
            log.info("  %s: %d read, %d ingested, %d candidate rows", f.name, r, i, h)
            totals = [totals[0] + r, totals[1] + i, totals[2] + h]
        merged = self.bridge.store.backfill_truncated()
        if merged:
            log.info("reconciled %d elided address(es) to full addresses", merged)
        return tuple(totals)  # type: ignore[return-value]


# =============================================================================
# 7.  TELEGRAM SOURCE
# =============================================================================

def harvest_urls(msg) -> list[str]:  # noqa: ANN001
    """Pull every URL out of a message's entities.

    PA Beacon renders "Polymarket 链接" and "市场链接" as hyperlinks whose
    visible text contains no address at all. Without this the leaderboard
    topic yields only elided addresses.
    """
    urls: list[str] = []
    try:
        for ent, text in (msg.get_entities_text() or []):
            url = getattr(ent, "url", None)
            if url:
                urls.append(url)
            elif type(ent).__name__ == "MessageEntityUrl" and text:
                urls.append(text)
    except Exception:  # noqa: BLE001 — entity parsing must never break ingest
        pass
    if getattr(msg, "web_preview", None) is not None:
        wp_url = getattr(getattr(msg, "web_preview", None), "url", None)
        if wp_url:
            urls.append(wp_url)
    return urls


class TelegramSource:
    """Telethon MTProto client — reads forum history AND live messages.

    A *user* session, not a bot:
      - bots cannot read group history, and "all past transactions" is the
        explicit requirement;
      - PA Beacon is read-only ("Sending messages is not allowed in this
        group"), so a bot could not be added even with admin rights.
    """

    def __init__(self, cfg: Config, bridge: Bridge) -> None:
        self.cfg = cfg
        self.bridge = bridge
        self.api_id = _env_int("TELEGRAM_API_ID", 0)
        self.api_hash = os.environ.get("TELEGRAM_API_HASH", "")
        self.client = None
        self.entity = None

    def _require_creds(self) -> None:
        if not self.api_id or not self.api_hash:
            raise SystemExit(
                "Missing TELEGRAM_API_ID / TELEGRAM_API_HASH.\n"
                "Get them at https://my.telegram.org -> API development tools,\n"
                "then create a .env file next to this script:\n"
                "    TELEGRAM_API_ID=1234567\n"
                "    TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789\n"
            )

    async def connect(self):
        from telethon import TelegramClient  # type: ignore

        self._require_creds()
        self.client = TelegramClient(str(SESSION_PATH), self.api_id, self.api_hash)
        await self.client.start()
        me = await self.client.get_me()
        log.info("Telegram connected as %s (id=%s)", getattr(me, "username", "?"), me.id)
        return self.client

    async def resolve_chat(self):
        target = self.cfg["telegram"]["chat"]
        t = str(target).strip()
        try:
            self.entity = await self.client.get_entity(
                int(t) if re.fullmatch(r"-?\d+", t) else t
            )
        except Exception:  # noqa: BLE001 — fall back to a title scan
            for d in await self.client.get_dialogs(limit=500):
                if (d.name or "").strip().lower() == t.lower():
                    self.entity = d.entity
                    break
        if self.entity is None:
            raise SystemExit(
                f"Could not resolve chat {target!r}. Run `list-chats` to see what "
                "this account can reach. For a t.me/c/<N>/... link the id is -100<N>."
            )
        log.info("chat: %s (id=%s)",
                 getattr(self.entity, "title", "?"), self.entity.id)
        return self.entity

    async def fetch_topics(self) -> list[tuple[int, str]]:
        """(topic_id, title) for a forum; [] for a plain group."""
        from telethon.tl.functions.channels import GetForumTopicsRequest  # type: ignore

        try:
            res = await self.client(GetForumTopicsRequest(
                channel=self.entity, offset_date=0, offset_id=0,
                offset_topic=0, limit=100,
            ))
            return [(t.id, getattr(t, "title", f"topic {t.id}"))
                    for t in res.topics if hasattr(t, "id")]
        except Exception as exc:  # noqa: BLE001
            log.warning("not a forum, or topics unavailable: %s", exc)
            return []

    async def _match_topics(self) -> list[dict[str, Any]]:
        """Bind configured topics to live ids, matching by id then by title."""
        live = await self.fetch_topics()
        by_id = {tid: title for tid, title in live}
        by_title = {title.strip(): tid for tid, title in live}

        bound: list[dict[str, Any]] = []
        for spec in self.cfg["telegram"]["topics"]:
            if not spec.get("enabled"):
                continue
            tid, title = spec.get("id"), (spec.get("title") or "").strip()
            if tid is not None and tid in by_id:
                title = by_id[tid]
            elif title in by_title:
                tid = by_title[title]
            elif live:
                log.warning(
                    "topic %r not found in the forum (available: %s) — skipping",
                    title or tid, ", ".join(t for _, t in live) or "none",
                )
                continue
            bound.append({**spec, "id": tid, "title": title or str(tid)})
        return bound

    # ---- commands -------------------------------------------------------

    async def list_chats(self) -> None:
        await self.connect()
        print(f"\n{'ID':>18}  {'TYPE':<9} TITLE")
        print("-" * 78)
        for d in await self.client.get_dialogs(limit=500):
            kind = "channel" if d.is_channel else ("group" if d.is_group else "user")
            print(f"{d.id:>18}  {kind:<9} {d.name}")
        print()
        await self.client.disconnect()

    async def list_topics(self) -> None:
        await self.connect()
        await self.resolve_chat()
        topics = await self.fetch_topics()
        if not topics:
            print("\nThis chat has no forum topics.\n")
        else:
            print(f"\n{'TOPIC ID':>9}  TITLE")
            print("-" * 60)
            for tid, title in topics:
                print(f"{tid:>9}  {title}")
            print("\nPaste these ids into bridge_config.json -> telegram.topics[].id\n")
        await self.client.disconnect()

    async def backfill(self, override_limit: int = 0) -> int:
        await self.connect()
        await self.resolve_chat()
        topics = await self._match_topics()
        if not topics:
            raise SystemExit("No enabled topics resolved — nothing to backfill.")

        total = 0
        for spec in topics:
            title, kind = spec["title"], spec["kind"]
            limit = override_limit or int(spec.get("backfill_limit") or 1000)
            log.info("backfilling %r (kind=%s, limit=%d)", title, kind, limit)

            kwargs: dict[str, Any] = {"limit": limit}
            if spec["id"] is not None:
                kwargs["reply_to"] = spec["id"]

            n = hits = 0
            try:
                async for msg in self.client.iter_messages(self.entity, **kwargs):
                    text = msg.message or ""
                    if not text:
                        continue
                    if self.bridge.store.have_message(title, msg.id):
                        continue
                    ts = (msg.date.replace(tzinfo=timezone.utc).timestamp()
                          if msg.date else time.time())
                    cands = self.bridge.ingest(
                        title, kind, msg.id, ts, text, harvest_urls(msg)
                    )
                    n += 1
                    hits += len(cands)
                    if n % 250 == 0:
                        self.bridge.store.commit()
                        log.info("  ...%d scanned, %d candidate rows", n, hits)
            except Exception as exc:  # noqa: BLE001 — one topic must not kill the sweep
                log.error("backfill of %r stopped early: %s", title, exc)
            self.bridge.store.commit()
            log.info("  %r done: %d new messages, %d candidate rows", title, n, hits)
            total += hits

        merged = self.bridge.store.backfill_truncated()
        if merged:
            log.info("reconciled %d elided address(es) to full addresses", merged)
        await self.client.disconnect()
        return total

    async def run_live(self, on_batch) -> None:
        from telethon import events  # type: ignore

        await self.connect()
        await self.resolve_chat()
        topics = await self._match_topics()
        by_id = {t["id"]: t for t in topics if t["id"] is not None}
        default = topics[0] if topics else {"title": "unknown", "kind": "generic"}
        log.info("live topics: %s", ", ".join(t["title"] for t in topics) or "(all)")

        @self.client.on(events.NewMessage(chats=self.entity))
        async def _handler(event):  # noqa: ANN001
            text = event.message.message or ""
            if not text:
                return
            rt = getattr(event.message, "reply_to", None)
            tid = (getattr(rt, "reply_to_top_id", None)
                   or getattr(rt, "reply_to_msg_id", None)) if rt else None
            spec = by_id.get(tid)
            if spec is None:
                if by_id:
                    return  # a topic we deliberately don't watch
                spec = default
            ts = (event.message.date.replace(tzinfo=timezone.utc).timestamp()
                  if event.message.date else time.time())
            try:
                cands = self.bridge.ingest(
                    spec["title"], spec["kind"], event.message.id, ts,
                    text, harvest_urls(event.message),
                )
                self.bridge.store.backfill_truncated()
                self.bridge.store.commit()
                if cands:
                    log.info("LIVE [%s] %d candidate(s): %s", spec["title"], len(cands),
                             ", ".join(c.identifier or c.wallet_trunc or "?" for c in cands))
                    await on_batch(cands)
                else:
                    log.debug("LIVE [%s] no extraction: %.90s", spec["title"], text)
            except Exception as exc:  # noqa: BLE001 — never kill the listener
                log.exception("handler error: %s", exc)

        log.info("live listener running — Ctrl-C to stop")
        await self.client.run_until_disconnected()


# =============================================================================
# 8.  CLI
# =============================================================================

def _money(v: Any) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "-"


def print_candidates(store: Store, verdict: Optional[str] = None,
                     limit: int = 0) -> None:
    rows = store.candidates(verdict=verdict, limit=limit)
    if not rows:
        print("(no candidates)")
        return
    print(f"\n{'SCORE':>6} {'VERDICT':<13}{'T1':<5}{'MENT':>5}  IDENTIFIER")
    print("-" * 104)
    for r in rows:
        ident = (r["address"] or (f"@{r['profile']}" if r["profile"] else None)
                 or r["trunc_key"] or "?")
        if r["profile"] and r["address"]:
            ident = f"{r['address']}  ({r['profile']})"
        score = f"{r['score']:.1f}" if r["score"] is not None else "  -"
        tag = " [INJECTED]" if r["injected_at"] else ""
        print(f"{score:>6} {r['verdict']:<13}{(r['tier1'] or '-'):<5}"
              f"{r['mentions']:>5}  {ident}{tag}")
        if r["reason"]:
            print(f"       {r['reason']}")
        feed_bits = []
        if r["feed_board"]:
            rank = r["feed_best_rank"]
            feed_bits.append(r["feed_board"] + (f"#{rank}" if rank not in (None, 9999) else ""))
        if r["feed_profit"] is not None:
            feed_bits.append(f"feed pnl {_money(r['feed_profit'])}")
        if r["feed_profit_15d"] is not None:
            feed_bits.append(f"15d pnl {_money(r['feed_profit_15d'])}")
        if r["feed_win_rate"] is not None:
            feed_bits.append(f"feed win {r['feed_win_rate']:.1f}%")
        if r["max_reported"]:
            feed_bits.append(f"max trade {_money(r['max_reported'])}")
        if feed_bits:
            print("       feed: " + " | ".join(feed_bits))
        if r["score_detail"]:
            try:
                st = json.loads(r["score_detail"]).get("stats", {})
                if st:
                    print(f"       chain: pnl={_money(st.get('total_pnl_usd'))}"
                          f" vol={_money(st.get('volume_usd'))}"
                          f" win={st.get('win_rate', 0):.0%}"
                          f" p90={_money(st.get('p90_position_usd'))}"
                          f" mkts={st.get('distinct_markets')}"
                          f" idle={st.get('days_since_last_trade')}d")
            except Exception:  # noqa: BLE001
                pass
    print(f"\n{len(rows)} candidate(s)\n")


async def cmd_login(cfg: Config, args) -> None:
    bridge = Bridge(cfg)
    src = TelegramSource(cfg, bridge)
    await src.connect()
    await src.client.disconnect()
    print(f"\nSession saved to {SESSION_PATH}.session — you won't be asked again.\n")
    bridge.close()


async def cmd_list_chats(cfg: Config, args) -> None:
    bridge = Bridge(cfg)
    await TelegramSource(cfg, bridge).list_chats()
    bridge.close()


async def cmd_list_topics(cfg: Config, args) -> None:
    bridge = Bridge(cfg)
    await TelegramSource(cfg, bridge).list_topics()
    bridge.close()


async def cmd_test_parse(cfg: Config, args) -> None:
    """Parse text from a file or stdin. No Telegram, no network, no DB writes.

    Blocks are separated by a line containing only `---`. Prefix a block with
    `#kind: leaderboard` to force a parser; otherwise it is auto-detected.
    """
    bridge = Bridge(cfg, db_path=Path(":memory:"))  # scratch DB, never written
    raw = (Path(args.file).read_text(encoding="utf-8-sig") if args.file
           else sys.stdin.read())
    blocks = [b.strip() for b in re.split(r"^---+$", raw, flags=re.MULTILINE) if b.strip()]

    hits = 0
    for i, block in enumerate(blocks, 1):
        kind = args.kind or "generic"
        m = re.match(r"#kind:\s*(\w+)\s*\n", block)
        if m:
            kind, block = m.group(1), block[m.end():]
        elif not args.kind:
            kind = detect_kind(block)

        en = bridge.translator.translate(block)
        cands = bridge.extractor.extract(block, kind)
        print("=" * 96)
        print(f"MESSAGE {i}   kind={kind}")
        print(f"  ZH: {block[:220].replace(chr(10), ' / ')}")
        print(f"  EN: {en[:220].replace(chr(10), ' / ')}")
        if not cands:
            print("  >> NO EXTRACTION")
            continue
        hits += 1
        for c in cands:
            f = c.feed
            print(f"  >> addr={c.wallet or '-'}")
            print(f"     trunc={c.wallet_trunc or '-'}  profile={c.profile or '-'}")
            print(f"     side={c.side or '-'}  amount={_money(c.amount_usd)}"
                  f"  outcome={c.outcome or '-'}")
            print(f"     market={(c.market or '-')[:70]}")
            if not f.is_empty():
                print(f"     feed: profit={_money(f.profit_usd)} 15d={_money(f.profit_15d_usd)}"
                      f" roi={f.roi_pct} win={f.win_rate_pct}%"
                      f" avgpos={_money(f.avg_position_usd)}"
                      f" board={f.board or '-'}#{f.board_rank or '-'}")
            print(f"     -> homerun identifier: {c.identifier!r}"
                  + ("" if c.identifier else "   << UNRESOLVABLE (elided address only)"))
    print("=" * 96)
    print(f"\n{hits}/{len(blocks)} blocks produced an extraction.\n")
    bridge.close()


async def cmd_dump_unparsed(cfg: Config, args) -> None:
    store = Store()
    rows = store.unparsed_messages(limit=args.limit or 30)
    print(f"\n{len(rows)} stored message(s) produced no extraction:\n")
    for r in rows:
        print("-" * 90)
        print(f"[{r['topic']}] #{r['msg_id']}")
        print(r["text_zh"][:600])
    store.close()


async def cmd_backfill(cfg: Config, args) -> None:
    bridge = Bridge(cfg)
    await TelegramSource(cfg, bridge).backfill(args.limit)
    p, f = bridge.run_tier1()
    log.info("tier-1 prefilter: %d passed, %d eliminated", p, f)
    for row in bridge.store.message_counts():
        log.info("  %s: %d messages, %s parsed", row["topic"], row["n"], row["parsed"])
    print_candidates(bridge.store, limit=args.limit or 40)
    bridge.close()


async def cmd_import_export(cfg: Config, args) -> None:
    """Ingest a Telegram Desktop JSON export — no API, no rate limits."""
    if not args.file:
        raise SystemExit(
            "usage: import-export --file <path to result.json or its folder>\n"
            "Produce it with Telegram Desktop: Settings -> Advanced ->\n"
            "Export Telegram data -> select PA Beacon -> format JSON."
        )
    bridge = Bridge(cfg)
    read, ingested, hits = ExportImporter(cfg, bridge).run(
        Path(args.file), all_topics=args.all_topics, limit=args.limit
    )
    log.info("import complete: %d read, %d ingested, %d candidate rows",
             read, ingested, hits)
    p, f = bridge.run_tier1()
    log.info("tier-1 prefilter: %d passed, %d eliminated", p, f)
    for row in bridge.store.message_counts():
        log.info("  %s: %d messages, %s parsed", row["topic"], row["n"], row["parsed"])
    print_candidates(bridge.store, limit=40)
    bridge.close()


async def cmd_score(cfg: Config, args) -> None:
    bridge = Bridge(cfg)
    bridge.store.backfill_truncated()
    p, f = bridge.run_tier1()
    log.info("tier-1 prefilter: %d passed, %d eliminated", p, f)
    async with HomerunClient(cfg["homerun"]) as hr:
        if not await hr.health():
            raise SystemExit(
                f"homerun backend unreachable at {cfg['homerun']['base_url']}.\n"
                "Is it running? Set HOMERUN_BASE_URL in .env if it's on another machine."
            )
        n = await bridge.score_tier2(hr, limit=args.limit, force=args.force)
    log.info("tier-2 scored %d wallet(s)", n)
    bridge.apply_top_percent()
    print_candidates(bridge.store, limit=args.limit or 40)
    bridge.close()


async def cmd_inject(cfg: Config, args) -> None:
    bridge = Bridge(cfg)
    async with HomerunClient(cfg["homerun"]) as hr:
        if not await hr.health():
            raise SystemExit(f"homerun backend unreachable at {cfg['homerun']['base_url']}")
        n = await bridge.inject_qualified(hr, dry_run=args.dry_run)
    log.info("%s %d wallet(s)", "would inject" if args.dry_run else "injected", n)
    bridge.close()


async def cmd_review(cfg: Config, args) -> None:
    store = Store()
    print_candidates(store, verdict=args.verdict, limit=args.limit)
    store.close()


async def cmd_run(cfg: Config, args) -> None:
    """Full daemon: backfill once, then live + periodic score/inject."""
    bridge = Bridge(cfg)
    hcfg = cfg["homerun"]
    auto = bool(hcfg["auto_inject"])
    log.info("auto_inject = %s", auto)

    src = TelegramSource(cfg, bridge)
    if not args.no_backfill:
        await src.backfill()

    lock = asyncio.Lock()

    async def cycle(_batch=None) -> None:
        if lock.locked():
            return  # a cycle is in flight; this batch rides the next one
        async with lock:
            try:
                bridge.store.backfill_truncated()
                p, f = bridge.run_tier1()
                if p or f:
                    log.info("tier-1: %d passed, %d eliminated", p, f)
                async with HomerunClient(hcfg) as hr:
                    if not await hr.health():
                        log.error("homerun unreachable — retrying next cycle")
                        return
                    # The daemon caps each cycle so a burst of new wallets
                    # can't monopolise homerun's rate limiter; the backlog
                    # drains over subsequent cycles.
                    await bridge.score_tier2(
                        hr, limit=int(hcfg["max_scores_per_cycle"])
                    )
                    bridge.apply_top_percent()
                    if auto:
                        await bridge.inject_qualified(hr)
                    else:
                        waiting = [r for r in bridge.store.candidates(verdict="qualified")
                                   if not r["injected_at"]]
                        if waiting:
                            log.info("%d qualified wallet(s) awaiting review — "
                                     "run `inject`, or set AUTO_INJECT=true", len(waiting))
            except Exception as exc:  # noqa: BLE001 — the daemon must not die
                log.exception("evaluation cycle failed: %s", exc)

    async def periodic() -> None:
        interval = float(hcfg["rescore_interval_hours"]) * 3600.0
        while True:
            await asyncio.sleep(interval)
            log.info("periodic rescore")
            await cycle()

    await cycle()
    asyncio.create_task(periodic())
    await src.run_live(cycle)
    bridge.close()


COMMANDS = {
    "login": cmd_login,
    "list-chats": cmd_list_chats,
    "list-topics": cmd_list_topics,
    "import-export": cmd_import_export,
    "test-parse": cmd_test_parse,
    "dump-unparsed": cmd_dump_unparsed,
    "backfill": cmd_backfill,
    "score": cmd_score,
    "rescore": cmd_score,
    "inject": cmd_inject,
    "review": cmd_review,
    "run": cmd_run,
}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="PA Beacon (Telegram) -> homerun smart-money wallet bridge",
    )
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--file", help="test-parse: sample file (blocks split on a --- line); "
                                   "import-export: result.json or the folder holding it")
    ap.add_argument("--all-topics", action="store_true",
                    help="import-export: ingest every topic, not just the enabled ones")
    ap.add_argument("--kind", help="test-parse: force a parser "
                                   "(smart_money|large_buy|worldcup|leaderboard|generic)")
    ap.add_argument("--limit", type=int, default=0, help="max items for this command")
    ap.add_argument("--force", action="store_true", help="score: re-score everything")
    ap.add_argument("--dry-run", action="store_true", help="inject: print, don't POST")
    ap.add_argument("--no-backfill", action="store_true", help="run: skip the history sweep")
    ap.add_argument("--verdict", help="review: pending|qualified|eliminated|unresolvable|error")
    args = ap.parse_args()

    cfg = Config.load()
    logging.basicConfig(
        level=getattr(logging, str(cfg["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("telethon", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        asyncio.run(COMMANDS[args.command](cfg, args))
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
