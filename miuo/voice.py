"""Voix off de Miuo.

Windows : synthèse vocale intégrée (System.Speech / SAPI), voix française choisie automatiquement
si elle est installée (Paramètres Windows > Heure et langue > Voix). Aucun module à installer.
Les phrases sont lues dans l'ordre, sur un fil séparé : la voix ne ralentit jamais le trading.
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
from typing import Callable

PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $v = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'fr*' } | Select-Object -First 1
  if ($v) { $s.SelectVoice($v.VoiceInfo.Name) }
} catch {}
$s.Rate = [int]$env:MIUO_TTS_RATE
$s.Volume = [int]$env:MIUO_TTS_VOLUME
if ($env:MIUO_TTS_WAV) { $s.SetOutputToWaveFile($env:MIUO_TTS_WAV) }
$s.Speak($env:MIUO_TTS_TEXT)
$s.Dispose()
"""


def speak_blocking(text: str, rate: int = 0, volume: int = 100, wav_path: str | None = None) -> bool:
    """Prononce `text` (ou l'écrit dans un .wav). Renvoie False si aucune synthèse n'est disponible."""
    text = " ".join(text.split())
    if not text:
        return True
    if sys.platform == "win32":
        env = dict(os.environ, MIUO_TTS_TEXT=text, MIUO_TTS_RATE=str(max(-10, min(10, int(rate)))),
                   MIUO_TTS_VOLUME=str(max(0, min(100, int(volume)))))
        if wav_path:
            env["MIUO_TTS_WAV"] = wav_path
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-Command", PS_SCRIPT], env=env, capture_output=True, timeout=120,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    for cmd in (["espeak-ng", "-v", "fr"], ["espeak", "-v", "fr"], ["say", "-v", "Thomas"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd + [text], capture_output=True, timeout=120)
            return True
    return False


class Voice:
    def __init__(self, enabled: bool = True, rate: int = 0, volume: int = 100,
                 speaker: Callable[[str], None] | None = None, log: Callable[[str], None] | None = None):
        self.enabled = enabled
        self.rate, self.volume = rate, volume
        self.log = log or (lambda m: None)
        self._speaker = speaker
        self.history: list[str] = []
        self._q: queue.Queue = queue.Queue()
        self._warned = False
        threading.Thread(target=self._worker, daemon=True).start()

    def say(self, text: str) -> None:
        self.history.append(text)
        if self.enabled:
            self._q.put(text)

    def wait(self, timeout: float = 30) -> None:
        """Attend que tout soit prononcé (tests)."""
        done = threading.Event()
        self._q.put(done)
        done.wait(timeout)

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if isinstance(item, threading.Event):
                item.set()
                continue
            try:
                if self._speaker:
                    self._speaker(item)
                elif not speak_blocking(item, self.rate, self.volume) and not self._warned:
                    self._warned = True
                    self.log("Voix off indisponible sur ce système (synthèse vocale introuvable).")
            except Exception as e:
                if not self._warned:
                    self._warned = True
                    self.log(f"Voix off : {e}")
