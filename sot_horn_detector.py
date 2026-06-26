"""
Sea of Thieves - Fort of Fortune / Reaper Fortress Horn Detector
Capture loopback WASAPI -> sliding window -> spectral cross-correlation -> alerte

Usage:
    pip install sounddevice numpy scipy soundfile
    python sot_horn_detector.py [--threshold 0.65] [--template sot_horn_template.wav]

Mettre le fichier sot_horn_template.wav dans le même dossier.
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
from scipy.signal import correlate, resample_poly
from scipy.io import wavfile


# ─── CONFIG ────────────────────────────────────────────────────────────────────

DEFAULT_TEMPLATE = "sot_horn_template.wav"
DEFAULT_THRESHOLD = 0.65       # corrélation normalisée [0-1], ajuster selon faux positifs
BLOCK_SIZE = 4096              # samples par callback
HOP_SECONDS = 0.5             # fréquence de check (sliding window hop)
COOLDOWN_SECONDS = 15         # délai min entre deux alertes


# ─── CHARGEMENT TEMPLATE ────────────────────────────────────────────────────────

def load_template(path: str, target_sr: int) -> np.ndarray:
    y, sr = sf.read(path, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)  # mono
    if sr != target_sr:
        # resample
        from math import gcd
        g = gcd(target_sr, sr)
        y = resample_poly(y, target_sr // g, sr // g).astype(np.float32)
    y /= np.max(np.abs(y)) + 1e-9
    return y


# ─── ALERTE ────────────────────────────────────────────────────────────────────

def alert(score: float):
    print(f"\n{'='*50}")
    print(f"  ⚓  FORT DETECTED  |  score={score:.3f}")
    print(f"{'='*50}\n")

    # Notification Windows via PowerShell (aucune dépendance externe)
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

    # Bip système fallback
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        print("\a")  # terminal bell


# ─── DÉTECTION ─────────────────────────────────────────────────────────────────

def normalized_xcorr(a: np.ndarray, b: np.ndarray) -> float:
    """Corrélation croisée normalisée max entre a et b (b = template)."""
    if len(a) < len(b):
        return 0.0
    # Trim a à une fenêtre raisonnable (2x template)
    a = a[-len(b)*2:]
    corr = correlate(a, b, mode="full")
    norm = np.sqrt(np.sum(a**2) * np.sum(b**2)) + 1e-9
    return float(np.max(np.abs(corr)) / norm)


def spectral_similarity(a: np.ndarray, b: np.ndarray, sr: int) -> float:
    """Similarité cosinus sur le spectre de magnitude (plus robuste au bruit de fond)."""
    n = min(len(a), len(b))
    if n < 512:
        return 0.0
    # Prendre la même longueur
    fa = np.abs(np.fft.rfft(a[-n:], n=n))
    fb = np.abs(np.fft.rfft(b[:n], n=n))
    norm = (np.linalg.norm(fa) * np.linalg.norm(fb)) + 1e-9
    return float(np.dot(fa, fb) / norm)


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SoT Horn Detector")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="Seuil corrélation [0-1], défaut=0.65")
    parser.add_argument("--list-devices", action="store_true",
                        help="Lister les devices audio et quitter")
    parser.add_argument("--device", type=int, default=None,
                        help="Index du device loopback (voir --list-devices)")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        sys.exit(0)

    # Trouver le device loopback WASAPI automatiquement
    device_idx = args.device
    if device_idx is None:
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            name = d["name"].lower()
            if ("loopback" in name or "stereo mix" in name or
                    "what u hear" in name or "wave out" in name or
                    "mix" in name) and d["max_input_channels"] > 0:
                device_idx = i
                print(f"[*] Device loopback auto-détecté: [{i}] {d['name']}")
                break
        if device_idx is None:
            print("[!] Aucun device loopback trouvé automatiquement.")
            print("[!] Lance avec --list-devices et passe --device <idx>")
            print("[!] Sur Windows: activer 'Stereo Mix' dans Paramètres son > Enregistrement")
            sd.query_devices()
            sys.exit(1)

    # Sample rate du device
    device_info = sd.query_devices(device_idx, "input")
    sr = int(device_info["default_samplerate"])
    print(f"[*] Sample rate: {sr} Hz")

    # Charger template
    template_path = Path(args.template)
    if not template_path.exists():
        # Chercher dans le même dossier que le script
        template_path = Path(__file__).parent / args.template
    if not template_path.exists():
        print(f"[!] Template introuvable: {args.template}")
        sys.exit(1)

    template = load_template(str(template_path), sr)
    print(f"[*] Template chargé: {len(template)/sr:.2f}s")
    print(f"[*] Seuil: {args.threshold}")
    print(f"[*] Écoute en cours... (Ctrl+C pour quitter)\n")

    # Buffer circulaire (3x template pour la sliding window)
    buf_len = len(template) * 3
    buf = np.zeros(buf_len, dtype=np.float32)
    buf_lock = threading.Lock()

    audio_q = queue.Queue()
    last_alert = 0.0

    def callback(indata, frames, time_info, status):
        if status:
            print(f"[warn] {status}")
        chunk = indata[:, 0].copy()  # mono
        audio_q.put(chunk)

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

            # Score combiné: xcorr + spectral similarity
            score_xcorr = normalized_xcorr(window, template)
            score_spec = spectral_similarity(window, template, sr)
            score = 0.6 * score_xcorr + 0.4 * score_spec

            # Debug (commenter pour moins de verbosité)
            print(f"[{time.strftime('%H:%M:%S')}] xcorr={score_xcorr:.3f}  spec={score_spec:.3f}  combined={score:.3f}")

            now = time.time()
            if score >= args.threshold and (now - last_alert) > COOLDOWN_SECONDS:
                last_alert = now
                alert(score)

    # Lancer détecteur dans thread séparé
    t = threading.Thread(target=detector_loop, daemon=True)
    t.start()

    try:
        with sd.InputStream(
            device=device_idx,
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