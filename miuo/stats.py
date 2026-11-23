"""Statistiques de performance et tests de significativité."""
from __future__ import annotations

import numpy as np


def max_drawdown(equity: np.ndarray) -> float:
    equity = np.asarray(equity, float)
    if equity.size == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    return float(np.max((peak - equity) / peak))


def summarize(r: np.ndarray, pnl: np.ndarray | None = None) -> dict:
    r = np.asarray(r, float)
    n = int(r.size)
    if n == 0:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "expectancy_r": 0.0,
                "sharpe_r": 0.0, "t_stat": 0.0, "total_r": 0.0}
    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    std = r.std(ddof=1) if n > 1 else 0.0
    return {
        "trades": n,
        "win_rate": float((r > 0).mean()),
        "profit_factor": float(gains / losses) if losses > 0 else float("inf"),
        "expectancy_r": float(r.mean()),
        "sharpe_r": float(r.mean() / std) if std > 0 else 0.0,
        "t_stat": float(r.mean() / std * np.sqrt(n)) if std > 0 else 0.0,
        "total_r": float(r.sum()),
        "total_pnl": float(np.sum(pnl)) if pnl is not None else None,
    }


def bootstrap_pvalue(r: np.ndarray, iters: int = 5000, seed: int = 0) -> float:
    """p-value unilatérale de H0 « espérance <= 0 » par bootstrap recentré."""
    r = np.asarray(r, float)
    if r.size < 2:
        return 1.0
    rng = np.random.default_rng(seed)
    obs = r.mean()
    centered = r - obs
    idx = rng.integers(0, r.size, size=(iters, r.size))
    boot = centered[idx].mean(axis=1)
    return float((np.sum(boot >= obs) + 1) / (iters + 1))


def monte_carlo_drawdown(r: np.ndarray, risk_per_trade: float, iters: int = 2000,
                         percentile: float = 95.0, seed: int = 1) -> float:
    """Drawdown max au percentile donné, en rééchantillonnant l'ordre et la composition des trades."""
    r = np.asarray(r, float)
    if r.size == 0:
        return 0.0
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, r.size, size=(iters, r.size))
    growth = np.clip(1 + r[idx] * risk_per_trade, 1e-9, None)
    eq = np.cumprod(growth, axis=1)
    eq = np.concatenate([np.ones((iters, 1)), eq], axis=1)
    peak = np.maximum.accumulate(eq, axis=1)
    dd = np.max((peak - eq) / peak, axis=1)
    return float(np.percentile(dd, percentile))
