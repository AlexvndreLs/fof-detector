#Requires AutoHotkey v2.0
#SingleInstance Force

; ─── CONFIG ───────────────────────────────────────────────────────────────────
DETECTOR_PATH := "C:\Users\Alexandre\fof_detector\sot_horn_detector_v3.py"
PYTHON_PATH   := "C:\miniconda\envs\sot\python.exe"
FLAG_FILE     := "C:\Users\Alexandre\fof_detector\fort_detected.txt"
BOAT_TOGGLE   := false

; ─── HOTKEYS (double/triple appui anti-accident) ──────────────────────────────
; Ctrl+Shift+F10 → start loop + détecteur
; Ctrl+Shift+F11 → stop tout

~^+F10:: {
    StartLoop()
}

~^+F11:: {
    StopLoop()
}

; ─── STATE ────────────────────────────────────────────────────────────────────
global running      := false
global detector_pid := 0
global ahk_pid      := ProcessExist()  ; PID du script AHK lui-même

; ─── FONCTIONS ────────────────────────────────────────────────────────────────

StopLoop() {
    global running, detector_pid
    running := false
    if detector_pid != 0 {
        ProcessClose(detector_pid)
        detector_pid := 0
    }
    ToolTip("🛑 Loop stoppée")
    SetTimer(() => ToolTip(), -3000)
}

StartLoop() {
    global running
    if running {
        ToolTip("⚠️ Loop déjà active")
        SetTimer(() => ToolTip(), -2000)
        return
    }
    running := true

    ; Lancer le détecteur Python en continu dès le début
    StartDetector()

    ToolTip("🏴‍☠️ Loop démarrée  |  Ctrl+Shift+F11 pour stop")
    Loop {
        if !running
            break
        if FileExist(FLAG_FILE) {
            FileDelete(FLAG_FILE)
            ToolTip("⚓ FORT DETECTED - Stop !")
            StopLoop()
            break
        }
        RunSession()
        if !running
            break
    }
}

StartDetector() {
    global detector_pid, PYTHON_PATH, DETECTOR_PATH, FLAG_FILE
    ; Kill l'ancien si encore en vie
    if detector_pid != 0 {
        ProcessClose(detector_pid)
        detector_pid := 0
    }
    if FileExist(FLAG_FILE)
        FileDelete(FLAG_FILE)
    ; Lancer en mode infini (sans --listen) - AHK gère le timing
    Run(PYTHON_PATH . " " . DETECTOR_PATH, , "Hide", &detector_pid)
}

RunSession() {
    global running, detector_pid, BOAT_TOGGLE

    ; ── Quitter la partie ──────────────────────────────────────────────────
    Send("{Escape}")
    Sleep(500)
    Loop 7
        Send("{Down}")
    Sleep(200)
    Send("{Enter}")
    Sleep(500)
    Send("{Enter}")

    ; Attendre menu principal (15s) en vérifiant le flag
    loop 15 {
        if !running
            return
        if FileExist(FLAG_FILE)
            return
        Sleep(1000)
    }

    ; ── Menu principal ─────────────────────────────────────────────────────
    Send("{Space}")   ; lancer
    loop 5 {
        if !running
            return
        if FileExist(FLAG_FILE)
            return
        Sleep(1000)
    }

    Send("{Space}")   ; jouer
    Sleep(300)
    Send("{Space}")   ; aventure
    Sleep(300)
    Send("{Space}")   ; haute mer
    Sleep(300)

    ; ── Guilde ────────────────────────────────────────────────────────────
    Send("{Right}")
    Sleep(100)
    Send("{Right}")
    Sleep(100)
    Send("{Enter}")
    Sleep(300)
    Send("{Enter}")
    Sleep(500)

    ; ── Choix bateau ──────────────────────────────────────────────────────
    Send("{Up}")
    Sleep(100)
    Send("{Left}")
    Sleep(100)
    Send("{Left}")
    Sleep(200)

    if BOAT_TOGGLE {
        Send("{Enter}")
    } else {
        Send("{Right}")
        Sleep(100)
        Send("{Enter}")
    }
    BOAT_TOGGLE := !BOAT_TOGGLE

    Sleep(300)
    Send("{Enter}")   ; confirmer départ
    Sleep(300)
    Send("{Down}")    ; mode guilde ouvert
    Sleep(100)
    Send("{Enter}")
    Sleep(300)
    Send("{Enter}")   ; confirmation finale

    ; ── Attendre 45s (chargement + son) ───────────────────────────────────
    loop 45 {
        if !running
            return
        if FileExist(FLAG_FILE)
            return
        Sleep(1000)
    }

    ; Pas de fort → le détecteur Python continue, on relance juste la session
}
