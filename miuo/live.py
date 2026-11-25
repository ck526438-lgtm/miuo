"""Moteur de trading en temps réel, automatique.

Identique au backtest : signal à la clôture d'une bougie, entrée à l'ouverture de la suivante.
- connexion automatique à MT5 et reconnexion sans fin (attente progressive) en cas de coupure ;
- filtre IA chargé depuis le rapport de validation ;
- analyse de marché à chaque bougie, voix off pour chaque trade, clôture et alerte.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .analysis import analyze, asset_name, fr, trade_speech
from .config import MiuoConfig
from .execution import BrokerError, PaperBroker, bars_between
from .notify import Notifier
from .risk import RiskManager
from .strategy import get_strategy
from .validation import check_gate
from .voice import Voice


class LiveEngine:
    def __init__(self, cfg: MiuoConfig, broker, log: Callable[[str], None] = print,
                 allow_real: bool = False, report_file: Path | None = None, voice: Voice | None = None,
                 on_analysis: Callable[[str], None] | None = None, on_account: Callable[[dict], None] | None = None):
        self.cfg = cfg
        self.broker = broker
        self.log = log
        self.allow_real = allow_real
        self.report_file = report_file
        self.on_analysis = on_analysis or (lambda t: None)
        self.on_account = on_account or (lambda a: None)
        self.notifier = Notifier(cfg.notify)
        self.voice = voice or Voice(cfg.voice.enabled, cfg.voice.rate, cfg.voice.volume, log=log)
        self.strategy = get_strategy(cfg.strategy.name)
        self.params = self.strategy.params(cfg.strategy.params)
        self.risk = RiskManager(cfg.risk, cfg.market)
        self.ai = None
        self.view = None
        self.stop_event = threading.Event()
        self.last_bar = None
        self.last_brief = 0.0
        self.open_tickets: set[int] = set()
        self.ready = False
        self.state_file = cfg.data_path / f"live_state_{cfg.market.symbol}_{cfg.broker.magic}.json"
        self.stop_file = cfg.data_path / "STOP"

    def say(self, text: str) -> None:
        self.voice.say(text)

    # ------------------------------------------------------------------ démarrage
    def prepare(self) -> bool:
        self.broker.connect()
        acct = self.broker.account()
        self.on_account(acct)
        kind = "DÉMO" if acct["is_demo"] else "RÉEL"
        self.log(f"Connecté : compte {kind} {acct.get('login')} @ {acct.get('server')} — equity {acct['equity']:.2f} {acct['currency']}")
        ok, msg, rep = check_gate(self.cfg, acct["is_demo"], self.report_file)
        self.log(msg)
        if not ok:
            self.say("Trading refusé. " + msg)
            return False
        if not acct["is_demo"] and not self.allow_real:
            self.log("Compte RÉEL détecté : confirmation explicite requise (option « autoriser le réel »).")
            self.say("Compte réel détecté. Coche l'autorisation du compte réel pour continuer.")
            return False
        if rep is not None and rep.passed and rep.strategy == self.strategy.name:
            self.params = self.strategy.params(rep.deploy_params)
            self.log(f"Paramètres validés chargés : {rep.deploy_params}")
        if rep is not None and rep.ai_enabled and self.cfg.ai.enabled and rep.strategy == self.strategy.name:
            from .ai import AIFilter
            path = self.cfg.data_path / rep.ai_model_file
            if path.exists():
                self.ai = AIFilter.load(path)
                self.log(f"IA chargée ({self.ai.n_train} trades d'apprentissage).")
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
        self.say(f"Miuo est connecté au compte {'démo' if acct['is_demo'] else 'réel'}. "
                 f"Surveillance de {asset_name(self.cfg.market.symbol)} en {self.cfg.market.timeframe}"
                 f"{', avec filtre IA' if self.ai else ''}.")
        return True

    # ------------------------------------------------------------------ boucle
    def step(self) -> None:
        if self.stop_event.is_set() or self.stop_file.exists():
            self.kill("Arrêt d'urgence demandé")
            return
        bars = self.broker.bars(self.cfg.broker.live_bars)
        if len(bars) < 250:
            return
        now = bars.index[-1]
        acct = self.broker.account()
        positions = self.broker.positions()
        self._sync_closed(positions, now)

        if now == self.last_bar:
            return
        self.last_bar = now
        self.on_account(acct)
        self.risk.update(now.to_pydatetime(), acct["equity"])
        if self.risk.killed:
            self.kill(self.risk.kill_reason)
            return

        mf = None
        try:
            from .ai import market_features
            mf = market_features(bars)
            self.view = analyze(bars, self.cfg.market.symbol, mf)
            self.on_analysis(self.view.text())
            every = self.cfg.voice.brief_every_minutes
            if every and time.time() - self.last_brief >= every * 60:
                self.last_brief = time.time()
                self.say(self.view.speech())
        except Exception as e:  # l'analyse ne doit jamais bloquer le trading
            self.log(f"Analyse indisponible : {e}")

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
        side = "ACHAT" if direction == 1 else "VENTE"
        ai_p = None
        if self.ai is not None and mf is not None:
            ai_p = float(self.ai.proba(mf.iloc[[-1]], [direction], [sig["tp_r"]])[0])
            need = float(self.ai.threshold(sig["tp_r"]))
            if not np.isfinite(ai_p) or ai_p < need:
                self.log(f"Signal {side} rejeté par l'IA : probabilité {ai_p:.0%} < {need:.0%} requise.")
                return
        ok, reason = self.risk.can_trade(now.to_pydatetime(), acct["equity"], self.broker.spread_points())
        if not ok:
            self.log(f"Signal {side} ignoré : {reason}")
            return
        lots = self.risk.lot_size(acct["equity"], float(sig["stop_dist"]))
        if lots <= 0:
            self.log("Signal ignoré : capital insuffisant pour respecter le risque avec le lot minimum.")
            return
        sd, tpr = float(sig["stop_dist"]), float(sig["tp_r"])
        res = self.broker.open_market(direction, lots, sd, tpr)
        if res.ok:
            self.risk.on_trade_opened()
            self.open_tickets.add(res.ticket)
            sl, tp = res.price - direction * sd, res.price + direction * tpr * sd
            why = self.strategy.reason.get(direction, "")
            msg = (f"{side} {lots} lot(s) {self.cfg.market.symbol} @ {res.price:.2f} — stop {sl:.2f}, objectif {tp:.2f} "
                   f"({tpr}R){f', IA {ai_p:.0%}' if ai_p is not None else ''} — ticket {res.ticket}\n   Raison : {why}")
            self.log(msg)
            self.notifier.send(msg)
            self.say(trade_speech(self.cfg.market.symbol, direction, res.price, lots, sl, tp, tpr, why,
                                  self.view, ai_p, self.cfg.voice.announce_analysis))
        else:
            self.log(f"Ordre refusé : {res.message}")
            self.notifier.send(f"⚠ Ordre refusé : {res.message}")
            self.say(f"Attention, ordre refusé par le broker : {res.message}")
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
            self.say(f"Trade clôturé en {'gain' if pnl >= 0 else 'perte'} de {fr(abs(pnl))} dollars.")
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
        self.say(f"Arrêt de Miuo. {reason}.")

    def stop(self) -> None:
        """Arrêt normal : on laisse les positions protégées par leur stop côté serveur."""
        self.stop_event.set()

    def speak_analysis(self) -> None:
        if self.view is not None:
            self.say(self.view.speech())

    def _reconnect(self) -> None:
        delay = 2.0
        self.log("Connexion MT5 perdue : reconnexion automatique…")
        self.say("Connexion perdue avec MetaTrader. Reconnexion en cours.")
        while not self.stop_event.is_set():
            try:
                self.broker.reconnect()
                if self.broker.is_connected():
                    self.log("Reconnecté à MT5.")
                    self.say("Reconnecté.")
                    return
            except Exception as e:
                self.log(f"Reconnexion échouée ({e}) — nouvel essai dans {delay:.0f} s")
            self.stop_event.wait(delay)
            delay = min(delay * 2, 60.0)

    def run(self) -> None:
        if not self.ready and not self.prepare():
            return
        paper = isinstance(self.broker, PaperBroker)
        while not self.stop_event.is_set():
            try:
                if not paper and not self.broker.is_connected():
                    self._reconnect()
                    continue
                self.step()
            except BrokerError as e:
                self.log(f"Erreur broker : {e}")
                self._reconnect()
            except Exception as e:  # une erreur ne doit jamais tuer la boucle silencieusement
                self.log(f"Erreur : {e}")
                self.stop_event.wait(5)
            if paper:
                if not self.broker.advance():
                    break
            else:
                self.stop_event.wait(self.cfg.broker.poll_seconds)
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
