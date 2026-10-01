"""Instrument registry: logical pairs -> venue instruments with roles (D-008).

A *pair* (e.g. ``XAUUSD``) is what the AI and the user reason about. An *instrument* is a
concrete tradable/observable symbol on one venue (e.g. ``mt5:XAUUSD@`` or
``binance_usdm:XAUUSDT``). Storage is organised per instrument (one hot DB file each, one
writer each); tables keep the spec's names (``xauusd_candles_1m`` ...).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .settings import InstrumentCfg, Settings
from .timeframes import Timeframe
from .timeutil import parse_date_spec

_SAFE_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Instrument:
    pair: str
    venue: str
    symbol: str                      # venue-native symbol, e.g. "XAUUSD@"
    roles: tuple[str, ...]
    datatypes: tuple[str, ...]
    timeframes: tuple[Timeframe, ...]
    start_specs: dict[str, str] = field(default_factory=dict, hash=False, compare=False)
    contract: dict | None = field(default=None, hash=False, compare=False)

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.symbol}"

    @property
    def table_prefix(self) -> str:
        """``BTCUSD@`` -> ``btcusd``; ``BTCUSDT`` -> ``btcusdt``."""
        return _SAFE_RE.sub("", self.symbol.lower())

    @property
    def file_stem(self) -> str:
        return _SAFE_RE.sub("_", self.symbol.lower()).strip("_").upper()

    def table(self, datatype: str, timeframe: Timeframe | None = None) -> str:
        name = f"{self.table_prefix}_{datatype}"
        return f"{name}_{timeframe.value}" if timeframe is not None else name

    def has_role(self, role: str) -> bool:
        return role in self.roles

    @property
    def is_context(self) -> bool:
        """A recorded-for-context instrument (B17): candles only, its symbol may be missing at the broker without
        harming any other instrument, and nothing trades or quotes it."""
        return self.roles == ("cross_context",)

    def start_ms(self, datatype: str, *, now: int | None = None) -> int | None:
        spec = self.start_specs.get(datatype)
        return None if spec is None else parse_date_spec(spec, now=now)

    def hot_db_path(self, data_dir: Path) -> Path:
        return data_dir / "hot" / self.venue / f"{self.file_stem}.db"


class InstrumentRegistry:
    def __init__(self, instruments: list[Instrument]) -> None:
        self._all = instruments
        self._by_key: dict[str, Instrument] = {}
        for inst in instruments:
            existing = self._by_key.get(inst.key)
            if existing is not None and existing.pair != inst.pair:
                # The same venue symbol shared by two pairs would mean two writers for one file.
                raise ValueError(f"instrument {inst.key} is configured for two pairs: {existing.pair}, {inst.pair}")
            self._by_key[inst.key] = inst

    @classmethod
    def from_settings(cls, settings: Settings, *, mt5_profile: str | None = None) -> "InstrumentRegistry":
        profile = mt5_profile or settings.mt5.data_profile
        out: list[Instrument] = []
        for pair_name, pair in settings.enabled_pairs().items():
            pair_tfs = tuple(pair.timeframes or settings.timeframes)
            for icfg in pair.instruments:
                out.append(
                    Instrument(
                        pair=pair_name,
                        venue=icfg.venue,
                        symbol=_resolve_symbol(icfg, profile),
                        roles=tuple(icfg.roles),
                        datatypes=tuple(icfg.datatypes),
                        timeframes=tuple(icfg.timeframes or pair_tfs),
                        start_specs=dict(icfg.start),
                        contract=icfg.contract.model_dump() if icfg.contract else None,
                    )
                )
        return cls(out)

    def all(self) -> list[Instrument]:
        return list(self._all)

    def get(self, key: str) -> Instrument:
        return self._by_key[key]

    def for_venue(self, venue: str) -> list[Instrument]:
        return [i for i in self._all if i.venue == venue]

    def for_pair(self, pair: str) -> list[Instrument]:
        return [i for i in self._all if i.pair == pair]

    def with_role(self, pair: str, role: str) -> list[Instrument]:
        return [i for i in self.for_pair(pair) if i.has_role(role)]

    def primary(self, pair: str) -> Instrument:
        return self.with_role(pair, "analysis_primary")[0]

    def pairs(self) -> list[str]:
        return sorted({i.pair for i in self._all})


def sibling_feeds(settings: Settings) -> list[Instrument]:
    """D-051: the candles of a correlated-group sibling that no system of this project folder records.

    A pair's ``market.cross`` block (Phase 5 B6) reads the analysis-primary candles of the other members of its
    ``risk.correlated_groups`` group (BTC <-> ETH) from ``data/hot/<venue>/<SYMBOL>.db``. While the sibling runs from
    this folder (enabled here, or one of its ``instances:``) its own ingest writes that file. When it does not —
    every pair is its own project folder — this system records the sibling's candles itself, into its own data
    folder: candles only, the sibling's own timeframes and start. Nothing of the sibling is analysed, gated or traded
    here (the feed is never part of the trading registry; only the ingest records it)."""
    enabled = settings.enabled_pairs()
    out: dict[str, Instrument] = {}
    for group in settings.risk.correlated_groups:
        if not any(p in enabled for p in group):
            continue
        for other in group:
            if other in enabled or other in settings.instances or other not in settings.pairs:
                continue
            pcfg = settings.pairs[other]
            only = settings.model_copy(update={"pairs": {other: pcfg.model_copy(update={"enabled": True})}})
            try:
                inst = InstrumentRegistry.from_settings(only).primary(other)
            except (ValueError, IndexError):
                continue
            if "candles" not in inst.datatypes:
                continue
            feed = replace(inst, datatypes=("candles",),
                           start_specs={k: v for k, v in inst.start_specs.items() if k == "candles"})
            out.setdefault(feed.key, feed)
    return list(out.values())


def _resolve_symbol(icfg: InstrumentCfg, mt5_profile: str) -> str:
    if icfg.symbol_by_profile:
        try:
            return icfg.symbol_by_profile[mt5_profile]
        except KeyError as exc:
            raise ValueError(f"no symbol configured for MT5 profile {mt5_profile!r}: {icfg.symbol_by_profile}") from exc
    assert icfg.symbol is not None
    return icfg.symbol
