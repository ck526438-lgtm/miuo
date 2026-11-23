"""Gestion du risque : taille de position, pertes journalières, drawdown, séries de pertes, spread.

Le même objet est utilisé en backtest et en réel, pour que les deux se comportent à l'identique.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from .config import MarketConfig, RiskConfig


class RiskManager:
    def __init__(self, risk: RiskConfig, market: MarketConfig):
        self.cfg = risk
        self.mkt = market
        self.day = None
        self.day_start_equity = None
        self.peak_equity = None
        self.trades_today = 0
        self.consec_losses = 0
        self.cooldown_until: datetime | None = None
        self.killed = False
        self.kill_reason = ""

    # ------------------------------------------------------------------ taille
    def lot_size(self, equity: float, stop_dist: float) -> float:
        """Lots tels que la perte au stop (+ commission) = risk_per_trade * equity.

        Arrondi vers le bas. Renvoie 0 si même le lot minimum dépasserait le risque autorisé :
        on ne gonfle jamais le risque pour atteindre le lot minimum.
        """
        if stop_dist <= 0 or equity <= 0:
            return 0.0
        risk_amount = equity * self.cfg.risk_per_trade
        loss_per_lot = stop_dist * self.mkt.contract_size + self.mkt.commission_per_lot
        raw = risk_amount / loss_per_lot
        step = self.mkt.lot_step
        lots = math.floor(raw / step + 1e-9) * step
        lots = min(lots, self.mkt.max_lot)
        if lots < self.mkt.min_lot:
            return 0.0
        return round(lots, 8)

    # ------------------------------------------------------------------ état
    def update(self, now: datetime, equity: float) -> None:
        d = now.date()
        if d != self.day:
            self.day = d
            self.day_start_equity = equity
            self.trades_today = 0
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity
        if not self.killed and self.peak_equity and equity <= self.peak_equity * (1 - self.cfg.max_drawdown):
            self.killed = True
            self.kill_reason = (f"Drawdown max atteint ({self.cfg.max_drawdown:.0%}) : "
                                f"equity {equity:.2f} / pic {self.peak_equity:.2f}")

    def can_trade(self, now: datetime, equity: float, spread_points: float) -> tuple[bool, str]:
        self.update(now, equity)
        if self.killed:
            return False, self.kill_reason
        if self.day_start_equity and equity <= self.day_start_equity * (1 - self.cfg.max_daily_loss):
            return False, f"Perte journalière max atteinte ({self.cfg.max_daily_loss:.0%})"
        if self.trades_today >= self.cfg.max_trades_per_day:
            return False, "Nombre max de trades du jour atteint"
        if self.cooldown_until and now < self.cooldown_until:
            return False, f"Pause après {self.cfg.max_consecutive_losses} pertes consécutives"
        if spread_points > self.cfg.max_spread_points:
            return False, f"Spread trop large ({spread_points:.0f} pts)"
        return True, "ok"

    def on_trade_opened(self) -> None:
        self.trades_today += 1

    def on_trade_closed(self, pnl: float, now: datetime) -> None:
        if pnl < 0:
            self.consec_losses += 1
            if self.consec_losses >= self.cfg.max_consecutive_losses:
                self.cooldown_until = now + timedelta(minutes=self.cfg.cooldown_minutes)
                self.consec_losses = 0
        else:
            self.consec_losses = 0

    def kill(self, reason: str) -> None:
        self.killed, self.kill_reason = True, reason

    # ------------------------------------------------------------------ persistance
    def to_dict(self) -> dict:
        return {"day": self.day.isoformat() if self.day else None,
                "day_start_equity": self.day_start_equity, "peak_equity": self.peak_equity,
                "trades_today": self.trades_today, "consec_losses": self.consec_losses,
                "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
                "killed": self.killed, "kill_reason": self.kill_reason}

    def load_dict(self, d: dict) -> None:
        from datetime import date
        self.day = date.fromisoformat(d["day"]) if d.get("day") else None
        self.day_start_equity = d.get("day_start_equity")
        self.peak_equity = d.get("peak_equity")
        self.trades_today = d.get("trades_today", 0)
        self.consec_losses = d.get("consec_losses", 0)
        cu = d.get("cooldown_until")
        self.cooldown_until = datetime.fromisoformat(cu) if cu else None
        self.killed = d.get("killed", False)
        self.kill_reason = d.get("kill_reason", "")
