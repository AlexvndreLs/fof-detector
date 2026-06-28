"""Sea of Thieves - Anti-False Positive Horn Detector (Advanced Fusion Engine v7.9).

Implémente un moteur de détection hybride à surface de synergie multiplicative
et verrou de persistance temporelle contigu. Le score explose à 100% si le produit 
CSV * Hilbert franchit le seuil, à condition que les trames valides soient 
regroupées d'affilée et non diffuses sur le buffer de 15 secondes.
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
#  PANNEAU DE CONFIGURATION DES SEUILS ET MULTIPLICATEURS (AJUSTÉ)
# ==============================================================================

# --- CONFIGURATION DE LA SURFACE DE SYNERGIE IMMÉDIATE ("LE BOUM") ---
# Calcule le produit s_csv * s_temp.
SYNERGY_PRODUCT_THRESHOLD: float = 0.055  # Seuil de surface optimal pour accrocher les cornes lointaines
SYNERGY_FLOOR_CSV: float = 0.070         # Plancher de sécurité CSV (7.0%) sous lequel l'explosion est interdite

# --- VERROU DE PERSISTANCE TEMPORELLE CONTIGU (ANTI-BRUIT DIFFUS) ---
# Nombre minimal de trames valides consécutives (d'affilée) requises dans la zone utile
MIN_TRAMES_REQUISES: int = 4              # 4 trames d'affilée ~ 72ms de continuité de signal stable
SEUIL_TRAME_VALIDE: float = 12.0          # Score minimal (0-20) qu'une trame locale doit faire pour être dite "valide"

# --- ÉCHELLE DE PONDÉRATION COMMUNE (Si la synergie n'est pas atteinte) ---
WEIGHT_HARMONIC_CSV: float = 0.65
WEIGHT_ENVELOPE_HILBERT: float = 0.35
HILBERT_ENV_THRESHOLD: float = 0.45  

# --- PARAMÈTRES DE COURBURE ET PALIERS (COMPORTEMENT PAR DÉFAUT) ---
EXPONENT_LOCAL_HILBERT: float = 1.8
PENALTY_EXPONENT_HILBERT: float = 2.0
PALIER_START_MULTIPLY: float = 5.0
PALIER_ABSOLUTE_CERTITUDE: float = 12.0
TRANSITION_CURVE_SHAPE: float = 2.5

# --- SEUIL DE DÉCLENCHEMENT DE L'ALERTE FINALE ---
COMBINED_MATCH_THRESHOLD: float = 50.0

# --- CONFIGURATION TECHNIQUE DSP ---
N_FFT: int = 2048
HOP_LENGTH: int = 512
FREQ_TOLERANCE_HZ: float = 12.0          # Maintien de la tolérance large pour stabiliser le CSV
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
    """Moteur d'évaluation multi-critères à contrôle de densité temporelle contiguë."""

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

    def evaluate_hybrid_score(self, live_signal: np.ndarray) -> Tuple[float, float, float]:
        """Analyse le signal et valide via la matrice de surface et le verrou contigu."""
        # 1. Analyse Harmonique Glissante (CSV)
        live_timeline: List[List[Tuple[float, float]]] = []
        for i in range(0, len(live_signal) - N_FFT, HOP_LENGTH):
            frame = live_signal[i:i + N_FFT] * self.window
            fft_mag = np.abs(np.fft.rfft(frame, n=N_FFT))
            live_timeline.append(self.extract_live_frame_peaks(fft_mag))

        ref_len = len(self.ref_local)
        if len(live_timeline) < ref_len or ref_len == 0:
            return 0.0, 0.0, 0.0

        max_csv_score: float = 0.0
        best_step: int = 0
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
                best_step = step
                start_sample = step * HOP_LENGTH
                end_sample = start_sample + (ref_len * HOP_LENGTH)
                if end_sample <= len(live_signal):
                    best_window_signal = live_signal[start_sample:end_sample]

        # ==============================================================================
        # VERROU DE PERSISTANCE TEMPORELLE CONTIGU (ANTI-BRUIT DIFFUS)
        # ==============================================================================
        trames_consecutives = 0
        max_trames_consecutives = 0

        for r_idx in range(ref_len):
            ref_peaks = self.ref_local[r_idx]["peaks"]
            live_peaks = live_timeline[best_step + r_idx]
            
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
            
            # Évaluation de la compacité locale de la trame unitaire
            if (frame_match_score / 5.0) >= SEUIL_TRAME_VALIDE:
                trames_consecutives += 1
                if trames_consecutives > max_trames_consecutives:
                    max_trames_consecutives = trames_consecutives
            else:
                trames_consecutives = 0  # Rupture de l'enchaînement contigu

        # Si les trames concordantes sont éparpillées au lieu d'être regroupées, on écrase le score
        if max_trames_consecutives < MIN_TRAMES_REQUISES:
            max_csv_score = 0.0

        s_csv = max_csv_score / 100.0

        # 2. Analyse Temporelle (Hilbert)
        s_temp: float = 0.0
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

        # ==============================================================================
        # CRITÈRE DE SYNERGIE CROISÉE ADAPTATIF PAR SURFACE
        # ==============================================================================
        synergy_area = s_csv * s_temp
        
        if synergy_area >= SYNERGY_PRODUCT_THRESHOLD and s_csv >= SYNERGY_FLOOR_CSV:
            # S'active si les trames valides ont passé le verrou de contiguïté avec succès
            return 100.0, max_csv_score, s_temp

        # --- COMPORTEMENT PAR DÉFAUT (Si pas de synergie) ---
        activation_temp = 1.0
        if s_temp > HILBERT_ENV_THRESHOLD:
            excess_temp = (s_temp - HILBERT_ENV_THRESHOLD) / (1.0 - HILBERT_ENV_THRESHOLD + 1e-6)
            activation_temp = 1.0 + (excess_temp ** EXPONENT_LOCAL_HILBERT) * 0.5
        else:
            activation_temp = (s_temp / (HILBERT_ENV_THRESHOLD + 1e-6)) ** PENALTY_EXPONENT_HILBERT

        base_score = (s_csv * WEIGHT_HARMONIC_CSV) + (s_temp * WEIGHT_ENVELOPE_HILBERT)
        brute_scale = base_score * 20.0 * activation_temp

        if brute_scale >= PALIER_ABSOLUTE_CERTITUDE:
            fused_score = 100.0
        elif brute_scale > PALIER_START_MULTIPLY:
            delta_paliers = PALIER_ABSOLUTE_CERTITUDE - PALIER_START_MULTIPLY
            t = (brute_scale - PALIER_START_MULTIPLY) / delta_paliers
            curve_factor = t ** TRANSITION_CURVE_SHAPE
            start_val = brute_scale * 3.0
            fused_score = start_val + (100.0 - start_val) * curve_factor
        else:
            fused_score = brute_scale * 3.0

        final_score_pct = min(100.0, max(0.0, fused_score))
        return final_score_pct, max_csv_score, s_temp


def alert(score_combined: float, score_csv: float, score_temp: float, rms: float, label: str) -> None:
    """Envoie une alerte vers le webhook Discord."""
    print(f"DISCORD ALERTE ENVOYÉE ! | Fusion Exp: {score_combined:.2f}% | Force: {label}")
    if not DISCORD_WEBHOOK:
        return

    payload = {
        "content": f"🛡️ **[Fusion Engine V7.9] HORN CONTINUOUS MATCH !**\n"
                   f"• **Indice de Fusion Final** : `{score_combined:.2f}%` (Seuil: {COMBINED_MATCH_THRESHOLD}%)\n"
                   f"• **Accord Harmonique (CSV)** : `{score_csv:.1f}%` (Plancher: {SYNERGY_FLOOR_CSV * 100:.1f}%)\n"
                   f"• **Accord Temporel (Hilbert)** : `{score_temp:.3f}` \n"
                   f"• **Produit de Surface Dynamique** : `{score_csv/100 * score_temp:.4f}` (Seuil: {SYNERGY_PRODUCT_THRESHOLD})\n"
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
    parser = argparse.ArgumentParser(description="SoT Horn Detector - Advanced Fusion Engine v7.9")
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

    print(f"[*] Moteur de Fusion v7.9 armé [Vérificateur de Blocs Contigus Actif].")
    print("─" * 125)

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

            combined_score, csv_score, temp_score = matcher.evaluate_hybrid_score(current_lot)
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
                f"EXP COMBINÉ: {combined_score:.2f}%/{COMBINED_MATCH_THRESHOLD:.0f}% | "
                f"(CSV: {csv_score:.1f}% , Hilbert: {temp_score:.2f}) | "
                f"Statut: {status_str}"
            )

            if is_match and not is_silent:
                now = time.time()
                if (now - last_alert) > COOLDOWN_SECONDS:
                    last_alert = now
                    strength = "FORT" if raw_rms > 0.04 else ("FAIBLE" if raw_rms > 0.01 else "CRITIQUE")
                    alert(combined_score, csv_score, temp_score, raw_rms, strength)

    threading.Thread(target=detector_loop, daemon=True).start()

    try:
        with sd.InputStream(device=args.device, channels=2, samplerate=sr, blocksize=8192, dtype="float32", callback=callback):
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[*] Arrêt de l'écoute.")


if __name__ == "__main__":
    main()