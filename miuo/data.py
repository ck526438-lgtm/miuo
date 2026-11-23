"""Chargement des données OHLC (CSV exporté de MT5, CSV simple) et générateurs synthétiques pour les tests."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

REQUIRED = ["open", "high", "low", "close"]


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().strip("<>").lower() for c in df.columns]
    if "date" in df.columns and "time" in df.columns:
        # Export MT5 : <DATE> 2024.01.02  <TIME> 01:05:00
        df["datetime"] = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str),
                                        format="mixed")
    elif "time" in df.columns:
        t = df["time"]
        df["datetime"] = pd.to_datetime(t, unit="s") if np.issubdtype(t.dtype, np.number) \
            else pd.to_datetime(t, format="mixed")
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], format="mixed")
    else:
        raise ValueError("Colonne de date introuvable (attendu: time, datetime ou date+time)")
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"Colonnes manquantes : {missing}")
    keep = REQUIRED + [c for c in ("spread", "tick_volume", "tickvol") if c in df.columns]
    out = df.set_index("datetime")[keep].astype(float)
    if "tickvol" in out.columns:
        out = out.rename(columns={"tickvol": "tick_volume"})
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.dropna(subset=REQUIRED)
    bad = (out["high"] < out[["open", "close"]].max(axis=1)) | (out["low"] > out[["open", "close"]].min(axis=1))
    return out[~bad]


def load_csv(path: str | Path) -> pd.DataFrame:
    """Charge un CSV (séparateur auto : virgule, point-virgule ou tabulation)."""
    df = pd.read_csv(path, sep=None, engine="python")
    return _normalize(df)


def from_mt5_rates(rates) -> pd.DataFrame:
    """Convertit le tableau numpy renvoyé par mt5.copy_rates_* en DataFrame."""
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df.rename(columns={"time": "datetime"})
    return _normalize(df)


def data_fingerprint(df: pd.DataFrame) -> str:
    h = hashlib.sha256()
    h.update(str(len(df)).encode())
    h.update(str(df.index[0]).encode() + str(df.index[-1]).encode())
    h.update(np.ascontiguousarray(df["close"].to_numpy()).tobytes())
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- synthétique
def synthetic(n_bars: int = 60_000, freq: str = "5min", start_price: float = 2000.0,
              vol: float = 0.01, regime_drift: float = 0.0, regime_len: int = 300,
              seed: int = 0, spread_points: int = 25) -> pd.DataFrame:
    """Série OHLC 24h/5j.

    regime_drift = 0  -> marche aléatoire pure : aucune stratégie ne doit y trouver d'avantage.
    regime_drift > 0  -> tendances persistantes plantées : une stratégie de cassure doit les capter.
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-06", periods=int(n_bars * 1.5), freq=freq)
    idx = idx[idx.dayofweek < 5][:n_bars]
    sigma = vol * np.sqrt(int(pd.Timedelta(freq).total_seconds() // 60) / 1440)
    drift = np.zeros(n_bars)
    if regime_drift:
        n_reg = n_bars // regime_len + 1
        signs = rng.choice([-1.0, 1.0], size=n_reg)
        drift = np.repeat(signs, regime_len)[:n_bars] * regime_drift * sigma
    rets = drift + sigma * rng.standard_normal(n_bars)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]])
    wick = np.abs(rng.standard_normal((2, n_bars))) * sigma * close * 0.5
    high = np.maximum(open_, close) + wick[0]
    low = np.minimum(open_, close) - wick[1]
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "spread": float(spread_points)}, index=idx)
