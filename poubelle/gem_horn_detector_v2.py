import argparse
import time
import threading
import queue
import sys
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from scipy.signal import correlate, resample_poly, butter, sosfilt


# ─── CONFIGURATION ─────────────────────────────────────────────────────────────

DEFAULT_TEMPLATE = "sot_horn_template.wav"

# Updated calibration parameters derived from your 2-second template log
STRICT_SPEC_THRESHOLD = 0.65   # Captures the clear plateau (>0.75) from 23:30:52 to 23:30:57
STRICT_XCORR_THRESHOLD = 0.16  # Validates the time-domain envelope peak (hit 0.191)
NOISE_GATE_RMS = 0.05         # Continues to block low-energy background static

BLOCK_SIZE = 4096
HOP_SECONDS = 0.3              # Faster analysis window for precise capture
COOLDOWN_SECONDS = 3


# ─── SIGNAL PROCESSING UTILITIES ───────────────────────────────────────────────

def butter_bandpass(low: float, high: float, sr: int, order: int = 4) -> np.ndarray:
    nyq = sr / 2
    sos = butter(order, [low / nyq, high / nyq], btype="band", output="sos")
    return sos


def apply_bandpass(y: np.ndarray, sos: np.ndarray) -> np.ndarray:
    return sosfilt(sos, y).astype(np.float32)


def load_template(path: str, target_sr: int) -> np.ndarray:
    y, sr = sf.read(path, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != target_sr:
        from math import gcd
        g = gcd(target_sr, sr)
        y = resample_poly(y, target_sr // g, sr // g).astype(np.float32)
    y /= np.max(np.abs(y)) + 1e-9
    return y


def normalized_xcorr(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < len(b):
        return 0.0
    a = a[-len(b) * 2:]
    corr = correlate(a, b, mode="full")
    norm = np.sqrt(np.sum(a ** 2) * np.sum(b ** 2)) + 1e-9
    return float(np.max(np.abs(corr)) / norm)


def spectral_similarity(a: np.ndarray, b: np.ndarray) -> float:
    n = min(len(a), len(b))
    if n < 512:
        return 0.0
    fa = np.abs(np.fft.rfft(a[-n:], n=n))
    fb = np.abs(np.fft.rfft(b[:n], n=n))
    norm = (np.linalg.norm(fa) * np.linalg.norm(fb)) + 1e-9
    return float(np.dot(fa, fb) / norm)


# ─── NOTIFICATION SYSTEM ───────────────────────────────────────────────────────

def alert(spec_score: float, rms_force: float, strength_label: str):
    print(f"\n{'=' * 60}")
    print(f"  ⚓  MATCH CONFIRMED | Quality: {spec_score:.3f} | Strength: {strength_label} (RMS: {rms_force:.4f})")
    print(f"{'=' * 60}\n")

    try:
        import subprocess
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
            "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$template.SelectSingleNode('//text[@id=1]').InnerText = 'Sea of Thieves';"
            f"$template.SelectSingleNode('//text[@id=2]').InnerText = 'FORT DETECTED ({strength_label}) ! 🏴‍☠️';"
            "$notif = [Windows.UI.Notifications.ToastNotification]::new($template);"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('SoT Horn Detector').Show($notif);"
        )
        subprocess.Popen(
            ["powershell", "-WindowStyle", "Hidden", "-Command", ps],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        print("\a")


# ─── CORE DETECTION ENGINE ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Zero False-Positive SoT Detector")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--device", type=int, default=96)
    parser.add_argument("--low", type=float, default=40, help="Bandpass low cut")
    parser.add_argument("--high", type=float, default=250, help="Bandpass high cut")
    args = parser.parse_args()

    device_info = sd.query_devices(args.device, "input")
    sr = int(device_info["default_samplerate"])
    
    sos = butter_bandpass(args.low, args.high, sr)
    
    template_path = Path(args.template)
    if not template_path.exists():
        template_path = Path(__file__).parent / args.template
    if not template_path.exists():
        print(f"[!] Template missing: {args.template}")
        sys.exit(1)

    template_raw = load_template(str(template_path), sr)
    template = apply_bandpass(template_raw, sos)
    template /= np.max(np.abs(template)) + 1e-9

    print(f"[*] Engine online. Noise Gate active context: RMS > {NOISE_GATE_RMS}")
    print(f"[*] Listening on device [{args.device}]...")

    buf_len = len(template) * 3
    buf = np.zeros(buf_len, dtype=np.float32)
    buf_lock = threading.Lock()
    audio_q = queue.Queue()
    last_alert = 0.0

    def callback(indata, frames, time_info, status):
        audio_q.put(indata.mean(axis=1).copy())

    def detector_loop():
        nonlocal buf, last_alert
        hop_samples = int(HOP_SECONDS * sr)
        accumulated = 0

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

            with buf_lock:
                window = buf.copy()

            # 1. Evaluate signal amplitude before processing (Noise Gate)
            analysis_window = window[-len(template):]
            raw_rms = np.sqrt(np.mean(analysis_window ** 2))
            
            if raw_rms < NOISE_GATE_RMS:
                # The stream is too quiet; skip processing completely to prevent false positives
                continue

            # 2. Filter and isolate the target band
            window_bp = apply_bandpass(window, sos)
            
            # 3. Structural & Spectral similarity metrics
            score_spec = spectral_similarity(window_bp, template)
            score_xcorr = normalized_xcorr(window_bp, template)

            # Debug logs to observe live environmental changes
            print(f"[{time.strftime('%H:%M:%S')}] RMS={raw_rms:.5f} | Spec={score_spec:.3f} | XCorr={score_xcorr:.3f}")

            # 4. Strict Validation Logic (Both parameters must clear their respective limits)
            if score_spec >= STRICT_SPEC_THRESHOLD and score_xcorr >= STRICT_XCORR_THRESHOLD:
                now = time.time()
                if (now - last_alert) > COOLDOWN_SECONDS:
                    last_alert = now
                    
                    # Determine signal strength dynamically
                    if raw_rms > 0.15:
                        strength = "CRITICAL / VERY LOUD"
                    elif raw_rms > 0.05:
                        strength = "DISTINCT / MEDIUM"
                    else:
                        strength = "FAINT / DISTANT"
                        
                    alert(score_spec, raw_rms, strength)

    t = threading.Thread(target=detector_loop, daemon=True)
    t.start()

    try:
        with sd.InputStream(device=args.device, channels=2, samplerate=sr, blocksize=BLOCK_SIZE, dtype="float32", callback=callback):
            while True:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[*] Offline.")


if __name__ == "__main__":
    main()