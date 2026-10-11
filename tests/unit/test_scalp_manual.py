"""D-058: the owner's manual deals (scalp/scorer.manual_trades / attribute) - magic 0 on the pair's symbol only, IN /
OUT pairing by position, the P&L sum with the costs, open trades, the attribution to the brief's hour."""
from types import SimpleNamespace as NS

from tradingsystem.scalp.scorer import attribute, manual_trades

T0 = 1_760_000_000_000
MIN = 60_000
H = 3_600_000
OFFSET = 3 * H                       # the fake server clock runs 3 h ahead of UTC
IN, OUT, OUT_BY, BUY, SELL = 0, 1, 3, 0, 1


def deal(pid, entry, typ, price, at_utc, vol=0.01, profit=0.0, commission=0.0, swap=0.0, fee=0.0, magic=0,
         symbol="XAUUSD@", comment=""):
    return NS(position_id=pid, entry=entry, type=typ, price=price, time_msc=at_utc + OFFSET, volume=vol, profit=profit,
              commission=commission, swap=swap, fee=fee, magic=magic, symbol=symbol, comment=comment, ticket=pid * 10)


class Clock:
    def __init__(self):
        self.calls = 0

    def __call__(self, server_ms):
        self.calls += 1
        return server_ms - OFFSET


def test_pairing_pnl_and_filters():
    deals = [
        deal(1, IN, SELL, 4196.1, T0 + 42 * MIN, commission=-0.07, comment="my scalp"),
        deal(1, OUT, BUY, 4192.9, T0 + 55 * MIN, profit=3.20, commission=-0.07, swap=-0.01),
        deal(2, IN, BUY, 4181.5, T0 + 70 * MIN),
        deal(2, OUT_BY, SELL, 4183.0, T0 + 80 * MIN, profit=1.50),      # closed by an opposite position
        deal(3, IN, BUY, 4190.0, T0 + 90 * MIN),                         # still open
        deal(4, IN, BUY, 4190.0, T0 + 91 * MIN, magic=12345),            # the desk's
        deal(5, IN, BUY, 1.1, T0 + 92 * MIN, symbol="EURUSD@"),          # another symbol
        deal(6, OUT, SELL, 4199.0, T0 + 5 * MIN, profit=2.0),            # opened before the window: no IN
        deal(7, IN, SELL, 4200.0, T0 - 10 * MIN),                        # opened before t0
    ]
    clock = Clock()
    rows = manual_trades(deals, symbol="XAUUSD@", server_to_utc=clock, t0=T0, t1=T0 + 2 * H)
    assert [r["position_id"] for r in rows] == [1, 2, 3]
    assert clock.calls >= 4                                            # every IN and OUT time went through the model
    r1 = rows[0]
    assert r1["side"] == "SELL" and r1["entry"] == 4196.1 and r1["exit"] == 4192.9 and r1["volume"] == 0.01
    assert r1["open_ms"] == T0 + 42 * MIN and r1["close_ms"] == T0 + 55 * MIN
    assert r1["pnl_usd"] == 3.05 and r1["open"] is False and r1["comment"] == "my scalp"
    assert r1["brief_id"] is None and r1["followed_plan"] is False
    assert rows[1]["pnl_usd"] == 1.5 and rows[1]["exit"] == 4183.0 and rows[1]["open"] is False
    assert rows[2]["open"] is True and rows[2]["exit"] is None and rows[2]["close_ms"] is None
    assert manual_trades([], symbol="XAUUSD@", server_to_utc=clock, t0=T0, t1=T0 + H) == []
    assert manual_trades(None, symbol="XAUUSD@", server_to_utc=clock, t0=T0, t1=T0 + H) == []


def test_partial_exits_are_volume_weighted():
    deals = [deal(9, IN, BUY, 4200.0, T0 + MIN, vol=0.02),
             deal(9, OUT, SELL, 4202.0, T0 + 2 * MIN, vol=0.01, profit=2.0),
             deal(9, OUT, SELL, 4204.0, T0 + 3 * MIN, vol=0.01, profit=4.0)]
    r = manual_trades(deals, symbol="XAUUSD@", server_to_utc=Clock(), t0=T0, t1=T0 + H)[0]
    assert r["exit"] == 4203.0 and r["pnl_usd"] == 6.0 and r["close_ms"] == T0 + 3 * MIN and r["volume"] == 0.02


def test_missing_cost_fields_are_zero():
    d = NS(position_id=1, entry=IN, type=BUY, price=4200.0, time_msc=T0 + MIN + OFFSET, volume=0.01, profit=0.0,
           magic=0, symbol="XAUUSD@")
    r = manual_trades([d], symbol="XAUUSD@", server_to_utc=Clock(), t0=T0, t1=T0 + H)[0]
    assert r["pnl_usd"] == 0.0 and r["comment"] == "" and r["open"] is True


def rec(bid, as_of, plans):
    return {"id": bid, "as_of_ms": as_of, "brief": {"plans": plans}}


def test_attribution_to_the_brief_hour_and_followed_plan():
    recs = [rec("A", T0, [{"side": "SELL", "entry": 4196.5}, {"side": "BUY", "entry": 4181.5}]),
            rec("B", T0 + H, [{"side": "BUY", "entry": 4190.0}])]
    trades = [{"side": "SELL", "entry": 4196.1, "open_ms": T0 + 42 * MIN},       # A, plan 0 within tol 1.0
              {"side": "BUY", "entry": 4196.1, "open_ms": T0 + 43 * MIN},        # A, wrong side
              {"side": "BUY", "entry": 4192.0, "open_ms": T0 + H + 10 * MIN},    # B, 2.0 away: not followed
              {"side": "BUY", "entry": 4190.0, "open_ms": T0 + 2 * H},           # after B's hour: no brief
              {"side": "BUY", "entry": 4190.0, "open_ms": T0 - 1}]
    attribute(trades, recs[::-1], 1.0)                                         # any order of records
    assert [t["brief_id"] for t in trades] == ["A", "A", "B", None, None]
    assert [t["followed_plan"] for t in trades] == [True, False, False, False, False]
    assert trades[0]["plan_idx"] == 0
    attribute(trades, [], 1.0)
    assert all(t["brief_id"] is None and t["followed_plan"] is False for t in trades)


def test_attribution_prefers_the_latest_brief_holding_the_open():
    recs = [rec("A", T0, []), rec("A2", T0 + 30 * MIN, [{"side": "BUY", "entry": 4190.0}])]     # an on-demand brief
    trades = [{"side": "BUY", "entry": 4190.4, "open_ms": T0 + 45 * MIN}]
    attribute(trades, recs, 0.5)
    assert trades[0]["brief_id"] == "A2" and trades[0]["followed_plan"] is True
