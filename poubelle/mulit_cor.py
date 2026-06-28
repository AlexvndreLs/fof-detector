"""
Sea of Thieves - Anti-False Positive Horn Detector (Pure CSV Matcher + Live Hilbert)
Moteur basé sur ton code CSV local fonctionnel, avec intégration d'une métrique Hilbert parallèle.
"""

import argparse
import csv
import json
import queue
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Any, Tuple

import numpy as np
import sounddevice as sd
from scipy.signal import butter, sosfilt, hilbert

try:
    from config import DISCORD_WEBHOOK
except ImportError:
    DISCORD_WEBHOOK = ""

# Configuration DSP d'origine
N_FFT: int = 2048
HOP_LENGTH: int = 512
FREQ_TOLERANCE_HZ: float = 6.5
NOISE_GATE_RMS: float = 0.002
COOLDOWN_SECONDS: int = 15

# Paramètres pour la métrique Hilbert parallèle
BANDPASS_LOW: float = 35.0
BANDPASS_HIGH: float = 220.0

# Seuils indépendants (Pas de multiplication, chacun sa note)
MATCH_CONFIDENCE_THRESHOLD: float = 75.0  # Ton seuil CSV habituel
HILBERT_LIVE_THRESHOLD: float = 0.50      # Seuil pour la métrique de volume parallèle


def butter_bandpass(low: float, high: float, sr: int, order: int = 4) -> np.ndarray:
    nyq = sr / 2
    return butter(order, [low / nyq, high / nyq], btype="band", output="sos")


def apply_bandpass(y: np.ndarray, sos: np.ndarray) -> np.ndarray:
    return sosfilt(sos, y).astype(np.float32)


def get_smooth_envelope(signal: np.ndarray) -> np.ndarray:
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


class PatternMatcherWithParallelHilbert:
    """Conserve ton moteur CSV intact et ajoute l'analyse d'enveloppe en parallèle."""

    def __init__(self, sr: int) -> None:
        self.sr = sr
        self.window = np.hanning(N_FFT)
        self.fft_freqs = np.linspace(0, sr / 2, int(1 + N_FFT // 2))
        self.ref_local: List[Dict[str, Any]] = []
        self.sos = butter_bandpass(BANDPASS_LOW, BANDPASS_HIGH, sr)
        self.template_env: np.ndarray = np.array([], dtype=np.float32)

    def load_profiles(self, local_csv_path: str) -> None:
        path = Path(local_csv_path)
        if not path.exists():
            raise FileNotFoundError(f"Fichier référence manquant : {path}")

        with open(path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                self.ref_local.append({
                    "frame_idx": int(row["frame_idx"]),
                    "peaks": [(float(row[f"f{idx}"]), float(row[f"p{idx}"])) for idx in range(1, 6)]
                })

    def generate_template_envelope(self, template_wav_path: str) -> None:
        import soundfile as sf
        from math import gcd
        from scipy.signal import resample_poly

        y, sr = sf.read(template_wav_path, dtype="float32", always_2d=False)
        if y.ndim > 1:
            y = y.mean(axis=1)
        if sr != self.sr:
            g = gcd(self.sr, sr)
            y = resample_poly(y, self.sr // g, sr // g).astype(np.float32)

        y_filt = apply_bandpass(y, self.sos)
        self.template_env = get_smooth_envelope(y_filt)

    def extract_live_frame_peaks(self, fft_mag: np.ndarray) -> List[Tuple[float, float]]:
        local_max = np.max(fft_mag) + 1e-6
        valid_indices = np.where((self.fft_freqs >= 20.0) & (self.fft_freqs <= 1000.0))[0]
        if len(valid_indices) == 0:
            return [(0.0, 0.0)] * 5

        filtered_mags = fft_mag[valid_indices]
        filtered_freqs = self.fft_freqs[valid_indices]
        sorted_lex = np.argsort(filtered_mags)[::-1]

        peaks: List[Tuple[float, float]] = []
        for idx in sorted_lex[:5]:
            peaks.append((float(filtered_freqs[idx]), float((filtered_mags[idx] / local_max) * 100.0)))
        while len(peaks) < 5:
            peaks.append((0.0, 0.0))
        return peaks

    def score_window(self, live_signal: np.ndarray) -> Tuple[float, float]:
        """Ton algorithme CSV d'origine + calcul de la métrique Hilbert en parallèle."""
        live_timeline: List[List[Tuple[float, float]]] = []
        for i in range(0, len(live_signal) - N_FFT, HOP_LENGTH):
            frame = live_signal[i:i + N_FFT] * self.window
            fft_mag = np.abs(np.fft.rfft(frame, n=N_FFT))
            live_timeline.append(self.extract_live_frame_peaks(fft_mag))

        ref_len = len(self.ref_local)
        if len(live_timeline) < ref_len or ref_len == 0:
            return 0.0, 0.0

        max_csv_score: float = 0.0
        best_window_signal: np.ndarray = np.array([], dtype=np.float32)
        search_limit = len(live_timeline) - ref_len + 1

        # Ton scan glissant d'origine pour le CSV
        for step in range(0, search_limit, 2):
            total_window_score = 0.0
            for r_idx in range(ref_len):
                ref_peaks = self.ref_local[r_idx]["peaks"]
                live_peaks = live_timeline[step + r_idx]

                frame_match_score = 0.0
                for r_freq, r_pct in ref_peaks:
                    if r_pct < 10.0:
                        frame_match_score += 20.0
                        continue
                    for l_freq, l_pct in live_peaks:
                        if abs(l_freq - r_freq) <= FREQ_TOLERANCE_HZ:
                            variance_penalty = max(0.0, (100.0 - abs(l_pct - r_pct)) / 100.0)
                            frame_match_score += 20.0 * variance_penalty
                            break
                total_window_score += frame_match_score / 5.0

            normalized_window_score = (total_window_score / ref_len)
            if normalized_window_score > max_csv_score:
                max_csv_score = normalized_window_score
                start_sample = step * HOP_LENGTH
                end_sample = start_sample + (ref_len * HOP_LENGTH)
                if end_sample <= len(live_signal):
                    best_window_signal = live_signal[start_sample:end_sample]

        # LA MÉTRIQUE EN PLUS (Calculée systématiquement sur la meilleure zone pour ne pas rester à 0)
        hilbert_score: float = 0.0
        if best_window_signal.size > 0:
            filtered_zone = apply_bandpass(best_window_signal, self.sos)
            live_env = get_smooth_envelope(filtered_zone)
            
            # Utilisation de la ressemblance de variance pour éviter le bug des tailles de tableaux
            total_energy = np.sum(best_window_signal ** 2) + 1e-9
            band_energy = np.sum(filtered_zone ** 2)
            energy_ratio = float(band_energy / total_energy)
            
            env_variance = np.var(live_env)
            ref_variance = np.var(self.template_env) if self.template_env.size > 0 else 0.05
            
            hilbert_score = float(energy_ratio * (1.0 - min(1.0, abs(env_variance - ref_variance) / (ref_variance + 1e-6))))

        return max_csv_score, hilbert_score


def alert(score_csv: float, score_hilbert: float, rms: float, label: str) -> None:
    print(f"DISCORD ALERTE ENVOYÉE ! | CSV: {score_csv:.2f}% | Hilbert: {score_hilbert:.2f} | Force: {label}")
    if not DISCORD_WEBHOOK:
        return

    payload = {
        "content": f"🛡️ **[Moteur CSV + Métrique Hilbert] CO-VALIDATION !**\n"
                   f"• **Volume (RMS)** : `{rms:.4f}` ({label})\n"
                   f"• **Score CSV (Fréquences)** : `{score_csv:.1f}%` (Seuil: {MATCH_CONFIDENCE_THRESHOLD}%)\n"
                   f"• **Métrique Temporelle (Volume)** : `{score_hilbert:.2f}` (Seuil: {HILBERT_LIVE_THRESHOLD})"
    }
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req): pass
    except Exception as e:
        print(f"[Discord] Erreur Webhook: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SoT Horn Detector - CSV Engine + Parallel Metric")
    parser.add_argument("--device", type=int, default=96)
    args = parser.parse_args()

    device_info = sd.query_devices(args.device, "input")
    sr = int(device_info["default_samplerate"])
    print(f"[*] Analyse active sur l'index : [{args.device}] {device_info['name']}")

    matcher = PatternMatcherWithParallelHilbert(sr=sr)
    try:
        matcher.load_profiles("horn_profile_local.csv")
        matcher.generate_template_envelope("sot_horn_template.wav")
    except Exception as e:
        print(f"[Erreur Critique] Échec d'initialisation : {e}")
        sys.exit(1)

    print(f"[*] Moteur CSV actif. Métrique temporelle parallèle en ligne.")
    print("─" * 95)

    buf_len = sr * 15
    buf = np.zeros(buf_len, dtype=np.float32)
    buf_lock = threading.Lock()
    audio_q = queue.Queue()
    last_alert = 0.0

    def callback(indata, frames, time_info, status):
        audio_q.put(indata.mean(axis=1).copy())

    def detector_loop() -> None:
        nonlocal buf, last_alert
        hop_samples = sr * 5
        accumulated = 0
        warmup_cycles = 3

        while True:
            try: chunk = audio_q.get(timeout=1.0)
            except queue.Empty: continue

            with buf_lock:
                buf = np.roll(buf, -len(chunk))
                buf[-len(chunk):] = chunk
                accumulated += len(chunk)

            if accumulated < hop_samples: continue
            accumulated = 0

            if warmup_cycles > 0:
                print(f"[*] Attente stabilisation... (Encore {warmup_cycles * 5}s)")
                warmup_cycles -= 1
                continue

            with buf_lock: current_lot = buf.copy()

            current_time = time.strftime("%H:%M:%S")
            raw_rms = float(np.sqrt(np.mean(current_lot ** 2)))

            # Ton calcul CSV d'origine + l'enveloppe en parallèle
            csv_score, hilbert_score = matcher.score_window(current_lot)
            is_silent = raw_rms < NOISE_GATE_RMS
            
            # Co-validation : Les deux métriques indépendantes doivent passer leur propre douane
            is_match = (csv_score >= MATCH_CONFIDENCE_THRESHOLD) and (hilbert_score >= HILBERT_LIVE_THRESHOLD)

            if is_silent:
                status_str = "SILENCIEUX"
            elif is_match:
                status_str = "MATCH TRANSMISSION CO-VALIDÉ !!"
            else:
                status_str = "Aucun Match"

            # Ton affichage d'origine complété avec la nouvelle métrique
            print(
                f"[{current_time}] RMS: {raw_rms:.5f} | "
                f"Confidence (CSV): {csv_score:.2f}%/{MATCH_CONFIDENCE_THRESHOLD:.0f}% | "
                f"Métrique Temp: {hilbert_score:.2f}/{HILBERT_LIVE_THRESHOLD:.2f} | "
                f"Statut: {status_str}"
            )

            if is_match and not is_silent:
                now = time.time()
                if (now - last_alert) > COOLDOWN_SECONDS:
                    last_alert = now
                    strength = "FORT" if raw_rms > 0.04 else ("FAIBLE" if raw_rms > 0.01 else "CRITIQUE")
                    alert(csv_score, hilbert_score, raw_rms, strength)

    threading.Thread(target=detector_loop, daemon=True).start()

    try:
        with sd.InputStream(device=args.device, channels=2, samplerate=sr, blocksize=8192, dtype="float32", callback=callback):
            while True: time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[*] Arrêt du détecteur.")


if __name__ == "__main__":
    main()