"""Sea of Thieves - Anti-False Positive Horn Detector (Advanced Fusion Engine v8.0).

Implémente un moteur de détection hybride à trois axes : surface de synergie 
multiplicative (CSV * Hilbert) combinée à une validation par Centroïde Spectral
strict pour éliminer les bruits parasites à dynamique similaire.
"""

import argparse
import csv
import json
import queue
import sys
import threading
import time
import urllib.request
from math import gcd
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import sounddevice as sd
from scipy.signal import butter, hilbert, resample_poly, sosfilt

try:
    from config import DISCORD_WEBHOOK
except ImportError:
    DISCORD_WEBHOOK = ""

# ==============================================================================
#  PANNEAU DE CONFIGURATION DES SEUILS ET MULTIPLICATEURS (V8.0 TRIPLE AXE)
# ==============================================================================

# --- NOUVELLE MÉTRIQUE : FILTRE DE CENTROÏDE SPECTRAL ---
MAX_SPECTRAL_CENTROID_HZ: float = 250.0  

# --- CONFIGURATION DE LA SURFACE DE SYNERGIE IMMÉDIATE ("LE BOUM") ---
SYNERGY_PRODUCT_THRESHOLD: float = 0.0750 
SYNERGY_FLOOR_CSV: float = 0.086         # Plancher harmonique de sécurité (8.6%)

# --- SEUIL DE DÉCLENCHEMENT DE L'ALERTE FINALE ---
# Ajouté à 50.0 pour corriger la NameError. Comme le moteur v8.0 renvoie 100.0 
# en cas de succès, n'importe quelle valeur entre 1 et 100 fonctionne ici.
COMBINED_MATCH_THRESHOLD: float = 50.0

# --- CONFIGURATION TECHNIQUE DSP ---
N_FFT: int = 2048
HOP_LENGTH: int = 512
FREQ_TOLERANCE_HZ: float = 12.0          
NOISE_GATE_RMS: float = 0.002
COOLDOWN_SECONDS: int = 15

# Bornes de filtrage de la corne
BANDPASS_LOW: float = 35.0
BANDPASS_HIGH: float = 220.0


def butter_bandpass(low: float, high: float, sr: int, order: int = 4) -> np.ndarray:
    """Génère les coefficients de filtrage au format Second-Order Sections (SOS)."""
    nyq = sr / 2
    return butter(order, [low / nyq, high / nyq], btype="band", output="sos")


def apply_bandpass(y: np.ndarray, sos: np.ndarray) -> np.ndarray:
    """Applique un filtre passe-bande causal de Butterworth."""
    return sosfilt(sos, y).astype(np.float32)


def get_smooth_envelope(signal: np.ndarray) -> np.ndarray:
    """Calcule l'enveloppe analytique via la transformée de Hilbert et la lisse."""
    if len(signal) == 0 or np.all(signal == 0):
        return np.zeros_like(signal)
    
    analytic_signal = hilbert(signal)
    amplitude_envelope = np.abs(analytic_signal)
    
    kernel_size = 256
    if len(amplitude_envelope) > kernel_size:
        kernel = np.ones(kernel_size) / kernel_size
        smoothed = np.convolve(amplitude_envelope, kernel, mode="same")
    else:
        smoothed = amplitude_envelope
        
    norm_factor = np.max(smoothed) + 1e-9
    smoothed /= norm_factor
    return smoothed.astype(np.float32)


class AdvancedFusionMatcher:
    """Moteur de fusion tri-critères : Empreinte CSV, Enveloppe Hilbert et Centroïde."""

    def __init__(self, sr: int) -> None:
        """Initialise le matcher et génère les filtres SOS."""
        self.sr = sr
        self.window = np.hanning(N_FFT)
        self.fft_freqs = np.linspace(0, sr / 2, int(1 + N_FFT // 2))
        self.ref_local: List[Dict[str, Any]] = []
        self.sos = butter_bandpass(BANDPASS_LOW, BANDPASS_HIGH, sr)
        self.template_env: np.ndarray = np.array([], dtype=np.float32)

    def load_profiles(self, local_csv_path: str) -> None:
        """Parse le fichier CSV contenant l'empreinte fréquentielle de référence."""
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
        """Génère l'enveloppe de référence temporelle à partir du gabarit WAV."""
        import soundfile as sf
        
        y, sr = sf.read(template_wav_path, dtype="float32", always_2d=False)
        if y.ndim > 1:
            y = y.mean(axis=1)
        if sr != self.sr:
            g = gcd(self.sr, sr)
            y = resample_poly(y, self.sr // g, sr // g).astype(np.float32)

        y_filt = apply_bandpass(y, self.sos)
        self.template_env = get_smooth_envelope(y_filt)

    def extract_live_frame_peaks(self, fft_mag: np.ndarray) -> List[Tuple[float, float]]:
        """Extrait les coordonnées des pics spectraux prédominants d'une trame."""
        local_max = np.max(fft_mag) + 1e-6
        valid_indices = np.where((self.fft_freqs >= 20.0) & (self.fft_freqs <= 1000.0))[0]
        if len(valid_indices) == 0:
            return [(0.0, 0.0)] * 5

        filtered_mags = fft_mag[valid_indices]
        filtered_freqs = self.fft_freqs[valid_indices]
        sorted_indices = np.argsort(filtered_mags)[::-1]

        peaks: List[Tuple[float, float]] = []
        for idx in sorted_indices[:5]:
            peaks.append((float(filtered_freqs[idx]), float((filtered_mags[idx] / local_max) * 100.0)))
        while len(peaks) < 5:
            peaks.append((0.0, 0.0))
        return peaks

    def _calculate_spectral_centroid(self, signal: np.ndarray) -> float:
        """Calcule le Centroïde Spectral (centre de masse des fréquences) du segment."""
        if signal.size == 0 or np.all(signal == 0):
            return 0.0
        
        # On analyse la première partie du signal correspondant à la taille de la FFT
        frame = signal[:N_FFT] if len(signal) >= N_FFT else np.pad(signal, (0, N_FFT - len(signal)))
        fft_mag = np.abs(np.fft.rfft(frame * self.window, n=N_FFT))
        
        # Restriction à la bande audible basse pour éviter le bruit blanc haute fréquence
        idx_valid = np.where(self.fft_freqs <= 1500.0)[0]
        mags = fft_mag[idx_valid]
        freqs = self.fft_freqs[idx_valid]
        
        sum_mags = np.sum(mags)
        if sum_mags == 0:
            return 0.0
            
        return float(np.sum(mags * freqs) / sum_mags)

    def evaluate_hybrid_score(self, live_signal: np.ndarray) -> Tuple[float, float, float, float]:
        """Analyse le signal et applique l'arbitrage tri-critères (CSV, Hilbert, Centroïde)."""
        # 1. Analyse Harmonique (CSV)
        live_timeline: List[List[Tuple[float, float]]] = []
        for i in range(0, len(live_signal) - N_FFT, HOP_LENGTH):
            frame = live_signal[i:i + N_FFT] * self.window
            fft_mag = np.abs(np.fft.rfft(frame, n=N_FFT))
            live_timeline.append(self.extract_live_frame_peaks(fft_mag))

        ref_len = len(self.ref_local)
        if len(live_timeline) < ref_len or ref_len == 0:
            return 0.0, 0.0, 0.0, 0.0

        max_csv_score: float = 0.0
        best_window_signal: np.ndarray = np.array([], dtype=np.float32)
        search_limit = len(live_timeline) - ref_len + 1

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
                start_sample = step * HOP_LENGTH
                end_sample = start_sample + (ref_len * HOP_LENGTH)
                if end_sample <= len(live_signal):
                    best_window_signal = live_signal[start_sample:end_sample]

        s_csv = max_csv_score / 100.0

        # 2. Analyse Temporelle (Hilbert) et Métrique Spectrale Extérieure
        s_temp: float = 0.0
        centroid_hz: float = 0.0
        
        if best_window_signal.size > 0:
            filtered_zone = apply_bandpass(best_window_signal, self.sos)
            live_env = get_smooth_envelope(filtered_zone)
            
            total_energy = np.sum(best_window_signal ** 2) + 1e-9
            band_energy = np.sum(filtered_zone ** 2)
            energy_ratio = float(band_energy / total_energy)
            
            env_variance = np.var(live_env)
            ref_variance = np.var(self.template_env) if self.template_env.size > 0 else 0.05
            
            s_temp = float(energy_ratio * (1.0 - min(1.0, abs(env_variance - ref_variance) / (ref_variance + 1e-6))))
            s_temp = max(0.0, min(1.0, s_temp))
            
            # Calcul du troisième axe discriminant
            centroid_hz = self._calculate_spectral_centroid(best_window_signal)

        # ==============================================================================
        # ARBITRAGE DU TRIPLE VERROU DE SÉCURITÉ
        # ==============================================================================
        synergy_area = s_csv * s_temp
        
        # Condition 1 : Produit de surface validé
        # Condition 2 : Plancher harmonique respecté
        # Condition 3 : Le centre de gravité spectral est bas (Garanti que le son est lourd/grave)
        if (synergy_area >= SYNERGY_PRODUCT_THRESHOLD and 
            s_csv >= SYNERGY_FLOOR_CSV and 
            centroid_hz <= MAX_SPECTRAL_CENTROID_HZ):
            
            return 100.0, max_csv_score, s_temp, centroid_hz

        # Si l'un des verrous (notamment le centroïde) échoue, le score s'effondre
        return 0.0, max_csv_score, s_temp, centroid_hz


def alert(score_combined: float, score_csv: float, score_temp: float, centroid: float, rms: float, label: str) -> None:
    """Envoie une alerte enrichie vers le webhook Discord."""
    print(f"DISCORD ALERTE ENVOYÉE ! | Fusion Exp: {score_combined:.2f}% | Force: {label}")
    if not DISCORD_WEBHOOK:
        return

    payload = {
        "content": f"🛡️ **[Fusion Engine V8.0] HORN TRIPLE VERROU VALIDATED !**\n"
                   f"• **Indice de Fusion Final** : `{score_combined:.2f}%` \n"
                   f"• **Accord Harmonique (CSV)** : `{score_csv:.1f}%` (Plancher: {SYNERGY_FLOOR_CSV * 100:.1f}%)\n"
                   f"• **Accord Temporel (Hilbert)** : `{score_temp:.3f}` \n"
                   f"• **Centroïde Spectral** : `{centroid:.1f} Hz` (Max Autorisé: {MAX_SPECTRAL_CENTROID_HZ} Hz)\n"
                   f"• **Volume Global (RMS)** : `{rms:.5f}`"
    }
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req) as r:
            r.read()
    except Exception as e:
        print(f"[Discord] Erreur Webhook: {e}")


def main() -> None:
    """Point d'entrée principal."""
    parser = argparse.ArgumentParser(description="SoT Horn Detector - Advanced Fusion Engine v8.0")
    parser.add_argument("--device", type=int, default=96)
    args = parser.parse_args()

    try:
        device_info = sd.query_devices(args.device, "input")
    except Exception as e:
        print(f"[Erreur] ID matériel audio invalide : {e}")
        sys.exit(1)
        
    sr = int(device_info["default_samplerate"])
    print(f"[*] Analyse active sur le périphérique : [{args.device}] {device_info['name']}")

    matcher = AdvancedFusionMatcher(sr=sr)
    try:
        matcher.load_profiles("horn_profile_local.csv")
        matcher.generate_template_envelope("sot_horn_template.wav")
    except Exception as e:
        print(f"[Erreur Critique] Échec fichiers : {e}")
        sys.exit(1)

    print(f"[*] Moteur Tri-Axe v8.0 opérationnel [Filtre Centroïde Spectral Actif].")
    print("─" * 135)

    buf_len = sr * 15
    buf = np.zeros(buf_len, dtype=np.float32)
    buf_lock = threading.Lock()
    audio_q: queue.Queue = queue.Queue()
    last_alert = 0.0

    def callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        audio_q.put(indata.mean(axis=1).copy())

    def detector_loop() -> None:
        nonlocal buf, last_alert
        hop_samples = sr * 5
        accumulated = 0
        warmup_cycles = 3

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
                print(f"[*] Synchronisation du flux audio... (Encore {warmup_cycles * 5}s)")
                warmup_cycles -= 1
                continue

            with buf_lock:
                current_lot = buf.copy()

            current_time = time.strftime("%H:%M:%S")
            raw_rms = float(np.sqrt(np.mean(current_lot ** 2)))

            combined_score, csv_score, temp_score, centroid_hz = matcher.evaluate_hybrid_score(current_lot)
            is_silent = raw_rms < NOISE_GATE_RMS
            is_match = combined_score >= COMBINED_MATCH_THRESHOLD

            if is_silent:
                status_str = "SILENCIEUX"
            elif is_match:
                status_str = "DÉTECTION VALIDÉE !!! 💥"
            else:
                status_str = "Aucun Match"

            print(
                f"[{current_time}] RMS: {raw_rms:.5f} | "
                f"EXP COMBINÉ: {combined_score:.2f}% | "
                f"(CSV: {csv_score:.1f}% , Hilbert: {temp_score:.2f} , Centroid: {centroid_hz:.1f}Hz) | "
                f"Statut: {status_str}"
            )

            if is_match and not is_silent:
                now = time.time()
                if (now - last_alert) > COOLDOWN_SECONDS:
                    last_alert = now
                    strength = "FORT" if raw_rms > 0.04 else ("FAIBLE" if raw_rms > 0.01 else "CRITIQUE")
                    alert(combined_score, csv_score, temp_score, centroid_hz, raw_rms, strength)

    threading.Thread(target=detector_loop, daemon=True).start()

    try:
        with sd.InputStream(device=args.device, channels=2, samplerate=sr, blocksize=8192, dtype="float32", callback=callback):
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[*] Fermeture du flux audio.")


if __name__ == "__main__":
    main()