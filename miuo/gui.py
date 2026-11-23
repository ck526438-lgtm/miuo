"""Application de bureau Miuo (tkinter, incluse dans Python — aucun module graphique à installer)."""
from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .config import TIMEFRAME_MINUTES, MiuoConfig
from .strategy import STRATEGIES

BG, PANEL, FIELD = "#05060a", "#0c1120", "#141b30"
BLUE, VIOLET, TEXT, MUTED = "#3d9bff", "#b57bff", "#dfe8ff", "#7d8bb0"
GREEN, RED = "#3ddc97", "#ff5c7a"


def asset(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "assets" / name


class App:
    def __init__(self, config_path: str = "config.json"):
        self.config_path = config_path
        self.cfg = MiuoConfig.load(config_path)
        self.q: queue.Queue = queue.Queue()
        self.engine = None
        self.engine_thread = None

        self.root = tk.Tk()
        self.root.title(f"Miuo {__version__}")
        self.root.geometry("1040x700")
        self.root.minsize(900, 600)
        self.root.configure(bg=BG)
        try:
            self.root.iconbitmap(default=str(asset("miuo.ico")))
        except tk.TclError:
            pass
        self._style()
        self._header()
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.tab_dash = ttk.Frame(nb, style="Panel.TFrame")
        self.tab_val = ttk.Frame(nb, style="Panel.TFrame")
        self.tab_set = ttk.Frame(nb, style="Panel.TFrame")
        nb.add(self.tab_dash, text="  Trading  ")
        nb.add(self.tab_val, text="  Validation de l'avantage  ")
        nb.add(self.tab_set, text="  Paramètres  ")
        self._build_dashboard()
        self._build_validation()
        self._build_settings()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(150, self._pump)

    # ------------------------------------------------------------------ style
    def _style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=PANEL, foreground=TEXT, fieldbackground=FIELD, font=("Segoe UI", 10))
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(10, 6), borderwidth=0)
        s.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", BLUE)])
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("TLabel", background=PANEL, foreground=TEXT)
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("Title.TLabel", font=("Segoe UI Semibold", 12), foreground=BLUE)
        s.configure("TButton", background=FIELD, foreground=TEXT, borderwidth=0, padding=(14, 7))
        s.map("TButton", background=[("active", "#1d2745")])
        s.configure("Accent.TButton", background=BLUE, foreground="#020409")
        s.map("Accent.TButton", background=[("active", VIOLET)])
        s.configure("Danger.TButton", background=RED, foreground="#020409")
        s.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT, borderwidth=0)
        s.configure("TCombobox", fieldbackground=FIELD, foreground=TEXT, background=FIELD, arrowcolor=BLUE)
        s.configure("TCheckbutton", background=PANEL, foreground=TEXT)
        s.configure("TLabelframe", background=PANEL, foreground=BLUE, bordercolor="#1c2440")
        s.configure("TLabelframe.Label", background=PANEL, foreground=BLUE)

    def _header(self):
        h = tk.Frame(self.root, bg=BG)
        h.pack(fill="x", padx=14, pady=10)
        try:
            self.logo = tk.PhotoImage(file=str(asset("miuo_96.png"))).subsample(2, 2)
            tk.Label(h, image=self.logo, bg=BG).pack(side="left")
        except tk.TclError:
            pass
        t = tk.Frame(h, bg=BG)
        t.pack(side="left", padx=10)
        tk.Label(t, text="MIUO", bg=BG, fg=TEXT, font=("Segoe UI Black", 20)).pack(anchor="w")
        tk.Label(t, text="Avantage prouvé · risque maîtrisé · exécution sans faute", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self.status = tk.Label(h, text="● Arrêté", bg=BG, fg=MUTED, font=("Segoe UI Semibold", 11))
        self.status.pack(side="right")

    def _logbox(self, parent):
        box = tk.Text(parent, bg="#070a14", fg=TEXT, insertbackground=TEXT, relief="flat",
                      font=("Consolas", 10), wrap="word", padx=10, pady=8)
        box.tag_configure("ok", foreground=GREEN)
        box.tag_configure("bad", foreground=RED)
        box.tag_configure("info", foreground=BLUE)
        return box

    # ------------------------------------------------------------------ trading
    def _build_dashboard(self):
        f = self.tab_dash
        top = ttk.Frame(f, style="Panel.TFrame")
        top.pack(fill="x", padx=12, pady=12)
        self.acct_lbl = ttk.Label(top, text="Compte : non connecté", style="Muted.TLabel")
        self.acct_lbl.pack(side="left")
        ttk.Button(top, text="⛔ Arrêt d'urgence", style="Danger.TButton", command=self.emergency).pack(side="right", padx=4)
        ttk.Button(top, text="■ Arrêter", command=self.stop_engine).pack(side="right", padx=4)
        ttk.Button(top, text="▶ Démarrer", style="Accent.TButton", command=self.start_engine).pack(side="right", padx=4)
        self.allow_real = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Autoriser un compte RÉEL", variable=self.allow_real).pack(side="right", padx=12)
        ttk.Label(f, text="Journal", style="Title.TLabel").pack(anchor="w", padx=12)
        self.log_box = self._logbox(f)
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.log("Bienvenue. 1) Renseigne MT5 dans Paramètres  2) Valide l'avantage  3) Démarre sur DÉMO.", "info")

    def log(self, msg: str, tag: str | None = None):
        if tag is None:
            tag = "bad" if any(k in msg for k in ("⛔", "⚠", "Erreur", "refus", "Refus")) else \
                  "ok" if any(k in msg for k in ("ACHAT", "VENTE", "validé", "Connecté")) else None
        self.log_box.insert("end", msg + "\n", tag or ())
        self.log_box.see("end")

    def _qlog(self, msg: str):
        self.q.put(("log", msg))

    def start_engine(self):
        if self.engine_thread and self.engine_thread.is_alive():
            return
        self._save_settings(silent=True)
        if self.allow_real.get() and not messagebox.askyesno(
                "Compte réel", "Miuo peut trader de l'argent réel si le rapport de validation est valide.\n"
                               "Les pertes sont possibles. Continuer ?"):
            return

        def worker():
            try:
                from .execution import MT5Broker
                from .live import LiveEngine
                self.engine = LiveEngine(self.cfg, MT5Broker(self.cfg, log=self._qlog), log=self._qlog,
                                         allow_real=self.allow_real.get())
                if self.engine.prepare():
                    a = self.engine.broker.account()
                    self.q.put(("acct", f"Compte {'DÉMO' if a['is_demo'] else 'RÉEL'} {a['login']} · "
                                        f"{a['equity']:.2f} {a['currency']} · {self.cfg.market.symbol} {self.cfg.market.timeframe}"))
                    self.q.put(("status", "run"))
                    self.engine.run()
            except Exception as e:
                self._qlog(f"Erreur : {e}")
            self.q.put(("status", "stop"))

        self.engine_thread = threading.Thread(target=worker, daemon=True)
        self.engine_thread.start()

    def stop_engine(self):
        if self.engine:
            self.engine.stop()
            self.log("Arrêt demandé — les positions ouvertes restent protégées par leur stop.", "info")

    def emergency(self):
        if self.engine and self.engine_thread and self.engine_thread.is_alive():
            if messagebox.askyesno("Arrêt d'urgence", "Fermer toutes les positions Miuo et arrêter ?"):
                self.engine.stop_file.write_text("stop", encoding="utf-8")
        else:
            self.log("Miuo n'est pas en marche.", "info")

    # ------------------------------------------------------------------ validation
    def _build_validation(self):
        f = self.tab_val
        bar = ttk.Frame(f, style="Panel.TFrame")
        bar.pack(fill="x", padx=12, pady=12)
        ttk.Label(bar, text="Stratégie").pack(side="left")
        self.strat_var = tk.StringVar(value=self.cfg.strategy.name)
        ttk.Combobox(bar, textvariable=self.strat_var, values=list(STRATEGIES), width=18, state="readonly").pack(side="left", padx=6)
        ttk.Label(bar, text="Source").pack(side="left", padx=(12, 0))
        self.src_var = tk.StringVar(value="MT5")
        ttk.Combobox(bar, textvariable=self.src_var, values=["MT5", "Fichier CSV"], width=12, state="readonly").pack(side="left", padx=6)
        ttk.Label(bar, text="Bougies").pack(side="left", padx=(12, 0))
        self.bars_var = tk.StringVar(value="150000")
        ttk.Entry(bar, textvariable=self.bars_var, width=9).pack(side="left", padx=6)
        ttk.Button(bar, text="Lancer la validation", style="Accent.TButton", command=self.run_validation).pack(side="right")

        body = ttk.Frame(f, style="Panel.TFrame")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.val_box = self._logbox(body)
        self.val_box.configure(width=60)
        self.val_box.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(body, style="Panel.TFrame")
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        ttk.Label(right, text="Courbe hors échantillon (en R)", style="Title.TLabel").pack(anchor="w")
        self.canvas = tk.Canvas(right, bg="#070a14", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, pady=(4, 0))
        self.val_box.insert("end", "La validation teste la stratégie uniquement sur des données qu'elle n'a jamais vues\n"
                                   "(walk-forward + holdout), avec spread, slippage et commission.\n"
                                   "Le compte RÉEL reste bloqué tant que tous les tests ne sont pas verts.\n", "info")

    def run_validation(self):
        self._save_settings(silent=True)
        self.cfg.strategy.name = self.strat_var.get()
        csv = None
        if self.src_var.get() == "Fichier CSV":
            csv = filedialog.askopenfilename(filetypes=[("CSV", "*.csv *.txt"), ("Tous", "*.*")])
            if not csv:
                return
        try:
            n_bars = int(self.bars_var.get())
        except ValueError:
            n_bars = 150_000
        self.val_box.delete("1.0", "end")

        def vlog(m):
            self.q.put(("vlog", m))

        def worker():
            try:
                from .data import load_csv
                from .validation import report_path, validate
                if csv:
                    df = load_csv(csv)
                else:
                    from .execution import MT5Broker
                    b = MT5Broker(self.cfg)
                    b.connect()
                    try:
                        df = b.history(n_bars)
                    finally:
                        b.disconnect()
                vlog(f"{len(df)} bougies chargées ({df.index[0]} → {df.index[-1]})")
                rep = validate(df, self.cfg, progress=vlog)
                rep.save(report_path(self.cfg))
                self.q.put(("report", rep))
            except Exception as e:
                vlog(f"Erreur : {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _draw_curve(self, values):
        c = self.canvas
        c.delete("all")
        c.update_idletasks()
        w, h = max(c.winfo_width(), 200), max(c.winfo_height(), 150)
        if len(values) < 2:
            c.create_text(w / 2, h / 2, text="Pas assez de trades", fill=MUTED)
            return
        lo, hi = min(min(values), 0), max(max(values), 0)
        span = (hi - lo) or 1
        pad = 16
        y0 = h - pad - (0 - lo) / span * (h - 2 * pad)
        c.create_line(pad, y0, w - pad, y0, fill="#1c2440", dash=(3, 3))
        pts = []
        for i, v in enumerate(values):
            pts += [pad + i / (len(values) - 1) * (w - 2 * pad), h - pad - (v - lo) / span * (h - 2 * pad)]
        c.create_line(*pts, fill=BLUE if values[-1] > 0 else RED, width=2, smooth=True)
        c.create_text(w - pad, pad, anchor="ne", fill=VIOLET, text=f"{values[-1]:+.1f} R sur {len(values)} trades")

    # ------------------------------------------------------------------ paramètres
    def _build_settings(self):
        f = self.tab_set
        self.vars: dict[str, tuple[object, str, tk.Variable]] = {}
        grid = ttk.Frame(f, style="Panel.TFrame")
        grid.pack(fill="both", expand=True, padx=12, pady=12)

        def section(title, col, rows):
            lf = ttk.LabelFrame(grid, text=title, padding=10)
            lf.grid(row=0, column=col, sticky="nsew", padx=6)
            for r, (label, obj, attr, kind) in enumerate(rows):
                ttk.Label(lf, text=label).grid(row=r, column=0, sticky="w", pady=4)
                val = getattr(obj, attr)
                var = tk.StringVar(value=str(val))
                if kind == "tf":
                    w = ttk.Combobox(lf, textvariable=var, values=list(TIMEFRAME_MINUTES), width=20, state="readonly")
                else:
                    w = ttk.Entry(lf, textvariable=var, width=24, show="•" if kind == "secret" else "")
                w.grid(row=r, column=1, sticky="ew", padx=(8, 0), pady=4)
                self.vars[f"{title}.{attr}"] = (obj, attr, var)
            grid.columnconfigure(col, weight=1)

        c = self.cfg
        section("MetaTrader 5", 0, [
            ("Identifiant", c.broker, "login", "int"), ("Mot de passe", c.broker, "password", "secret"),
            ("Serveur", c.broker, "server", "str"), ("Chemin terminal64.exe", c.broker, "terminal_path", "str"),
            ("Numéro magique", c.broker, "magic", "int"), ("Symbole", c.market, "symbol", "str"),
            ("Unité de temps", c.market, "timeframe", "tf")])
        section("Risque", 1, [
            ("Risque par trade (0.005 = 0,5 %)", c.risk, "risk_per_trade", "float"),
            ("Perte max / jour", c.risk, "max_daily_loss", "float"),
            ("Drawdown max (kill switch)", c.risk, "max_drawdown", "float"),
            ("Trades max / jour", c.risk, "max_trades_per_day", "int"),
            ("Pertes consécutives avant pause", c.risk, "max_consecutive_losses", "int"),
            ("Spread max (points)", c.risk, "max_spread_points", "int"),
            ("Commission / lot", c.market, "commission_per_lot", "float")])
        section("Alertes Telegram", 2, [
            ("Token du bot", c.notify, "telegram_token", "secret"),
            ("Chat ID", c.notify, "telegram_chat_id", "str")])
        ttk.Button(f, text="Enregistrer", style="Accent.TButton", command=self._save_settings).pack(anchor="e", padx=18, pady=(0, 14))

    def _save_settings(self, silent: bool = False):
        for obj, attr, var in self.vars.values():
            cur = getattr(obj, attr)
            raw = var.get().strip()
            try:
                val = type(cur)(float(raw)) if isinstance(cur, int) and not isinstance(cur, bool) else \
                      float(raw) if isinstance(cur, float) else raw
            except ValueError:
                if not silent:
                    messagebox.showerror("Paramètres", f"Valeur invalide pour {attr} : {raw}")
                return
            setattr(obj, attr, val)
        self.cfg.strategy.name = self.strat_var.get()
        self.cfg.save(self.config_path)
        if not silent:
            self.log("Paramètres enregistrés.", "info")

    # ------------------------------------------------------------------ boucle UI
    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "vlog":
                    self.val_box.insert("end", payload + "\n")
                    self.val_box.see("end")
                elif kind == "report":
                    self.val_box.insert("end", "\n" + payload.summary() + "\n", "ok" if payload.passed else "bad")
                    self.val_box.see("end")
                    self._draw_curve(payload.oos_equity_r)
                elif kind == "acct":
                    self.acct_lbl.configure(text=payload)
                elif kind == "status":
                    on = payload == "run"
                    self.status.configure(text="● En marche" if on else "● Arrêté", fg=GREEN if on else MUTED)
        except queue.Empty:
            pass
        self.root.after(150, self._pump)

    def _on_close(self):
        if self.engine_thread and self.engine_thread.is_alive():
            if not messagebox.askyesno("Quitter", "Miuo est en marche. Arrêter et quitter ?\n"
                                                  "(les positions restent protégées par leur stop)"):
                return
            self.engine.stop()
        self.root.destroy()

    def mainloop(self):
        self.root.mainloop()


def run_gui(config_path: str = "config.json"):
    App(config_path).mainloop()
