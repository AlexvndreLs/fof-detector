"""
Sea of Thieves - Anti-False Positive Horn Detector (Multi-Metric Data-Driven Engine)
Combine la comparaison de matrice CSV locale avec l'extraction d'enveloppe de Hilbert
et le filtrage passe-bande de Butterworth pour une détection infaillible.
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
from scipy.signal import butter, sosfilt, hilbert, correlate

try:
    from config import DISCORD_WEBHOOK
except ImportError:
    DISCORD_WEBHOOK = ""

# Configuration DSP
N_FFT: int = 2048
HOP_LENGTH: int = 512
FREQ_TOLERANCE_HZ: float = 6.5
NOISE_GATE_RMS: float = 0.002
COOLDOWN_SECONDS: int = 15

# Isolation stricte de l'enveloppe temporelle (Repris de last_try.py)
BANDPASS_LOW: float = 35.0
BANDPASS_HIGH: float = 220.0

# Seuils combinés
MATCH_CONFIDENCE_THRESHOLD: float = 75.0  # Seuil CSV Local
HILBERT_XCORR_THRESHOLD: float = 0.60     # Validation de la courbe de volume temporelle


def butter_bandpass(low: float, high: float, sr: int, order: int = 4) -> np.ndarray:
    """Génère les coefficients SOS pour le filtre passe-bande."""
    nyq = sr / 2
    return butter(order, [low / nyq, high / nyq], btype="band", output="sos")


def apply_bandpass(y: np.ndarray, sos: np.ndarray) -> np.ndarray:
    """Applique le filtre passe-bande de Butterworth."""
    return sosfilt(sos, y).astype(np.float32)


def get_smooth_envelope(signal: np.ndarray) -> np.ndarray:
    """Extrait l'enveloppe lissée de Hilbert (Repris de last_try.py)."""
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


class MultiMetricPatternMatcher:
    """Moteur de détection hybride : CSV Local + Enveloppe de Hilbert."""

    def __init__(self, sr: int) -> None:
        self.sr = sr
        self.window = np.hanning(N_FFT)
        self.fft_freqs = np.linspace(0, sr / 2, int(1 + N_FFT // 2))
        self.ref_local: List[Dict[str, Any]] = []
        self.sos = butter_bandpass(BANDPASS_LOW, BANDPASS_HIGH, sr)
        self.template_env: np.ndarray = np.array([], dtype=np.float32)

    def load_profiles(self, local_csv_path: str) -> None:
        """Charge le profil de pics depuis le CSV."""
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
        """Génère l'enveloppe temporelle théorique attendue à partir du WAV original."""
        import soundfile as sf
        from math import gcd
        from scipy.signal import resample_poly

        y, sr = sf.read(template_wav_path, dtype="float32", always_2d=False)
        if y.ndim > 1:
            y = y.mean(axis=1)
        if sr != self.sr:
            g = gcd(self.sr, sr)
            y = resample_poly(y, self.sr // g, sr // g).astype(np.float32)

        # Même traitement que live: passe-bande + Hilbert
        y_filt = apply_bandpass(y, self.sos)
        self.template_env = get_smooth_envelope(y_filt)

    def extract_live_frame_peaks(self, fft_mag: np.ndarray) -> List[Tuple[float, float]]:
        """Extrait les pics locaux d'une tranche de 42ms."""
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

    def evaluate_metrics(self, live_signal: np.ndarray) -> Tuple[float, float]:
        """Analyse le signal et extrait le score CSV et le score de Hilbert."""
        # 1. Calcul de la timeline fréquentielle
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

        # Scan glissant pour la matrice CSV
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

            normalized_score = (total_window_score / ref_len)
            if normalized_score > max_csv_score:
                max_csv_score = normalized_score
                # On sauvegarde l'emplacement temporel exact du meilleur match pour Hilbert
                start_sample = step * HOP_LENGTH
                end_sample = start_sample + (ref_len * HOP_LENGTH)
                if end_sample <= len(live_signal):
                    best_window_signal = live_signal[start_sample:end_sample]

        # 2. SÉCURITÉ DE HILBERT (S'active uniquement si le profil de pics dépasse 10%)
        max_hilbert_score: float = 0.0
        if max_csv_score > 10.0 and best_window_signal.size > 0:
            # Filtrage de Butterworth 35-220Hz sur la zone suspecte
            filtered_zone = apply_bandpass(best_window_signal, self.sos)
            # Extraction de la courbe de volume via Hilbert
            live_env = get_smooth_envelope(filtered_zone)
            
            # Corrélation croisée normalisée 1D entre enveloppes (Repris de last_try.py)
            if len(live_env) >= len(self.template_env):
                corr = correlate(live_env, self.template_env, mode="full")
                norm = np.sqrt(np.sum(live_env ** 2) * np.sum(self.template_env ** 2)) + 1e-9
                max_hilbert_score = float(np.max(np.abs(corr)) / norm)

        return max_csv_score, max_hilbert_score


def alert(score_csv: float, score_hilbert: float, rms: float, label: str) -> None:
    print(f"DISCORD ALERTE ENVOYÉE ! | CSV: {score_csv:.1f}% | Hilbert: {score_hilbert:.2f} | Force: {label}")
    if not DISCORD_WEBHOOK:
        return

    payload = {
        "content": f"🛡️ **[Hybride Engine V5] HORN DETECTED AND CO-VALIDATED !**\n"
                   f"• **Force du signal** : `{label}` (RMS: {rms:.4f})\n"
                   f"• **Adéquation Matrix (CSV)** : `{score_csv:.1f}%` (Seuil: {MATCH_CONFIDENCE_THRESHOLD}%)\n"
                   f"• **Corrélation Volume (Hilbert)** : `{score_hilbert:.3f}` (Seuil: {HILBERT_XCORR_THRESHOLD})"
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
    parser = argparse.ArgumentParser(description="SoT Horn Detector - Hybride Engine V5")
    parser.add_argument("--device", type=int, default=96)
    args = parser.parse_args()

    device_info = sd.query_devices(args.device, "input")
    sr = int(device_info["default_samplerate"])
    print(f"[*] Analyse active sur : [{args.device}] {device_info['name']}")

    matcher = MultiMetricPatternMatcher(sr=sr)
    try:
        matcher.load_profiles("horn_profile_local.csv")
        matcher.generate_template_envelope("sot_horn_template.wav")
    except Exception as e:
        print(f"[Erreur Critique] Impossible de charger les patterns : {e}")
        sys.exit(1)

    print(f"[*] Profils et Enveloppes de Hilbert chargés. Surveillance en cours...")
    print("─" * 90)

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
                print(f"[*] Stabilisation du flux... (Encore {warmup_cycles * 5}s)")
                warmup_cycles -= 1
                continue

            with buf_lock: current_lot = buf.copy()

            current_time = time.strftime("%H:%M:%S")
            raw_rms = float(np.sqrt(np.mean(current_lot ** 2)))

            # Extraction des deux métriques indépendantes
            csv_score, hilbert_score = matcher.evaluate_metrics(current_lot)
            is_silent = raw_rms < NOISE_GATE_RMS

            # La décision finale requiert la validation simultanée des DEUX mondes (Pics + Volume)
            is_match = (csv_score >= MATCH_CONFIDENCE_THRESHOLD) and (hilbert_score >= HILBERT_XCORR_THRESHOLD)

            if is_silent:
                status_str = "SILENCIEUX"
            elif is_match:
                status_str = "MATCH HYBRIDE TOTAL !!"
            else:
                status_str = "Aucun Match"

            print(
                f"[{current_time}] RMS: {raw_rms:.5f} | "
                f"CSV: {csv_score:.1f}%/{MATCH_CONFIDENCE_THRESHOLD:.0f}% | "
                f"Hilbert: {hilbert_score:.2f}/{HILBERT_XCORR_THRESHOLD:.2f} | "
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
        print("\n[*] Arrêt du système.")


if __name__ == "__main__":
    main()