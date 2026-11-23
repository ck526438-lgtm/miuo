"""Configuration de Miuo (sérialisée en JSON)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240}


@dataclass
class MarketConfig:
    symbol: str = "XAUUSD"
    timeframe: str = "M5"
    point: float = 0.01              # taille d'un point de prix
    contract_size: float = 100.0     # 1 lot d'or = 100 onces
    default_spread_points: int = 25  # utilisé si les données n'ont pas de colonne spread
    slippage_points: int = 5         # glissement défavorable appliqué à chaque exécution
    commission_per_lot: float = 7.0  # aller-retour, en devise du compte
    lot_step: float = 0.01
    min_lot: float = 0.01
    max_lot: float = 50.0


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.005        # 0,5 % du capital risqué par trade
    max_daily_loss: float = 0.03         # -3 % sur la journée => arrêt jusqu'au lendemain
    max_drawdown: float = 0.15           # -15 % depuis le plus haut => kill switch définitif
    max_trades_per_day: int = 4
    max_consecutive_losses: int = 4
    cooldown_minutes: int = 120          # pause après la série de pertes
    max_spread_points: int = 45          # pas d'entrée si le spread est trop large


@dataclass
class StrategyConfig:
    name: str = "session_breakout"
    params: dict = field(default_factory=dict)  # vide = paramètres issus de la validation


@dataclass
class GateConfig:
    """Seuils que la stratégie doit franchir hors échantillon pour être autorisée en réel."""
    min_trades: int = 100
    min_profit_factor: float = 1.15
    min_expectancy_r: float = 0.05
    alpha: float = 0.05                  # divisé par le nombre de tentatives (anti p-hacking)
    min_positive_folds: float = 0.6
    mc_dd_percentile: float = 95.0
    mc_max_drawdown: float = 0.25
    holdout_frac: float = 0.2
    n_folds: int = 5
    validity_days: int = 30
    bootstrap_iter: int = 5000


@dataclass
class BrokerConfig:
    login: int = 0
    password: str = ""
    server: str = ""
    terminal_path: str = ""
    magic: int = 52643801
    deviation_points: int = 20
    poll_seconds: float = 1.0


@dataclass
class NotifyConfig:
    telegram_token: str = ""
    telegram_chat_id: str = ""


@dataclass
class MiuoConfig:
    market: MarketConfig = field(default_factory=MarketConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    data_dir: str = "miuo_data"

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MiuoConfig":
        def build(kls, sub):
            known = {k: v for k, v in (sub or {}).items() if k in kls.__dataclass_fields__}
            return kls(**known)

        return cls(
            market=build(MarketConfig, d.get("market")),
            risk=build(RiskConfig, d.get("risk")),
            strategy=build(StrategyConfig, d.get("strategy")),
            gate=build(GateConfig, d.get("gate")),
            broker=build(BrokerConfig, d.get("broker")),
            notify=build(NotifyConfig, d.get("notify")),
            data_dir=d.get("data_dir", "miuo_data"),
        )

    def save(self, path: str | Path = "config.json") -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path = "config.json") -> "MiuoConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))
