"""
Spectrogram Frequency Extractor - Chronological Profile Generator.
Analyzes an audio file block by block (42.6ms) to extract and rank frequencies.
"""

import argparse
from math import gcd
from pathlib import Path
from typing import List, Tuple

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

# Configuration DSP stricte
WINDOW_SIZE: int = 2048  # 2048 échantillons = ~42.6 ms à 48 kHz
HOP_LENGTH: int = 512    # Recouvrement pour ne rater aucun transitoire
TARGET_SR: int = 48000   # Fréquence d'échantillonnage de référence


class AudioFrequencyAnalyzer:
    """Handles audio loading, resampling, and chronological frame-by-frame FFT analysis."""

    def __init__(self, file_path: str, target_sr: int = TARGET_SR) -> None:
        """
        Initializes the analyzer with target file paths and sampling rate.

        Args:
            file_path: Path to the target audio file (.wav).
            target_sr: The sampling rate to conform to for analysis.
        """
        self.file_path = Path(file_path)
        self.target_sr = target_sr
        self.signal: np.ndarray = np.array([], dtype=np.float32)

    def load_and_preprocess(self) -> None:
        """Loads the audio file, downmixes to mono, and resamples if necessary."""
        if not self.file_path.exists():
            raise FileNotFoundError(f"Audio file not found: {self.file_path}")

        y, sr = sf.read(str(self.file_path), dtype="float32", always_2d=False)
        
        # Mixage mono si stéréo
        if y.ndim > 1:
            y = y.mean(axis=1)

        # Rééchantillonnage polyphasé si nécessaire
        if sr != self.target_sr:
            g = gcd(self.target_sr, sr)
            y = resample_poly(y, self.target_sr // g, sr // g).astype(np.float32)

        self.signal = y

    def analyze_chronologically(self, min_freq: float = 0.0, max_freq: float = 2000.0) -> None:
        """
        Executes a frame-by-frame FFT analysis and prints ranked frequencies.

        Args:
            min_freq: Lower bound frequency to display (Hz).
            max_freq: Upper bound frequency to display (Hz).
        """
        window = np.hanning(WINDOW_SIZE)
        fft_freqs = np.linspace(0, self.target_sr / 2, int(1 + WINDOW_SIZE // 2))

        # Étape 1 : Analyse globale préliminaire pour trouver le pic absolu (référence du 100%)
        global_max_magnitude: float = 1e-9
        for i in range(0, len(self.signal) - WINDOW_SIZE, HOP_LENGTH):
            frame = self.signal[i:i + WINDOW_SIZE] * window
            fft_mag = np.abs(np.fft.rfft(frame, n=WINDOW_SIZE))
            local_max = np.max(fft_mag)
            if local_max > global_max_magnitude:
                global_max_magnitude = local_max

        # Étape 2 : Analyse chronologique par tranche de 42.6 ms
        print(f"[*] Analyse chronologique de : {self.file_path.name}")
        print(f"[*] Résolution temporelle : {WINDOW_SIZE / self.target_sr * 1000:.1f} ms per tranche")
        print(f"[*] Spectre analysé : {min_freq} Hz - {max_freq} Hz\n")
        print(f"{'Tranche Temporelle':<20} | {'Fréquences détectées (Tricées par % Importance)':<50}")
        print("─" * 90)

        frame_idx: int = 0
        for i in range(0, len(self.signal) - WINDOW_SIZE, HOP_LENGTH):
            # Calcul du timing exact de la tranche
            start_ms = (i / self.target_sr) * 1000
            end_ms = start_ms + ((WINDOW_SIZE / self.target_sr) * 1000)
            time_label = f"{start_ms:.1f}ms - {end_ms:.1f}ms"

            # Application de la fenêtre de Hanning et FFT
            frame = self.signal[i:i + WINDOW_SIZE] * window
            fft_mag = np.abs(np.fft.rfft(frame, n=WINDOW_SIZE))

            # Extraction et filtrage des pics de la tranche
            detected_peaks: List[Tuple[float, float]] = []
            for idx, mag in enumerate(fft_mag):
                freq = fft_freqs[idx]
                if min_freq <= freq <= max_freq:
                    # Calcul de l'importance relative par rapport au max absolu du fichier
                    importance_pct = (mag / global_max_magnitude) * 100.0
                    # On ignore le bruit résiduel sous les 2% d'importance
                    if importance_pct >= 2.0:
                        detected_peaks.append((freq, importance_pct))

            # Tri des fréquences par ordre décroissant d'importance (%)
            detected_peaks.sort(key=lambda x: x[1], reverse=True)

            # Formatage de la ligne d'affichage (Top 5 des fréquences les plus importantes de la tranche)
            if detected_peaks:
                peaks_str_list = [f"{freq:.1f}Hz ({pct:.1f}%)" for freq, pct in detected_peaks[:5]]
                peaks_output = " | ".join(peaks_str_list)
                print(f"{time_label:<20} | {peaks_output}")
            else:
                print(f"{time_label:<20} | [Silence / Aucune fréquence significative]")

            frame_idx += 1


def main() -> None:
    """Main execution block parsing arguments and running the analysis pipeline."""
    parser = argparse.ArgumentParser(description="Extract ranked frequencies per 42ms block.")
    parser.add_argument("--file", default="sot_horn_template.wav", help="Target audio file.")
    args = parser.parse_args()

    try:
        analyzer = AudioFrequencyAnalyzer(file_path=args.file)
        analyzer.load_and_preprocess()
        # On restreint de 0 à 500 Hz pour zoomer sur la zone d'intérêt de ta corne
        analyzer.analyze_chronologically(min_freq=20.0, max_freq=500.0)
    except Exception as e:
        print(f"[Erreur] Impossible d'analyser le fichier : {e}")


if __name__ == "__main__":
    main()