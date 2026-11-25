"""Analyse de marché lisible (affichée et lue par la voix off)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .ai import market_features

ASSET_NAMES = {"XAUUSD": "l'or", "XAGUSD": "l'argent", "EURUSD": "l'euro dollar", "GBPUSD": "la livre dollar",
               "USDJPY": "le dollar yen", "BTCUSD": "le bitcoin", "US30": "le Dow Jones", "NAS100": "le Nasdaq"}


def fr(x: float, d: int = 2) -> str:
    """Nombre au format français (virgule décimale), lisible par la synthèse vocale."""
    return f"{x:.{d}f}".replace(".", ",")


def asset_name(symbol: str) -> str:
    s = symbol.upper()
    for k, v in ASSET_NAMES.items():
        if s.startswith(k):
            return v
    return symbol


def _trend(dist: float, slope: float) -> str:
    if dist > 0.3 and slope > 0:
        return "haussière"
    if dist < -0.3 and slope < 0:
        return "baissière"
    return "sans direction claire"


def _session(hour: int) -> str:
    if 1 <= hour < 9:
        return "session asiatique"
    if 9 <= hour < 15:
        return "session de Londres"
    if 15 <= hour < 19:
        return "chevauchement Londres et New York"
    return "session de New York"


@dataclass
class MarketView:
    symbol: str
    time: pd.Timestamp
    price: float
    trend_h4: str
    trend_h1: str
    trend_m5: str
    volatility: str
    atr: float
    rsi: float
    rsi_label: str
    session: str
    pdh: float
    pdl: float
    day_high: float
    day_low: float

    def bias(self) -> str:
        votes = [self.trend_h4, self.trend_h1]
        if all(v == "haussière" for v in votes):
            return "acheteur"
        if all(v == "baissière" for v in votes):
            return "vendeur"
        return "neutre"

    def text(self) -> str:
        lines = [f"{self.symbol} — {self.time:%d/%m %H:%M} — prix {fr(self.price)}",
                 f"Tendance  H4 : {self.trend_h4}   H1 : {self.trend_h1}   M5 : {self.trend_m5}",
                 f"Biais : {self.bias()}",
                 f"Volatilité : {self.volatility} (ATR {fr(self.atr)})",
                 f"RSI 14 : {self.rsi:.0f} ({self.rsi_label})",
                 f"Veille : haut {fr(self.pdh)} / bas {fr(self.pdl)}",
                 f"Aujourd'hui : haut {fr(self.day_high)} / bas {fr(self.day_low)}",
                 f"Moment : {self.session}"]
        return "\n".join(lines)

    def speech(self) -> str:
        name = asset_name(self.symbol)
        parts = [f"Point sur {name} à {fr(self.price)}.",
                 f"Tendance {self.trend_h4} en H4 et {self.trend_h1} en H1, biais {self.bias()}.",
                 f"Volatilité {self.volatility}, RSI à {self.rsi:.0f}, {self.rsi_label}."]
        if np.isfinite(self.pdh) and np.isfinite(self.pdl):
            near = "plus haut" if abs(self.price - self.pdh) < abs(self.price - self.pdl) else "plus bas"
            lvl = self.pdh if near == "plus haut" else self.pdl
            parts.append(f"Niveau le plus proche : le {near} de la veille à {fr(lvl)}.")
        parts.append(f"Nous sommes en {self.session}.")
        return " ".join(parts)


def analyze(bars: pd.DataFrame, symbol: str, mf: pd.DataFrame | None = None) -> MarketView:
    mf = mf if mf is not None else market_features(bars)
    last = mf.iloc[-1]
    rank = last["atr_rank"]
    vol = "faible" if rank < 0.3 else "élevée" if rank > 0.75 else "normale"
    r = float(last["rsi"])
    rlab = "zone de surachat" if r >= 70 else "zone de survente" if r <= 30 else "neutre"
    t = bars.index[-1]
    today = bars[bars.index.normalize() == t.normalize()]
    return MarketView(
        symbol=symbol, time=t, price=float(bars["close"].iloc[-1]),
        trend_h4=_trend(last["dist_ema960"], last["slope960"]),
        trend_h1=_trend(last["dist_ema240"], last["slope240"]),
        trend_m5=_trend(last["dist_ema20"], last["ret12"]),
        volatility=vol, atr=float(last["atr"]), rsi=r, rsi_label=rlab, session=_session(t.hour),
        pdh=float(last["pdh"]), pdl=float(last["pdl"]),
        day_high=float(today["high"].max()), day_low=float(today["low"].min()),
    )


def trade_speech(symbol: str, direction: int, price: float, lots: float, sl: float, tp: float, tp_r: float,
                 reason: str, view: MarketView | None, ai_p: float | None, with_analysis: bool = True) -> str:
    side = "Achat" if direction == 1 else "Vente"
    s = [f"{side} sur {asset_name(symbol)} à {fr(price)}, {fr(lots)} lot.",
         f"Stop à {fr(sl)}, objectif à {fr(tp)}, rapport gain sur risque de {fr(tp_r, 1)} pour 1.",
         f"Raison : {reason}."]
    if with_analysis and view is not None:
        s.append(f"Tendance {view.trend_h4} en H4 et {view.trend_h1} en H1, volatilité {view.volatility}, "
                 f"RSI à {view.rsi:.0f}, en {view.session}.")
    if ai_p is not None and np.isfinite(ai_p):
        exp = ai_p * tp_r - (1 - ai_p)
        s.append(f"Confiance de l'IA : {ai_p * 100:.0f} pour cent, espérance prévue {fr(exp, 2)} R.")
    return " ".join(s)
