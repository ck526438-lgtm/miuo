"""Ligne de commande : python -m miuo [gui|validate|live|fetch|paper]"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import MiuoConfig


def _data(args, cfg):
    from .data import load_csv, synthetic
    if getattr(args, "csv", None):
        return load_csv(args.csv)
    if getattr(args, "synthetic", False):
        return synthetic(args.bars)
    from .execution import MT5Broker
    b = MT5Broker(cfg)
    b.connect()
    try:
        return b.history(args.bars)
    finally:
        b.disconnect()


def main(argv=None) -> int:
    if getattr(sys, "frozen", False):  # exe lancé au démarrage de Windows : travailler à côté de l'exe
        import os
        from pathlib import Path
        os.chdir(Path(sys.executable).parent)
    for stream in (sys.stdout, sys.stderr):  # console Windows : éviter les erreurs d'encodage
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="miuo", description=f"Miuo {__version__} — bot de trading MT5 à avantage validé")
    ap.add_argument("--config", default="config.json")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="ouvre l'application (par défaut)")

    v = sub.add_parser("validate", help="prouve (ou réfute) l'avantage statistique")
    v.add_argument("--csv", help="CSV OHLC (export MT5 accepté) ; sinon téléchargement depuis MT5")
    v.add_argument("--bars", type=int, default=150_000)
    v.add_argument("--synthetic", action="store_true", help="données aléatoires (démo : doit être REFUSÉ)")
    v.add_argument("--strategy")

    f = sub.add_parser("fetch", help="télécharge l'historique MT5 en CSV")
    f.add_argument("--bars", type=int, default=150_000)
    f.add_argument("--out", default="history.csv")

    lv = sub.add_parser("live", help="trading en temps réel sur MT5")
    lv.add_argument("--allow-real", action="store_true", help="autorise un compte RÉEL (rapport validé exigé)")

    vo = sub.add_parser("voice", help="teste la voix off")
    vo.add_argument("--text", default="Bonjour, je suis Miuo. Achat sur l'or à 2650,40, stop à 2646,90.")
    vo.add_argument("--wav", help="écrit le son dans un fichier .wav au lieu de le jouer")

    pp = sub.add_parser("paper", help="rejoue un CSV avec le moteur temps réel")
    pp.add_argument("--csv", required=True)

    args = ap.parse_args(argv)
    cfg = MiuoConfig.load(args.config)

    if args.cmd in (None, "gui"):
        from .gui import run_gui
        run_gui(args.config)
        return 0

    if args.cmd == "validate":
        from .validation import report_path, validate
        if args.strategy:
            cfg.strategy.name = args.strategy
        df = _data(args, cfg)
        rep = validate(df, cfg, progress=print)
        out = report_path(cfg)
        rep.save(out)
        print()
        print(rep.summary())
        print(f"\nRapport : {out}")
        return 0 if rep.passed else 2

    if args.cmd == "fetch":
        df = _data(args, cfg)
        df.to_csv(args.out, index_label="time")
        print(f"{len(df)} bougies → {args.out}")
        return 0

    if args.cmd == "live":
        from .execution import MT5Broker
        from .live import LiveEngine
        eng = LiveEngine(cfg, MT5Broker(cfg), allow_real=args.allow_real)
        try:
            eng.run()
        except KeyboardInterrupt:
            eng.stop()
        return 0

    if args.cmd == "voice":
        from .voice import speak_blocking
        ok = speak_blocking(args.text, cfg.voice.rate, cfg.voice.volume, wav_path=args.wav)
        print("Voix OK" if ok else "Synthèse vocale indisponible")
        return 0 if ok else 3

    if args.cmd == "paper":
        from .data import load_csv
        from .execution import PaperBroker
        from .live import LiveEngine
        broker = PaperBroker(load_csv(args.csv), cfg)
        LiveEngine(cfg, broker, log=print).run()
        print(f"Capital final : {broker.balance:.2f}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
