"""
Replays historical Coinbase market data through the REAL NeuravexAgent +
PaperExchange, the way the simulation account runs in production: a tick
every few minutes on 1h candles (including the still-forming current hour
the live feed returns), followed by the per-position exit sweep.

Two modes, so a change can be judged against what was running before:

  --mode old  reproduces the behaviour before 2026-10-08:
              strategies see the forming candle, 0.1% cost estimate,
              no stop-loss, take-profit on GROSS profit, take-profit
              sells don't count for the cooldown, no strategy learning,
              no daily-loss stop, circuit breaker halts without flattening.
  --mode new  the current code paths (closed candles, one trade per
              candle, 0.3% costs, ATR stop-loss, net take-profit, learning,
              flatten on floor/drawdown).

Usage (from the repo root):
  python scripts/replay_backtest.py --start 2026-09-26T12:00 --end 2026-10-08T06:35 --mode old
  python scripts/replay_backtest.py --start 2026-09-26T12:00 --end 2026-10-08T06:35 --mode new --take-profit 30

Candles are cached under data/replay_cache/. Results are approximate: ticks
are every --step-minutes (production ticks every minute), and fills use the
last 5-minute close plus PaperExchange's fee/slippage model.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import csv
import json
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trading_engine.execution.route_costs import ROUND_TRIP_COST, slippage_bps_by_symbol
from trading_engine.supervisor import ClosedTrade as SupervisorTrade
from trading_engine.agent import AgentConfig, NeuravexAgent  # noqa: E402
from trading_engine.decision_engine import Decision, ScoreBreakdown  # noqa: E402
from trading_engine.execution.exchange_interface import Candle, OrderType, Side  # noqa: E402
from trading_engine.execution.paper_exchange import PaperExchange  # noqa: E402
from trading_engine.execution.public_market_data import CoinbasePublicMarketDataExchange  # noqa: E402
from trading_engine.risk.exits import atr_stop_price, evaluate_exit  # noqa: E402
from trading_engine.risk.risk_engine import (  # noqa: E402
    DRAWDOWN_HALT_COOLDOWN, PortfolioState, RiskLimitBreach, check_account_wide_limits,
)
from trading_engine.strategies.breakout import BreakoutStrategy  # noqa: E402
from trading_engine.strategies.mean_reversion import MeanReversionStrategy  # noqa: E402
from trading_engine.strategies.momentum import MomentumStrategy  # noqa: E402
from trading_engine.strategies.performance import performance_score, update_running_stats  # noqa: E402
from trading_engine.strategies.trend_following import TrendFollowingStrategy  # noqa: E402

# The USDC pairs from the production simulation account's coin list.
DEFAULT_SYMBOLS = [
    "WMATIC/USDC", "WETH/USDC", "WBTC/USDC", "COMP/USDC", "BAL/USDC", "1INCH/USDC", "SUSHI/USDC",
    "MANA/USDC", "SAND/USDC", "CRV/USDC", "UNI/USDC", "AAVE/USDC", "LINK/USDC",
]
BAR = 300  # 5-minute bars
HOUR = 3600
WARMUP_HOURS = 320


# ---------------------------------------------------------------- data ----

def fetch_5m(symbol: str, start: int, end: int, cache_dir: Path) -> np.ndarray:
    """[[t, open, high, low, close, volume], ...] ascending, from Coinbase."""
    cache = cache_dir / f"{symbol.replace('/', '-')}_{start}_{end}.json"
    if cache.exists():
        return np.array(json.loads(cache.read_text()))
    product = CoinbasePublicMarketDataExchange._to_product_id(symbol)
    rows: dict[int, list] = {}
    t = start
    with httpx.Client(timeout=20) as client:
        while t < end:
            t2 = min(t + 300 * BAR, end)
            for attempt in range(5):
                r = client.get(
                    f"https://api.exchange.coinbase.com/products/{product}/candles",
                    params={"granularity": BAR, "start": _iso(t), "end": _iso(t2)},
                )
                if r.status_code == 429:
                    time.sleep(1 + attempt)
                    continue
                r.raise_for_status()
                break
            for ts, low, high, open_, close, vol in r.json():
                rows[int(ts)] = [int(ts), open_, high, low, close, vol]
            t = t2
            time.sleep(0.12)
    data = [rows[k] for k in sorted(rows) if start <= k < end]
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Write-then-rename, so parallel runs never see (or clash on) a half-written file.
    tmp = cache.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    try:
        os.replace(tmp, cache)
    except PermissionError:
        # Windows: a parallel run has the same cache file open — it holds
        # the same data, so this copy can simply be dropped.
        if not cache.exists():
            raise
        tmp.unlink(missing_ok=True)
    return np.array(data)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class HourlyView:
    """Candles of `tf` seconds (1h by default) as the live feed returns them
    at a given moment: every closed candle plus the still-forming one."""

    def __init__(self, bars: np.ndarray, tf: int = HOUR):
        self.tf = tf
        self.bar_t = bars[:, 0].astype(int)
        self.bars = bars
        hours = defaultdict(list)
        for row in bars:
            hours[int(row[0]) // tf * tf].append(row)
        self.hour_starts = sorted(hours)
        self.hourly = [self._candle(h, np.array(hours[h])) for h in self.hour_starts]

    @staticmethod
    def _candle(hour_start: int, rows: np.ndarray) -> Candle:
        return Candle(
            open_time=datetime.fromtimestamp(hour_start, tz=timezone.utc),
            open=float(rows[0, 1]), high=float(rows[:, 2].max()), low=float(rows[:, 3].min()),
            close=float(rows[-1, 4]), volume=float(rows[:, 5].sum()),
        )

    def at(self, now: int, limit: int = 300) -> tuple[list[Candle], float | None]:
        """(candles, last price) known at `now` — bars that ENDED by now."""
        last_bar = bisect.bisect_right(self.bar_t, now - BAR) - 1
        if last_bar < 0:
            return [], None
        current_hour = now // self.tf * self.tf
        n_closed = bisect.bisect_left(self.hour_starts, current_hour)
        candles = self.hourly[max(0, n_closed - limit):n_closed]
        first_in_hour = bisect.bisect_left(self.bar_t, current_hour)
        if first_in_hour <= last_bar:
            candles = candles + [self._candle(current_hour, self.bars[first_in_hour:last_bar + 1])]
        return candles[-limit:], float(self.bars[last_bar, 4])


# ------------------------------------------------------------ plumbing ----

@dataclass
class ClosedTrade:
    symbol: str
    opened: float | None
    closed_at: datetime
    pnl: float
    reason: str
    order_id: str = ""


class ReplayExchange(PaperExchange):
    """PaperExchange that records every fill (cost basis is dropped on a
    full sell, so the P&L is captured just before)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fees = 0.0
        self.slippage = 0.0
        self.orders = 0
        self.closed: list[ClosedTrade] = []
        self.now: datetime | None = None
        self.next_sell_reason = "signal"

    async def create_order(self, symbol, side, order_type, quantity, price=None):
        base = symbol.split("/")[0]
        basis = self._cost_basis.get(base)
        result = await super().create_order(symbol, side, order_type, quantity, price)
        self.orders += 1
        self.fees += result.fee
        self.slippage += quantity * result.average_fill_price * self.slippage_bps / 10_000
        if side == Side.sell and basis is not None:
            pnl = (result.average_fill_price - basis) * quantity - result.fee
            self.closed.append(ClosedTrade(symbol, basis, self.now, pnl, self.next_sell_reason, result.order_id))
        self.next_sell_reason = "signal"
        return result


class ReplayStore:
    """In-memory DataStore mirroring SqlDataStore's simulated behaviour."""

    def __init__(self, exchange: ReplayExchange, mode: str):
        self.exchange = exchange
        self.mode = mode
        self.prices: dict[str, float] = {}
        self.now: datetime | None = None
        self.exec_log: list[tuple[datetime, str, str]] = []
        self.recorded: set = set()
        self.next_id = 0
        self.decisions: dict[str, Decision] = {}
        self.equity_history: list[tuple[datetime, float]] = []
        self.drawdown_halt_at: datetime | None = None
        self.day_start_equity: dict = {}
        self.risk_events = Counter()
        self.buy_strategies: dict[str, list[str]] = {}
        self.buy_regimes: dict[str, str | None] = {}
        self.closed_trades: list[SupervisorTrade] = []
        self.strategy_stats: dict[str, list] = defaultdict(lambda: [0.0, 0.0, 0])

    def equity(self) -> float:
        total = 0.0
        for asset, qty in self.exchange.get_all_balances().items():
            if asset == "USDC":
                total += qty
            elif qty > 0:
                total += qty * self.prices.get(f"{asset}/USDC", 0.0)
        return total

    # DataStore protocol
    def save_signal(self, symbol, signal):
        pass

    def save_decision(self, decision, account_id):
        if decision.id is None:
            self.next_id += 1
            decision.id = str(self.next_id)
        self.decisions[decision.id] = decision
        if decision.executed and decision.id not in self.recorded and decision.action in ("BUY", "SELL"):
            self.recorded.add(decision.id)
            self.exec_log.append((self.now, decision.symbol, decision.action))
            if decision.action == "BUY":
                self.buy_strategies[decision.symbol] = [s.strategy_name for s in decision.contributing_signals]
                self.buy_regimes[decision.symbol] = decision.entry_regime
        return decision.id

    def save_order_result(self, order_result, decision_id, account_id):
        decision = self.decisions.get(decision_id)
        if decision is not None and decision.reasons_for and decision.reasons_for[0].startswith("[risk_circuit_breaker]"):
            for trade in reversed(self.exchange.closed):
                if trade.order_id == order_result.order_id:
                    trade.reason = "circuit_breaker"
                    break

    def save_risk_event(self, event_type, severity, message, context):
        self.risk_events[event_type] += 1

    def save_pending_execution(self, account_id, decision_id, plan):
        pass

    def has_pending_order(self, account_id, symbol, side):
        return False

    def note_drawdown_halt(self, account_id, message):
        # Same rule as SqlDataStore.note_drawdown_halt (new mode only — the
        # old code had no drawdown stop expiry).
        if self.mode == "new" and (
            self.drawdown_halt_at is None or self.now >= self.drawdown_halt_at + DRAWDOWN_HALT_COOLDOWN
        ):
            self.drawdown_halt_at = self.now

    def drawdown_peak(self, equity: float) -> float:
        history = self.equity_history
        if self.drawdown_halt_at is not None and self.now >= self.drawdown_halt_at + DRAWDOWN_HALT_COOLDOWN:
            resume = self.drawdown_halt_at + DRAWDOWN_HALT_COOLDOWN
            history = [(t, e) for t, e in history if t >= resume]
        return max([e for _, e in history] + [equity])

    def record_realized_trade(self, account_id, symbol, entry_price, order_result):
        if self.mode != "new" or not entry_price:
            return
        names = self.buy_strategies.get(symbol) or []
        qty = order_result.filled_quantity
        realized = (order_result.average_fill_price - entry_price) * qty - order_result.fee
        self.closed_trades.append(SupervisorTrade(self.now, realized, names, self.buy_regimes.get(symbol)))
        for name in names:
            wr, exp, n = self.strategy_stats[name]
            self.strategy_stats[name] = list(update_running_stats(wr, exp, n, won=realized > 0, pnl_per_unit=realized / qty))

    def recent_closed_trades(self, account_id):
        return self.closed_trades

    def strategy_performance(self) -> dict[str, float]:
        out = {}
        for name, (wr, _exp, n) in self.strategy_stats.items():
            score = performance_score(wr, n)
            if score is not None:
                out[name] = score
        return out

    def get_portfolio_state(self, account_id):
        equity = self.equity()
        peak = self.drawdown_peak(equity)
        if self.mode == "new":
            day = self.now.date()
            daily_start = self.day_start_equity.setdefault(day, equity)
            daily_pnl = equity - daily_start
        else:  # old code: daily_pnl was hardcoded to 0
            daily_start, daily_pnl = equity, 0.0
        balances, basis = self.exchange.get_all_balances(), self.exchange.get_cost_basis()
        open_positions = {
            f"{a}/USDC": {"notional": q * basis.get(a, 0.0), "quantity": q, "entry_price": basis.get(a, 0.0)}
            for a, q in balances.items() if a != "USDC" and q > 0
        }
        hour_ago = self.now - timedelta(hours=1)
        last_by_symbol: dict[str, datetime] = {}
        for ts, sym, _ in self.exec_log:
            last_by_symbol[sym] = ts
        buys = [ts for ts, _, act in self.exec_log if act == "BUY"]
        return PortfolioState(
            equity=equity, peak_equity=peak, cash=equity, open_positions=open_positions,
            daily_pnl=daily_pnl, daily_start_equity=daily_start,
            trades_this_hour=sum(1 for ts, _, _ in self.exec_log if ts >= hour_ago),
            last_entry_time=buys[-1] if buys else None,
            last_signal_time_by_symbol=last_by_symbol,
        )


def _exit_decision(symbol: str, reason: str, detail: str) -> Decision:
    d = Decision(symbol=symbol, action="SELL", direction="SHORT", confidence=1.0,
                 score=ScoreBreakdown(0, 0, 0, 0, 0, 0, 0, 0, 0), reasons_for=[f"[{reason}] {detail}"])
    d.executed = True
    return d


async def exit_sweep(mode: str, exchange: ReplayExchange, store: ReplayStore, views: dict, now_ts: int,
                     take_profit: float | None) -> None:
    """Mirrors tasks.py's simulated exit sweep (new) or the old
    _apply_per_trade_take_profit (old)."""
    basis = exchange.get_cost_basis()
    for asset, qty in list(exchange.get_all_balances().items()):
        if asset == "USDC" or qty <= 0 or asset not in basis:
            continue
        symbol = f"{asset}/USDC"
        price = store.prices.get(symbol)
        if price is None:
            continue
        if mode == "old":
            if take_profit and (price - basis[asset]) * qty >= take_profit:
                exchange.next_sell_reason = "take_profit"
                await exchange.create_order(symbol, Side.sell, OrderType.market, qty)
            continue
        candles, _ = views[symbol].at(now_ts)
        closed = [c for c in candles if c.open_time.timestamp() + views[symbol].tf <= now_ts]
        stop = atr_stop_price(basis[asset], np.array([c.high for c in closed]),
                              np.array([c.low for c in closed]), np.array([c.close for c in closed]))
        exit_ = evaluate_exit(quantity=qty, cost_basis=basis[asset], price=price, stop_price=stop,
                              take_profit_usdc=take_profit)
        if exit_ is None:
            continue
        exchange.next_sell_reason = exit_.reason
        result = await exchange.create_order(symbol, Side.sell, OrderType.market, qty)
        store.save_decision(_exit_decision(symbol, exit_.reason, exit_.detail), "replay")
        store.record_realized_trade("replay", symbol, basis[asset], result)


# ---------------------------------------------------------------- main ----

async def replay(args) -> dict:
    start = int(datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc).timestamp()) // BAR * BAR
    end = int(datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc).timestamp()) // BAR * BAR
    cache_dir = ROOT / "data" / "replay_cache"
    tf = {"1h": HOUR, "4h": 4 * HOUR}[args.timeframe]
    views = {}
    for sym in args.symbols:
        bars = fetch_5m(sym, start - WARMUP_HOURS * tf, end, cache_dir)
        if len(bars):
            views[sym] = HourlyView(bars, tf)
        else:
            print(f"  (no data for {sym}, skipped)", file=sys.stderr)

    exchange = ReplayExchange(
        starting_balances={"USDC": args.equity}, max_price_age_seconds=10**9,
        slippage_bps_by_symbol=slippage_bps_by_symbol() if args.real_costs else None,
    )
    store = ReplayStore(exchange, args.mode)
    sim_now = {"t": datetime.fromtimestamp(start, tz=timezone.utc).replace(tzinfo=None)}
    new = args.mode == "new"
    config = AgentConfig(
        symbols=list(views), max_trade_risk=0.02, max_position_size=0.25, max_portfolio_exposure=0.70,
        max_daily_loss=0.05, max_drawdown=0.10, min_balance_floor=args.floor, max_open_positions=5,
        minimum_confidence=0.70, minimum_expected_edge=0.003, cooldown_seconds=900, max_trades_per_hour=4,
        min_time_between_entries_seconds=300, trading_mode="paper", execution_mode="server",
        closed_candles_only=new, one_trade_per_candle=new,
        estimated_transaction_cost=0.003 if new else 0.001,
        timeframe=args.timeframe, min_agreeing_strategies=args.min_agree,
        entry_regimes=tuple(args.entry_regimes) if args.entry_regimes else None,
        timeframe_aware_regime=not args.legacy_regime,
        supervisor_enabled=args.supervisor,
        round_trip_cost_by_symbol=ROUND_TRIP_COST if args.real_costs else None,
    )
    strategies = [TrendFollowingStrategy(), MomentumStrategy(), MeanReversionStrategy(), BreakoutStrategy()]
    agent = NeuravexAgent(exchange=exchange, strategies=strategies, store=store, config=config,
                          account_id="replay", clock=lambda: sim_now["t"])
    if args.no_circuit_breaker:
        config.max_drawdown, config.max_daily_loss, config.min_balance_floor = 1.0, 1.0, None

    curve, halted_steps, first_halt = [], 0, None
    step = args.step_minutes * 60
    for now_ts in range(start, end, step):
        now = datetime.fromtimestamp(now_ts, tz=timezone.utc).replace(tzinfo=None)
        sim_now["t"] = store.now = exchange.now = now
        for sym, view in views.items():
            candles, price = view.at(now_ts)
            if price is None:
                continue
            exchange.load_candles(sym, args.timeframe, candles)
            exchange.update_price(sym, price)
            store.prices[sym] = price
        agent.strategy_performance = store.strategy_performance()

        halted = False
        if args.mode == "old":
            try:
                check_account_wide_limits(store.get_portfolio_state("replay"), config.max_daily_loss,
                                          config.max_drawdown, config.min_balance_floor)
            except RiskLimitBreach:
                halted = True  # old behaviour: stop opening trades, keep holding
        if halted:
            store.risk_events["risk_limit_breach"] += 1
        else:
            breaches_before = store.risk_events["risk_limit_breach"]
            await agent.run_tick()
            halted = store.risk_events["risk_limit_breach"] > breaches_before
        if halted:
            halted_steps += 1
            first_halt = first_halt or now
        await exit_sweep(args.mode, exchange, store, views, now_ts, args.take_profit)

        equity = store.equity()
        store.equity_history.append((now, equity))
        curve.append((now.isoformat(), round(equity, 2)))

    eq = np.array([e for _, e in curve])
    bh = []
    for view in views.values():
        _, p0 = view.at(start)
        _, p1 = view.at(end)
        if p0 and p1:
            bh.append(p1 / p0 - 1)
    peak = np.maximum.accumulate(eq)
    wins = [t.pnl for t in exchange.closed if t.pnl > 0]
    losses = [t.pnl for t in exchange.closed if t.pnl <= 0]
    result = {
        "mode": args.mode, "label": args.label, "take_profit": args.take_profit, "period": f"{args.start} -> {args.end}",
        "start_equity": args.equity, "end_equity": round(float(eq[-1]), 2),
        "return_pct": round(100 * (eq[-1] / args.equity - 1), 2),
        "max_drawdown_pct": round(100 * float(((peak - eq) / peak).max()), 2),
        "buy_hold_basket_pct": round(100 * float(np.mean(bh)), 2) if bh else None,
        "buy_hold_median_pct": round(100 * float(np.median(bh)), 2) if bh else None,
        "orders": exchange.orders, "closed_trades": len(exchange.closed),
        "fees": round(exchange.fees, 2), "slippage": round(exchange.slippage, 2),
        "win_rate_pct": round(100 * len(wins) / len(exchange.closed), 1) if exchange.closed else None,
        "avg_win": round(float(np.mean(wins)), 2) if wins else None,
        "avg_loss": round(float(np.mean(losses)), 2) if losses else None,
        "exit_reasons": dict(Counter(t.reason for t in exchange.closed)),
        "pnl_by_symbol": {k: round(v, 2) for k, v in sorted(
            ((s, sum(t.pnl for t in exchange.closed if t.symbol == s)) for s in views), key=lambda kv: kv[1])},
        "halted_pct_of_time": round(100 * halted_steps / max(1, len(curve)), 1),
        "first_halt": first_halt.isoformat() if first_halt else None,
        "strategy_stats": {k: {"win_rate": round(v[0], 2), "trades": v[2]} for k, v in store.strategy_stats.items()},
    }
    if args.curve_csv:
        with open(args.curve_csv, "w", newline="") as f:
            csv.writer(f).writerows([("time", "equity"), *curve])
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, help="UTC, e.g. 2026-09-26T12:00")
    ap.add_argument("--end", required=True)
    ap.add_argument("--mode", choices=["old", "new"], default="new")
    ap.add_argument("--take-profit", type=float, default=5.0, help="per-trade target in USDC; 0 = none")
    ap.add_argument("--equity", type=float, default=11_403.03)
    ap.add_argument("--floor", type=float, default=9000.0)
    ap.add_argument("--step-minutes", type=int, default=5)
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--no-circuit-breaker", action="store_true", help="judge the strategy without the drawdown/floor halt")
    ap.add_argument("--curve-csv")
    ap.add_argument("--timeframe", choices=["1h", "4h"], default="1h")
    ap.add_argument("--min-agree", type=int, default=1, help="strategies that must agree on an entry")
    ap.add_argument("--entry-regimes", nargs="+", help="e.g. BULL STRONG_BULL")
    ap.add_argument("--label", help="free text echoed in the result")
    ap.add_argument("--real-costs", action="store_true", help="measured on-chain cost per coin")
    ap.add_argument("--supervisor", action="store_true", help="feedback layer: stake and strategy gate")
    ap.add_argument("--legacy-regime", action="store_true", help="old 1h volatility/crash assumptions on any timeframe")
    args = ap.parse_args()
    args.take_profit = args.take_profit or None
    print(json.dumps(asyncio.run(replay(args)), indent=2))


if __name__ == "__main__":
    main()
