"""
Sea of Thieves - Fort Detector v2
Bandpass filter sur la bande du klaxon avant cross-correlation -> robuste au bruit de fond

Usage:
    python sot_horn_detector_v2.py [--threshold 0.65] [--device 96]
"""

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


# ─── CONFIG ────────────────────────────────────────────────────────────────────

DEFAULT_TEMPLATE = "sot_horn_template.wav"
DEFAULT_THRESHOLD = 0.65
DEFAULT_DEVICE = 96
BLOCK_SIZE = 4096
HOP_SECONDS = 0.5
COOLDOWN_SECONDS = 15

# Bande fréquentielle du klaxon SOT (Hz)
BANDPASS_LOW = 80
BANDPASS_HIGH = 600


# ─── UTILS ─────────────────────────────────────────────────────────────────────

def butter_bandpass(low, high, sr, order=4):
    nyq = sr / 2
    sos = butter(order, [low / nyq, high / nyq], btype="band", output="sos")
    return sos


def apply_bandpass(y, sos):
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


# ─── ALERTE ────────────────────────────────────────────────────────────────────

def alert(score: float):
    print(f"\n{'=' * 50}")
    print(f"  ⚓  FORT DETECTED  |  score={score:.3f}")
    print(f"{'=' * 50}\n")

    try:
        import subprocess
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
            "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$template.SelectSingleNode('//text[@id=1]').InnerText = 'Sea of Thieves';"
            "$template.SelectSingleNode('//text[@id=2]').InnerText = 'FORT DETECTED ! 🏴‍☠️';"
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


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SoT Horn Detector v2 - bandpass")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--device", type=int, default=DEFAULT_DEVICE)
    parser.add_argument("--low", type=float, default=BANDPASS_LOW, help="Bandpass low Hz")
    parser.add_argument("--high", type=float, default=BANDPASS_HIGH, help="Bandpass high Hz")
    parser.add_argument("--list-devices", action="store_true")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        sys.exit(0)

    device_info = sd.query_devices(args.device, "input")
    sr = int(device_info["default_samplerate"])
    print(f"[*] Device: [{args.device}] {device_info['name']}")
    print(f"[*] Sample rate: {sr} Hz")

    # Bandpass filter
    sos = butter_bandpass(args.low, args.high, sr)
    print(f"[*] Bandpass: {args.low}-{args.high} Hz")

    # Template
    template_path = Path(args.template)
    if not template_path.exists():
        template_path = Path(__file__).parent / args.template
    if not template_path.exists():
        print(f"[!] Template introuvable: {args.template}")
        sys.exit(1)

    template_raw = load_template(str(template_path), sr)
    template = apply_bandpass(template_raw, sos)
    template /= np.max(np.abs(template)) + 1e-9
    print(f"[*] Template chargé: {len(template)/sr:.2f}s")
    print(f"[*] Seuil: {args.threshold}")
    print(f"[*] Écoute en cours... (Ctrl+C pour quitter)\n")

    buf_len = len(template) * 3
    buf = np.zeros(buf_len, dtype=np.float32)
    buf_lock = threading.Lock()
    audio_q = queue.Queue()
    last_alert = 0.0

    def callback(indata, frames, time_info, status):
        if status:
            print(f"[warn] {status}")
        audio_q.put(indata[:, 0].copy())

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

            # Appliquer bandpass sur la fenêtre
            window_bp = apply_bandpass(window, sos)
            window_bp /= np.max(np.abs(window_bp)) + 1e-9

            score_xcorr = normalized_xcorr(window_bp, template)
            score_spec = spectral_similarity(window_bp, template)
            score = 0.6 * score_xcorr + 0.4 * score_spec

            print(f"[{time.strftime('%H:%M:%S')}] xcorr={score_xcorr:.3f}  spec={score_spec:.3f}  combined={score:.3f}")

            now = time.time()
            if score >= args.threshold and (now - last_alert) > COOLDOWN_SECONDS:
                last_alert = now
                alert(score)

    t = threading.Thread(target=detector_loop, daemon=True)
    t.start()

    try:
        with sd.InputStream(
            device=args.device,
            channels=1,
            samplerate=sr,
            blocksize=BLOCK_SIZE,
            dtype="float32",
            callback=callback,
        ):
            while True:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[*] Arrêt.")


if __name__ == "__main__":
    main()
