"""
Sea of Thieves - Anti-False Positive Horn Detector (Version Ultra-Focus 20-200Hz)
Analyse par corrélation 2D normalisée sur bande chirurgicale stricte basse fréquence.
"""

import argparse
import json
import queue
import sys
import threading
import time
import urllib.request
from math import gcd
from pathlib import Path
from typing import Dict, Any

import numpy as np
import sounddevice as sd
import soundfile as sf
from scipy.signal import resample_poly, correlate2d

try:
    from config import DISCORD_WEBHOOK
except ImportError:
    DISCORD_WEBHOOK = ""

DEFAULT_TEMPLATE: str = "sot_horn_template.wav"

# ─── CONFIGURATION DSP CHIRURGICALE (20Hz - 200Hz) ───────────────────────────
N_FFT: int = 2048
HOP_LENGTH: int = 512
N_MELS: int = 128       

# Filtrage ultra-serré basé sur ton analyse graphique
F_MIN: float = 20.0    # On descend à 20 Hz pour englober totalement le premier pic
F_MAX: float = 200.0   # On coupe net à 200 Hz juste après le troisième pic principal

# Seuils de détection
STRICT_2D_CORR_THRESHOLD: float = 60.0  
NOISE_GATE_RMS: float = 0.002           
COOLDOWN_SECONDS: int = 15


def hz_to_mel(hz: float) -> float:
    return 2595.0 * np.log10(1.0 + hz / 700.0)


def mel_to_hz(mel: float) -> float:
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


def get_mel_filterbank(sr: int, n_fft: int, n_mels: int, fmin: float, fmax: float) -> np.ndarray:
    fft_freqs = np.linspace(0, sr / 2, int(1 + n_fft // 2))
    mel_min = hz_to_mel(fmin)
    mel_max = hz_to_mel(fmax)
    mel_pts = np.linspace(mel_min, mel_max, n_mels + 2)
    hz_pts = mel_to_hz(mel_pts)
    
    bins = np.floor((n_fft + 1) * hz_pts / sr).astype(int)
    fb = np.zeros((n_mels, len(fft_freqs)))
    
    for m in range(1, n_mels + 1):
        for k in range(bins[m - 1], bins[m]):
            fb[m - 1, k] = (k - bins[m - 1]) / (bins[m] - bins[m - 1])
        for k in range(bins[m], bins[m + 1]):
            fb[m - 1, k] = (bins[m + 1] - k) / (bins[m + 1] - bins[m])
            
    return fb


def compute_mel_spectrogram(signal: np.ndarray, sr: int) -> np.ndarray:
    window = np.hanning(N_FFT)
    frames = []
    for i in range(0, len(signal) - N_FFT, HOP_LENGTH):
        frame = signal[i:i + N_FFT] * window
        fft_mag = np.abs(np.fft.rfft(frame, n=N_FFT))
        frames.append(fft_mag)
        
    if not frames:
        return np.zeros((N_MELS, 1), dtype=np.float32)
        
    stft_matrix = np.array(frames).T
    fb = get_mel_filterbank(sr, N_FFT, N_MELS, F_MIN, F_MAX)
    mel_spec = np.dot(fb, stft_matrix)
    
    log_mel_spec = np.log10(mel_spec + 1e-6)
    
    min_val = np.min(log_mel_spec)
    max_val = np.max(log_mel_spec)
    if (max_val - min_val) > 1e-6:
        log_mel_spec = (log_mel_spec - min_val) / (max_val - min_val)
        
    log_mel_spec[log_mel_spec < 0.5] = 0.0
        
    return log_mel_spec.astype(np.float32)


def load_template_spectrogram(path: str, target_sr: int) -> np.ndarray:
    y, sr = sf.read(path, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != target_sr:
        g = gcd(target_sr, sr)
        y = resample_poly(y, target_sr // g, sr // g).astype(np.float32)
        
    return compute_mel_spectrogram(y, target_sr)


def alert(score_2d: float, rms_force: float, strength_label: str) -> None:
    print(f"DISCORD ALERTE ENVOYÉE ! | Match: {score_2d:.2f} | Force: {strength_label}")

    if not DISCORD_WEBHOOK:
        return

    payload: Dict[str, Any] = {
        "content": f"**[Ultra-Focus 20-200Hz] FORT DETECTED !**\n"
                   f"• **Force du signal** : `{strength_label}` (RMS: {rms_force:.4f})\n"
                   f"• **Score Match (0-100)** : `{score_2d:.2f}`"
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


def main() -> None:
    parser = argparse.ArgumentParser(description="SoT Horn Detector - Ultra Focus")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--device", type=int, default=96)
    args = parser.parse_args()

    try:
        device_info = sd.query_devices(args.device, "input")
    except Exception:
        print(f"[Erreur] Périphérique ID {args.device} introuvable.")
        sys.exit(1)
        
    sr: int = int(device_info["default_samplerate"])
    print(f"[*] Analyse active sur : [{args.device}] {device_info['name']}")

    template_path = Path(args.template)
    if not template_path.exists():
        template_path = Path(__file__).parent / args.template

    if not template_path.exists():
        print(f"[Erreur] Fichier template {args.template} introuvable.")
        sys.exit(1)

    print("[*] Configuration de la matrice de référence...")
    template_spec = load_template_spectrogram(str(template_path), sr)
    t_frames = template_spec.shape[1]
    
    print(f"[*] Empreinte chargée ({t_frames} frames). Zone filtrée : 20Hz - 200Hz.")
    print(f"[*] Remplissage initial du buffer (Patientez 15 secondes)...")
    print("─" * 80)

    buf_len: int = sr * 15
    buf = np.zeros(buf_len, dtype=np.float32)
    buf_lock = threading.Lock()
    audio_q: queue.Queue = queue.Queue()
    last_alert: float = 0.0

    def callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        audio_q.put(indata.mean(axis=1).copy())

    def detector_loop() -> None:
        nonlocal buf, last_alert
        hop_samples: int = sr * 5
        accumulated: int = 0
        warmup_cycles: int = 3 

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

            if warmup_cycles > 0:
                print(f"[*] Chargement de la mémoire audio... (Encore {warmup_cycles * 5}s)")
                warmup_cycles -= 1
                if warmup_cycles == 0:
                    print("[*] Flux audio stabilisé. Début de la surveillance active.")
                    print("─" * 80)
                continue

            with buf_lock:
                current_lot = buf.copy()

            current_time = time.strftime('%H:%M:%S')
            raw_rms = np.sqrt(np.mean(current_lot ** 2))
            
            lot_spec = compute_mel_spectrogram(current_lot, sr)
            
            if lot_spec.shape[1] < t_frames:
                continue

            corr_2d = correlate2d(lot_spec, template_spec, mode='valid')
            norm_factor = np.sqrt(np.sum(lot_spec ** 2) * np.sum(template_spec ** 2)) + 1e-9
            max_score = float(np.max(corr_2d) / norm_factor) * 100.0

            is_silent = raw_rms < NOISE_GATE_RMS
            
            if is_silent:
                status_str = "SILENCIEUX"
            elif max_score >= STRICT_2D_CORR_THRESHOLD:
                status_str = "MATCH !!"
            else:
                status_str = "Aucun Match"

            print(f"[{current_time}] RMS: {raw_rms:.5f} | Match: {max_score:.2f}/{STRICT_2D_CORR_THRESHOLD:.1f} | Statut: {status_str}")

            if max_score >= STRICT_2D_CORR_THRESHOLD and not is_silent:
                now = time.time()
                if (now - last_alert) > COOLDOWN_SECONDS:
                    last_alert = now
                    
                    if raw_rms > 0.04:
                        strength = "FORT / PROCHE"
                    elif raw_rms > 0.01:
                        strength = "LOINTAIN / DISCRET"
                    else:
                        strength = "CRITIQUE"
                        
                    alert(max_score, raw_rms, strength)

    t = threading.Thread(target=detector_loop, daemon=True)
    t.start()

    try:
        with sd.InputStream(device=args.device, channels=2, samplerate=sr, blocksize=8192, dtype="float32", callback=callback):
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[*] Arrêt du système.")

if __name__ == "__main__":
    main()