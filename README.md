<p align="center"><img src="assets/miuo.png" width="180" alt="Miuo"></p>

<h1 align="center">Miuo</h1>
<p align="center"><b>Le bot de trading MT5 qui doit prouver son avantage avant de risquer ton argent.</b><br>
Avantage statistique validé · gestion du risque stricte · exécution sans doublon ni position sans stop</p>

---

## Pourquoi Miuo est différent

La plupart des bots sont « puissants » sur un backtest et perdent en réel, parce qu'ils ont été
ajustés au passé. Miuo inverse la logique : **il refuse de trader sur un compte réel tant qu'il n'a pas
démontré, sur des données qu'il n'a jamais vues, que sa stratégie gagne après les frais.**

| Pilier | Ce que fait Miuo |
|---|---|
| **Avantage prouvé** | Walk-forward ancré + holdout final jamais utilisé + p-value bootstrap + Monte Carlo. Le seuil de significativité se durcit à chaque nouvelle tentative (anti p-hacking). |
| **Risque maîtrisé** | 0,5 % par trade, -3 %/jour → pause, -15 % depuis le pic → kill switch, pause après 4 pertes, filtre de spread, jamais de martingale. |
| **Exécution sans erreur** | Stop et objectif posés côté serveur dans l'ordre, revérification anti-doublon avant chaque envoi, normalisation lots/prix, reprises sur erreurs transitoires, réconciliation au redémarrage. |
| **Backtest honnête** | Entrée à la bougie suivante, spread réel, slippage, commission, stop prioritaire si stop et objectif tombent dans la même bougie, gaps. |

> ⚠️ Aucun logiciel ne garantit des gains. Miuo te dit honnêtement **si** une stratégie a un avantage
> sur tes données — et la plupart du temps, la bonne réponse est « non ». Commence toujours en démo.

## Installation rapide (Windows)

1. Télécharge `Miuo-windows.zip` dans l'onglet **[Releases](../../releases)** (ou dans l'onglet *Actions* → dernier build → *Artifacts*).
2. Installe **MetaTrader 5**, connecte-toi à ton compte **démo**, active le bouton **Algo Trading**.
3. Lance **`Miuo.exe`**.

## Utilisation en 3 étapes

1. **Paramètres** : identifiant, mot de passe, serveur MT5, symbole (`XAUUSD` par défaut), unité de temps (`M5`), risque.
2. **Validation de l'avantage** : choisis une stratégie, source `MT5` (téléchargement automatique) ou un CSV, puis *Lancer la validation*.
   Tu obtiens un rapport ✔/✘ et la courbe des trades hors échantillon.
3. **Trading** : *Démarrer*.
   - **Compte démo** : toujours autorisé (forward test), même si la validation a échoué — c'est l'étape suivante logique.
   - **Compte réel** : bloqué tant que le rapport n'est pas validé, récent (30 jours) et pour la même stratégie, **et** que la case « Autoriser un compte RÉEL » n'est pas cochée.

**Arrêter** laisse les positions ouvertes protégées par leur stop. **Arrêt d'urgence** ferme tout immédiatement
(ou crée un fichier `miuo_data/STOP`).

## Les tests de validation

| Test | Seuil par défaut |
|---|---|
| Trades hors échantillon | ≥ 100 |
| Profit factor hors échantillon | ≥ 1,15 |
| Espérance par trade | ≥ 0,05 R |
| p-value bootstrap (H0 : espérance ≤ 0) | < 0,05 ÷ nombre de tentatives sur ce marché |
| Plis walk-forward gagnants | ≥ 60 % |
| Drawdown Monte Carlo (95e percentile) | ≤ 25 % |
| Holdout (20 % les plus récents, jamais vus) | espérance > 0 |

Tous les seuils sont modifiables dans `config.json` (section `gate`). Les tests automatiques vérifient que
Miuo **refuse** des données aléatoires (0 faux positif sur 12 essais) et **accepte** des données où un avantage a été planté.

## Stratégies incluses

- **`session_breakout`** — cassure du range asiatique pendant la matinée de Londres, dans le sens de l'EMA 200. Une seule cassure par jour.
- **`trend_pullback`** — repli sur l'EMA rapide dans la tendance de l'EMA lente, confirmé par une bougie de reprise.

Pour ajouter la tienne : une classe dans `miuo/strategy.py` avec `signals()` et `param_grid`, puis l'ajouter à `STRATEGIES`.
Les signaux doivent être causaux (le test `test_signals_are_causal` le vérifie).

## Ligne de commande

```bash
pip install -r requirements.txt
python -m miuo                                   # application
python -m miuo validate                          # validation sur l'historique MT5
python -m miuo validate --csv xauusd_m5.csv      # validation sur un export MT5 (CSV)
python -m miuo validate --synthetic              # démonstration : doit être REFUSÉ
python -m miuo live                              # trading (démo)
python -m miuo live --allow-real                 # trading réel (rapport validé exigé)
python -m miuo paper --csv xauusd_m5.csv         # rejoue un CSV avec le moteur temps réel
```

Alertes Telegram : renseigne `telegram_token` et `telegram_chat_id` dans les paramètres.

## Architecture

```
miuo/
├── config.py       configuration JSON
├── data.py         CSV / MT5 / données synthétiques
├── features.py     indicateurs causaux (EMA, ATR, RSI)
├── strategy.py     stratégies + grilles de paramètres
├── risk.py         gestionnaire de risque (identique backtest et réel)
├── backtest.py     moteur de backtest pessimiste
├── stats.py        métriques, bootstrap, Monte Carlo
├── validation.py   walk-forward, holdout, rapport, verrou du compte réel
├── execution.py    MT5Broker (réel) et PaperBroker (simulation)
├── live.py         moteur temps réel
├── notify.py       Telegram
└── gui.py          application de bureau
```

## Développement

```bash
pip install -r requirements-dev.txt
pytest -q
```

Chaque push sur `main` lance les tests puis construit `Miuo.exe` et `miuo-cli.exe` sur Windows.
Un tag `v*` (ex. `git tag v1.0.0 && git push --tags`) publie une Release avec les exécutables.

`config.json` contient ton mot de passe MT5 : il est ignoré par git, ne le partage pas.

## Licence

MIT. Logiciel fourni sans garantie ; le trading comporte un risque de perte en capital.
