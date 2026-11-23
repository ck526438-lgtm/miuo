"""Stratégies de Miuo.

Convention : le signal calculé à la clôture de la bougie t est exécuté à l'ouverture de t+1.
Chaque stratégie renvoie un DataFrame aligné sur les données :
    signal    : +1 achat, -1 vente, 0 rien
    stop_dist : distance du stop en prix (> 0 quand signal != 0)
    tp_r      : objectif en multiple du risque (R)
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

from .features import atr, ema, rsi


class Strategy:
    name = "base"
    description = ""
    default_params: dict = {}
    param_grid: dict = {}

    def params(self, overrides: dict | None = None) -> dict:
        p = dict(self.default_params)
        p.update(overrides or {})
        return p

    def grid(self) -> list[dict]:
        keys = list(self.param_grid)
        combos = itertools.product(*(self.param_grid[k] for k in keys))
        return [self.params(dict(zip(keys, c))) for c in combos]

    def signals(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError

    @staticmethod
    def _pack(df, long, short, stop_dist, tp_r) -> pd.DataFrame:
        sig = np.where(long, 1, np.where(short, -1, 0))
        sd = np.where(sig != 0, stop_dist, np.nan)
        out = pd.DataFrame({"signal": sig, "stop_dist": sd, "tp_r": float(tp_r)}, index=df.index)
        out.loc[~(out["stop_dist"] > 0), "signal"] = 0
        return out


class SessionBreakout(Strategy):
    """Cassure du range asiatique pendant la matinée européenne, dans le sens de la tendance de fond.

    Hypothèse d'avantage : l'arrivée de la liquidité de Londres prolonge les cassures du range nocturne.
    Les heures sont celles du serveur du broker.
    """
    name = "session_breakout"
    description = "Cassure du range de session (Asie -> Londres) filtrée par la tendance"
    default_params = {"range_start": 0, "range_end": 7, "trade_end": 12, "atr_n": 14,
                      "sl_atr": 1.5, "tp_r": 2.0, "ema_trend": 200, "max_hold": 48}
    param_grid = {"sl_atr": [1.0, 1.5, 2.0], "tp_r": [1.5, 2.0, 3.0], "trade_end": [11, 14]}

    def signals(self, df, params):
        p = self.params(params)
        hour = df.index.hour
        day = df.index.normalize()
        in_range = (hour >= p["range_start"]) & (hour < p["range_end"])
        rh = df["high"].where(in_range).groupby(day).cummax().groupby(day).ffill()
        rl = df["low"].where(in_range).groupby(day).cummin().groupby(day).ffill()
        window = (hour >= p["range_end"]) & (hour < p["trade_end"])
        trend = ema(df["close"], p["ema_trend"])
        c, pc = df["close"], df["close"].shift(1)
        long = window & (c > rh) & (pc <= rh) & (c > trend)
        short = window & (c < rl) & (pc >= rl) & (c < trend)
        # une seule cassure par jour
        first = (long | short).astype(int).groupby(day).cumsum() == 1
        long, short = long & first, short & first
        return self._pack(df, long.to_numpy(), short.to_numpy(),
                          (atr(df, p["atr_n"]) * p["sl_atr"]).to_numpy(), p["tp_r"])


class TrendPullback(Strategy):
    """Achat des replis sur la moyenne rapide dans une tendance établie (et inversement)."""
    name = "trend_pullback"
    description = "Repli sur EMA rapide dans la tendance EMA lente"
    default_params = {"ema_fast": 20, "ema_slow": 100, "atr_n": 14, "rsi_n": 14,
                      "sl_atr": 1.5, "tp_r": 2.0, "max_hold": 48}
    param_grid = {"sl_atr": [1.0, 1.5, 2.0], "tp_r": [1.5, 2.0, 3.0], "ema_fast": [20, 50]}

    def signals(self, df, params):
        p = self.params(params)
        f, s = ema(df["close"], p["ema_fast"]), ema(df["close"], p["ema_slow"])
        r = rsi(df["close"], p["rsi_n"])
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        long = (f > s) & (l <= f) & (c > f) & (c > o) & (r < 70)
        short = (f < s) & (h >= f) & (c < f) & (c < o) & (r > 30)
        # éviter les signaux en rafale : un signal seulement si le précédent est absent
        long &= ~long.shift(1, fill_value=False)
        short &= ~short.shift(1, fill_value=False)
        return self._pack(df, long.to_numpy(), short.to_numpy(),
                          (atr(df, p["atr_n"]) * p["sl_atr"]).to_numpy(), p["tp_r"])


STRATEGIES: dict[str, Strategy] = {s.name: s for s in (SessionBreakout(), TrendPullback())}


def get_strategy(name: str) -> Strategy:
    if name not in STRATEGIES:
        raise KeyError(f"Stratégie inconnue : {name}. Disponibles : {list(STRATEGIES)}")
    return STRATEGIES[name]
