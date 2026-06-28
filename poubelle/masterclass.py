"""
Real-Time Pure-Data Audio Signature Detector.

Ingests multi-channel device feeds, processes frames sequentially,
and quantifies deviation maps directly against exported CSV data profiles.
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

try:
    from config import DISCORD_WEBHOOK
except ImportError:
    DISCORD_WEBHOOK = ""

# DSP Ingestion constants
N_FFT: int = 2048
HOP_LENGTH: int = 512
FREQ_TOLERANCE_HZ: float = 6.5
NOISE_GATE_RMS: float = 0.002
COOLDOWN_SECONDS: int = 15

# Match sensitivity threshold (0.0 to 100.0%)
# Lowering this accommodates noisy background distortion; raising it increases exclusivity.
MATCH_CONFIDENCE_THRESHOLD: float = 75.0


class CSVPatternMatcher:
    """Manages structural distance evaluation against reference frames."""

    def __init__(self, sr: int) -> None:
        """Initializes the engine and defines spectral structures."""
        self.sr = sr
        self.window = np.hanning(N_FFT)
        self.fft_freqs = np.linspace(0, sr / 2, int(1 + N_FFT // 2))
        self.ref_local: List[Dict[str, Any]] = []

    def load_profiles(self, local_csv_path: str) -> None:
        """Loads the frame profile from the filesystem into memory."""
        path = Path(local_csv_path)
        if not path.exists():
            raise FileNotFoundError(f"Missing reference map file: {path}")

        with open(path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                parsed_row = {
                    "frame_idx": int(row["frame_idx"]),
                    "peaks": [
                        (float(row[f"f{idx}"]), float(row[f"p{idx}"]))
                        for idx in range(1, 6)
                    ],
                }
                self.ref_local.append(parsed_row)

    def extract_live_frame_peaks(self, fft_mag: np.ndarray) -> List[Tuple[float, float]]:
        """Identifies top peaks inside active live slices."""
        local_max = np.max(fft_mag)
        if local_max < 1e-6:
            local_max = 1e-6

        valid_indices = np.where((self.fft_freqs >= 20.0) & (self.fft_freqs <= 1000.0))[0]
        if len(valid_indices) == 0:
            return [(0.0, 0.0)] * 5

        filtered_mags = fft_mag[valid_indices]
        filtered_freqs = self.fft_freqs[valid_indices]
        sorted_lex = np.argsort(filtered_mags)[::-1]

        peaks: List[Tuple[float, float]] = []
        for idx in sorted_lex[:5]:
            freq = float(filtered_freqs[idx])
            pct = float((filtered_mags[idx] / local_max) * 100.0)
            peaks.append((freq, pct))

        while len(peaks) < 5:
            peaks.append((0.0, 0.0))
        return peaks

    def score_window(self, live_signal: np.ndarray) -> float:
        """Cross-examines game slices against loaded reference profile rows."""
        # 1. Transform raw buffer segments into matching structural lists
        live_timeline: List[List[Tuple[float, float]]] = []
        for i in range(0, len(live_signal) - N_FFT, HOP_LENGTH):
            frame = live_signal[i:i + N_FFT] * self.window
            fft_mag = np.abs(np.fft.rfft(frame, n=N_FFT))
            live_timeline.append(self.extract_live_frame_peaks(fft_mag))

        ref_len = len(self.ref_local)
        if len(live_timeline) < ref_len or ref_len == 0:
            return 0.0

        max_sequence_score: float = 0.0
        search_limit = len(live_timeline) - ref_len + 1

        # 2. Slide the template profile window over the game audio timeline
        for step in range(0, search_limit, 2):
            total_window_score = 0.0

            for r_idx in range(ref_len):
                ref_peaks = self.ref_local[r_idx]["peaks"]
                live_peaks = live_timeline[step + r_idx]

                frame_match_score = 0.0
                # Match each of the 5 reference frequencies with the live snapshot
                for r_freq, r_pct in ref_peaks:
                    if r_pct < 10.0:  # Skip background noise floor profiles
                        frame_match_score += 20.0
                        continue

                    # Look for this target frequency within the live frame's peaks
                    for l_freq, l_pct in live_peaks:
                        if abs(l_freq - r_freq) <= FREQ_TOLERANCE_HZ:
                            # Evaluate proximity of intensity distribution
                            pct_delta = abs(l_pct - r_pct)
                            variance_penalty = max(0.0, (100.0 - pct_delta) / 100.0)
                            frame_match_score += 20.0 * variance_penalty
                            break

                total_window_score += frame_match_score / 5.0

            normalized_window_score = (total_window_score / ref_len)
            if normalized_window_score > max_sequence_score:
                max_sequence_score = normalized_window_score

        return max_sequence_score


def alert(score: float, rms: float, label: str) -> None:
    """Dispatches webhook payloads to remote endpoints when validated."""
    print(f"DISCORD ALERTE ENVOYÉE ! | Data Confidence: {score:.2f}% | Force: {label}")
    if not DISCORD_WEBHOOK:
        return

    payload: Dict[str, Any] = {
        "content": f"**[Data-Driven Engine V3] HORN CONFIRMED !**\n"
                   f"• **Signal Amplitude** : `{label}` (RMS: {rms:.4f})\n"
                   f"• **Data Profile Confidence** : `{score:.2f}%`"
    }
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req):
            pass
    except Exception as e:
        print(f"[Discord] Connection breakdown: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Live Profile Matcher.")
    parser.add_argument("--device", type=int, default=96)
    args = parser.parse_args()

    try:
        device_info = sd.query_devices(args.device, "input")
    except Exception:
        print(f"[Error] Audio endpoint index {args.device} unreachable.")
        sys.exit(1)

    sr = int(device_info["default_samplerate"])
    print(f"[*] Ingestion active on endpoint: [{args.device}] {device_info['name']}")

    matcher = CSVPatternMatcher(sr=sr)
    try:
        print("[*] Parsing CSV fingerprint patterns...")
        matcher.load_profiles("horn_profile_local.csv")
    except Exception as e:
        print(f"[Critical Error] Failed to read exported matrix arrays: {e}")
        sys.exit(1)

    print(f"[*] Active profile successfully mapped from CSV ({len(matcher.ref_local)} rows).")
    print("[*] Allocating rolling stream memory fields (15s stabilization delay)...")
    print("─" * 90)

    buf_len = sr * 15
    buf = np.zeros(buf_len, dtype=np.float32)
    buf_lock = threading.Lock()
    audio_q: queue.Queue = queue.Queue()
    last_alert: float = 0.0

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
                print(f"[*] Stabilizing input buffer segments... (Remaining: {warmup_cycles * 5}s)")
                warmup_cycles -= 1
                if warmup_cycles == 0:
                    print("[*] Matrix analysis engine active. Monitoring live feeds.")
                    print("─" * 90)
                continue

            with buf_lock:
                current_lot = buf.copy()

            current_time = time.strftime("%H:%M:%S")
            raw_rms = float(np.sqrt(np.mean(current_lot ** 2)))

            # Calculate the pure pattern validation score
            confidence_score = matcher.score_window(current_lot)
            is_silent = raw_rms < NOISE_GATE_RMS

            if is_silent:
                status_str = "SILENT"
            elif confidence_score >= MATCH_CONFIDENCE_THRESHOLD:
                status_str = "MATCH !!"
            else:
                status_str = "No Match"

            print(
                f"[{current_time}] RMS: {raw_rms:.5f} | "
                f"Confidence: {confidence_score:.2f}%/{MATCH_CONFIDENCE_THRESHOLD:.1f}% | "
                f"Status: {status_str}"
            )

            if confidence_score >= MATCH_CONFIDENCE_THRESHOLD and not is_silent:
                now = time.time()
                if (now - last_alert) > COOLDOWN_SECONDS:
                    last_alert = now
                    strength = "STRONG" if raw_rms > 0.04 else ("FAINT" if raw_rms > 0.01 else "CRITICAL")
                    alert(confidence_score, raw_rms, strength)

    t = threading.Thread(target=detector_loop, daemon=True)
    t.start()

    try:
        with sd.InputStream(
            device=args.device,
            channels=2,
            samplerate=sr,
            blocksize=8192,
            dtype="float32",
            callback=callback,
        ):
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[*] Exiting structural monitor pipelines.")


if __name__ == "__main__":
    main()