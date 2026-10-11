"""The scalp brief the model returns (D-058): the coming hour's picture, 3-5 key levels, conditional plans with a
confidence, the no-scalp conditions, the news clock and a warning about the desk's own holdings. Validated like the
desk's answers (``ai/contract.py``): over-long prose is trimmed, a wrong price order is rejected (and repaired once)."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ..ai.contract import Notes, Text, _M

RECORD_VERSION = 1
LevelKind = Literal["support", "resistance", "liquidity_high", "liquidity_low", "order_block", "fvg", "round", "pdh",
                    "pdl", "asia_high", "asia_low", "vwap", "poc", "other"]
Side = Literal["BUY", "SELL"]
EntryType = Literal["limit", "stop", "market_after_rejection"]
Regime = Literal["trend_up", "trend_down", "range", "volatile_news", "mixed"]
Bias = Literal["bullish", "bearish", "neutral"]
RR_TOLERANCE = 0.05            # the stated rr may differ this much (relative) from |target-entry| / |entry-stop|


class ScalpLevel(_M):
    price: float = Field(gt=0, description="The level, in the analysis instrument's prices")
    kind: LevelKind
    timeframe: Literal["1h", "15m", "5m"]
    strength: Literal["strong", "medium", "weak"]
    why: Text(220) = Field(description="Why it matters this hour, from the payload (structure, zone, liquidity, clock)")


class ScalpPlan(_M):
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


class ScalpBrief(_M):
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
