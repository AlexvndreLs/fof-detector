"""
Sea of Thieves - Anti-False Positive Horn Detector (Version Spectrogramme 2D)
Analyse par corrélation croisée 2D de spectrogrammes de Mel (Pattern Matching).
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
from typing import Dict, Any, Tuple

import numpy as np
import sounddevice as sd
import soundfile as sf
from scipy.signal import resample_poly, correlate2d

# ─── CONFIGURATION DISCORD VIA CONFIG.PY ──────────────────────────────────────
try:
    from config import DISCORD_WEBHOOK
except ImportError:
    DISCORD_WEBHOOK = ""

DEFAULT_TEMPLATE: str = "sot_horn_template.wav"

# Configuration DSP d'analyse (Sweet Spot 48 kHz)
N_FFT: int = 2048
HOP_LENGTH: int = 512
N_MELS: int = 64
F_MIN: float = 100.0   # On ignore les infra-basses du navire
F_MAX: float = 1500.0  # Zone fréquentielle utile de la corne

# Seuils de détection
STRICT_2D_CORR_THRESHOLD: float = 0.75  # Seuil de similarité de l'empreinte 2D
NOISE_GATE_RMS: float = 0.002
COOLDOWN_SECONDS: int = 15


# ─── OUTILS DE TRAITEMENT DU SIGNAL (SPECTROGRAMME) ───────────────────────────

def hz_to_mel(hz: float) -> float:
    """Convertit une fréquence en Hz vers l'échelle Mel."""
    return 2595.0 * np.log10(1.0 + hz / 700.0)


def mel_to_hz(mel: float) -> float:
    """Convertit une valeur Mel vers l'échelle Hz."""
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


def get_mel_filterbank(sr: int, n_fft: int, n_mels: int, fmin: float, fmax: float) -> np.ndarray:
    """Génère une matrice de filtres Mel triangulaires."""
    fft_freqs = np.linspace(0, sr / 2, int(1 + n_fft // 2))
    mel_min = hz_to_mel(fmin)
    mel_max = hz_to_mel(fmax)
    mel_pts = np.linspace(mel_min, mel_max, n_mels + 2)
    hz_pts = mel_to_hz(mel_pts)
    
    # Bins FFT correspondants
    bins = np.floor((n_fft + 1) * hz_pts / sr).astype(int)
    fb = np.zeros((n_mels, len(fft_freqs)))
    
    for m in range(1, n_mels + 1):
        for k in range(bins[m - 1], bins[m]):
            fb[m - 1, k] = (k - bins[m - 1]) / (bins[m] - bins[m - 1])
        for k in range(bins[m], bins[m + 1]):
            fb[m - 1, k] = (bins[m + 1] - k) / (bins[m + 1] - bins[m])
            
    return fb


def compute_mel_spectrogram(signal: np.ndarray, sr: int) -> np.ndarray:
    """Calcule le spectrogramme de Mel d'un signal (Log-amplitude)."""
    # 1. Short-Time Fourier Transform (STFT) avec fenêtre de Hann
    window = np.hanning(N_FFT)
    frames = []
    for i in range(0, len(signal) - N_FFT, HOP_LENGTH):
        frame = signal[i:i + N_FFT] * window
        fft_mag = np.abs(np.fft.rfft(frame, n=N_FFT))
        frames.append(fft_mag)
        
    if not frames:
        return np.zeros((N_MELS, 1), dtype=np.float32)
        
    stft_matrix = np.array(frames).T  # Forme : (Freq, Temps)
    
    # 2. Application du banc de filtres Mel
    fb = get_mel_filterbank(sr, N_FFT, N_MELS, F_MIN, F_MAX)
    mel_spec = np.dot(fb, stft_matrix)
    
    # 3. Passage à l'échelle logarithmique (dB-like)
    log_mel_spec = np.log10(mel_spec + 1e-6)
    
    # Normalisation globale pour la robustesse de la corrélation
    log_mel_spec -= np.mean(log_mel_spec)
    std_dev = np.std(log_mel_spec)
    if std_dev > 1e-6:
        log_mel_spec /= std_dev
        
    return log_mel_spec.astype(np.float32)


def load_template_spectrogram(path: str, target_sr: int) -> np.ndarray:
    """Charge le template et extrait son empreinte 2D (Spectrogramme)."""
    y, sr = sf.read(path, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != target_sr:
        g = gcd(target_sr, sr)
        y = resample_poly(y, target_sr // g, sr // g).astype(np.float32)
        
    return compute_mel_spectrogram(y, target_sr)


# ─── PIPELINE D'ALERTE ─────────────────────────────────────────────────────────

def alert(score_2d: float, rms_force: float, strength_label: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  ⚓  FORT DETECTED (2D Engine) | Match_Score: {score_2d:.3f} | Force: {strength_label}")
    print(f"{'=' * 60}\n")

    if not DISCORD_WEBHOOK:
        return

    payload: Dict[str, Any] = {
        "content": f"🏴‍☠️ **[Spectrogram-2D Engine] FORT DETECTED !**\n"
                   f"• **Force du signal** : `{strength_label}` (RMS: {rms_force:.4f})\n"
                   f"• **Similarité Empreinte 2D** : `{score_2d:.3f}`"
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


# ─── MAIN ENGINE ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="SoT Horn Detector - Spectrogram 2D Mode")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--device", type=int, default=96)
    args = parser.parse_args()

    try:
        device_info = sd.query_devices(args.device, "input")
    except Exception:
        print(f"[Erreur] Périphérique ID {args.device} introuvable.")
        sys.exit(1)
        
    sr: int = int(device_info["default_samplerate"])
    print(f"[*] Analyse matricielle active sur le flux : [{args.device}] {device_info['name']}")

    template_path = Path(args.template)
    if not template_path.exists():
        template_path = Path(__file__).parent / args.template

    if not template_path.exists():
        print(f"[Erreur] Fichier template {args.template} introuvable.")
        sys.exit(1)

    # Extraction de la matrice d'empreinte de la corne
    print("[*] Génération de la matrice de référence (Spectrogramme de Mel)...")
    template_spec = load_template_spectrogram(str(template_path), sr)
    t_frames = template_spec.shape[1]
    print(f"[*] Empreinte chargée. Taille de la matrice : {template_spec.shape} ({t_frames} frames temporelles).")

    # Buffer de stockage pour accumuler le lot de ~35 secondes
    # 35 secondes à 48kHz = ~1 680 000 échantillons
    buf_len: int = sr * 35
    buf = np.zeros(buf_len, dtype=np.float32)
    buf_lock = threading.Lock()
    audio_q: queue.Queue = queue.Queue()
    last_alert: float = 0.0

    def callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        audio_q.put(indata.mean(axis=1).copy())

    def detector_loop() -> None:
        nonlocal buf, last_alert
        # On traite par lot : on attend d'avoir cumulé l'équivalent de 5 secondes de nouvelles données
        hop_samples: int = sr * 5
        accumulated: int = 0

        while True:
            try:
                chunk = audio_q.get(timeout=1.0)
            except queue.Empty:
                continue

            with buf_lock:
                buf = np.roll(buf, -len(chunk))
                buf[-len(chunk):] = chunk
                accumulated += len(chunk)

            # Si on n'a pas accumulé assez de nouveautés pour le traitement par lot, on attend
            if accumulated < hop_samples:
                continue
            accumulated = 0

            with buf_lock:
                current_lot = buf.copy()

            raw_rms = np.sqrt(np.mean(current_lot ** 2))
            if raw_rms < NOISE_GATE_RMS:
                continue

            # 1. Calcul du spectrogramme de Mel global du lot de 35s
            lot_spec = compute_mel_spectrogram(current_lot, sr)
            
            if lot_spec.shape[1] < t_frames:
                continue

            # 2. Corrélation croisée 2D entre le gros spectrogramme et notre template
            # 'valid' signifie que la petite matrice doit tenir entièrement dans la grande
            corr_2d = correlate2d(lot_spec, template_spec, mode='valid')
            
            # Normalisation à la volée du score de corrélation
            # (Approximation du coefficient de corrélation de Pearson en 2D)
            norm_factor = (np.linalg.norm(lot_spec) * np.linalg.norm(template_spec)) + 1e-9
            max_score = float(np.max(corr_2d) / (norm_factor * 0.01)) # Facteur d'échelle empirique pour l'espace de Mel

            print(f"[{time.strftime('%H:%M:%S')}] Analyse Lot 35s | RMS={raw_rms:.5f} | Score_Max_2D={max_score:.3f}")

            # 3. Seuil de décision
            if max_score >= STRICT_2D_CORR_THRESHOLD:
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

    # Block size de capture à 8192 pour être ultra léger sur l'I/O CPU
    try:
        with sd.InputStream(device=args.device, channels=2, samplerate=sr, blocksize=8192, dtype="float32", callback=callback):
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[*] Arrêt du système de détection.")

if __name__ == "__main__":
    main()