#Requires AutoHotkey v2.0
#SingleInstance Force

; ─── CONFIG ───────────────────────────────────────────────────────────────────
DETECTOR_PATH := "C:\Users\Alexandre\fof_detector\sot_horn_detector_v3.py"
PYTHON_PATH   := "C:\miniconda\envs\sot\python.exe"
FLAG_FILE     := "C:\Users\Alexandre\fof_detector\fort_detected.txt"
BOAT_TOGGLE   := false

; ─── HOTKEYS ──────────────────────────────────────────────────────────────────
; Ctrl+Alt+F → start loop (depuis menu principal)
; Ctrl+Alt+O → stop tout

~^!f:: {
    StartLoop()
}

~^!o:: {
    StopLoop()
}

; ─── STATE ────────────────────────────────────────────────────────────────────
global running      := false
global detector_pid := 0

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
    StartDetector()
    ToolTip("🏴‍☠️ Loop démarrée  |  Ctrl+Alt+O pour stop")
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
    if detector_pid != 0 {
        ProcessClose(detector_pid)
        detector_pid := 0
    }
    if FileExist(FLAG_FILE)
        FileDelete(FLAG_FILE)
    Run(PYTHON_PATH . " " . DETECTOR_PATH, , "Hide", &detector_pid)
}

RunSession() {
    global running, BOAT_TOGGLE

    ; ── Jouer (depuis menu principal) ─────────────────────────────────────
    Send("{Space}")   ; jouer
    Sleep(1000)
    Send("{Space}")   ; aventure
    Sleep(1000)
    Send("{Space}")   ; haute mer
    Sleep(1000)

    ; ── Guilde ────────────────────────────────────────────────────────────
    Send("{Right}")
    Sleep(500)
    Send("{Right}")
    Sleep(500)
    Send("{Enter}")
    Sleep(1000)
    Send("{Enter}")
    Sleep(1000)

    ; ── Choix bateau ──────────────────────────────────────────────────────
    Send("{Up}")
    Sleep(300)
    Send("{Left}")
    Sleep(300)
    Send("{Left}")
    Sleep(300)

    if BOAT_TOGGLE {
        Send("{Enter}")
    } else {
        Send("{Right}")
        Sleep(300)
        Send("{Enter}")
    }
    BOAT_TOGGLE := !BOAT_TOGGLE

    Sleep(1000)
    Send("{Enter}")   ; confirmer départ
    Sleep(1000)
    Send("{Down}")    ; mode guilde ouvert
    Sleep(300)
    Send("{Enter}")
    Sleep(1000)
    Send("{Enter}")   ; confirmation finale

    ; ── Attendre 45s (chargement + son) ───────────────────────────────────
    loop 45 {
        if !running
            return
        if FileExist(FLAG_FILE)
            return
        Sleep(1000)
    }

    ; ── Quitter la partie → retour menu ───────────────────────────────────
    Send("{Enter}")
    Sleep(1000)
    Send("{Escape}")
    Sleep(1000)
    Loop 7 {
        Send("{Down}")
        Sleep(500)
    }
    Sleep(300)
    Send("{Enter}")
    Sleep(1000)
    Send("{Enter}")

    ; ── Attendre écran titre (15s) ────────────────────────────────────────
    loop 15 {
        if !running
            return
        if FileExist(FLAG_FILE)
            return
        Sleep(1000)
    }

    ; ── Entrée + attendre 10s ─────────────────────────────────────────────
    Send("{Enter}")
    loop 10 {
        if !running
            return
        if FileExist(FLAG_FILE)
            return
        Sleep(1000)
    }

    ; ── Fermer popup écran titre ───────────────────────────────────────────
    Send("{Escape}")
    Sleep(500)
    Send("{Escape}")
    Sleep(1000)
}
