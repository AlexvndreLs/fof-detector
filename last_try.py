"""
Sea of Thieves - Fort of Fortune / Reaper Fortress Horn Detector (Hilbert + Filter Mode)
Capture loopback WASAPI -> 4s sliding window -> Bandpass Filter -> Hilbert Envelope -> Discord Alert.

Optimisé d'après l'analyse spectrale : pics à 43.2 Hz et 183.9 Hz.
"""

import argparse
import time
import threading
import queue
import sys
import urllib.request
import json
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from scipy.signal import correlate, resample_poly, hilbert, butter, sosfilt

# ─── CONFIGURATION DISCORD VIA CONFIG.PY ──────────────────────────────────────
try:
    from config import DISCORD_WEBHOOK
except ImportError:
    DISCORD_WEBHOOK = ""

# ─── PARAMÈTRES CONFIGURÉS SUR TES FRÉQUENCES ESSENTIELLES ────────────────────
DEFAULT_TEMPLATE = "sot_horn_template.wav"  # Ton fichier de 4 secondes

# Isolation stricte de tes pics (43.2 Hz, 183.9 Hz, 93.8 Hz)
BANDPASS_LOW: float = 35.0    # Coupe sous ton Top 1 (43.2 Hz)
BANDPASS_HIGH: float = 700  # Coupe au-dessus de ton Top 2 (183.9 Hz)

STRICT_XCORR_THRESHOLD: float = 0.92  # Seuil de corrélation d'enveloppe
NOISE_GATE_RMS: float = 0.01         # Capte les bruits sourds très lointains
BLOCK_SIZE: int = 96000                # Échantillons par callback audio
HOP_SECONDS: float = 0.4              # Pas de calcul (décalage de la fenêtre)
COOLDOWN_SECONDS: int = 5            # Limite le spam sur ton salon Discord


# ─── TRAITEMENT DU SIGNAL (PASSE-BANDE & HILBERT) ──────────────────────────────

def butter_bandpass(low: float, high: float, sr: int, order: int = 4) -> np.ndarray:
    """Génère les coefficients SOS pour le filtre passe-bande de Butterworth."""
    nyq = sr / 2
    sos = butter(order, [low / nyq, high / nyq], btype="band", output="sos")
    return sos


def apply_bandpass(y: np.ndarray, sos: np.ndarray) -> np.ndarray:
    """Applique le filtre passe-bande au signal."""
    return sosfilt(sos, y).astype(np.float32)


def get_envelope(signal: np.ndarray) -> np.ndarray:
    """Extrait l'enveloppe de modulation lissée via la transformée de Hilbert."""
    if len(signal) == 0 or np.all(signal == 0):
        return np.zeros_like(signal)
        
    analytic_signal = hilbert(signal)
    amplitude_envelope = np.abs(analytic_signal)
    
    # Moyenne glissante pour lisser la micro-texture du bruit
    kernel_size = 256
    if len(amplitude_envelope) > kernel_size:
        kernel = np.ones(kernel_size) / kernel_size
        smoothed = np.convolve(amplitude_envelope, kernel, mode='same')
    else:
        smoothed = amplitude_envelope
        
    # Normalisation locale [0 - 1]
    smoothed /= (np.max(smoothed) + 1e-9)
    return smoothed.astype(np.float32)


def load_template(path: str, target_sr: int, sos: np.ndarray) -> np.ndarray:
    """Charge le template, le filtre sur la bande du klaxon et extrait son enveloppe."""
    y, sr = sf.read(path, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != target_sr:
        from math import gcd
        g = gcd(target_sr, sr)
        y = resample_poly(y, target_sr // g, sr // g).astype(np.float32)
    
    # Filtrer puis extraire l'enveloppe de référence
    y_filtered = apply_bandpass(y, sos)
    return get_envelope(y_filtered)


def normalized_envelope_xcorr(env_a: np.ndarray, env_b: np.ndarray) -> float:
    """Calcule la corrélation croisée normalisée entre deux enveloppes de Hilbert."""
    if len(env_a) < len(env_b):
        return 0.0
    env_a = env_a[-len(env_b) * 2:]
    corr = correlate(env_a, env_b, mode="full")
    norm = np.sqrt(np.sum(env_a ** 2) * np.sum(env_b ** 2)) + 1e-9
    return float(np.max(np.abs(corr)) / norm)


# ─── PIPELINE D'ALERTE DISCORD ─────────────────────────────────────────────────

def alert(score: float, rms_force: float, strength_label: str) -> None:
    """Affiche l'alerte en console locale et l'envoie sur Discord."""
    print(f"\n{'=' * 60}")
    print(f"  ⚓  FORT DETECTED | Env_XCorr: {score:.3f} | Force: {strength_label} (RMS: {rms_force:.4f})")
    print(f"{'=' * 60}\n")

    if not DISCORD_WEBHOOK:
        print("[*] Alerte Discord ignorée : DISCORD_WEBHOOK est vide ou introuvable.")
        return

    payload = {
        "content": f"🏴‍☠️ **[Hilbert-Filter Engine] FORT DETECTED !**\n"
                   f"• **Force du signal** : `{strength_label}` (RMS: {rms_force:.4f})\n"
                   f"• **Similarité d'enveloppe** : `{score:.3f}`"
    }
    
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req) as response:
            pass
    except Exception as e:
        print(f"[Discord] Erreur d'envoi du webhook : {e}")


# ─── MOTEUR DE DÉTECTION PRINCIPAL ─────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="SoT Horn Detector - Hilbert + Filter Mode")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--device", type=int, default=96)
    args = parser.parse_args()

    try:
        device_info = sd.query_devices(args.device, "input")
    except Exception as e:
        print(f"[!] Impossible de trouver le périphérique d'index {args.device} : {e}")
        print("[*] Lance le script d'origine avec '--list-devices' pour vérifier les index.")
        sys.exit(1)
        
    sr = int(device_info["default_samplerate"])
    print(f"[*] Moteur en ligne sur le périphérique : [{args.device}] {device_info['name']}")
    print(f"[*] Fréquence d'échantillonnage : {sr} Hz")

    # Initialisation du filtre passe-bande chirurgical
    sos = butter_bandpass(BANDPASS_LOW, BANDPASS_HIGH, sr)
    print(f"[*] Filtre passe-bande actif : {BANDPASS_LOW} Hz - {BANDPASS_HIGH} Hz")

    # Chargement du template de 4 secondes
    template_path = Path(args.template)
    if not template_path.exists():
        template_path = Path(__file__).parent / args.template
    if not template_path.exists():
        print(f"[!] Erreur : Le fichier template '{args.template}' est introuvable.")
        sys.exit(1)

    template_env = load_template(str(template_path), sr, sos)
    print(f"[*] Enveloppe filtrée de référence chargée ({len(template_env)/sr:.2f}s).")
    print(f"[*] Statut Discord Webhook : {'Connecté' if DISCORD_WEBHOOK else 'Désactivé (config.py non configuré)'}")
    print(f"[*] Analyse du flux audio en cours...")

    # Buffer circulaire dimensionné (3x la taille du template)
    buf_len = len(template_env) * 3
    buf = np.zeros(buf_len, dtype=np.float32)
    buf_lock = threading.Lock()
    audio_q: queue.Queue = queue.Queue()
    last_alert = 0.0

    def callback(indata, frames, time_info, status):
        if status:
            print(f"[Audio Stream Warning] {status}")
        # Mixage complet gauche/droite (Stéréo Loopback)
        audio_q.put(indata.mean(axis=1).copy())

    def detector_loop():
        nonlocal buf, last_alert
        hop_samples = int(HOP_SECONDS * sr)
        accumulated = 0
        window_samples = len(template_env)  # Taille fixe de 4 secondes pour l'analyse

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
                # On extrait EXACTEMENT une fenêtre continue de 4 secondes (taille du template)
                window = buf[-window_samples:].copy()

            # 1. Analyse de l'énergie (Noise Gate) sur ces 4 secondes de signal brut
            raw_rms = np.sqrt(np.mean(window ** 2))
            if raw_rms < NOISE_GATE_RMS:
                continue

            # 2. Nettoyage fréquentiel chirurgical (Élimine instantanément tes notifs PC)
            window_filtered = apply_bandpass(window, sos)

            # 3. Extraction de la courbe de volume lissée (Hilbert)
            window_env = get_envelope(window_filtered)

            # 4. Corrélation d'enveloppe stabilisée (tableaux de tailles identiques)
            score_env_xcorr = normalized_envelope_xcorr(window_env, template_env)

            # Log de monitoring en temps réel
            print(f"[{time.strftime('%H:%M:%S')}] RMS={raw_rms:.5f} | Env_XCorr={score_env_xcorr:.3f}")

            # 5. Logique d'évaluation et de décision
            if score_env_xcorr >= STRICT_XCORR_THRESHOLD:
                now = time.time()
                if (now - last_alert) > COOLDOWN_SECONDS:
                    last_alert = now
                    
                    if raw_rms > 0.04:
                        strength = "FORT / PROCHE"
                    elif raw_rms > 0.01:
                        strength = "LOINTAIN / DISCRET"
                    else:
                        strength = "TRÈS ÉLOIGNÉ / SEUIL CRITIQUE"
                        
                    alert(score_env_xcorr, raw_rms, strength)

    # Lancement du thread d'analyse en arrière-plan
    t = threading.Thread(target=detector_loop, daemon=True)
    t.start()

    try:
        with sd.InputStream(
            device=args.device,
            channels=2,  # Écoute gauche et droite pour le stéréo mix
            samplerate=sr,
            blocksize=BLOCK_SIZE,
            dtype="float32",
            callback=callback,
        ):
            while True:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[*] Arrêt propre du détecteur.")


if __name__ == "__main__":
    main()