"""IA de Miuo : filtre des signaux par machine learning (méta-labeling).

La stratégie propose un trade ; l'IA estime la probabilité qu'il atteigne son objectif à partir de
l'état du marché (tendance multi-horizon, volatilité, momentum, niveaux de la veille, heure).
Le trade n'est pris que si l'espérance prévue p*TP - (1-p) dépasse `min_edge_r`.

Le modèle est toujours entraîné sur le passé et jugé sur le futur (walk-forward) : s'il n'améliore
pas les résultats hors échantillon, la validation le désactive.
"""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from .features import atr, ema, rsi

DIRECTIONAL = ["dist_ema20", "dist_ema240", "dist_ema960", "slope240", "slope960", "ret12", "ret48",
               "body", "rsi_c", "dist_pdh", "dist_pdl"]
NEUTRAL = ["atr_rank", "range_bar", "hour_sin", "hour_cos", "dow"]
FEATURES = [f"d_{c}" for c in DIRECTIONAL] + NEUTRAL + ["tp_r"]


def market_features(df: pd.DataFrame) -> pd.DataFrame:
    """Variables de marché, toutes causales. Sur M5 : EMA 240 ≈ EMA 20 en H1, EMA 960 ≈ EMA 20 en H4."""
    c, o, h, l = df["close"], df["open"], df["high"], df["low"]
    a = atr(df, 14).replace(0, np.nan)
    e20, e240, e960 = ema(c, 20), ema(c, 240), ema(c, 960)
    day = df.index.normalize()
    daily = df.groupby(day).agg(dh=("high", "max"), dl=("low", "min")).shift(1)
    pdh = pd.Series(daily["dh"].reindex(day).to_numpy(), index=df.index)
    pdl = pd.Series(daily["dl"].reindex(day).to_numpy(), index=df.index)
    hour = df.index.hour + df.index.minute / 60
    r14 = rsi(c, 14)
    f = pd.DataFrame({
        "dist_ema20": (c - e20) / a, "dist_ema240": (c - e240) / a, "dist_ema960": (c - e960) / a,
        "slope240": (e240 - e240.shift(12)) / a, "slope960": (e960 - e960.shift(48)) / a,
        "ret12": (c - c.shift(12)) / a, "ret48": (c - c.shift(48)) / a,
        "body": (c - o) / a, "rsi_c": r14 / 100 - 0.5,
        "dist_pdh": (c - pdh) / a, "dist_pdl": (c - pdl) / a,
        "atr_rank": a.rolling(500, min_periods=100).rank(pct=True),
        "range_bar": (h - l) / a,
        "hour_sin": np.sin(2 * np.pi * hour / 24), "hour_cos": np.cos(2 * np.pi * hour / 24),
        "dow": df.index.dayofweek / 4.0,
        "atr": a, "ema20": e20, "ema240": e240, "ema960": e960, "pdh": pdh, "pdl": pdl, "rsi": r14,
    }, index=df.index)
    return f


def signal_matrix(mf: pd.DataFrame, direction: np.ndarray, tp_r: np.ndarray) -> pd.DataFrame:
    X = pd.DataFrame(index=mf.index)
    d = np.asarray(direction, float)
    for col in DIRECTIONAL:
        X[f"d_{col}"] = mf[col].to_numpy() * d
    for col in NEUTRAL:
        X[col] = mf[col].to_numpy()
    X["tp_r"] = np.asarray(tp_r, float)
    return X[FEATURES]


class AIFilter:
    MIN_SAMPLES = 60

    def __init__(self, min_edge_r: float = 0.05):
        self.min_edge_r = min_edge_r
        self.model = None
        self.n_train = 0
        self.base_rate = None

    @property
    def ready(self) -> bool:
        return self.model is not None

    def threshold(self, tp_r) -> np.ndarray:
        return (1 + self.min_edge_r) / (np.asarray(tp_r, float) + 1)

    def fit(self, df_index: pd.DatetimeIndex, mf: pd.DataFrame, sig: pd.DataFrame, trades: pd.DataFrame) -> bool:
        """Apprend sur les trades passés : quelle situation de marché a mené à un gain ?"""
        from sklearn.ensemble import HistGradientBoostingClassifier

        self.model = None
        if len(trades) < self.MIN_SAMPLES:
            return False
        pos = df_index.get_indexer(pd.DatetimeIndex(trades["entry_time"])) - 1  # bougie du signal
        ok = pos >= 0
        pos = pos[ok]
        y = (trades["r"].to_numpy()[ok] > 0).astype(int)
        if len(np.unique(y)) < 2:
            return False
        X = signal_matrix(mf.iloc[pos], trades["direction"].to_numpy()[ok], sig["tp_r"].to_numpy()[pos])
        self.model = HistGradientBoostingClassifier(max_depth=3, max_iter=200, learning_rate=0.05,
                                                    min_samples_leaf=20, l2_regularization=1.0,
                                                    random_state=0)
        self.model.fit(X.to_numpy(), y)
        self.n_train, self.base_rate = int(len(y)), float(y.mean())
        return True

    def proba(self, mf: pd.DataFrame, direction, tp_r) -> np.ndarray:
        X = signal_matrix(mf, direction, tp_r).to_numpy()
        return self.model.predict_proba(X)[:, 1]

    def apply(self, sig: pd.DataFrame, mf: pd.DataFrame) -> pd.DataFrame:
        """Retire les signaux dont l'espérance prévue est insuffisante ; ajoute la colonne ai_p."""
        out = sig.copy()
        out["ai_p"] = np.nan
        if not self.ready:
            return out
        idx = np.flatnonzero(out["signal"].to_numpy() != 0)
        if idx.size == 0:
            return out
        p = self.proba(mf.iloc[idx], out["signal"].to_numpy()[idx], out["tp_r"].to_numpy()[idx])
        keep = p >= self.threshold(out["tp_r"].to_numpy()[idx])
        sigcol = out["signal"].to_numpy().copy()
        sigcol[idx[~keep]] = 0
        out["signal"] = sigcol
        ai_p = out["ai_p"].to_numpy().copy()
        ai_p[idx] = p
        out["ai_p"] = ai_p
        return out

    def save(self, path: str | Path) -> None:
        Path(path).write_bytes(pickle.dumps({"model": self.model, "min_edge_r": self.min_edge_r,
                                             "n_train": self.n_train, "base_rate": self.base_rate}))

    @classmethod
    def load(cls, path: str | Path) -> "AIFilter":
        d = pickle.loads(Path(path).read_bytes())
        f = cls(d["min_edge_r"])
        f.model, f.n_train, f.base_rate = d["model"], d["n_train"], d["base_rate"]
        return f
