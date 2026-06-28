"""
Sea of Thieves - Anti-False Positive Horn Detector (Séquençage Temporel)
Analyse par sous-spécialisation des 5 pics fréquentiels et ordonnancement temporel.
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
from typing import Dict, Any, List

import numpy as np
import sounddevice as sd
import soundfile as sf
from scipy.signal import resample_poly

try:
    from config import DISCORD_WEBHOOK
except ImportError:
    DISCORD_WEBHOOK = ""

DEFAULT_TEMPLATE: str = "sot_horn_template.wav"

# Configuration DSP d'analyse
N_FFT: int = 2048
HOP_LENGTH: int = 512
COOLDOWN_SECONDS: int = 15

# ─── SOUS-SPÉCIALISATION FREQUENTIELLE (TES PICS REELS) ──────────────────────
# On cible chirurgicalement les fréquences exactes détectées sur ton graphique
TARGET_PICS: List[float] = [43.2, 66.9, 93.8, 140.0, 183.9]
FREQ_TOLERANCE: float = 8.0  # Tolérance en Hz autour de chaque pic

# Seuils de détection temporelle (0 à 100)
STRICT_2D_CORR_THRESHOLD: float = 65.0  
NOISE_GATE_RMS: float = 0.002           


def extract_pure_peaks_profile(signal: np.ndarray, sr: int) -> np.ndarray:
    """Extrait l'évolution temporelle des 5 pics spécifiques uniquement."""
    window = np.hanning(N_FFT)
    fft_freqs = np.linspace(0, sr / 2, int(1 + N_FFT // 2))
    
    # Trouver les indices FFT correspondants à tes pics
    peak_indices = []
    for freq in TARGET_PICS:
        idx = np.argmin(np.abs(fft_freqs - freq))
        # Définir une plage d'indices pour la tolérance
        idx_min = np.argmin(np.abs(fft_freqs - (freq - FREQ_TOLERANCE)))
        idx_max = np.argmin(np.abs(fft_freqs - (freq + FREQ_TOLERANCE))) + 1
        peak_indices.append((idx_min, idx_max))
        
    frames_profiles = []
    for i in range(0, len(signal) - N_FFT, HOP_LENGTH):
        frame = signal[i:i + N_FFT] * window
        fft_mag = np.abs(np.fft.rfft(frame, n=N_FFT))
        
        # Pour chaque pic, on prend l'énergie maximale dans la zone de tolérance
        current_profile = []
        for idx_min, idx_max in peak_indices:
            current_profile.append(np.max(fft_mag[idx_min:idx_max]) + 1e-6)
            
        frames_profiles.append(current_profile)
        
    if not frames_profiles:
        return np.zeros((len(TARGET_PICS), 1), dtype=np.float32)
        
    profile_matrix = np.array(frames_profiles).T  # Forme: (5, Nb_Frames)
    log_profile = np.log10(profile_matrix)
    
    # Normalisation absolue locale
    min_val = np.min(log_profile)
    max_val = np.max(log_profile)
    if (max_val - min_val) > 1e-6:
        log_profile = (log_profile - min_val) / (max_val - min_val)
        
    log_profile[log_profile < 0.4] = 0.0
    return log_profile.astype(np.float32)


def alert(score_2d: float, rms_force: float, strength_label: str) -> None:
    print(f"DISCORD ALERTE ENVOYÉE ! | Match Séquentiel: {score_2d:.2f} | Force: {strength_label}")

    if not DISCORD_WEBHOOK:
        return

    payload: Dict[str, Any] = {
        "content": f"**[Séquenceur Temporel 5-Pics] FORT DETECTED !**\n"
                   f"• **Force du signal** : `{strength_label}` (RMS: {rms_force:.4f})\n"
                   f"• **Adéquation Séquentielle (0-100)** : `{score_2d:.2f}`"
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
    parser = argparse.ArgumentParser(description="SoT Horn Detector - Séquenceur Temporel")
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

    print("[*] Extraction du profil temporel chirurgical du template...")
    # Charger template
    y, t_sr = sf.read(str(template_path), dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if t_sr != sr:
        g = gcd(sr, t_sr)
        y = resample_poly(y, sr // g, t_sr // g).astype(np.float32)
        
    template_profile = extract_pure_peaks_profile(y, sr)
    t_frames = template_profile.shape[1]
    template_energy = np.sum(template_profile ** 2)
    
    print(f"[*] Profil séquentiel chargé ({t_frames} frames). Analyse focalisée sur 5 fréquences.")
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
                    print("[*] Analyseurs séquentiels actifs.")
                    print("─" * 80)
                continue

            with buf_lock:
                current_lot = buf.copy()

            current_time = time.strftime('%H:%M:%S')
            raw_rms = np.sqrt(np.mean(current_lot ** 2))
            
            # Extraction du profil des 5 pics sur le lot de 15 secondes
            lot_profile = extract_pure_peaks_profile(current_lot, sr)
            
            if lot_profile.shape[1] < t_frames:
                continue

            # Corrélation temporelle glissante (1D alignée sur les canaux des pics)
            # On cherche la meilleure correspondance où les 5 pics s'allument en même temps ET dans la même durée
            best_match: float = 0.0
            search_limit = lot_profile.shape[1] - t_frames + 1
            
            for step in range(search_limit):
                sub_matrix = lot_profile[:, step:step + t_frames]
                # Produit scalaire des structures
                num = np.sum(sub_matrix * template_profile)
                den = np.sqrt(np.sum(sub_matrix ** 2) * template_energy) + 1e-9
                score = (num / den) * 100.0
                if score > best_match:
                    best_match = score

            is_silent = raw_rms < NOISE_GATE_RMS
            
            if is_silent:
                status_str = "SILENCIEUX"
            elif best_match >= STRICT_2D_CORR_THRESHOLD:
                status_str = "MATCH SÉQUENTIEL !!"
            else:
                status_str = "Aucun Match"

            print(f"[{current_time}] RMS: {raw_rms:.5f} | Séquence: {best_match:.2f}/{STRICT_2D_CORR_THRESHOLD:.1f} | Statut: {status_str}")

            if best_match >= STRICT_2D_CORR_THRESHOLD and not is_silent:
                now = time.time()
                if (now - last_alert) > COOLDOWN_SECONDS:
                    last_alert = now
                    
                    if raw_rms > 0.04:
                        strength = "FORT / PROCHE"
                    elif raw_rms > 0.01:
                        strength = "LOINTAIN / DISCRET"
                    else:
                        strength = "CRITIQUE"
                        
                    alert(best_match, raw_rms, strength)

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