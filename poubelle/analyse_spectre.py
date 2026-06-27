import numpy as np
import soundfile as sf
import matplotlib.pyplot as plt
from scipy.signal import find_peaks

# Nom du fichier de 4 secondes à analyser
FILE_PATH = "sot_horn_template.wav"

def extraire_frequences_clefs(file_path: str):
    # 1. Lecture du fichier audio
    try:
        y, sr = sf.read(file_path, dtype="float32")
    except Exception as e:
        print(f"[!] Impossible de lire le fichier {file_path} : {e}")
        return

    # Si stéréo, mixage en mono
    if y.ndim > 1:
        y = y.mean(axis=1)

    print(f"[*] Analyse de : {file_path}")
    print(f"[*] Fréquence d'échantillonnage : {sr} Hz")
    print(f"[*] Durée totale : {len(y)/sr:.2f} secondes")

    # 2. Calcul de la FFT (Transformée de Fourier)
    n = len(y)
    fft_data = np.fft.rfft(y, n=n)
    freqs = np.fft.rfftfreq(n, d=1/sr)
    amplitudes = np.abs(fft_data)

    # Normalisation de l'amplitude
    amplitudes /= np.max(amplitudes) + 1e-9

    # 3. Recherche des pics de fréquence principaux (seuils min de distance et hauteur)
    peaks, _ = find_peaks(amplitudes, height=0.2, distance=int(20 / (sr/n)))
    
    # Trier les pics par ordre d'importance (amplitude décroissante)
    important_peaks = peaks[np.argsort(amplitudes[peaks])][::-1]

    print("\n" + "="*40)
    print("   FRÉQUENCES ESSENTIELLES DÉTECTÉES   ")
    print("="*40)
    for i, peak_idx in enumerate(important_peaks[:5]):
        freq = freqs[peak_idx]
        amp = amplitudes[peak_idx]
        print(f" Top {i+1} : {freq:.1f} Hz (Intensité: {amp:.2f})")
    print("="*40)

    # 4. Affichage du graphique spectral
    plt.figure(figsize=(10, 4))
    plt.plot(freqs, amplitudes, label="Spectre d'amplitude", color='royalblue')
    plt.plot(freqs[important_peaks[:3]], amplitudes[important_peaks[:3]], "ro", label="Pics principaux")
    
    # Zoom sur la zone utile de l'audio humain (0 à 2000 Hz)
    plt.xlim(0, 2000)
    plt.title(f"Analyse Fréquentielle - {file_path}")
    plt.xlabel("Fréquence (Hz)")
    plt.ylabel("Amplitude Normalisée")
    plt.grid(True, linestyle="--", alpha=0.6)
    plt.legend()
    plt.show()

if __name__ == "__main__":
    extraire_frequences_clefs(FILE_PATH)