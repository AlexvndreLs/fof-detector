import argparse
import time
import threading
import queue
import sys
import urllib.request
import json
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from scipy.signal import correlate, resample_poly

# ─── CONFIGURATION DISCORD VIA CONFIG.PY ──────────────────────────────────────
try:
    from config import DISCORD_WEBHOOK
except ImportError:
    DISCORD_WEBHOOK = ""

# ─── SEUILS DE CALIBRATION (UNIQUEMENT TEMPOREL) ──────────────────────────────
STRICT_XCORR_THRESHOLD = 0.16  # Seuil ajusté d'après tes logs (le klaxon monte à ~0.19)
NOISE_GATE_RMS = 0.005         # Ignore le silence ou bruit de fond ultra-faible
BLOCK_SIZE = 4096
HOP_SECONDS = 0.3
COOLDOWN_SECONDS = 3          # Remonté à 15s pour éviter le spam de messages Discord


# ─── CHARGEMENT TEMPLATE ────────────────────────────────────────────────────────

def load_template(path: str, target_sr: int) -> np.ndarray:
    y, sr = sf.read(path, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)  # Mixage Stéréo -> Mono
    if sr != target_sr:
        from math import gcd
        g = gcd(target_sr, sr)
        y = resample_poly(y, target_sr // g, sr // g).astype(np.float32)
    y /= np.max(np.abs(y)) + 1e-9
    return y


def normalized_xcorr(a: np.ndarray, b: np.ndarray) -> float:
    """Corrélation croisée normalisée dans le domaine temporel brut."""
    if len(a) < len(b):
        return 0.0
    a = a[-len(b) * 2:]
    corr = correlate(a, b, mode="full")
    norm = np.sqrt(np.sum(a ** 2) * np.sum(b ** 2)) + 1e-9
    return float(np.max(np.abs(corr)) / norm)


# ─── ALERTE DISCORD & CONSOLE ──────────────────────────────────────────────────

def alert(score: float, rms_force: float, strength_label: str):
    """Affiche l'alerte en console et l'envoie sur le webhook Discord si configuré."""
    print(f"\n{'=' * 60}")
    print(f"  ⚓  FORT DETECTED | XCorr: {score:.3f} | Force: {strength_label} (RMS: {rms_force:.4f})")
    print(f"{'=' * 60}\n")

    if not DISCORD_WEBHOOK:
        return

    payload = {
        "content": f"🏴‍☠️ **[Sea of Thieves] FORT DETECTED !**\n"
                   f"• **Force du signal** : `{strength_label}` (RMS: {rms_force:.4f})\n"
                   f"• **Score de corrélation** : `{score:.3f}`"
    }
    
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req) as response:
            if response.status != 204:
                print(f"[Discord] Erreur d'envoi : Code {response.status}")
    except Exception as e:
        print(f"[Discord] Impossible d'envoyer l'alerte : {e}")


# ─── MAIN ENGINE ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SoT Horn Detector - Raw Temporal Mode")
    parser.add_argument("--template", default="sot_horn_template2.wav")
    parser.add_argument("--device", type=int, default=96)
    args = parser.parse_args()

    device_info = sd.query_devices(args.device, "input")
    sr = int(device_info["default_samplerate"])
    print(f"[*] Analyseur actif sur le périphérique: [{args.device}] {device_info['name']}")
    print(f"[*] Fréquence d'échantillonnage: {sr} Hz")

    # Chargement du template de 2 secondes
    template_path = Path(args.template)
    if not template_path.exists():
        template_path = Path(__file__).parent / args.template
    if not template_path.exists():
        print(f"[!] Template introuvable: {args.template}")
        sys.exit(1)

    template = load_template(str(template_path), sr)
    print(f"[*] Template chargé avec succès ({len(template)/sr:.2f}s).")
    print(f"[*] Statut Discord Webhook: {'Connecté' if DISCORD_WEBHOOK else 'Désactivé (config.py vide)'}")
    print(f"[*] Écoute en cours (Filtres fréquentiels désactivés)...")

    buf_len = len(template) * 3
    buf = np.zeros(buf_len, dtype=np.float32)
    buf_lock = threading.Lock()
    audio_q = queue.Queue()
    last_alert = 0.0

    def callback(indata, frames, time_info, status):
        # Capturer les deux canaux et en faire la moyenne (stéréo complet)
        audio_q.put(indata.mean(axis=1).copy())

    def detector_loop():
        nonlocal buf, last_alert
        hop_samples = int(HOP_SECONDS * sr)
        accumulated = 0

        while True:
            try:
                chunk = audio_q.get(timeout=1.0)
            except queue.Empty:
                continue

            with buf_lock:
                buf = np.roll(buf, -len(chunk))
                buf[-len(chunk):] = chunk
                accumulated += len(chunk)

            if accumulated < hop_samples:
                continue
            accumulated = 0

            with buf_lock:
                window = buf.copy()

            # 1. Fenêtre d'analyse équivalente à la durée du template
            analysis_window = window[-len(template):]
            raw_rms = np.sqrt(np.mean(analysis_window ** 2))
            
            # 2. Filtre de bruit (Noise Gate)
            if raw_rms < NOISE_GATE_RMS:
                continue

            # 3. Corrélation sur le signal brut (aucun filtre fréquentiel pour ne rien bloquer)
            score_xcorr = normalized_xcorr(window, template)

            # Log de monitoring en direct
            print(f"[{time.strftime('%H:%M:%S')}] RMS={raw_rms:.5f} | XCorr={score_xcorr:.3f}")

            # 4. Logique de décision
            if score_xcorr >= STRICT_XCORR_THRESHOLD:
                now = time.time()
                if (now - last_alert) > COOLDOWN_SECONDS:
                    last_alert = now
                    
                    if raw_rms > 0.04:
                        strength = "FORT / PROCHE"
                    else:
                        strength = "FAIBLE / LOINTAIN"
                        
                    alert(score_xcorr, raw_rms, strength)

    t = threading.Thread(target=detector_loop, daemon=True)
    t.start()

    try:
        with sd.InputStream(
            device=args.device,
            channels=2,  # Écoute obligatoire des canaux gauche et droit
            samplerate=sr,
            blocksize=BLOCK_SIZE,
            dtype="float32",
            callback=callback,
        ):
            while True:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[*] Arrêt du détecteur.")


if __name__ == "__main__":
    main()