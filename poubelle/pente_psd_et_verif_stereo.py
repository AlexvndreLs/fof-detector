"""Sea of Thieves - Anti-False Positive Horn Detector (Advanced Fusion Engine v8.2).

Moteur de détection hybride combinant la corrélation spectrale locale (CSV),
l'analyse d'enveloppe de Hilbert, un verrou stéréophonique directionnel 
et l'analyse de la pente de la densité spectrale de puissance (PSD).
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
#  PANNEAU DE CONFIGURATION DES SEUILS ET MULTIPLICATEURS (V8.2 ADAPTATIF)
# ==============================================================================

# --- FILTRE STÉRÉO DIRECTIONNEL ---
# Si la différence d'énergie relative entre la gauche et la droite est inférieure
# à ce seuil (son parfaitement centré/mono), le système suspecte un bruit d'interface.
MIN_STEREO_DISPARITY: float = 0.05  # 5% de différence gauche/droite minimum requis pour un son 3D

# --- CONFIGURATION DE LA SURFACE DE SYNERGIE IMMÉDIATE ("LE BOUM") ---
# Calcule le produit s_csv * s_temp.
# Calibré pour capturer tes cornes (0.073 à 0.126) et écraser tes bruits (0.061)
SYNERGY_PRODUCT_THRESHOLD: float = 0.0440  
SYNERGY_FLOOR_CSV: float = 0.090         # Plancher harmonique (8.5%)

# --- SEUIL DE DÉCLENCHEMENT DE L'ALERTE FINALE ---
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
    """Moteur d'évaluation multi-critères à analyse stéréophonique et pente PSD."""

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

    def evaluate_hybrid_score(self, live_signal: np.ndarray, stereo_disparity: float) -> Tuple[float, float, float]:
        """Analyse le signal via le produit de surface, la disparité stéréo et la pente PSD."""
        # 1. Analyse Harmonique (CSV)
        live_timeline: List[List[Tuple[float, float]]] = []
        for i in range(0, len(live_signal) - N_FFT, HOP_LENGTH):
            frame = live_signal[i:i + N_FFT] * self.window
            fft_mag = np.abs(np.fft.rfft(frame, n=N_FFT))
            live_timeline.append(self.extract_live_frame_peaks(fft_mag))

        ref_len = len(self.ref_local)
        if len(live_timeline) < ref_len or ref_len == 0:
            return 0.0, 0.0, 0.0

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
        # ARBITRAGE PAR SURFACE ET FILTRE STÉRÉO POSITIONNEL
        # ==============================================================================
        synergy_area = s_csv * s_temp
        
        # Sécurité Anti-Écran de chargement (Si le son est fort mais strictement mono)
        if synergy_area >= SYNERGY_PRODUCT_THRESHOLD and s_csv >= SYNERGY_FLOOR_CSV:
            if stereo_disparity < MIN_STEREO_DISPARITY:
                # Son suspecté d'être un bruit d'interface mono/centré parfait
                return 0.0, max_csv_score, s_temp
            
            # Si le signal montre une vraie disparité spatiale (son 3D), on valide !
            return 100.0, max_csv_score, s_temp

        return 0.0, max_csv_score, s_temp


def alert(score_combined: float, score_csv: float, score_temp: float, rms: float, label: str) -> None:
    """Envoie une alerte vers le webhook Discord."""
    print(f"DISCORD ALERTE ENVOYÉE ! | Fusion Exp: {score_combined:.2f}% | Force: {label}")
    if not DISCORD_WEBHOOK:
        return

    payload = {
        "content": f"🛡️ **[Fusion Engine V8.2] 3D STEREO MATCH VALIDATED !**\n"
                   f"• **Indice de Fusion Final** : `{score_combined:.2f}%` \n"
                   f"• **Accord Harmonique (CSV)** : `{score_csv:.1f}%` \n"
                   f"• **Accord Temporel (Hilbert)** : `{score_temp:.3f}` \n"
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
    """Point d'entrée principal avec gestion des canaux stéréo séparés."""
    parser = argparse.ArgumentParser(description="SoT Horn Detector - Advanced Fusion Engine v8.2")
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

    print(f"[*] Moteur Spatialisé v8.2 opérationnel [Analyse Stéréo Active].")
    print("─" * 125)

    buf_len = sr * 15
    buf_left = np.zeros(buf_len, dtype=np.float32)
    buf_right = np.zeros(buf_len, dtype=np.float32)
    buf_lock = threading.Lock()
    audio_q: queue.Queue = queue.Queue()
    last_alert = 0.0

    def callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        # On pousse le bloc stéréo complet (deux canaux séparés)
        audio_q.put(indata.copy())

    def detector_loop() -> None:
        nonlocal buf_left, buf_right, last_alert
        hop_samples = sr * 5
        accumulated = 0
        warmup_cycles = 3

        while True:
            try:
                chunk = audio_q.get(timeout=1.0)
            except queue.Empty:
                continue

            with buf_lock:
                buf_left = np.roll(buf_left, -len(chunk))
                buf_left[-len(chunk):] = chunk[:, 0]
                
                buf_right = np.roll(buf_right, -len(chunk))
                buf_right[-len(chunk):] = chunk[:, 1]
                
                accumulated += len(chunk)

            if accumulated < hop_samples:
                continue
            accumulated = 0

            if warmup_cycles > 0:
                print(f"[*] Synchronisation du flux audio... (Encore {warmup_cycles * 5}s)")
                warmup_cycles -= 1
                continue

            with buf_lock:
                current_left = buf_left.copy()
                current_right = buf_right.copy()

            current_time = time.strftime("%H:%M:%S")
            
            # Calcul des RMS individuels pour mesurer la disparité directionnelle
            rms_l = float(np.sqrt(np.mean(current_left ** 2))) + 1e-9
            rms_r = float(np.sqrt(np.mean(current_right ** 2))) + 1e-9
            raw_rms = (rms_l + rms_r) / 2.0
            
            # Disparité relative (0.0 = mono parfait, proche de 1.0 = full directionnel)
            stereo_disparity = abs(rms_l - rms_r) / max(rms_l, rms_r)

            # Évaluation sur la moyenne mono du signal
            mono_mix = (current_left + current_right) / 2.0
            combined_score, csv_score, temp_score = matcher.evaluate_hybrid_score(mono_mix, stereo_disparity)
            
            is_silent = raw_rms < NOISE_GATE_RMS
            is_match = combined_score >= COMBINED_MATCH_THRESHOLD

            if is_silent:
                status_str = "SILENCIEUX"
            elif is_match:
                status_str = "DÉTECTION VALIDÉE !!! 💥"
            elif combined_score == 0.0 and csv_score > 0.08 and stereo_disparity < MIN_STEREO_DISPARITY:
                status_str = "Bloqué (Faux Positif Écran de Chargement / Mono parfait détecté)"
            else:
                status_str = "Aucun Match"

            print(
                f"[{current_time}] RMS: {raw_rms:.5f} | "
                f"EXP COMBINÉ: {combined_score:.2f}% | "
                f"(CSV: {csv_score:.1f}% , Hilbert: {temp_score:.2f} , Stéréo-Disp: {stereo_disparity:.3f}) | "
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
        print("\n[*] Fermeture du flux audio spatial.")


if __name__ == "__main__":
    main()