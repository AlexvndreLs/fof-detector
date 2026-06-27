"""
Sea of Thieves - Anti-False Positive Horn Detector (Controlled Window Mode)
Se réveille sur pression de F10, analyse pendant 30s, puis retourne en veille.
"""

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
import keyboard  # REQUIS : pip install keyboard
from scipy.signal import correlate, resample_poly, hilbert, butter, sosfilt

# ─── CONFIGURATION DISCORD VIA CONFIG.PY ──────────────────────────────────────
try:
    from config import DISCORD_WEBHOOK
except ImportError:
    DISCORD_WEBHOOK = ""

# ─── CONFIGURATION DES SEUILS AJUSTÉS ──────────────────────────────────────────
DEFAULT_TEMPLATE = "sot_horn_template.wav"

BANDPASS_LOW: float = 35.0
BANDPASS_HIGH: float = 450.0

STRICT_XCORR_THRESHOLD: float = 0.45  # Seuil enveloppe temporelle (Hilbert)
STRICT_SPEC_THRESHOLD: float = 0.65   # Seuil spectral (Tue l'écran de chargement)

NOISE_GATE_RMS: float = 0.004
BLOCK_SIZE: int = 4096
HOP_SECONDS: float = 0.4
DETECTION_WINDOW_SECONDS: float = 30.0  # Durée de l'analyse à la taverne


# ─── TRAITEMENT DU SIGNAL ──────────────────────────────────────────────────────

def butter_bandpass(low: float, high: float, sr: int, order: int = 4) -> np.ndarray:
    nyq = sr / 2
    return butter(order, [low / nyq, high / nyq], btype="band", output="sos")


def apply_bandpass(y: np.ndarray, sos: np.ndarray) -> np.ndarray:
    return sosfilt(sos, y).astype(np.float32)


def get_envelope(signal: np.ndarray) -> np.ndarray:
    if len(signal) == 0 or np.all(signal == 0):
        return np.zeros_like(signal)
    analytic_signal = hilbert(signal)
    amplitude_envelope = np.abs(analytic_signal)
    kernel_size = 256
    if len(amplitude_envelope) > kernel_size:
        kernel = np.ones(kernel_size) / kernel_size
        smoothed = np.convolve(amplitude_envelope, kernel, mode='same')
    else:
        smoothed = amplitude_envelope
    smoothed /= (np.max(smoothed) + 1e-9)
    return smoothed.astype(np.float32)


def spectral_similarity(a: np.ndarray, b: np.ndarray) -> float:
    n = min(len(a), len(b))
    if n < 512:
        return 0.0
    fa = np.abs(np.fft.rfft(a[-n:], n=n))
    fb = np.abs(np.fft.rfft(b[:n], n=n))
    norm = (np.linalg.norm(fa) * np.linalg.norm(fb)) + 1e-9
    return float(np.dot(fa, fb) / norm)


def normalized_envelope_xcorr(env_a: np.ndarray, env_b: np.ndarray) -> float:
    if len(env_a) < len(env_b):
        return 0.0
    env_a = env_a[-len(env_b) * 2:]
    corr = correlate(env_a, env_b, mode="full")
    norm = np.sqrt(np.sum(env_a ** 2) * np.sum(env_b ** 2)) + 1e-9
    return float(np.max(np.abs(corr)) / norm)


def load_template(path: str, target_sr: int, sos: np.ndarray):
    y, sr = sf.read(path, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != target_sr:
        from math import gcd
        g = gcd(target_sr, sr)
        y = resample_poly(y, target_sr // g, sr // g).astype(np.float32)
    
    y_filtered = apply_bandpass(y, sos)
    return y_filtered, get_envelope(y_filtered)


# ─── PIPELINE D'ALERTE ─────────────────────────────────────────────────────────

def alert(xcorr_score: float, spec_score: float, rms_force: float, strength_label: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  ⚓  FORT DETECTED | Env_XCorr: {xcorr_score:.3f} | Spec: {spec_score:.3f} | Force: {strength_label}")
    print(f"{'=' * 60}\n")

    if not DISCORD_WEBHOOK:
        return

    payload = {
        "content": f"🏴‍☠️ **[Anti-Load Engine] FORT DETECTED !**\n"
                   f"• **Force du signal** : `{strength_label}` (RMS: {rms_force:.4f})\n"
                   f"• **Similarité d'enveloppe** : `{xcorr_score:.3f}`\n"
                   f"• **Identité fréquentielle** : `{spec_score:.3f}`"
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
        print(f"[Discord] Erreur webhook : {e}")


# ─── CORE ENGINE ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="SoT Horn Detector - Controlled Window Mode")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--device", type=int, default=96)
    args = parser.parse_args()

    device_info = sd.query_devices(args.device, "input")
    sr = int(device_info["default_samplerate"])
    
    sos = butter_bandpass(BANDPASS_LOW, BANDPASS_HIGH, sr)
    template_path = Path(args.template)
    if not template_path.exists():
        template_path = Path(__file__).parent / args.template

    template_raw_filtered, template_env = load_template(str(template_path), sr, sos)
    
    print(f"\n[*] ========================================================")
    print(f"[*] MOTEUR AUDIO EN VEILLE ACTIVED (Filtre + Hilbert + FFT)")
    print(f"[*] En attente du signal F10 envoyé par le script AHK...")
    print(f"[*] ========================================================\n")

    buf_len = len(template_env) * 3
    buf = np.zeros(buf_len, dtype=np.float32)
    buf_lock = threading.Lock()
    audio_q = queue.Queue()

    detection_active = False
    activation_time = 0.0
    window_samples = len(template_env)

    def callback(indata, frames, time_info, status):
        audio_q.put(indata.mean(axis=1).copy())

    def detector_loop():
        nonlocal buf, detection_active, activation_time
        hop_samples = int(HOP_SECONDS * sr)
        accumulated = 0

        while True:
            # Vérification de l'activation clavier globale (F10)
            if keyboard.is_pressed('f10') and not detection_active:
                detection_active = True
                activation_time = time.time()
                print(f"\n[{time.strftime('%H:%M:%S')}] ▲ SIGNAL REÇU (F10) : Détection active pour {DETECTION_WINDOW_SECONDS}s...")
                # Vider le buffer pour éviter d'analyser des vieux bruits résiduels
                with buf_lock:
                    buf.fill(0)
                time.sleep(0.5)

            try:
                chunk = audio_q.get(timeout=0.2)
            except queue.Empty:
                continue

            # Si le mode écoute n'est pas actif, on ignore complètement l'audio
            if not detection_active:
                continue

            # Arrêt automatique après 30 secondes
            if time.time() - activation_time > DETECTION_WINDOW_SECONDS:
                detection_active = False
                print(f"[{time.strftime('%H:%M:%S')}] ▼ FIN DES 30S : Le détecteur retourne en veille.\n")
                continue

            with buf_lock:
                buf = np.roll(buf, -len(chunk))
                buf[-len(chunk):] = chunk
                accumulated += len(chunk)

            if accumulated < hop_samples:
                continue
            accumulated = 0

            with buf_lock:
                window = buf[-window_samples:].copy()

            raw_rms = np.sqrt(np.mean(window ** 2))
            if raw_rms < NOISE_GATE_RMS:
                continue

            window_filtered = apply_bandpass(window, sos)
            window_env = get_envelope(window_filtered)
            
            score_env_xcorr = normalized_envelope_xcorr(window_env, template_env)
            score_spec = spectral_similarity(window_filtered, template_raw_filtered)

            print(f"[{time.strftime('%H:%M:%S')}] ANALYSE EN COURS -> RMS={raw_rms:.5f} | Env_XCorr={score_env_xcorr:.3f} | Spec={score_spec:.3f}")

            if score_env_xcorr >= STRICT_XCORR_THRESHOLD and score_spec >= STRICT_SPEC_THRESHOLD:
                # Pas besoin de cooldown long car la fenêtre de 30s s'arrête de toute façon très vite
                if raw_rms > 0.04:
                    strength = "FORT / PROCHE"
                elif raw_rms > 0.01:
                    strength = "LOINTAIN / DISCRET"
                else:
                    strength = "TRÈS ÉLOIGNÉ / SEUIL CRITIQUE"
                    
                alert(score_env_xcorr, score_spec, raw_rms, strength)
                # Une fois trouvé, on peut désactiver l'écoute pour éviter le spam
                detection_active = False
                print(f"[{time.strftime('%H:%M:%S')}] ▼ ALERT SENT : Retour en veille anticipé.\n")

    t = threading.Thread(target=detector_loop, daemon=True)
    t.start()

    try:
        with sd.InputStream(device=args.device, channels=2, samplerate=sr, blocksize=BLOCK_SIZE, dtype="float32", callback=callback):
            while True:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[*] Arrêt du détecteur.")

if __name__ == "__main__":
    main()