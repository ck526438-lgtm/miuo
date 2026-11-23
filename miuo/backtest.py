"""Backtest bougie par bougie, volontairement pessimiste.

- Entrée à l'ouverture de la bougie suivant le signal (jamais au prix de clôture du signal).
- Prix = BID ; l'achat se fait à l'ASK (bid + spread), la vente à découvert se clôture à l'ASK.
- Slippage défavorable sur l'entrée et sur les stops ; commission par lot.
- Si le stop et l'objectif sont touchés dans la même bougie, on compte le STOP.
- Gaps : si l'ouverture saute au-delà du stop, sortie au prix d'ouverture (pire que le stop).
- Le RiskManager est le même qu'en réel.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import MiuoConfig
from .risk import RiskManager


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series
    initial_equity: float

    @property
    def r(self) -> np.ndarray:
        return self.trades["r"].to_numpy() if len(self.trades) else np.array([])


TRADE_COLUMNS = ["entry_time", "exit_time", "direction", "entry", "exit", "sl", "tp", "lots",
                 "pnl", "r", "reason"]


def run_backtest(df: pd.DataFrame, sig: pd.DataFrame, cfg: MiuoConfig, max_hold: int = 48,
                 initial_equity: float = 10_000.0) -> BacktestResult:
    n = len(df)
    m = cfg.market
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    spread_pts = df["spread"].to_numpy(float) if "spread" in df.columns else np.full(n, float(m.default_spread_points))
    spread_pts = np.where(np.isfinite(spread_pts) & (spread_pts > 0), spread_pts, m.default_spread_points)
    times = df.index.to_pydatetime()
    s_sig = sig["signal"].to_numpy()
    s_sd = sig["stop_dist"].to_numpy(float)
    s_tp = sig["tp_r"].to_numpy(float)

    point, cs, comm = m.point, m.contract_size, m.commission_per_lot
    slip = m.slippage_points * point
    risk = RiskManager(cfg.risk, m)
    equity = float(initial_equity)
    eq = np.empty(n)
    trades: list[tuple] = []
    pos = None
    pending = None

    def close(i, px, reason):
        nonlocal equity, pos
        pnl = (px - pos["entry"]) * pos["d"] * pos["lots"] * cs - comm * pos["lots"]
        equity += pnl
        trades.append((pos["t0"], times[i], pos["d"], pos["entry"], px, pos["sl"], pos["tp"],
                       pos["lots"], pnl, pnl / pos["risk_amt"], reason))
        risk.on_trade_closed(pnl, times[i])
        pos = None

    for i in range(n):
        t = times[i]
        sp = spread_pts[i] * point

        # 1) entrée à l'ouverture
        if pos is None and pending is not None:
            d, sd, tpr = pending
            ok, _ = risk.can_trade(t, equity, spread_pts[i])
            lots = risk.lot_size(equity, sd) if ok else 0.0
            if lots > 0:
                if d == 1:
                    entry = o[i] + sp + slip
                    sl, tp = entry - sd, entry + tpr * sd
                else:
                    entry = o[i] - slip
                    sl, tp = entry + sd, entry - tpr * sd
                pos = {"d": d, "entry": entry, "sl": sl, "tp": tp, "lots": lots, "i0": i, "t0": t,
                       "risk_amt": sd * cs * lots + comm * lots}
                risk.on_trade_opened()
        pending = None

        # 2) sorties (stop prioritaire)
        if pos is not None:
            held = i > pos["i0"]
            if pos["d"] == 1:
                po, ph, pl = o[i], h[i], l[i]                 # le long sort au BID
                if held and po <= pos["sl"]:
                    close(i, po - slip, "sl_gap")
                elif pl <= pos["sl"]:
                    close(i, pos["sl"] - slip, "sl")
                elif held and po >= pos["tp"]:
                    close(i, po, "tp_gap")
                elif ph >= pos["tp"]:
                    close(i, pos["tp"], "tp")
            else:
                po, ph, pl = o[i] + sp, h[i] + sp, l[i] + sp  # le short sort à l'ASK
                if held and po >= pos["sl"]:
                    close(i, po + slip, "sl_gap")
                elif ph >= pos["sl"]:
                    close(i, pos["sl"] + slip, "sl")
                elif held and po <= pos["tp"]:
                    close(i, po, "tp_gap")
                elif pl <= pos["tp"]:
                    close(i, pos["tp"], "tp")
            if pos is not None and i - pos["i0"] + 1 >= max_hold:
                close(i, c[i] - slip if pos["d"] == 1 else c[i] + sp + slip, "time")

        # 3) valorisation
        if pos is not None:
            mark = c[i] if pos["d"] == 1 else c[i] + sp
            eq[i] = equity + (mark - pos["entry"]) * pos["d"] * pos["lots"] * cs
        else:
            eq[i] = equity
        risk.update(t, equity)

        # 4) signal pour la bougie suivante
        if pos is None and s_sig[i] != 0 and np.isfinite(s_sd[i]):
            pending = (int(s_sig[i]), float(s_sd[i]), float(s_tp[i]))

    if pos is not None and n:
        close(n - 1, c[-1] - slip if pos["d"] == 1 else c[-1] + spread_pts[-1] * point + slip, "end")
        eq[-1] = equity

    return BacktestResult(pd.DataFrame(trades, columns=TRADE_COLUMNS),
                          pd.Series(eq, index=df.index, name="equity"), float(initial_equity))
