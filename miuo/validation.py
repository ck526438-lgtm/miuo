"""Validation statistique de l'avantage (« edge gate »).

Protocole :
1. Les 20 % les plus récents des données (holdout) sont mis de côté et jamais utilisés pour choisir.
2. Walk-forward ancré sur le reste : pour chaque pli, les paramètres sont optimisés sur le passé
   puis testés sur la période suivante, jamais vue. Seuls ces trades hors échantillon comptent.
3. Tests sur les trades hors échantillon : nombre, profit factor, espérance en R, p-value bootstrap
   (seuil alpha divisé par le nombre de tentatives déjà faites sur ce marché : anti p-hacking),
   proportion de plis gagnants, drawdown Monte Carlo.
4. Les paramètres finaux (optimisés sur tout le pré-holdout) doivent aussi gagner sur le holdout.

Le trading sur compte RÉEL est refusé tant qu'un rapport valide et récent n'existe pas.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .backtest import run_backtest
from .config import MiuoConfig
from .data import data_fingerprint
from .stats import bootstrap_pvalue, monte_carlo_drawdown, summarize
from .strategy import get_strategy


@dataclass
class Check:
    name: str
    value: float
    threshold: str
    passed: bool


@dataclass
class EdgeReport:
    passed: bool
    symbol: str
    timeframe: str
    strategy: str
    deploy_params: dict
    checks: list = field(default_factory=list)
    oos_stats: dict = field(default_factory=dict)
    holdout_stats: dict = field(default_factory=dict)
    folds: list = field(default_factory=list)
    attempts: int = 1
    alpha_adjusted: float = 0.05
    p_value: float = 1.0
    data_start: str = ""
    data_end: str = ""
    data_bars: int = 0
    data_hash: str = ""
    created_at: str = ""
    valid_until: str = ""
    oos_equity_r: list = field(default_factory=list)
    ai_enabled: bool = False
    ai_model_file: str = ""
    ai_comparison: dict = field(default_factory=dict)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False, default=float),
                              encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "EdgeReport":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def summary(self) -> str:
        lines = [f"Miuo — rapport d'avantage statistique : {'VALIDÉ ✅' if self.passed else 'REFUSÉ ❌'}",
                 f"{self.symbol} {self.timeframe} | stratégie {self.strategy} | {self.data_bars} bougies "
                 f"({self.data_start[:10]} → {self.data_end[:10]})",
                 f"Paramètres déployés : {self.deploy_params}", ""]
        for c in self.checks:
            lines.append(f"  {'✔' if c['passed'] else '✘'} {c['name']:<38} {c['value']:>10.4f}   ({c['threshold']})")
        lines.append("")
        if self.ai_comparison.get("avec_ia"):
            a, b = self.ai_comparison["avec_ia"], self.ai_comparison["sans_ia"]
            lines.append(f"IA : {'ACTIVÉE' if self.ai_enabled else 'désactivée'} — hors échantillon "
                         f"{a['expectancy_r']:+.3f} R/trade avec IA ({a['trades']} trades) contre "
                         f"{b['expectancy_r']:+.3f} R sans ({b['trades']} trades)")
        lines.append(f"Tentative n°{self.attempts} sur ce marché → alpha ajusté {self.alpha_adjusted:.4f}")
        if self.passed:
            lines.append(f"Valide jusqu'au {self.valid_until[:10]}.")
        return "\n".join(lines)


def report_path(cfg: MiuoConfig) -> Path:
    return cfg.data_path / f"edge_report_{cfg.market.symbol}_{cfg.market.timeframe}.json"


def _ledger_attempts(cfg: MiuoConfig, ledger: Path | None, record: bool) -> int:
    ledger = ledger or (cfg.data_path / "validation_ledger.json")
    entries = json.loads(ledger.read_text(encoding="utf-8")) if ledger.exists() else []
    key = {"symbol": cfg.market.symbol, "timeframe": cfg.market.timeframe}
    n = sum(1 for e in entries if e.get("symbol") == key["symbol"] and e.get("timeframe") == key["timeframe"])
    if record:
        entries.append({**key, "strategy": cfg.strategy.name, "time": datetime.now().isoformat()})
        ledger.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    return n + 1


def _score(r: np.ndarray, min_trades: int = 20) -> float:
    if r.size < min_trades:
        return -np.inf
    s = r.std(ddof=1)
    return float(r.mean() / s * np.sqrt(r.size)) if s > 0 else -np.inf


def validate(df: pd.DataFrame, cfg: MiuoConfig, ledger: Path | None = None, record_attempt: bool = True,
             progress: Callable[[str], None] | None = None) -> EdgeReport:
    log = progress or (lambda msg: None)
    g = cfg.gate
    strat = get_strategy(cfg.strategy.name)
    grid = strat.grid()
    log(f"Calcul des signaux pour {len(grid)} combinaisons de paramètres…")
    sigs = [strat.signals(df, p) for p in grid]  # causaux : calcul unique sur tout l'historique

    n = len(df)
    hold_start = int(n * (1 - g.holdout_frac))
    bounds = np.linspace(0, hold_start, g.n_folds + 2).astype(int)

    def bt(k: int, a: int, b: int):
        return run_backtest(df.iloc[a:b], sigs[k].iloc[a:b], cfg, max_hold=int(grid[k].get("max_hold", 48)))

    def best_on(a: int, b: int) -> int:
        scores = [_score(bt(k, a, b).r) for k in range(len(grid))]
        return int(np.argmax(scores))

    use_ai_cfg = cfg.ai.enabled
    mf = None
    if use_ai_cfg:
        from .ai import AIFilter, market_features
        mf = market_features(df)

    def ai_run(k: int, a_train: int, a: int, b: int):
        """Entraîne l'IA sur [0, a_train) puis rejoue [a, b) avec les signaux filtrés."""
        ai = AIFilter(cfg.ai.min_edge_r)
        train = bt(k, 0, a_train)
        if not ai.fit(df.index, mf, sigs[k], train.trades):
            return None, ai
        fs = ai.apply(sigs[k], mf)
        return run_backtest(df.iloc[a:b], fs.iloc[a:b], cfg, max_hold=int(grid[k].get("max_hold", 48))), ai

    base_r, base_pnl, ai_r, ai_pnl, folds = [], [], [], [], []
    ai_complete = use_ai_cfg
    for f in range(1, g.n_folds + 1):
        a_test, b_test = bounds[f], bounds[f + 1]
        k = best_on(0, a_test)
        res = bt(k, a_test, b_test)
        st = summarize(res.r)
        fold = {"fold": f, "test_start": str(df.index[a_test]), "test_end": str(df.index[b_test - 1]),
                "params": {key: grid[k][key] for key in strat.param_grid}, **st}
        base_r.append(res.r)
        base_pnl.append(res.trades["pnl"].to_numpy())
        msg = f"Pli {f}/{g.n_folds} : {st['trades']} trades, espérance {st['expectancy_r']:+.3f} R"
        if use_ai_cfg:
            res_ai, _ = ai_run(k, a_test, a_test, b_test)
            if res_ai is None:  # pas assez d'historique pour apprendre : l'IA laisse tout passer
                res_ai = res
            sa = summarize(res_ai.r)
            fold["ai"] = sa
            ai_r.append(res_ai.r)
            ai_pnl.append(res_ai.trades["pnl"].to_numpy())
            msg += f" | avec IA : {sa['trades']} trades, {sa['expectancy_r']:+.3f} R"
        folds.append(fold)
        log(msg)

    cat = lambda xs: np.concatenate(xs) if xs else np.array([])  # noqa: E731
    base_stats = summarize(cat(base_r), cat(base_pnl))
    ai_stats = summarize(cat(ai_r), cat(ai_pnl)) if ai_complete else None
    use_ai = bool(ai_stats and ai_stats["trades"] >= g.min_trades
                  and ai_stats["expectancy_r"] > base_stats["expectancy_r"])
    if use_ai_cfg:
        log(f"IA {'ACTIVÉE' if use_ai else 'désactivée'} : espérance hors échantillon "
            f"{ai_stats['expectancy_r']:+.3f} R avec IA contre {base_stats['expectancy_r']:+.3f} R sans.")
    oos = cat(ai_r) if use_ai else cat(base_r)
    oos_stats = ai_stats if use_ai else base_stats
    for fo in folds:  # les plis gagnants se comptent sur le mode retenu
        fo["chosen_expectancy_r"] = fo["ai"]["expectancy_r"] if use_ai else fo["expectancy_r"]

    k_final = best_on(0, hold_start)
    hold = None
    if use_ai:
        hold, _ = ai_run(k_final, hold_start, hold_start, n)
    if hold is None:
        hold = bt(k_final, hold_start, n)
    hold_stats = summarize(hold.r)
    log(f"Holdout : {hold_stats['trades']} trades, espérance {hold_stats['expectancy_r']:+.3f} R")

    ai_model_file = ""
    if use_ai:  # modèle de production : appris sur tout l'historique
        final_ai = AIFilter(cfg.ai.min_edge_r)
        if final_ai.fit(df.index, mf, sigs[k_final], bt(k_final, 0, n).trades):
            path = cfg.data_path / f"miuo_ai_{cfg.market.symbol}_{cfg.market.timeframe}.pkl"
            final_ai.save(path)
            ai_model_file = path.name

    attempts = _ledger_attempts(cfg, ledger, record_attempt)
    alpha_adj = g.alpha / attempts
    p = bootstrap_pvalue(oos, g.bootstrap_iter)
    mc_dd = monte_carlo_drawdown(oos, cfg.risk.risk_per_trade, percentile=g.mc_dd_percentile)
    pos_folds = float(np.mean([fo["chosen_expectancy_r"] > 0 for fo in folds])) if folds else 0.0

    checks = [
        Check("Trades hors échantillon", oos_stats["trades"], f">= {g.min_trades}", oos_stats["trades"] >= g.min_trades),
        Check("Profit factor hors échantillon", min(oos_stats["profit_factor"], 99.0), f">= {g.min_profit_factor}",
              oos_stats["profit_factor"] >= g.min_profit_factor),
        Check("Espérance par trade (R)", oos_stats["expectancy_r"], f">= {g.min_expectancy_r}",
              oos_stats["expectancy_r"] >= g.min_expectancy_r),
        Check("p-value bootstrap", p, f"< {alpha_adj:.4f}", p < alpha_adj),
        Check("Plis walk-forward gagnants", pos_folds, f">= {g.min_positive_folds}", pos_folds >= g.min_positive_folds),
        Check(f"Drawdown Monte Carlo p{g.mc_dd_percentile:.0f}", mc_dd, f"<= {g.mc_max_drawdown}", mc_dd <= g.mc_max_drawdown),
        Check("Holdout : espérance (R)", hold_stats["expectancy_r"], "> 0",
              hold_stats["trades"] >= 10 and hold_stats["expectancy_r"] > 0),
    ]
    now = datetime.now()
    report = EdgeReport(
        passed=all(c.passed for c in checks), symbol=cfg.market.symbol, timeframe=cfg.market.timeframe,
        strategy=strat.name, deploy_params=grid[k_final], checks=[asdict(c) for c in checks],
        oos_stats=oos_stats, holdout_stats=hold_stats, folds=folds, attempts=attempts,
        alpha_adjusted=alpha_adj, p_value=p, data_start=str(df.index[0]), data_end=str(df.index[-1]),
        data_bars=n, data_hash=data_fingerprint(df), created_at=now.isoformat(),
        valid_until=(now + timedelta(days=g.validity_days)).isoformat(),
        oos_equity_r=np.cumsum(oos).round(4).tolist(),
        ai_enabled=bool(ai_model_file), ai_model_file=ai_model_file,
        ai_comparison={"sans_ia": base_stats, "avec_ia": ai_stats},
    )
    return report


def check_gate(cfg: MiuoConfig, account_is_demo: bool, path: Path | None = None) -> tuple[bool, str, EdgeReport | None]:
    """Autorise le trading. Démo : toujours (c'est du forward test). Réel : rapport validé et récent obligatoire."""
    path = path or report_path(cfg)
    rep = EdgeReport.load(path) if Path(path).exists() else None
    if rep is None:
        if account_is_demo:
            return True, "Compte DÉMO : forward test sans rapport (paramètres par défaut).", None
        return False, "Aucun rapport de validation : lance d'abord la validation.", None
    problems = []
    if not rep.passed:
        problems.append("l'avantage statistique n'a pas été prouvé")
    if rep.strategy != cfg.strategy.name:
        problems.append(f"le rapport concerne la stratégie {rep.strategy}")
    if datetime.fromisoformat(rep.valid_until) < datetime.now():
        problems.append("le rapport a expiré, relance la validation sur des données récentes")
    if not problems:
        return True, "Avantage statistique validé.", rep
    if account_is_demo:
        return True, "Compte DÉMO (forward test) — attention : " + "; ".join(problems), rep
    return False, "Trading RÉEL refusé : " + "; ".join(problems), rep
