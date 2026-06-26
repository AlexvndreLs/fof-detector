#Requires AutoHotkey v2.0
#SingleInstance Force

; ─── CONFIG ───────────────────────────────────────────────────────────────────
DETECTOR_PATH := "C:\Users\Alexandre\fof_detector\sot_horn_detector_v3.py"
PYTHON_PATH   := "C:\miniconda\envs\sot\python.exe"
FLAG_FILE     := "C:\Users\Alexandre\fof_detector\fort_detected.txt"
BOAT_TOGGLE   := false

; ─── TIMINGS (ms) ─────────────────────────────────────────────────────────────
T_CURSOR       := 500    ; entre Q Q E
T_MENU_NAV     := 1000   ; entre Enter jouer/aventure/haute mer
T_AFTER_HAUTEMER := 10000 ; attente chargement avant sélection guilde
T_ARROW        := 500    ; entre flèches guilde
T_AFTER_GUILDE := 1000   ; après Enter guilde
T_BOAT_ARROW   := 500    ; entre flèches choix bateau
T_BEFORE_DEPART := 1000  ; avant confirmer départ
T_AFTER_DEPART := 1000   ; après confirmer départ
T_GUILDE_OUVERTE := 500  ; entre Down et Enter mode guilde ouvert
T_AFTER_CONFIRM := 1000  ; après confirmation finale
T_IN_GAME      := 45     ; secondes d'écoute en jeu
T_QUIT_ARROW   := 500    ; entre flèches menu quitter
T_TITLE_SCREEN := 15     ; secondes attente écran titre
T_AFTER_ENTER_TITLE := 10 ; secondes attente après Enter sur écran titre
T_POPUP_ESC    := 500    ; entre les deux Escape popup

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

WaitChecked(ms) {
    global running, FLAG_FILE
    steps := ms // 100
    loop steps {
        if !running
            return false
        if FileExist(FLAG_FILE)
            return false
        Sleep(100)
    }
    return true
}

RunSession() {
    global running, BOAT_TOGGLE
    global T_CURSOR, T_MENU_NAV, T_AFTER_HAUTEMER, T_ARROW, T_AFTER_GUILDE
    global T_BOAT_ARROW, T_BEFORE_DEPART, T_AFTER_DEPART, T_GUILDE_OUVERTE
    global T_AFTER_CONFIRM, T_IN_GAME, T_QUIT_ARROW, T_TITLE_SCREEN
    global T_AFTER_ENTER_TITLE, T_POPUP_ESC

    ; ── Positionner curseur ────────────────────────────────────────────────
    Send("q")
    WaitChecked(T_CURSOR)
    Send("q")
    WaitChecked(T_CURSOR)
    Send("e")
    WaitChecked(T_CURSOR)

    ; ── Jouer → Aventure → Haute Mer ──────────────────────────────────────
    Send("{Enter}")
    if !WaitChecked(T_MENU_NAV)
        return
    Send("{Enter}")
    if !WaitChecked(T_MENU_NAV)
        return
    Send("{Enter}")
    if !WaitChecked(T_AFTER_HAUTEMER)
        return

    ; ── Guilde ────────────────────────────────────────────────────────────
    Send("{Right}")
    if !WaitChecked(T_ARROW)
        return
    Send("{Right}")
    if !WaitChecked(T_ARROW)
        return
    Send("{Enter}")
    if !WaitChecked(T_AFTER_GUILDE)
        return
    Send("{Enter}")
    if !WaitChecked(T_AFTER_GUILDE)
        return

    ; ── Choix bateau ──────────────────────────────────────────────────────
    Send("{Up}")
    WaitChecked(T_BOAT_ARROW)
    Send("{Left}")
    WaitChecked(T_BOAT_ARROW)
    Send("{Left}")
    WaitChecked(T_BOAT_ARROW)

    if BOAT_TOGGLE {
        Send("{Enter}")
    } else {
        Send("{Right}")
        WaitChecked(T_BOAT_ARROW)
        Send("{Enter}")
    }
    BOAT_TOGGLE := !BOAT_TOGGLE

    ; ── Confirmer départ ──────────────────────────────────────────────────
    if !WaitChecked(T_BEFORE_DEPART)
        return
    Send("{Up}")
    WaitChecked(T_BOAT_ARROW)
    Send("{Enter}")
    if !WaitChecked(T_AFTER_DEPART)
        return

    ; ── Mode guilde ouvert ────────────────────────────────────────────────
    Send("{Down}")
    WaitChecked(T_GUILDE_OUVERTE)
    Send("{Enter}")
    if !WaitChecked(T_AFTER_CONFIRM)
        return
    Send("{Enter}")
    if !WaitChecked(T_AFTER_CONFIRM)
        return

    ; ── Confirmation avant chargement ─────────────────────────────────────
    if !WaitChecked(2500)
        return
    Send("{Enter}")
    if !WaitChecked(T_AFTER_CONFIRM)
        return

    ; ── Attendre en jeu (chargement + écoute son) ─────────────────────────
    loop T_IN_GAME {
        if !running
            return
        if FileExist(FLAG_FILE)
            return
        Sleep(1000)
    }

    ; ── Quitter la partie ─────────────────────────────────────────────────
    Send("{Enter}")
    WaitChecked(1000)
    Send("{Escape}")
    WaitChecked(1000)
    Loop 7 {
        Send("{Down}")
        WaitChecked(T_QUIT_ARROW)
    }
    WaitChecked(300)
    Send("{Enter}")
    WaitChecked(1000)
    Send("{Enter}")

    ; ── Attendre écran titre ──────────────────────────────────────────────
    loop T_TITLE_SCREEN {
        if !running
            return
        if FileExist(FLAG_FILE)
            return
        Sleep(1000)
    }

    ; ── Enter + attendre ──────────────────────────────────────────────────
    Send("{Enter}")
    loop T_AFTER_ENTER_TITLE {
        if !running
            return
        if FileExist(FLAG_FILE)
            return
        Sleep(1000)
    }

    ; ── Fermer popup ──────────────────────────────────────────────────────
    Send("{Escape}")
    WaitChecked(T_POPUP_ESC)
    Send("{Escape}")
    WaitChecked(1000)
}
