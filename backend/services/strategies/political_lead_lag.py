# SEED TEMPLATE — not the live strategy. The DB `strategies.source_code` is the
# runtime master (loaded by the orchestrator + backtester); edits here only seed
# fresh installs / reset-to-factory. See services/opportunity_strategy_catalog.py.
"""
Strategy: Political Lead-Lag (Polymarket whale -> Kalshi front-run)

Watches Polymarket whale-wallet activity and looks for the same event priced
on Kalshi. When a large Polymarket trade implies a probability shift that
Kalshi's own book hasn't caught up to yet, buys the same side on Kalshi
before that gap closes.

Thesis: Polymarket is on-chain and wallet-transparent, so a large directional
bet is visible the instant it lands. Kalshi runs a separate, independently
priced order book that reprices with a lag. If the gap between what
Polymarket now implies and what Kalshi still shows is wide enough — and the
signal is fresh enough that Kalshi genuinely hasn't caught up yet — front-
running that convergence on Kalshi is a repeatable edge.

Detection:
1. Subscribe to TRADER_ACTIVITY (whale-wallet signals), same firehose
   pipeline as traders_copy_trade / traders_confluence.
2. Gate: source trade notional >= min_source_notional_usd (default $10,000 —
   meaningfully large positions, not noise).
3. Gate: signal age <= max_signal_age_seconds (default 20s) — the whole
   thesis requires acting before Kalshi's book has repriced.
4. Match the Polymarket market's question text against Kalshi's live
   catalog (fuzzy Jaccard token match + outcome-compatibility guard, reusing
   the same matcher cross_platform.py uses for its own arb detection) to
   find the paired Kalshi market.
5. Gate: a same-direction price gap exists between what the whale trade
   implies and Kalshi's current price, above min_edge_percent after fees.
6. Gate: Kalshi-side liquidity/spread are actually tradeable.
7. Buy the same side on Kalshi.

Exit: hold for the convergence — take profit once Kalshi's price has closed
most of the gap toward the level the whale trade implied. Exit immediately
if the signal inverts (a fresh opposing whale trade, or Polymarket's own
price reversing hard against the original thesis) — the edge is dead the
moment the informed money reverses.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

from config import settings
from services.strategies.base import (
    BaseStrategy,
    DecisionCheck,
    ExitDecision,
    StrategyDecision,
)
from services.strategies.cross_platform import (
    _KalshiMarketCache,
    _jaccard_similarity,
    _tokenize,
)
from services.strategy_sdk import StrategySDK
from utils.converters import safe_float, to_confidence
from utils.kelly import kalshi_taker_fee
from utils.logger import get_logger
from utils.utcnow import utcnow

logger = get_logger(__name__)

# Same fuzzy-match bar cross_platform.py uses — keeps pairing behavior
# consistent between the two strategies rather than inventing a second
# threshold that could silently disagree with the arb scanner's own matches.
_MATCH_THRESHOLD = 0.34

# Hard ceiling on max_signal_age_seconds regardless of user config. This
# strategy's whole thesis is "Kalshi hasn't caught up yet" — beyond a
# minute, that's no longer a credible claim, so we never honor a looser
# window than this even if someone sets it in the UI.
_MAX_SIGNAL_AGE_HARD_CEILING_SECONDS = 60.0


POLITICAL_LEAD_LAG_DEFAULTS: dict[str, Any] = {
    # -- Signal gates --
    "min_source_notional_usd": 10_000.0,
    "max_signal_age_seconds": 20.0,
    "max_signal_age_seconds_hard_ceiling": _MAX_SIGNAL_AGE_HARD_CEILING_SECONDS,
    "min_confidence": 0.40,
    # -- Market pairing --
    "match_threshold": _MATCH_THRESHOLD,
    # -- Entry gates (Kalshi side) --
    "min_edge_percent": 3.0,
    "min_kalshi_liquidity_usd": 100.0,
    "max_kalshi_spread": 0.08,
    "max_entry_price": 0.95,
    "min_entry_price": 0.03,
    # -- Sizing --
    "min_order_size_usd": 5.0,
    "base_size_usd": 25.0,
    "max_size_usd": 250.0,
    "sizing_policy": "linear",  # "fixed" | "linear" | "adaptive"
    # -- Exit: hold for convergence, bail immediately on inversion --
    # No take-profit percent — the exit trigger is "Kalshi converged toward
    # what Polymarket implied", not a fixed price target.
    "convergence_close_fraction": 0.80,  # exit once 80% of the original gap has closed
    "inversion_stop_enabled": True,
    # How far price can move AGAINST the thesis (in raw probability terms)
    # before we call it inverted and close immediately, "asap" per spec.
    "inversion_move_threshold": 0.05,
    # Safety backstop only — this strategy is designed to hold until
    # convergence or inversion, not to time out. Generous default so a
    # stuck position still eventually surfaces for manual review.
    "max_hold_minutes": 4320.0,  # 3 days
}


def political_lead_lag_config_schema() -> dict[str, Any]:
    return {
        "param_fields": [
            {"key": "min_source_notional_usd", "label": "Min Whale Trade (USD)", "type": "number", "min": 0, "phase": "signal"},
            {"key": "max_signal_age_seconds", "label": "Max Signal Age (sec)", "type": "number", "min": 1, "max": _MAX_SIGNAL_AGE_HARD_CEILING_SECONDS, "phase": "signal"},
            {"key": "min_confidence", "label": "Min Confidence", "type": "number", "min": 0, "max": 1, "phase": "signal"},
            {"key": "match_threshold", "label": "Market Match Threshold", "type": "number", "min": 0.1, "max": 0.9, "phase": "signal"},
            {"key": "min_edge_percent", "label": "Min Edge (%)", "type": "number", "min": 0, "max": 100, "phase": "signal"},
            {"key": "min_kalshi_liquidity_usd", "label": "Min Kalshi Liquidity (USD)", "type": "number", "min": 0, "phase": "signal"},
            {"key": "max_kalshi_spread", "label": "Max Kalshi Spread", "type": "number", "min": 0, "max": 1, "phase": "signal"},
            {"key": "min_entry_price", "label": "Min Entry Price", "type": "number", "min": 0, "max": 1, "phase": "signal"},
            {"key": "max_entry_price", "label": "Max Entry Price", "type": "number", "min": 0, "max": 1, "phase": "signal"},
            {"key": "min_order_size_usd", "label": "Min Order Size (USD)", "type": "number", "min": 0, "phase": "execution"},
            {"key": "base_size_usd", "label": "Base Size (USD)", "type": "number", "min": 0, "phase": "execution"},
            {"key": "max_size_usd", "label": "Max Size (USD)", "type": "number", "min": 0, "phase": "execution"},
            {"key": "sizing_policy", "label": "Sizing Policy", "type": "enum", "options": ["fixed", "linear", "adaptive"], "phase": "execution"},
            {"key": "convergence_close_fraction", "label": "Convergence Take-Profit (fraction of gap closed)", "type": "number", "min": 0.1, "max": 1.0, "phase": "exit"},
            {"key": "inversion_stop_enabled", "label": "Inversion Stop Enabled", "type": "boolean", "phase": "exit"},
            {"key": "inversion_move_threshold", "label": "Inversion Move Threshold (prob.)", "type": "number", "min": 0.01, "max": 0.5, "phase": "exit"},
            {"key": "max_hold_minutes", "label": "Max Hold — safety backstop (min)", "type": "number", "min": 0, "phase": "exit"},
        ]
    }


def _to_utc(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _coerce_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(lo, min(hi, parsed))


def validate_political_lead_lag_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(POLITICAL_LEAD_LAG_DEFAULTS)
    if isinstance(raw, dict):
        cfg.update(raw)
    cfg["min_source_notional_usd"] = _coerce_float(cfg.get("min_source_notional_usd"), 10_000.0, 0.0, 10_000_000.0)
    cfg["max_signal_age_seconds"] = min(
        _MAX_SIGNAL_AGE_HARD_CEILING_SECONDS,
        _coerce_float(cfg.get("max_signal_age_seconds"), 20.0, 1.0, _MAX_SIGNAL_AGE_HARD_CEILING_SECONDS),
    )
    cfg["min_confidence"] = _coerce_float(cfg.get("min_confidence"), 0.40, 0.0, 1.0)
    cfg["match_threshold"] = _coerce_float(cfg.get("match_threshold"), _MATCH_THRESHOLD, 0.1, 0.9)
    cfg["min_edge_percent"] = _coerce_float(cfg.get("min_edge_percent"), 3.0, 0.0, 100.0)
    cfg["min_kalshi_liquidity_usd"] = _coerce_float(cfg.get("min_kalshi_liquidity_usd"), 100.0, 0.0, 10_000_000.0)
    cfg["max_kalshi_spread"] = _coerce_float(cfg.get("max_kalshi_spread"), 0.08, 0.0, 1.0)
    cfg["min_entry_price"] = _coerce_float(cfg.get("min_entry_price"), 0.03, 0.0, 1.0)
    cfg["max_entry_price"] = _coerce_float(cfg.get("max_entry_price"), 0.95, 0.0, 1.0)
    cfg["min_order_size_usd"] = _coerce_float(cfg.get("min_order_size_usd"), 5.0, 0.0, 1_000_000.0)
    cfg["base_size_usd"] = _coerce_float(cfg.get("base_size_usd"), 25.0, 0.0, 1_000_000.0)
    cfg["max_size_usd"] = _coerce_float(cfg.get("max_size_usd"), 250.0, 0.0, 1_000_000.0)
    cfg["convergence_close_fraction"] = _coerce_float(cfg.get("convergence_close_fraction"), 0.80, 0.1, 1.0)
    cfg["inversion_move_threshold"] = _coerce_float(cfg.get("inversion_move_threshold"), 0.05, 0.01, 0.5)
    cfg["max_hold_minutes"] = _coerce_float(cfg.get("max_hold_minutes"), 4320.0, 0.0, 100_000.0)
    return cfg


class PoliticalLeadLagStrategy(BaseStrategy):
    """Front-run Kalshi's reprice off fresh, large Polymarket whale flow."""

    strategy_type = "political_lead_lag"
    name = "Political Lead-Lag"
    description = (
        "Watches large Polymarket whale trades and buys the same side on the "
        "paired Kalshi market before Kalshi's own book catches up."
    )
    mispricing_type = "cross_market"
    source_key = "traders"
    allow_deduplication = False
    subscriptions = ["trader_activity"]

    DEFAULT_CONFIG = dict(POLITICAL_LEAD_LAG_DEFAULTS)
    default_config = dict(DEFAULT_CONFIG)

    def __init__(self):
        super().__init__()
        self._config = dict(self.DEFAULT_CONFIG)
        self._kalshi_cache = _KalshiMarketCache(api_url=settings.KALSHI_API_URL, ttl_seconds=30)

    def configure(self, config: dict) -> None:
        if config:
            for key in self.DEFAULT_CONFIG:
                if key in config:
                    self._config[key] = config[key]

    def _effective_config(self) -> dict:
        cfg = dict(self.DEFAULT_CONFIG)
        if isinstance(self._config, dict):
            cfg.update(self._config)
        runtime_cfg = getattr(self, "config", None)
        if isinstance(runtime_cfg, dict):
            cfg.update(runtime_cfg)
        return validate_political_lead_lag_config(cfg)

    # ------------------------------------------------------------------
    # Market pairing — single-lookup version of cross_platform's matcher.
    # cross_platform.py maintains an inverted token index because it scans
    # its ENTIRE Polymarket catalog every cycle; we only need to pair one
    # market per whale signal, so a direct linear scan over the (much
    # smaller, TTL-cached) Kalshi catalog is simpler and plenty fast for a
    # per-event lookup.
    # ------------------------------------------------------------------
    def _find_kalshi_pair(self, pm_question: str, match_threshold: float) -> Optional[tuple[Any, float]]:
        # No sport-outcome (Win/Draw/Away) disambiguation here — that guard
        # exists in cross_platform.py for 3-way soccer-style events, which
        # doesn't apply to political race markets (binary/multi-candidate,
        # no "draw" outcome), and it requires a full pm_market object with
        # a `.id` ticker that this question-text-only lookup doesn't have.
        pm_tokens = _tokenize(pm_question)
        if not pm_tokens:
            return None
        best_market = None
        best_score = 0.0
        for km in self._kalshi_cache.get_markets():
            km_tokens = _tokenize(km.question)
            if not km_tokens:
                continue
            score = _jaccard_similarity(pm_tokens, km_tokens)
            if score > best_score:
                best_score = score
                best_market = km
        if best_market is not None and best_score >= match_threshold:
            return best_market, best_score
        return None

    # ------------------------------------------------------------------
    # Evaluate / Should-Exit (unified strategy interface)
    # ------------------------------------------------------------------

    def custom_checks(self, signal: Any, context: dict, params: dict, payload: dict) -> list[DecisionCheck]:
        # Kept for interface parity with the other "traders" strategies;
        # the real gating happens in evaluate() below since this strategy
        # needs to make an external Kalshi lookup mid-decision, which the
        # generic custom_checks() contract (pure function of already-known
        # signal/payload data) isn't set up for.
        return []

    def evaluate(self, signal: Any, context: Any) -> StrategyDecision:
        context_payload = context if isinstance(context, dict) else {}
        params = validate_political_lead_lag_config(context_payload.get("params") or self._effective_config())

        payload = signal.payload_json if isinstance(getattr(signal, "payload_json", None), dict) else {}
        strategy_context = payload.get("strategy_context") if isinstance(payload.get("strategy_context"), dict) else {}
        copy_event = strategy_context.get("copy_event") if isinstance(strategy_context.get("copy_event"), dict) else {}
        source_trade = payload.get("source_trade") if isinstance(payload.get("source_trade"), dict) else {}
        if not source_trade and isinstance(strategy_context.get("source_trade"), dict):
            source_trade = strategy_context.get("source_trade")

        checks: list[DecisionCheck] = []

        # -- Gate 1: this is Polymarket whale flow, not something else --
        # Every wallet-activity signal in this system originates from
        # Polymarket tracking (Kalshi doesn't expose public per-order wallet
        # attribution the way an on-chain CLOB does) — but we check
        # explicitly rather than assume, so a future non-Polymarket source
        # can't silently feed this strategy bad pairs.
        source_platform = str(
            copy_event.get("platform") or source_trade.get("platform") or "polymarket"
        ).strip().lower()
        platform_ok = source_platform == "polymarket"
        checks.append(DecisionCheck(
            "source_platform", "Signal originates on Polymarket", platform_ok,
            detail=f"platform={source_platform}",
        ))
        if not platform_ok:
            return StrategyDecision("blocked", "Signal is not Polymarket-origin whale flow", checks=checks)

        # -- Gate 2: whale size --
        side = str(copy_event.get("side") or source_trade.get("side") or "").strip().upper()
        pm_price = safe_float(copy_event.get("price"), safe_float(source_trade.get("price"), 0.0))
        source_notional = safe_float(source_trade.get("source_notional_usd"), 0.0)
        if source_notional <= 0.0:
            source_size = safe_float(copy_event.get("size"), 0.0)
            source_notional = max(0.0, source_size * max(0.0, pm_price))
        min_notional = safe_float(params.get("min_source_notional_usd"), 10_000.0)
        notional_ok = source_notional >= min_notional
        checks.append(DecisionCheck(
            "whale_size", "Whale trade meets minimum notional", notional_ok,
            score=source_notional, detail=f"notional=${source_notional:,.0f} min=${min_notional:,.0f}",
        ))

        # -- Gate 3: freshness --
        detected_at = _to_utc(
            copy_event.get("detected_at") or source_trade.get("detected_at")
            or copy_event.get("timestamp") or source_trade.get("timestamp")
        )
        max_age = safe_float(params.get("max_signal_age_seconds"), 20.0)
        age_seconds = max_age + 1.0
        if detected_at is not None:
            age_seconds = max(0.0, (utcnow() - detected_at).total_seconds())
        fresh_ok = age_seconds <= max_age
        checks.append(DecisionCheck(
            "freshness", "Signal is fresh enough to front-run Kalshi", fresh_ok,
            score=age_seconds, detail=f"age={age_seconds:.1f}s max={max_age:.0f}s",
        ))

        confidence = to_confidence(getattr(signal, "confidence", copy_event.get("confidence")), 0.0)
        min_confidence = safe_float(params.get("min_confidence"), 0.40)
        confidence_ok = confidence >= min_confidence
        checks.append(DecisionCheck(
            "confidence", "Signal confidence threshold", confidence_ok,
            score=confidence, detail=f"confidence={confidence:.2f} min={min_confidence:.2f}",
        ))

        if not (notional_ok and fresh_ok and confidence_ok):
            return StrategyDecision("skipped", "Whale/freshness/confidence gate failed", checks=checks)

        # -- Gate 4: find the paired Kalshi market --
        pm_question = str(
            copy_event.get("market_question") or source_trade.get("market_question")
            or getattr(signal, "market_question", "") or ""
        )
        match_threshold = safe_float(params.get("match_threshold"), _MATCH_THRESHOLD)
        match = self._find_kalshi_pair(pm_question, match_threshold) if pm_question else None
        pair_found = match is not None
        checks.append(DecisionCheck(
            "kalshi_pair", "Matching Kalshi market found", pair_found,
            detail=f"question={pm_question[:60]!r}" if pm_question else "no question text on signal",
        ))
        if not pair_found:
            return StrategyDecision("skipped", "No Kalshi market pairs with this Polymarket question", checks=checks)

        kalshi_market, match_score = match
        kalshi_yes_price = safe_float(
            (kalshi_market.outcome_prices[0] if kalshi_market.outcome_prices else None), None
        )
        kalshi_liquidity = safe_float(kalshi_market.liquidity, 0.0)

        # -- Gate 5: liquidity floor --
        liquidity_ok = kalshi_liquidity >= safe_float(params.get("min_kalshi_liquidity_usd"), 100.0)
        checks.append(DecisionCheck(
            "kalshi_liquidity", "Kalshi liquidity floor", liquidity_ok,
            score=kalshi_liquidity, detail=f"liquidity=${kalshi_liquidity:,.0f}",
        ))
        if kalshi_yes_price is None:
            checks.append(DecisionCheck("kalshi_price", "Kalshi price available", False, detail="no outcome price on matched market"))
            return StrategyDecision("skipped", "Matched Kalshi market has no usable price", checks=checks)

        # -- Direction + edge --
        # BUY on Polymarket implies the whale expects YES probability to
        # rise; SELL implies the opposite. We buy the corresponding side on
        # Kalshi if Kalshi hasn't repriced to reflect that yet.
        buying_yes = side in {"BUY", "YES"}
        target_side = "yes" if buying_yes else "no"
        kalshi_side_price = kalshi_yes_price if buying_yes else (1.0 - kalshi_yes_price)
        pm_implied_price = pm_price if buying_yes else (1.0 - pm_price)

        gap = pm_implied_price - kalshi_side_price
        fee = kalshi_taker_fee(kalshi_side_price)
        edge_percent = max(0.0, (gap - fee)) * 100.0
        min_edge = safe_float(params.get("min_edge_percent"), 3.0)
        edge_ok = edge_percent >= min_edge and gap > 0.0
        checks.append(DecisionCheck(
            "edge", "Post-fee edge exceeds minimum", edge_ok,
            score=edge_percent,
            detail=f"pm_implied={pm_implied_price:.3f} kalshi={kalshi_side_price:.3f} edge={edge_percent:.2f}% min={min_edge:.2f}%",
        ))

        entry_ok = (
            params.get("min_entry_price", 0.03) <= kalshi_side_price <= params.get("max_entry_price", 0.95)
        )
        checks.append(DecisionCheck(
            "entry_price_bounds", "Kalshi entry price within bounds", entry_ok,
            score=kalshi_side_price,
        ))

        if not (liquidity_ok and edge_ok and entry_ok):
            return StrategyDecision("skipped", "Kalshi-side liquidity/edge/price gate failed", checks=checks)

        # -- Sizing --
        base_size = safe_float(params.get("base_size_usd"), 25.0)
        max_size = safe_float(params.get("max_size_usd"), 250.0)
        min_size = safe_float(params.get("min_order_size_usd"), 5.0)
        sizing_policy = str(params.get("sizing_policy") or "linear").strip().lower()
        if sizing_policy == "fixed":
            size_usd = base_size
        else:
            # linear / adaptive: scale with edge and confidence, same shape
            # as the other scanner strategies' sizing formula.
            size_usd = base_size * (1.0 + (edge_percent / 100.0)) * (0.75 + confidence)
        size_usd = max(min_size, min(max_size, size_usd))

        score = (edge_percent * 0.6) + (confidence * 30.0) + (match_score * 10.0)

        return StrategyDecision(
            "selected",
            f"Polymarket whale {side} implies {pm_implied_price:.3f} on {target_side.upper()}; "
            f"Kalshi still at {kalshi_side_price:.3f} ({edge_percent:.2f}% edge)",
            score=score,
            size_usd=size_usd,
            checks=checks,
            payload={
                "kalshi_market_id": kalshi_market.id,
                "kalshi_question": kalshi_market.question,
                "target_side": target_side,
                "pm_implied_price": pm_implied_price,
                "kalshi_entry_price": kalshi_side_price,
                "match_score": match_score,
                "source_notional_usd": source_notional,
            },
        )

    def should_exit(self, position: Any, market_state: dict) -> ExitDecision:
        """Hold for convergence; exit immediately on thesis inversion."""
        if market_state.get("is_resolved"):
            return self.default_exit_check(position, market_state)

        config = getattr(position, "config", None) or {}
        config = validate_political_lead_lag_config(dict(config) if isinstance(config, dict) else {})

        entry_price = safe_float(getattr(position, "entry_price", 0.0), 0.0)
        current_price = safe_float(market_state.get("current_price"), None)
        if current_price is None or current_price <= 0.0 or entry_price <= 0.0:
            return ExitDecision("hold", "No current price available for exit evaluation")

        strategy_context = getattr(position, "strategy_context", None) or {}
        pm_implied_price = safe_float(
            strategy_context.get("pm_implied_price") if isinstance(strategy_context, dict) else None,
            None,
        )

        # -- Inversion stop: exit ASAP if the thesis reversed --
        # A meaningful move AGAINST our entry direction (price falling below
        # entry by more than the configured threshold) means the informed
        # flow reversed, or we were simply wrong. Per spec: sell immediately,
        # don't wait for a stop-loss percentage to hit.
        if config.get("inversion_stop_enabled", True):
            inversion_threshold = safe_float(config.get("inversion_move_threshold"), 0.05)
            if current_price <= max(0.0, entry_price - inversion_threshold):
                return ExitDecision(
                    "close",
                    f"Inversion stop: price {current_price:.4f} fell {entry_price - current_price:.4f} "
                    f"below entry {entry_price:.4f} (threshold={inversion_threshold:.3f}) — thesis reversed",
                    close_price=current_price,
                )

        # -- Convergence take-profit --
        # The thesis is "Kalshi will converge toward what Polymarket
        # implied". Take profit once most of that original gap has closed,
        # rather than waiting for full resolution or a fixed percent target.
        if pm_implied_price is not None and pm_implied_price > entry_price:
            original_gap = pm_implied_price - entry_price
            closed_fraction = (current_price - entry_price) / original_gap if original_gap > 0.0 else 0.0
            target_fraction = safe_float(config.get("convergence_close_fraction"), 0.80)
            if closed_fraction >= target_fraction:
                return ExitDecision(
                    "close",
                    f"Convergence target hit: {closed_fraction * 100.0:.0f}% of the "
                    f"Polymarket-implied gap has closed (target={target_fraction * 100.0:.0f}%)",
                    close_price=current_price,
                )

        # -- Safety backstop only (not the intended exit path) --
        age_minutes = safe_float(getattr(position, "age_minutes", None), None)
        max_hold_minutes = safe_float(config.get("max_hold_minutes"), 4320.0)
        if age_minutes is not None and max_hold_minutes > 0.0 and age_minutes >= max_hold_minutes:
            return ExitDecision(
                "close",
                f"Max hold safety backstop reached ({age_minutes:.0f}m >= {max_hold_minutes:.0f}m) "
                f"without convergence or inversion",
                close_price=current_price,
            )

        return ExitDecision("hold", "Holding for convergence — thesis intact, no inversion")
