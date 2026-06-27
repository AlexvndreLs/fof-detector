"""Sea of Thieves - Fort Detector v3 (Optimisé).

Ce module implémente un détecteur de cornes de brume en temps réel pour
le jeu Sea of Thieves en utilisant une approche orientée objet (OOP) avec
filtrage passe-bande, corrélation croisée, et gestion propre des threads.
"""

import argparse
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
import numpy as np
import sounddevice as sd
import soundfile as sf
from scipy.signal import butter, correlate, resample_poly, sosfilt

# ─── PARAMÈTRES PAR DÉFAUT ────────────────────────────────────────────────────
DEFAULT_TEMPLATE: str = "sot_horn_template.wav"
DEFAULT_THRESHOLD: float = 0.55
DEFAULT_DEVICE: int = 96
BLOCK_SIZE: int = 4096
HOP_SECONDS: float = 0.5
COOLDOWN_SECONDS: float = 15
FLAG_FILE: Path = Path(__file__).parent / "fort_detected.txt"

# Signature fréquentielle optimisée basée sur vos analyses physiques réelles
BANDPASS_LOW: float = 32.0
BANDPASS_HIGH: float = 56.0
FILTER_ORDER: int = 8

# Niveau de puissance minimale (RMS) pour ignorer les bruits de fond de l'interface
VOLUME_MIN_THRESHOLD: float = 0.005

try:
    from config import DISCORD_WEBHOOK
except ImportError:
    DISCORD_WEBHOOK = ""


class SotHornDetector:
    """Détecteur audio de cornes de brume basé sur l'analyse spectrale et temporelle.

    Cette classe encapsule toute la logique de traitement du signal,
    d'écoute du flux audio système, et d'alerte.
    """

    def __init__(
        self,
        template_name: str = DEFAULT_TEMPLATE,
        threshold: float = DEFAULT_THRESHOLD,
        device_id: int = DEFAULT_DEVICE,
        listen_duration: int = 0,
        test_mode: bool = False
    ) -> None:
        """Initialise le détecteur avec les paramètres d'analyse et de périphérique.

        Args:
            template_name: Nom du fichier WAV contenant le signal de référence.
            threshold: Seuil de similarité combiné pour déclencher l'alerte.
            device_id: Identifiant de l'appareil de capture audio dans sounddevice.
            listen_duration: Temps d'écoute maximum en secondes (0 pour infini).
            test_mode: Si True, simule la détection sans couper le flux ni créer de flag.
        """
        self.template_name: str = template_name
        self.threshold: float = threshold
        self.device_id: int = device_id
        self.listen_duration: int = listen_duration
        self.test_mode: bool = test_mode

        self.sr: int = 44100  # Mis à jour selon le périphérique sélectionné
        self.sos: Optional[np.ndarray] = None
        self.template: Optional[np.ndarray] = None

        self.audio_queue: queue.Queue = queue.Queue()
        self.buffer_lock: threading.Lock = threading.Lock()
        self.detected_event: threading.Event = threading.Event()
        self.last_alert_time: float = 0.0
        self.buffer: Optional[np.ndarray] = None

    def _butter_bandpass(self) -> np.ndarray:
        """Génère les coefficients de filtre SOS (Second-Order Sections) de type Butterworth.

        Returns:
            Coefficients de filtrage SOS.
        """
        nyq: float = self.sr / 2.0
        sos: np.ndarray = butter(
            FILTER_ORDER,
            [BANDPASS_LOW / nyq, BANDPASS_HIGH / nyq],
            btype="band",
            output="sos"
        )
        return sos

    def _apply_bandpass(self, y: np.ndarray) -> np.ndarray:
        """Applique le filtre passe-bande SOS sur un signal audio.

        Args:
            y: Signal audio d'entrée.

        Returns:
            Signal filtré en float32.
        """
        if self.sos is None:
            raise ValueError("Le filtre SOS n'a pas été initialisé.")
        return sosfilt(self.sos, y).astype(np.float32)

    def _load_template(self, target_sr: int) -> np.ndarray:
        """Charge et normalise le signal du template audio de référence.

        Args:
            target_sr: Fréquence d'échantillonnage cible pour rééchantillonner le template.

        Returns:
            Signal du template normalisé.
        """
        template_path = Path(self.template_name)
        if not template_path.exists():
            template_path = Path(__file__).parent / self.template_name
        if not template_path.exists():
            raise FileNotFoundError(f"Template audio introuvable : {self.template_name}")

        y, sr = sf.read(str(template_path), dtype="float32", always_2d=False)
        if y.ndim > 1:
            y = y.mean(axis=1)
        if sr != target_sr:
            from math import gcd
            g: int = gcd(target_sr, sr)
            y = resample_poly(y, target_sr // g, sr // g).astype(np.float32)
        
        y /= np.max(np.abs(y)) + 1e-9
        return y

    def _normalized_xcorr(self, a: np.ndarray, b: np.ndarray) -> float:
        """Calcule la corrélation croisée normalisée entre la fenêtre glissante et le template.

        Args:
            a: Signal de la fenêtre courante.
            b: Signal du template de référence.

        Returns:
            Le coefficient de corrélation maximal (entre 0.0 et 1.0).
        """
        if len(a) < len(b):
            return 0.0
        a_segment: np.ndarray = a[-len(b) * 2:]
        corr: np.ndarray = correlate(a_segment, b, mode="full")
        norm: float = np.sqrt(np.sum(a_segment ** 2) * np.sum(b ** 2)) + 1e-9
        return float(np.max(np.abs(corr)) / norm)

    def _spectral_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Calcule la similarité spectrale (FFT) entre deux signaux.

        Args:
            a: Signal de la fenêtre courante.
            b: Signal du template de référence.

        Returns:
            Le score de similarité spectrale (entre 0.0 et 1.0).
        """
        n: int = min(len(a), len(b))
        if n < 512:
            return 0.0
        fa: np.ndarray = np.abs(np.fft.rfft(a[-n:], n=n))
        fb: np.ndarray = np.abs(np.fft.rfft(b[:n], n=n))
        norm: float = (np.linalg.norm(fa) * np.linalg.norm(fb)) + 1e-9
        return float(np.dot(fa, fb) / norm)

    def _alert(self, score: float) -> None:
        """Déclenche les alertes visuelles, sonores et réseau lors d'une détection réussie.

        Args:
            score: Score de corrélation combiné.
        """
        print(f"\n{'=' * 50}")
        print(f"  ⚓  FORT DETECTED  |  score={score:.3f}")
        print(f"{'=' * 50}\n")

        # Écriture du fichier drapeau lu par AHK
        FLAG_FILE.write_text("detected")

        # Notification Windows native (PowerShell)
        try:
            import subprocess
            ps: str = (
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

        # Bip système d'alerte
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            print("\a")

        # Notification Discord
        if DISCORD_WEBHOOK:
            try:
                import json
                import urllib.request
                payload: bytes = json.dumps({
                    "content": f"⚓ **FORT DETECTED** | score={score:.3f} | 🏴‍☠️ Vroum !"
                }).encode()
                req = urllib.request.Request(
                    DISCORD_WEBHOOK,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0"
                    },
                    method="POST"
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                print(f"[discord] erreur: {e}")

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: Dict[str, Any],
        status: sd.CallbackFlags
    ) -> None:
        """Callback appelé par sounddevice pour chaque bloc audio capturé."""
        if status:
            print(f"[warn] {status}")
        self.audio_queue.put(indata.mean(axis=1).copy())

    def _detector_loop(self) -> None:
        """Boucle principale s'exécutant dans un thread dédié pour analyser l'audio."""
        hop_samples: int = int(HOP_SECONDS * self.sr)
        accumulated: int = 0

        while not self.detected_event.is_set():
            try:
                chunk: np.ndarray = self.audio_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            with self.buffer_lock:
                self.buffer = np.roll(self.buffer, -len(chunk))
                self.buffer[-len(chunk):] = chunk
                accumulated += len(chunk)

            if accumulated < hop_samples:
                continue
            accumulated = 0

            with self.buffer_lock:
                window: np.ndarray = self.buffer.copy()

            window_bp: np.ndarray = self._apply_bandpass(window)

            # Étape de filtrage de l'énergie minimale (RMS)
            rms_volume: float = float(np.sqrt(np.mean(window_bp**2)))
            if rms_volume < VOLUME_MIN_THRESHOLD:
                continue

            # Normalisation sécurisée
            window_bp /= np.max(np.abs(window_bp)) + 1e-9

            score_xcorr: float = self._normalized_xcorr(window_bp, self.template)
            score_spec: float = self._spectral_similarity(window_bp, self.template)
            score: float = 0.2 * score_xcorr + 0.8 * score_spec

            print(
                f"[{time.strftime('%H:%M:%S')}] vol={rms_volume:.5f} "
                f"xcorr={score_xcorr:.3f} spec={score_spec:.3f} combined={score:.3f}"
            )

            now: float = time.time()
            if score >= self.threshold and (now - self.last_alert_time) > COOLDOWN_SECONDS:
                self.last_alert_time = now
                if self.test_mode:
                    print(f"\n[{time.strftime('%H:%M:%S')}] [TEST] ⚓ SEUIL FRANCHI (Simulé) | score={score:.3f} | Aucun flag créé.")
                else:
                    self._alert(score)
                    self.detected_event.set()

    def run(self) -> None:
        """Initialise le périphérique et démarre l'analyse audio."""
        try:
            device_info = sd.query_devices(self.device_id, "input")
        except Exception as e:
            print(f"[!] Erreur de périphérique : {e}")
            sys.exit(1)

        self.sr = int(device_info["default_samplerate"])
        print(f"[*] Appareil : [{self.device_id}] {device_info['name']}")
        print(f"[*] Échantillonnage : {self.sr} Hz")

        # Initialisation du filtre d'ordre supérieur renforcé (Butterworth ordre 8)
        self.sos = self._butter_bandpass()
        print(f"[*] Filtre passe-bande : {BANDPASS_LOW}-{BANDPASS_HIGH} Hz (Ordre {FILTER_ORDER})")

        # Chargement du gabarit de référence
        try:
            template_raw = self._load_template(self.sr)
        except Exception as e:
            print(f"[!] Erreur lors du chargement du template : {e}")
            sys.exit(1)

        self.template = self._apply_bandpass(template_raw)
        self.template /= np.max(np.abs(self.template)) + 1e-9
        print(f"[*] Template configuré : {len(self.template)/self.sr:.2f}s | Seuil : {self.threshold}")

        if self.test_mode:
            print("[*] ATTENTION : Mode TEST diagnostique actif.")
        elif self.listen_duration > 0:
            print(f"[*] Durée maximale d'écoute : {self.listen_duration}s")
        print("[*] En attente du signal... (Ctrl+C pour quitter)\n")

        # Allocation de la mémoire tampon globale
        buf_len: int = len(self.template) * 3
        self.buffer = np.zeros(buf_len, dtype=np.float32)

        # Lancement du thread d'analyse
        detector_thread = threading.Thread(target=self._detector_loop, daemon=True)
        detector_thread.start()

        start_time: float = time.time()
        try:
            with sd.InputStream(
                device=self.device_id,
                channels=1,
                samplerate=self.sr,
                blocksize=BLOCK_SIZE,
                dtype="float32",
                callback=self._audio_callback,
            ):
                while not self.detected_event.is_set():
                    if self.listen_duration > 0 and (time.time() - start_time) >= self.listen_duration:
                        if self.test_mode:
                            print(f"\n[*] [TEST] {self.listen_duration}s de simulation terminées. Fermeture forcée.")
                        else:
                            print(f"\n[*] {self.listen_duration}s écoulées - aucun fort trouvé.")
                        
                        # CORRECTION DU FREEZE : On force l'arrêt de l'analyse et du InputStream
                        self.detected_event.set()
                        break
                    time.sleep(0.1)
        except KeyboardInterrupt:
            pass

        print("[*] Arrêt du programme.")


def main() -> None:
    """Point d'entrée du script en CLI."""
    parser = argparse.ArgumentParser(description="SoT Horn Detector v3 - OOP Edition")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--device", type=int, default=DEFAULT_DEVICE)
    parser.add_argument("--listen", type=int, default=0)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        sys.exit(0)

    detector = SotHornDetector(
        template_name=args.template,
        threshold=args.threshold,
        device_id=args.device,
        listen_duration=args.listen,
        test_mode=args.test
    )
    detector.run()


if __name__ == "__main__":
    main()