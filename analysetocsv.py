"""
Audio Profile Generator for Sequential Pattern Matching.

Extracts chronological frequency profiles from a reference audio template
using global and local normalization schemas, exporting the data to CSV.
"""

import argparse
import csv
from math import gcd
from pathlib import Path
from typing import List, Tuple, Dict, Any

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly


class ProfileExporter:
    """Handles audio preprocessing, spectral analysis, and CSV profile exportation."""

    def __init__(
        self,
        file_path: str,
        target_sr: int = 48000,
        window_size: int = 4096,
        hop_length: int = 128,
    ) -> None:
        """Initializes the exporter with DSP parameters.

        Args:
            file_path: Path to the source WAV file.
            target_sr: Target sampling rate for analysis.
            window_size: Size of the FFT window.
            hop_length: Frame offset for short-time analysis.
        """
        self.file_path = Path(file_path)
        self.target_sr = target_sr
        self.window_size = window_size
        self.hop_length = hop_length
        self.signal: np.ndarray = np.array([], dtype=np.float32)

    def load_and_standardize(self) -> None:
        """Loads the audio file, downmixes to mono, and executes polyphase resampling."""
        if not self.file_path.exists():
            raise FileNotFoundError(f"Target audio file missing: {self.file_path}")

        data, sr = sf.read(str(self.file_path), dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.mean(axis=1)

        if sr != self.target_sr:
            g = gcd(self.target_sr, sr)
            data = resample_poly(
                data, self.target_sr // g, sr // g
            ).astype(np.float32)

        self.signal = data

    def extract_top_peaks(
        self, magnitudes: np.ndarray, freqs: np.ndarray, max_ref: float, top_n: int = 5
    ) -> List[Tuple[float, float]]:
        """Extracts the top N peaks within a given magnitude array.

        Args:
            magnitudes: Array of FFT bin magnitudes.
            freqs: Array of corresponding frequencies in Hz.
            max_ref: Reference value used for percentage normalization.
            top_n: Number of peak frequencies to retain.

        Returns:
            A list of tuples containing (frequency, normalized_percentage).
        """
        # Restrict analysis to safe low-mid spectrum (20 Hz - 1000 Hz)
        valid_indices = np.where((freqs >= 20.0) & (freqs <= 1000.0))[0]
        
        if len(valid_indices) == 0:
            return [(0.0, 0.0)] * top_n

        filtered_mags = magnitudes[valid_indices]
        filtered_freqs = freqs[valid_indices]

        # Sort indices based on descending magnitude order
        sorted_lex = np.argsort(filtered_mags)[::-1]
        
        peaks: List[Tuple[float, float]] = []
        for idx in sorted_lex[:top_n]:
            freq = float(filtered_freqs[idx])
            pct = float((filtered_mags[idx] / max_ref) * 100.0) if max_ref > 1e-6 else 0.0
            peaks.append((freq, pct))

        # Pad with zeros if fewer peaks are found
        while len(peaks) < top_n:
            peaks.append((0.0, 0.0))
            
        return peaks

    def process_and_export(self) -> None:
        """Computes short-time Fourier transforms and serializes metrics to CSV files."""
        window = np.hanning(self.window_size)
        fft_freqs = np.linspace(0, self.target_sr / 2, int(1 + self.window_size // 2))

        # Pass 1: Determine absolute global maximum magnitude for reference
        global_max_mag: float = 1e-9
        frames_mags: List[np.ndarray] = []

        for i in range(0, len(self.signal) - self.window_size, self.hop_length):
            frame = self.signal[i:i + self.window_size] * window
            fft_mag = np.abs(np.fft.rfft(frame, n=self.window_size))
            frames_mags.append(fft_mag)
            local_max = np.max(fft_mag)
            if local_max > global_max_mag:
                global_max_mag = local_max

        global_profile_rows: List[Dict[str, Any]] = []
        local_profile_rows: List[Dict[str, Any]] = []

        # Pass 2: Extract structured peak entries frame-by-frame
        frame_idx = 0
        for i, fft_mag in zip(
            range(0, len(self.signal) - self.window_size, self.hop_length), frames_mags
        ):
            start_ms = (i / self.target_sr) * 1000.0
            end_ms = start_ms + ((self.window_size / self.target_sr) * 1000.0)
            local_max_mag = np.max(fft_mag)

            # Compute parallel normalization strategies
            global_peaks = self.extract_top_peaks(fft_mag, fft_freqs, global_max_mag)
            local_peaks = self.extract_top_peaks(fft_mag, fft_freqs, local_max_mag)

            base_meta = {
                "frame_idx": frame_idx,
                "start_ms": round(start_ms, 2),
                "end_ms": round(end_ms, 2),
            }

            g_row = base_meta.copy()
            l_row = base_meta.copy()

            for n in range(5):
                g_row[f"f{n+1}"] = round(global_peaks[n][0], 1)
                g_row[f"p{n+1}"] = round(global_peaks[n][1], 2)
                l_row[f"f{n+1}"] = round(local_peaks[n][0], 1)
                l_row[f"p{n+1}"] = round(local_peaks[n][1], 2)

            global_profile_rows.append(g_row)
            local_profile_rows.append(l_row)
            frame_idx += 1

        headers = ["frame_idx", "start_ms", "end_ms"] + [
            f"{var}{num}" for num in range(1, 6) for var in ("f", "p")
        ]

        # Write profiles to disc
        for data_source, out_name in (
            (global_profile_rows, "horn_profile_global_conso.csv"),
            (local_profile_rows, "horn_profile_local_conso.csv"),
        ):
            with open(out_name, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
                writer.writerows(data_source)

        print(f"[*] Successfully exported {frame_idx} spectral frames.")
        print("    -> Created: horn_profile_global.csv")
        print("    -> Created: horn_profile_local.csv")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export pattern matching references.")
    parser.add_argument("--file", default="sot_horn_template.wav")
    args = parser.parse_args()

    try:
        exporter = ProfileExporter(file_path=args.file)
        exporter.load_and_standardize()
        exporter.process_and_export()
    except Exception as e:
        print(f"[Critical Error] Preprocessing failed: {e}")


if __name__ == "__main__":
    main()