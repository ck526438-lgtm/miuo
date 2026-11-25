import numpy as np
import pandas as pd
import pytest

from miuo.backtest import run_backtest
from miuo.config import MiuoConfig
from miuo.data import load_csv, synthetic
from miuo.execution import PaperBroker
from miuo.live import LiveEngine
from miuo.risk import RiskManager
from miuo.stats import bootstrap_pvalue, max_drawdown, monte_carlo_drawdown
from miuo.strategy import STRATEGIES
from miuo.validation import EdgeReport, check_gate, validate


@pytest.fixture
def cfg(tmp_path):
    c = MiuoConfig()
    c.data_dir = str(tmp_path)
    c.broker.live_bars = 600
    return c


def silent_voice():
    from miuo.voice import Voice
    return Voice(speaker=lambda t: None)


# ---------------------------------------------------------------- aucune fuite du futur
@pytest.mark.parametrize("name", list(STRATEGIES))
def test_signals_are_causal(name):
    df = synthetic(6000, regime_drift=0.25, seed=1)
    s = STRATEGIES[name]
    full = s.signals(df, s.params())
    for cut in (2500, 4100, 5999):
        part = s.signals(df.iloc[:cut], s.params())
        pd.testing.assert_series_equal(full["signal"].iloc[:cut], part["signal"], check_names=False)


# ---------------------------------------------------------------- risque
def test_lot_size_never_exceeds_risk(cfg):
    rm = RiskManager(cfg.risk, cfg.market)
    for stop in (0.5, 2.0, 7.3, 25.0):
        lots = rm.lot_size(10_000, stop)
        loss = lots * (stop * cfg.market.contract_size + cfg.market.commission_per_lot)
        assert loss <= 10_000 * cfg.risk.risk_per_trade + 1e-9
    assert rm.lot_size(100, 30.0) == 0.0  # trop petit compte : on ne force pas le lot minimum


def test_daily_loss_and_drawdown_kill(cfg):
    from datetime import datetime
    rm = RiskManager(cfg.risk, cfg.market)
    t = datetime(2024, 1, 2, 9)
    assert rm.can_trade(t, 10_000, 20)[0]
    assert not rm.can_trade(t, 9_600, 20)[0]           # -4 % sur la journée
    assert not rm.can_trade(t, 10_000, 99)[0]          # spread trop large
    rm.update(datetime(2024, 1, 3, 9), 8_400)          # -16 % depuis le pic
    assert rm.killed and not rm.can_trade(datetime(2024, 1, 4), 8_400, 20)[0]


def test_consecutive_losses_cooldown(cfg):
    from datetime import datetime, timedelta
    rm = RiskManager(cfg.risk, cfg.market)
    t = datetime(2024, 1, 2, 9)
    for _ in range(cfg.risk.max_consecutive_losses):
        rm.on_trade_closed(-10, t)
    assert not rm.can_trade(t + timedelta(minutes=5), 10_000, 20)[0]
    assert rm.can_trade(t + timedelta(minutes=cfg.risk.cooldown_minutes + 1), 10_000, 20)[0]


# ---------------------------------------------------------------- backtest
def test_costs_make_random_entries_lose(cfg):
    df = synthetic(20_000, seed=7)
    rng = np.random.default_rng(0)
    sig = pd.DataFrame({"signal": rng.choice([-1, 0, 0, 0, 1], len(df)), "stop_dist": 2.0, "tp_r": 1.0}, index=df.index)
    res = run_backtest(df, sig, cfg)
    assert len(res.trades) > 200
    assert res.trades["r"].mean() < 0  # spread + slippage + commission => perdant en moyenne


def test_stop_has_priority_when_both_hit(cfg):
    idx = pd.date_range("2024-01-02", periods=4, freq="5min")
    df = pd.DataFrame({"open": [100, 100, 100, 100], "high": [100.5, 110, 100.5, 100.5],
                       "low": [99.5, 90, 99.5, 99.5], "close": [100, 100, 100, 100], "spread": 0}, index=idx)
    sig = pd.DataFrame({"signal": [1, 0, 0, 0], "stop_dist": [2.0, np.nan, np.nan, np.nan], "tp_r": 2.0}, index=idx)
    cfg.market.slippage_points = 0
    t = run_backtest(df, sig, cfg).trades
    assert len(t) == 1 and t.iloc[0]["reason"] == "sl" and t.iloc[0]["r"] < 0


# ---------------------------------------------------------------- statistiques
def test_stats():
    rng = np.random.default_rng(0)
    assert bootstrap_pvalue(rng.normal(0, 1, 500)) > 0.05
    assert bootstrap_pvalue(rng.normal(0.3, 1, 500)) < 0.01
    assert max_drawdown(np.array([100, 120, 90, 130])) == pytest.approx(0.25)
    assert 0 < monte_carlo_drawdown(rng.normal(0.1, 1, 300), 0.01) < 1


# ---------------------------------------------------------------- edge gate
def test_gate_rejects_random_walk(cfg):
    rep = validate(synthetic(80_000, regime_drift=0.0, seed=11), cfg)
    assert not rep.passed


def test_gate_accepts_planted_edge(cfg):
    rep = validate(synthetic(80_000, regime_drift=0.25, seed=3), cfg)
    assert rep.passed, rep.summary()
    path = cfg.data_path / "r.json"
    rep.save(path)
    assert EdgeReport.load(path).passed
    assert check_gate(cfg, account_is_demo=False, path=path)[0]


def test_attempts_tighten_alpha(cfg):
    df = synthetic(30_000, seed=2)
    a1 = validate(df, cfg).alpha_adjusted
    a2 = validate(df, cfg).alpha_adjusted
    assert a2 == pytest.approx(a1 / 2)


def test_real_account_blocked_without_report(cfg):
    assert not check_gate(cfg, account_is_demo=False)[0]
    assert check_gate(cfg, account_is_demo=True)[0]


# ---------------------------------------------------------------- moteur temps réel
def test_live_engine_matches_backtest_and_never_doubles(cfg):
    df = synthetic(1600, regime_drift=0.25, seed=5)
    cfg.market.slippage_points = 0
    broker = PaperBroker(df, cfg)
    LiveEngine(cfg, broker, log=lambda m: None, voice=silent_voice()).run()
    s = STRATEGIES[cfg.strategy.name]
    p = s.params()
    bt = run_backtest(df.iloc[300:], s.signals(df, p).iloc[300:], cfg, p["max_hold"])
    assert len(broker._closed) == len(bt.trades)
    assert broker.balance == pytest.approx(10_000 + bt.trades["pnl"].sum(), rel=1e-3)
    assert broker.orders_sent == len(broker._closed)  # aucun ordre en double


def test_kill_switch_file_closes_everything(cfg):
    df = synthetic(1500, regime_drift=0.25, seed=5)
    broker = PaperBroker(df, cfg)
    eng = LiveEngine(cfg, broker, log=lambda m: None, voice=silent_voice())
    assert eng.prepare()
    broker._pos = {"ticket": 99, "direction": 1, "volume": 0.1, "price_open": 2000.0, "sl": 1990.0,
                   "tp": 2020.0, "time": df.index[300], "profit": 0}
    eng.stop_file.write_text("stop")
    eng.step()
    assert broker.positions() == [] and eng.risk.killed and eng.stop_event.is_set()


def test_load_mt5_export(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
                 "2024.01.02\t01:00:00\t2063.1\t2064.0\t2062.5\t2063.8\t812\t0\t25\n"
                 "2024.01.02\t01:05:00\t2063.8\t2065.2\t2063.0\t2064.9\t903\t0\t24\n")
    df = load_csv(p)
    assert len(df) == 2 and df["spread"].iloc[1] == 24 and str(df.index[0]) == "2024-01-02 01:00:00"
