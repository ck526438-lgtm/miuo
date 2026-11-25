"""Exécution des ordres.

MT5Broker   : connexion réelle à MetaTrader 5 (Windows), avec contrôles et reprises sur erreur.
PaperBroker : rejoue un historique bougie par bougie, avec la même interface (tests, simulation).

Principes « zéro erreur » :
- un seul trade ouvert par symbole et par numéro magique ; on revérifie AVANT chaque tentative
  d'envoi, donc une réponse perdue du serveur ne peut pas créer de doublon ;
- stop et objectif sont posés côté serveur dans l'ordre même : si Miuo plante, la position reste protégée ;
- volume et prix normalisés aux règles du symbole (pas de lot, décimales, distance minimale des stops) ;
- reprises uniquement sur les erreurs transitoires (requote, prix changé, timeout, connexion).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import TIMEFRAME_MINUTES, MiuoConfig
from .data import from_mt5_rates


class BrokerError(RuntimeError):
    pass


@dataclass
class OrderResult:
    ok: bool
    ticket: int = 0
    price: float = 0.0
    message: str = ""


def find_terminal() -> str | None:
    """Cherche terminal64.exe dans les emplacements d'installation habituels de MT5."""
    import glob
    import os
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes")]
    for root in roots:
        for path in glob.glob(os.path.join(root, "*", "terminal64.exe")):
            return path
    return None


def _round_step(x: float, step: float) -> float:
    return round(np.floor(x / step + 1e-9) * step, 8)


# =========================================================================== MetaTrader 5
RETRYABLE = {10004, 10012, 10020, 10021, 10024, 10031}  # requote, timeout, prix changé, pas de prix, trop de requêtes, connexion
DONE = {10008, 10009, 10010}


class MT5Broker:
    def __init__(self, cfg: MiuoConfig, log=print):
        try:
            import MetaTrader5 as mt5  # disponible uniquement sous Windows
        except ImportError as e:  # pragma: no cover
            raise BrokerError("Le module MetaTrader5 est introuvable (pip install MetaTrader5, Windows uniquement).") from e
        self.mt5 = mt5
        self.cfg = cfg
        self.log = log
        self.sym = cfg.market.symbol
        self.digits = 2
        self.stops_level = 0
        self.filling = None
        self.tf = getattr(mt5, f"TIMEFRAME_{cfg.market.timeframe}")

    # ------------------------------------------------------------------ connexion
    def connect(self) -> None:
        mt5, b = self.mt5, self.cfg.broker
        kwargs = {}
        if b.terminal_path:
            kwargs["path"] = b.terminal_path
        if b.login:
            kwargs.update(login=int(b.login), password=b.password, server=b.server)
        if not mt5.initialize(**kwargs):
            err = mt5.last_error()
            found = None if b.terminal_path else find_terminal()
            if not found or not mt5.initialize(path=found, **{k: v for k, v in kwargs.items() if k != "path"}):
                raise BrokerError(f"Connexion MT5 impossible : {err}. Ouvre MetaTrader 5 et connecte-toi à ton compte.")
            b.terminal_path = found
        ti = mt5.terminal_info()
        if ti is None or not ti.connected:
            raise BrokerError("Le terminal MT5 n'est pas connecté au serveur du broker.")
        if not ti.trade_allowed:
            raise BrokerError("Le trading algorithmique est désactivé dans MT5 (bouton « Algo Trading »).")
        if not mt5.symbol_select(self.sym, True):
            raise BrokerError(f"Symbole {self.sym} introuvable chez ce broker.")
        info = mt5.symbol_info(self.sym)
        m = self.cfg.market
        m.point, m.contract_size = info.point, info.trade_contract_size
        m.lot_step, m.min_lot, m.max_lot = info.volume_step, info.volume_min, info.volume_max
        self.digits, self.stops_level = info.digits, info.trade_stops_level
        fm = info.filling_mode
        self.filling = mt5.ORDER_FILLING_FOK if fm & 1 else mt5.ORDER_FILLING_IOC if fm & 2 else mt5.ORDER_FILLING_RETURN

    def disconnect(self) -> None:
        self.mt5.shutdown()

    def is_connected(self) -> bool:
        ti = self.mt5.terminal_info()
        return bool(ti and ti.connected and self.mt5.account_info() is not None)

    def reconnect(self) -> None:
        try:
            self.mt5.shutdown()
        except Exception:
            pass
        self.connect()

    # ------------------------------------------------------------------ lecture
    def account(self) -> dict:
        a = self.mt5.account_info()
        if a is None:
            raise BrokerError(f"account_info : {self.mt5.last_error()}")
        return {"equity": a.equity, "balance": a.balance, "currency": a.currency, "login": a.login,
                "server": a.server, "is_demo": a.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO}

    def bars(self, n: int) -> pd.DataFrame:
        """n dernières bougies CLÔTURÉES (la bougie en cours est exclue)."""
        rates = self.mt5.copy_rates_from_pos(self.sym, self.tf, 0, n + 1)
        if rates is None or len(rates) < 2:
            raise BrokerError(f"copy_rates : {self.mt5.last_error()}")
        return from_mt5_rates(rates).iloc[:-1]

    def history(self, n: int) -> pd.DataFrame:
        return self.bars(n)

    def tick(self) -> tuple[float, float]:
        t = self.mt5.symbol_info_tick(self.sym)
        if t is None or t.bid <= 0:
            raise BrokerError("Pas de cotation disponible.")
        return t.bid, t.ask

    def spread_points(self) -> float:
        bid, ask = self.tick()
        return (ask - bid) / self.cfg.market.point

    def positions(self) -> list[dict]:
        ps = self.mt5.positions_get(symbol=self.sym) or []
        out = []
        for p in ps:
            if p.magic != self.cfg.broker.magic:
                continue
            out.append({"ticket": p.ticket, "direction": 1 if p.type == self.mt5.POSITION_TYPE_BUY else -1,
                        "volume": p.volume, "price_open": p.price_open, "sl": p.sl, "tp": p.tp,
                        "time": pd.to_datetime(p.time, unit="s"), "profit": p.profit})
        return out

    def closed_pnl(self, ticket: int) -> float | None:
        deals = self.mt5.history_deals_get(position=ticket)
        if not deals or not any(d.entry == self.mt5.DEAL_ENTRY_OUT for d in deals):
            return None
        return float(sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals))

    # ------------------------------------------------------------------ ordres
    def _send(self, req: dict, check_dup: bool) -> OrderResult:
        mt5 = self.mt5
        last = "inconnu"
        for attempt in range(4):
            if check_dup and self.positions():
                p = self.positions()[0]
                return OrderResult(True, p["ticket"], p["price_open"], "position déjà ouverte (pas de doublon)")
            res = mt5.order_send(req)
            if res is None:
                last = str(mt5.last_error())
            elif res.retcode in DONE:
                return OrderResult(True, res.order or res.deal, res.price, "ok")
            elif res.retcode in RETRYABLE:
                last = f"{res.retcode} {res.comment}"
            else:
                return OrderResult(False, message=f"Refus du serveur {res.retcode} : {res.comment}")
            time.sleep(0.4 * (attempt + 1))
            if "price" in req:
                bid, ask = self.tick()
                old = req["price"]
                req["price"] = ask if req["type"] == mt5.ORDER_TYPE_BUY else bid
                if "sl" in req and req.get("sl"):
                    shift = req["price"] - old
                    req["sl"] = round(req["sl"] + shift, self.digits)
                    req["tp"] = round(req["tp"] + shift, self.digits)
        return OrderResult(False, message=f"Échec après reprises : {last}")

    def open_market(self, direction: int, lots: float, stop_dist: float, tp_r: float, comment: str = "Miuo") -> OrderResult:
        mt5, m = self.mt5, self.cfg.market
        lots = min(_round_step(lots, m.lot_step), m.max_lot)
        if lots < m.min_lot:
            return OrderResult(False, message="Volume inférieur au lot minimum")
        min_dist = (self.stops_level + 2) * m.point
        if stop_dist < min_dist:
            return OrderResult(False, message="Stop trop proche (règle de distance minimale du broker)")
        bid, ask = self.tick()
        price = ask if direction == 1 else bid
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": self.sym, "volume": lots,
               "type": mt5.ORDER_TYPE_BUY if direction == 1 else mt5.ORDER_TYPE_SELL,
               "price": price, "sl": round(price - direction * stop_dist, self.digits),
               "tp": round(price + direction * tp_r * stop_dist, self.digits),
               "deviation": self.cfg.broker.deviation_points, "magic": self.cfg.broker.magic,
               "comment": comment[:31], "type_time": mt5.ORDER_TIME_GTC, "type_filling": self.filling}
        check = mt5.order_check(req)
        if check is None or check.retcode not in (0, 10009):
            return OrderResult(False, message=f"order_check refusé : {getattr(check, 'comment', mt5.last_error())}")
        res = self._send(req, check_dup=True)
        if res.ok:  # vérification : la position doit exister et avoir son stop
            for _ in range(10):
                ps = self.positions()
                if ps:
                    if not ps[0]["sl"]:
                        self.log("⚠ Position sans stop détectée : fermeture de sécurité.")
                        self.close_position(ps[0])
                        return OrderResult(False, message="Stop non posé par le serveur — position fermée")
                    return OrderResult(True, ps[0]["ticket"], ps[0]["price_open"], "ok")
                time.sleep(0.2)
        return res

    def close_position(self, pos: dict) -> OrderResult:
        mt5 = self.mt5
        bid, ask = self.tick()
        d = pos["direction"]
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": self.sym, "volume": pos["volume"],
               "type": mt5.ORDER_TYPE_SELL if d == 1 else mt5.ORDER_TYPE_BUY,
               "position": pos["ticket"], "price": bid if d == 1 else ask,
               "deviation": self.cfg.broker.deviation_points, "magic": self.cfg.broker.magic,
               "comment": "Miuo close", "type_time": mt5.ORDER_TIME_GTC, "type_filling": self.filling}
        return self._send(req, check_dup=False)


# =========================================================================== Simulation
class PaperBroker:
    """Rejoue un DataFrame : la bougie `self.i` est « en cours », les précédentes sont clôturées."""

    def __init__(self, df: pd.DataFrame, cfg: MiuoConfig, equity: float = 10_000.0, start: int = 300):
        self.df, self.cfg = df, cfg
        self.i = start
        self.balance = equity
        self._pos: dict | None = None
        self._closed: dict[int, float] = {}
        self._next_ticket = 1
        self.orders_sent = 0

    def connect(self): pass
    def disconnect(self): pass
    def is_connected(self): return True
    def reconnect(self): pass

    def _spread(self) -> float:
        row = self.df.iloc[self.i]
        sp = row["spread"] if "spread" in self.df.columns else self.cfg.market.default_spread_points
        return float(sp) * self.cfg.market.point

    def tick(self):
        bid = float(self.df["open"].iloc[self.i])
        return bid, bid + self._spread()

    def spread_points(self):
        return self._spread() / self.cfg.market.point

    def account(self):
        eq = self.balance
        if self._pos:
            bid, ask = self.tick()
            p = self._pos
            mark = bid if p["direction"] == 1 else ask
            eq += (mark - p["price_open"]) * p["direction"] * p["volume"] * self.cfg.market.contract_size
        return {"equity": eq, "balance": self.balance, "currency": "USD", "login": 0, "server": "paper",
                "is_demo": True}

    def bars(self, n):
        return self.df.iloc[max(0, self.i - n):self.i]

    history = bars

    def positions(self):
        return [dict(self._pos)] if self._pos else []

    def closed_pnl(self, ticket):
        return self._closed.get(ticket)

    def open_market(self, direction, lots, stop_dist, tp_r, comment="Miuo"):
        self.orders_sent += 1
        if self._pos:
            return OrderResult(True, self._pos["ticket"], self._pos["price_open"], "position déjà ouverte (pas de doublon)")
        bid, ask = self.tick()
        price = ask if direction == 1 else bid
        t = self._next_ticket
        self._next_ticket += 1
        self._pos = {"ticket": t, "direction": direction, "volume": lots, "price_open": price,
                     "sl": price - direction * stop_dist, "tp": price + direction * tp_r * stop_dist,
                     "time": self.df.index[self.i], "profit": 0.0}
        return OrderResult(True, t, price, "ok")

    def _close(self, px):
        p = self._pos
        pnl = (px - p["price_open"]) * p["direction"] * p["volume"] * self.cfg.market.contract_size \
            - self.cfg.market.commission_per_lot * p["volume"]
        self.balance += pnl
        self._closed[p["ticket"]] = pnl
        self._pos = None

    def close_position(self, pos):
        if not self._pos:
            return OrderResult(False, message="aucune position")
        bid, ask = self.tick()
        t = self._pos["ticket"]
        self._close(bid if self._pos["direction"] == 1 else ask)
        return OrderResult(True, t, 0.0, "ok")

    def advance(self) -> bool:
        """Fait vivre la bougie en cours (stop/objectif) puis passe à la suivante."""
        if self._pos:
            r = self.df.iloc[self.i]
            sp = self._spread()
            p = self._pos
            if p["direction"] == 1:
                if r["low"] <= p["sl"]:
                    self._close(min(p["sl"], r["open"]))
                elif r["high"] >= p["tp"]:
                    self._close(p["tp"])
            else:
                if r["high"] + sp >= p["sl"]:
                    self._close(max(p["sl"], r["open"] + sp))
                elif r["low"] + sp <= p["tp"]:
                    self._close(p["tp"])
        self.i += 1
        return self.i < len(self.df)


def bars_between(t0, t1, timeframe: str) -> int:
    return int((pd.Timestamp(t1) - pd.Timestamp(t0)).total_seconds() // (60 * TIMEFRAME_MINUTES[timeframe]))
