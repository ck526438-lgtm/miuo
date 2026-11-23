"""Moteur de trading en temps réel (identique au backtest : signal à la clôture, entrée à la bougie suivante)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

from .config import MiuoConfig
from .execution import PaperBroker, bars_between
from .notify import Notifier
from .risk import RiskManager
from .strategy import get_strategy
from .validation import check_gate


class LiveEngine:
    def __init__(self, cfg: MiuoConfig, broker, log: Callable[[str], None] = print,
                 allow_real: bool = False, report_file: Path | None = None):
        self.cfg = cfg
        self.broker = broker
        self.log = log
        self.allow_real = allow_real
        self.report_file = report_file
        self.notifier = Notifier(cfg.notify)
        self.strategy = get_strategy(cfg.strategy.name)
        self.params = self.strategy.params(cfg.strategy.params)
        self.risk = RiskManager(cfg.risk, cfg.market)
        self.stop_event = threading.Event()
        self.last_bar = None
        self.open_tickets: set[int] = set()
        self.ready = False
        self.state_file = cfg.data_path / f"live_state_{cfg.market.symbol}_{cfg.broker.magic}.json"
        self.stop_file = cfg.data_path / "STOP"

    # ------------------------------------------------------------------ démarrage
    def prepare(self) -> bool:
        self.broker.connect()
        acct = self.broker.account()
        kind = "DÉMO" if acct["is_demo"] else "RÉEL"
        self.log(f"Connecté : compte {kind} {acct.get('login')} @ {acct.get('server')} — equity {acct['equity']:.2f} {acct['currency']}")
        ok, msg, rep = check_gate(self.cfg, acct["is_demo"], self.report_file)
        self.log(msg)
        if not ok:
            return False
        if not acct["is_demo"] and not self.allow_real:
            self.log("Compte RÉEL détecté : confirmation explicite requise (option « autoriser le réel »).")
            return False
        if rep is not None and rep.passed and rep.strategy == self.strategy.name:
            self.params = self.strategy.params(rep.deploy_params)
            self.log(f"Paramètres validés chargés : {rep.deploy_params}")
        self._load_state()
        for p in self.broker.positions():  # réconciliation après redémarrage
            if not p["sl"]:
                self.log(f"Position {p['ticket']} sans stop : fermeture de sécurité.")
                self.broker.close_position(p)
            else:
                self.open_tickets.add(p["ticket"])
                self.log(f"Position existante reprise : {p['ticket']}")
        if self.stop_file.exists():
            self.stop_file.unlink()
        self.ready = True
        self.notifier.send(f"Démarrage {self.cfg.market.symbol} {self.cfg.market.timeframe} ({kind})")
        return True

    # ------------------------------------------------------------------ boucle
    def step(self) -> None:
        if self.stop_event.is_set() or self.stop_file.exists():
            self.kill("Arrêt d'urgence demandé")
            return
        bars = self.broker.bars(600)
        if len(bars) < 250:
            return
        now = bars.index[-1]
        acct = self.broker.account()
        positions = self.broker.positions()
        self._sync_closed(positions, now)

        if now == self.last_bar:
            return
        self.last_bar = now
        self.risk.update(now.to_pydatetime(), acct["equity"])
        if self.risk.killed:
            self.kill(self.risk.kill_reason)
            return

        for p in positions:  # sortie temporelle, comme en backtest
            if bars_between(p["time"], now, self.cfg.market.timeframe) + 1 >= int(self.params.get("max_hold", 48)):
                r = self.broker.close_position(p)
                self.log(f"Sortie temporelle {p['ticket']} : {r.message}")
        positions = self.broker.positions()
        if positions:
            self._save_state()
            return

        sig = self.strategy.signals(bars, self.params).iloc[-1]
        if sig["signal"] == 0:
            return
        direction = int(sig["signal"])
        ok, reason = self.risk.can_trade(now.to_pydatetime(), acct["equity"], self.broker.spread_points())
        if not ok:
            self.log(f"Signal {'ACHAT' if direction == 1 else 'VENTE'} ignoré : {reason}")
            return
        lots = self.risk.lot_size(acct["equity"], float(sig["stop_dist"]))
        if lots <= 0:
            self.log("Signal ignoré : capital insuffisant pour respecter le risque avec le lot minimum.")
            return
        res = self.broker.open_market(direction, lots, float(sig["stop_dist"]), float(sig["tp_r"]))
        if res.ok:
            self.risk.on_trade_opened()
            self.open_tickets.add(res.ticket)
            msg = (f"{'ACHAT' if direction == 1 else 'VENTE'} {lots} lot(s) {self.cfg.market.symbol} @ {res.price:.2f} "
                   f"(stop {sig['stop_dist']:.2f}, objectif {sig['tp_r']}R) — ticket {res.ticket}")
            self.log(msg)
            self.notifier.send(msg)
        else:
            self.log(f"Ordre refusé : {res.message}")
            self.notifier.send(f"⚠ Ordre refusé : {res.message}")
        self._save_state()

    def _sync_closed(self, positions, now) -> None:
        live = {p["ticket"] for p in positions}
        for t in list(self.open_tickets - live):
            pnl = self.broker.closed_pnl(t)
            if pnl is None:
                continue
            self.open_tickets.discard(t)
            self.risk.on_trade_closed(pnl, now.to_pydatetime())
            msg = f"Trade {t} clôturé : {pnl:+.2f}"
            self.log(msg)
            self.notifier.send(msg)
        self.open_tickets |= live

    def kill(self, reason: str) -> None:
        for p in self.broker.positions():
            r = self.broker.close_position(p)
            self.log(f"Fermeture {p['ticket']} : {r.message}")
        self.risk.kill(reason)
        self.stop_event.set()
        self._save_state()
        self.log(f"⛔ Miuo arrêté : {reason}")
        self.notifier.send(f"⛔ Arrêt : {reason}")

    def stop(self) -> None:
        """Arrêt normal : on laisse les positions protégées par leur stop côté serveur."""
        self.stop_event.set()

    def run(self) -> None:
        if not self.ready and not self.prepare():
            return
        paper = isinstance(self.broker, PaperBroker)
        while not self.stop_event.is_set():
            try:
                self.step()
            except Exception as e:  # une erreur ne doit jamais tuer la boucle silencieusement
                self.log(f"Erreur : {e}")
                time.sleep(5)
            if paper:
                if not self.broker.advance():
                    break
            else:
                time.sleep(self.cfg.broker.poll_seconds)
        self._save_state()
        self.log("Boucle terminée.")

    # ------------------------------------------------------------------ état
    def _save_state(self) -> None:
        state = {"risk": self.risk.to_dict(), "open_tickets": sorted(self.open_tickets),
                 "last_bar": str(self.last_bar) if self.last_bar is not None else None}
        self.state_file.write_text(json.dumps(state, indent=1), encoding="utf-8")

    def _load_state(self) -> None:
        if self.state_file.exists():
            s = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.risk.load_dict(s.get("risk", {}))
            self.open_tickets = set(s.get("open_tickets", []))
            if self.risk.killed:
                self.log(f"Kill switch actif depuis la dernière session : {self.risk.kill_reason}. "
                         f"Supprime {self.state_file.name} pour réarmer après analyse.")
