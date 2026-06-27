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
from scipy.signal import correlate, resample_poly, hilbert

# ─── CONFIGURATION DISCORD VIA CONFIG.PY ──────────────────────────────────────
try:
    from config import DISCORD_WEBHOOK
except ImportError:
    DISCORD_WEBHOOK = ""

# ─── PARAMÈTRES DE CONFIGURATION RECALIBRÉS ────────────────────────────────────
DEFAULT_TEMPLATE = "sot_horn_template.wav" # Ton fichier de 4 secondes
STRICT_XCORR_THRESHOLD = 0.8  # Seuil d'enveloppe filtrée
NOISE_GATE_RMS = 0.005        # Sensibilité intermédiaire pour les forts lointains
BLOCK_SIZE = 56000
HOP_SECONDS = 0.4              # Fenêtre plus large adaptée à un signal de 4s
COOLDOWN_SECONDS = 15

# Bande fréquentielle pour isoler le klaxon avant Hilbert (Hz)
BANDPASS_LOW = 40
BANDPASS_HIGH = 280

# ─── EXTRACTION D'ENVELOPPE ANALYTIQUE (HILBERT) ───────────────────────────────

def get_envelope(signal: np.ndarray) -> np.ndarray:
    """Calcule l'enveloppe lissée du signal via la transformée de Hilbert.
    
    Cette méthode isole les variations macroscopiques (la durée/l'attaque)
    et élimine la micro-texture fréquentielle (les sons aigus des notifs).
    """
    # Éviter le calcul sur un bloc vide
    if len(signal) == 0 or np.all(signal == 0):
        return np.zeros_like(signal)
        
    # Signal analytique de Hilbert
    analytic_signal = hilbert(signal)
    amplitude_envelope = np.abs(analytic_signal)
    
    # Lissage par moyenne glissante pour stabiliser l'enveloppe globale
    kernel_size = 128
    if len(amplitude_envelope) > kernel_size:
        kernel = np.ones(kernel_size) / kernel_size
        smoothed = np.convolve(amplitude_envelope, kernel, mode='same')
    else:
        smoothed = amplitude_envelope
        
    # Normalisation locale de l'enveloppe [0 - 1]
    smoothed /= (np.max(smoothed) + 1e-9)
    return smoothed.astype(np.float32)


def load_template(path: str, target_sr: int) -> np.ndarray:
    """Charge le fichier audio et extrait directement son enveloppe de Hilbert."""
    y, sr = sf.read(path, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)  # Mixage Stéréo -> Mono
    if sr != target_sr:
        from math import gcd
        g = gcd(target_sr, sr)
        y = resample_poly(y, target_sr // g, sr // g).astype(np.float32)
    
    # On stocke l'enveloppe de référence
    return get_envelope(y)


def normalized_envelope_xcorr(env_a: np.ndarray, env_b: np.ndarray) -> float:
    """Corrélation croisée normalisée appliquée sur les enveloppes."""
    if len(env_a) < len(env_b):
        return 0.0
    # On isole une fenêtre d'analyse dynamique correspondant au double du template
    env_a = env_a[-len(env_b) * 2:]
    corr = correlate(env_a, env_b, mode="full")
    norm = np.sqrt(np.sum(env_a ** 2) * np.sum(env_b ** 2)) + 1e-9
    return float(np.max(np.abs(corr)) / norm)


# ─── ALERTE DISCORD ────────────────────────────────────────────────────────────

def alert(score: float, rms_force: float, strength_label: str):
    """Affiche l'événement en console et l'envoie sur Discord."""
    print(f"\n{'=' * 60}")
    print(f"  ⚓  FORT DETECTED (HILBERT) | Env_XCorr: {score:.3f} | Force: {strength_label}")
    print(f"{'=' * 60}\n")

    if not DISCORD_WEBHOOK:
        return

    payload = {
        "content": f"🏴‍☠️ **[Hilbert Engine] FORT DETECTED !**\n"
                   f"• **Distance estimée** : `{strength_label}` (RMS: {rms_force:.4f})\n"
                   f"• **Précision d'enveloppe** : `{score:.3f}`"
    }
    
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req) as response:
            pass
    except Exception as e:
        print(f"[Discord] Erreur d'envoi du webhook : {e}")


# ─── MAIN ENGINE ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SoT Horn Detector - Hilbert Envelope Mode")
    parser.add_argument("--template", default="sot_horn_template2.wav")  # Ton nouveau fichier par défaut
    parser.add_argument("--device", type=int, default=96)
    args = parser.parse_args()

    device_info = sd.query_devices(args.device, "input")
    sr = int(device_info["default_samplerate"])
    print(f"[*] Hilbert Engine en ligne sur le périphérique : [{args.device}] {device_info['name']}")

    template_path = Path(args.template)
    if not template_path.exists():
        template_path = Path(__file__).parent / args.template
    if not template_path.exists():
        print(f"[!] Erreur : Le fichier template '{args.template}' est introuvable.")
        sys.exit(1)

    # Extraction de l'enveloppe de référence de 2 secondes
    template_env = load_template(str(template_path), sr)
    print(f"[*] Enveloppe de référence chargée ({len(template_env)/sr:.2f}s).")
    print(f"[*] Mode de détection : Profil d'amplitude temporel (Hilbert).")
    print(f"[*] Monitoring actif (Émettra uniquement sur Discord)...")

    # Mémoire tampon (3x la taille du template de 2s)
    buf_len = len(template_env) * 3
    buf = np.zeros(buf_len, dtype=np.float32)
    buf_lock = threading.Lock()
    audio_q = queue.Queue()
    last_alert = 0.0

    def callback(indata, frames, time_info, status):
        # Capture complète stéréo gauche/droite
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

            # 1. Analyse de l'énergie sur la taille exacte du template
            analysis_window = window[-len(template_env):]
            raw_rms = np.sqrt(np.mean(analysis_window ** 2))
            
            # 2. Noise Gate de sécurité
            if raw_rms < NOISE_GATE_RMS:
                continue

            # 3. Calcul de l'enveloppe de Hilbert sur le flux audio actuel
            window_env = get_envelope(window)

            # 4. Corrélation des formes d'enveloppes
            score_env_xcorr = normalized_envelope_xcorr(window_env, template_env)

            # Log en direct dans le terminal
            print(f"[{time.strftime('%H:%M:%S')}] RMS={raw_rms:.5f} | Env_XCorr={score_env_xcorr:.3f}")

            # 5. Seuil de décision
            if score_env_xcorr >= STRICT_XCORR_THRESHOLD:
                now = time.time()
                if (now - last_alert) > COOLDOWN_SECONDS:
                    last_alert = now
                    
                    # Calibration dynamique de la force du fort
                    if raw_rms > 0.03:
                        strength = "FORT / PROCHE"
                    elif raw_rms > 0.008:
                        strength = "LOINTAIN / DISCRET"
                    else:
                        strength = "TRÈS ÉLOIGNÉ / SEUIL CRITIQUE"
                        
                    alert(score_env_xcorr, raw_rms, strength)

    t = threading.Thread(target=detector_loop, daemon=True)
    t.start()

    try:
        with sd.InputStream(
            device=args.device,
            channels=2,
            samplerate=sr,
            blocksize=BLOCK_SIZE,
            dtype="float32",
            callback=callback,
        ):
            while True:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[*] Arrêt du moteur Hilbert.")

if __name__ == "__main__":
    main()