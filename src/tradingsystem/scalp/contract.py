"""The scalp brief the model returns (D-058): the coming hour's picture, 3-5 key levels, conditional plans with a
confidence, the no-scalp conditions, the news clock and a warning about the desk's own holdings. Validated like the
desk's answers (``ai/contract.py``): over-long prose is trimmed, a wrong price order is rejected (and repaired once)."""
from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from ..ai.contract import Notes, Text, _M

RECORD_VERSION = 1
LevelKind = Literal["support", "resistance", "liquidity_high", "liquidity_low", "order_block", "fvg", "round", "pdh",
                    "pdl", "asia_high", "asia_low", "vwap", "poc", "other"]
Side = Literal["BUY", "SELL"]
EntryType = Literal["limit", "stop", "market_after_rejection"]
Regime = Literal["trend_up", "trend_down", "range", "volatile_news", "mixed"]
Bias = Literal["bullish", "bearish", "neutral"]
RR_TOLERANCE = 0.05            # the stated rr may differ this much (relative) from |target-entry| / |entry-stop|


class _S(_M):
    """The scalp models IGNORE keys outside the schema (the desk's answers forbid them): 2026-10-12 three of four
    briefs added `strength_note` / `timeframe_note` to their levels and paid a repair call each (~22 s, ~21k input
    tokens) for text that is thrown away anyway. A missing or wrong field is still rejected (and repaired once)."""
    model_config = ConfigDict(extra="ignore")


class ScalpLevel(_S):
    price: float = Field(gt=0, description="The level, in the analysis instrument's prices")
    kind: LevelKind
    timeframe: Literal["1h", "15m", "5m"]
    strength: Literal["strong", "medium", "weak"]
    why: Text(220) = Field(description="Why it matters this hour, from the payload (structure, zone, liquidity, clock)")


class ScalpPlan(_S):
    level_price: float = Field(gt=0, description="The level this plan belongs to")
    side: Side
    trigger: Text(200) = Field(description="If price does X ... (what must happen first)")
    entry_type: EntryType
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float = Field(gt=0)
    rr: float = Field(ge=1.0, le=10.0)
    max_hold_min: int = Field(ge=5, le=240)
    invalidation: Text(200) = Field(description="What kills the idea before the trigger")
    confidence: int = Field(ge=0, le=100, description="P(target before stop within max_hold_min | triggered), 0-100")

    @model_validator(mode="after")
    def _prices(self) -> "ScalpPlan":
        if self.side == "BUY" and not (self.stop < self.entry < self.target):
            raise ValueError("a BUY plan needs stop < entry < target")
        if self.side == "SELL" and not (self.stop > self.entry > self.target):
            raise ValueError("a SELL plan needs stop > entry > target")
        risk = abs(self.entry - self.stop)
        rr = abs(self.target - self.entry) / risk
        if abs(rr - self.rr) > RR_TOLERANCE * max(rr, 1.0):
            raise ValueError(f"rr {self.rr} does not match the prices (|target-entry| / |entry-stop| = {rr:.2f})")
        return self


class ScalpBrief(_S):
    regime: Regime
    bias: Bias
    bias_confidence: int = Field(ge=0, le=100)
    picture: Text(1200) = Field(description="The general picture for the coming hour (prose, the owner's language)")
    expect: Text(500) = Field(description="What should happen in general this hour, and what would prove it wrong")
    levels: list[ScalpLevel] = Field(min_length=3, max_length=5)
    plans: list[ScalpPlan] = Field(default_factory=list, max_length=5)
    no_scalp_now: bool = Field(description="True when nothing qualifies this hour (an empty plans list is a good answer)")
    no_scalp: Notes(6) = Field(default_factory=list, description="The conditions under which the owner should not scalp")
    news_clock: Notes(4) = Field(default_factory=list, description="The releases / clock events that matter this hour")
    desk_warning: Text(400) = Field(description="What the automatic desk holds and how a manual scalp must respect it")
    confidence_note: Text(300) = Field(default="", description="How the confidences were calibrated against the journal")
    trade_call: Text(500) = Field(default="", description="THE one trade to take now, in the owner's language: side, "
                                  "entry price, stop, target, the condition that must happen first, the time limit - "
                                  "the numbers of the plan best_plan points to; or 'no trade now' and why")
    best_plan: int | None = Field(default=None, ge=0, description="0-based index of the plan trade_call refers to "
                                  "(your best plan); null when there is none")
    manage_open: Literal["none", "hold", "close", "breakeven"] = Field(
        default="none", description="D-063: your decision on YOUR OPEN auto-trade shown in the user message - hold, "
                                    "close now, or move the stop to the entry (breakeven); none when there is no open trade")
    manage_reason: Text(300) = Field(default="", description="One sentence why (the owner's language)")

    @model_validator(mode="after")
    def _best_plan_exists(self) -> "ScalpBrief":
        if self.best_plan is not None and self.best_plan >= len(self.plans):
            self.best_plan = None                   # a dangling index is not worth a repair call: the code picks
        return self
