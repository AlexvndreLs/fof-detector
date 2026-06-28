#Requires AutoHotkey v2.0
#SingleInstance Force

; ─── CONFIGURATION DE CHEMINS ─────────────────────────────────────────────────
DETECTOR_PATH := "C:\Users\Alexandre\fof_detector\v8.4_compatible.py"
PYTHON_PATH   := "C:\miniconda\envs\sot\python.exe"
FLAG_FILE     := "C:\Users\Alexandre\fof_detector\fort_detected.txt"
BOAT_TOGGLE   := false

; ─── CONFIGURATION MODE TEST (DIAGNOSTIC) ─────────────────────────────────────
TEST_MODE     := false   ; true  = Mode test AHK uniquement (aucun lancement de Python, attente courte en jeu)
                        ; false = Mode réel (lance Python en tâche de fond, arrête dès qu'un fort est détecté)
SHOW_PYTHON   := true    ; true  = Affiche la console Python pendant T_IN_GAME
                        ; false = Python tourne caché en permanence

; ─── TIMINGS INTER-SAISIES (ms) ────────────────────────────────────────────────
T_CURSOR               := 500    ; entre Q Q E
T_MENU_NAV             := 1000   ; entre Enter jouer/aventure/haute mer
T_HAUTEMER_BEFORE_PY   := 2000   ; attente avant lancement Python (dans T_AFTER_HAUTEMER)
T_HAUTEMER_AFTER_PY    := 2000  ; attente après lancement Python (reste de T_AFTER_HAUTEMER)
T_ARROW                := 500    ; entre flèches guilde
T_AFTER_GUILDE         := 1000   ; après Enter guilde
T_BOAT_ARROW           := 500    ; entre flèches choix bateau
T_BEFORE_DEPART        := 1000   ; avant confirmer départ
T_AFTER_DEPART         := 1000   ; après confirmer départ
T_GUILDE_OUVERTE       := 500    ; entre Down et Enter mode guilde ouvert
T_AFTER_CONFIRM        := 1000   ; après confirmation finale
T_BEFORE_ENTER_DEPART  := 2500   ; attente avant Enter final de lancement matchmaking
T_IN_GAME              := 60     ; secondes d'écoute en jeu (en mode réel)
T_QUIT_ARROW           := 500    ; entre flèches menu quitter
T_TITLE_SCREEN         := 15     ; secondes attente écran titre
T_AFTER_ENTER_TITLE    := 10     ; secondes attente après Enter sur écran titre
T_POPUP_ESC            := 500    ; entre les deux Escape popup
T_STOP_DELAY           := 5000   ; attente avant taskkill (laisse Python envoyer Discord)
T_PYTHON_HIDE          := 10000  ; délai avant masquage fenêtre Python après T_IN_GAME (ms)

; ─── RACCOURCIS CLAVIER ────────────────────────────────────────────────────────
^+f:: {
    StartLoop()
}

^+q:: {
    StopLoop()
}

; ─── ÉTAT GLOBAL ──────────────────────────────────────────────────────────────
global running      := false
global detector_pid := 0

; ─── ACTIONS PRINCIPALES ──────────────────────────────────────────────────────

StopLoop() {
    global running, detector_pid
    running := false
    
    Sleep(T_STOP_DELAY)
    try {
        Run("taskkill /F /IM python.exe", , "Hide")
    }
    detector_pid := 0
    
    if FileExist(FLAG_FILE)
        try FileDelete(FLAG_FILE)
        
    ToolTip(" Macro COMPLÈTEMENT FERMÉE à " . FormatTime(, "HH:mm:ss"))
    SetTimer(() => ToolTip(), -5000)
    
    ExitApp
}

StartLoop() {
    global running, FLAG_FILE
    if running {
        ToolTip(" Boucle déjà active")
        SetTimer(() => ToolTip(), -2000)
        return
    }
    
    if FileExist(FLAG_FILE) {
        try FileDelete(FLAG_FILE)
    }
        
    running := true
    ToolTip(" Boucle de serveurs lancée | Ctrl+Shift+Q pour quitter")
    
    Loop {
        if !running
            break
        if FileExist(FLAG_FILE) {
            try FileDelete(FLAG_FILE)
            ToolTip("⚓ FORT TROUVÉ - Fin du script !")
            StopLoop()
            break
        }
        RunSession()
        if !running
            break
    }
}

StartDetector() {
    global detector_pid, PYTHON_PATH, DETECTOR_PATH, FLAG_FILE, TEST_MODE
    
    if TEST_MODE {
        return
    }
    
    try {
        Run("taskkill /F /IM python.exe", , "Hide")
    }
    Sleep(500)
    
    if FileExist(FLAG_FILE) {
        try FileDelete(FLAG_FILE)
    }
        
    cmd := "cmd.exe /k " . PYTHON_PATH . " " . DETECTOR_PATH
    Run(cmd, , "Hide", &detector_pid)
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
    global running, BOAT_TOGGLE, detector_pid, TEST_MODE, SHOW_PYTHON
    global T_CURSOR, T_MENU_NAV, T_HAUTEMER_BEFORE_PY, T_HAUTEMER_AFTER_PY, T_ARROW, T_AFTER_GUILDE
    global T_BOAT_ARROW, T_BEFORE_DEPART, T_AFTER_DEPART, T_GUILDE_OUVERTE
    global T_AFTER_CONFIRM, T_BEFORE_ENTER_DEPART, T_IN_GAME, T_QUIT_ARROW, T_TITLE_SCREEN
    global T_AFTER_ENTER_TITLE, T_POPUP_ESC, T_PYTHON_HIDE

    ; ── Alignement curseur principal ──────────────────────────────────────
    Send("q")
    WaitChecked(T_CURSOR)
    Send("q")
    WaitChecked(T_CURSOR)
    Send("e")
    WaitChecked(T_CURSOR)
    Send("{Down}")
    WaitChecked(T_CURSOR)

    ; ── Navigation de session (Jouer → Aventure → Haute Mer) ──────────────
    Send("{Enter}")
    if !WaitChecked(T_MENU_NAV)
        return
    Send("{Left}")
    WaitChecked(T_MENU_NAV)
    Send("{Enter}")
    if !WaitChecked(T_MENU_NAV)
        return
    Send("{Left}")
    WaitChecked(T_MENU_NAV)
    Send("{Enter}")
    if !WaitChecked(T_HAUTEMER_BEFORE_PY)
        return

    ; ── Lancement de l'écoute du signal (Uniquement si hors Mode Test) ───
    ; Lancé ici : 2s dans T_AFTER_HAUTEMER, soit ~15s avant T_IN_GAME
    if !TEST_MODE {
        StartDetector()
    } else {
        ToolTip("🧪 TEST MACRO : Pas de Python. Simulation en jeu (10s)...")
    }

    if !WaitChecked(T_HAUTEMER_AFTER_PY)
        return

    ; ── Sélection de la Guilde ────────────────────────────────────────────
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

    ; ── Choix Alternatif du Navire ───────────────────────────────────────
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

    ; ── Confirmation et Lancement du Matchmaking ─────────────────────────
    if !WaitChecked(T_BEFORE_DEPART)
        return
    Send("{Up}")
    WaitChecked(T_BOAT_ARROW)
    Send("{Enter}")
    if !WaitChecked(T_AFTER_DEPART)
        return

    ; ── Initialisation du Lobby Ouvert ─────────────────────────────────────
    Send("{Down}")
    WaitChecked(T_GUILDE_OUVERTE)
    Send("{Enter}")
    if !WaitChecked(T_AFTER_CONFIRM)
        return
    Send("{Enter}")
    if !WaitChecked(T_AFTER_CONFIRM)
        return

    ; ── Validation finale avant écran de chargement ──────────────────────
    if !WaitChecked(T_BEFORE_ENTER_DEPART)
        return
    Send("{Enter}")
    if !WaitChecked(T_AFTER_CONFIRM)
        return

    ; ── Attente de l'arrivée de la session de jeu ────────────────────────
    if !TEST_MODE && SHOW_PYTHON {
        WinWait("ahk_pid " . detector_pid, , 30)
        WinShow("ahk_pid " . detector_pid)
        WinActivate("ahk_pid " . detector_pid)
    }

    wait_time := TEST_MODE ? 10 : T_IN_GAME
    loop wait_time {
        if !running
            return
        if !TEST_MODE && FileExist(FLAG_FILE)
            return
        Sleep(1000)
    }

    if TEST_MODE {
        ToolTip("🏴‍☠️ Boucle de serveurs lancée | Ctrl+Shift+Q pour quitter")
    }

    ; ── OUVERTURE DU MENU DE PAUSE STANDARD ───────────────────────────────
    if !TEST_MODE && SHOW_PYTHON {
        WaitChecked(T_PYTHON_HIDE)
        WinHide("ahk_pid " . detector_pid)
    }
    if !WaitChecked(2000)
        return
    WinActivate("ahk_exe SoTGame.exe")
    WinWaitActive("ahk_exe SoTGame.exe", , 3)
    Send("{Escape}")

    if !WaitChecked(3000)
        return

    Loop 7 {
        Send("{Down}")
        if !WaitChecked(T_QUIT_ARROW)
            return
    }
    
    if !WaitChecked(300)
        return
        
    Send("{Enter}")
    if !WaitChecked(1000)
        return
        
    Send("{Enter}")

    ; ── NETTOYAGE RADICAL DE PYTHON (PLUS D'ÉCOUTE APPRÈS LA TAVERNE) ─────
    if !TEST_MODE {
        try {
            Run("taskkill /F /IM python.exe", , "Hide")
        }
        detector_pid := 0
    }
    
    ; ── Retour et attente de l'écran titre ───────────────────────────────
    loop T_TITLE_SCREEN {
        if !running
            return
        if !TEST_MODE && FileExist(FLAG_FILE)
            return
        Sleep(1000)
    }

    ; ── Re-matchmaking ────────────────────────────────────────────────────
    Send("{Enter}")
    loop T_AFTER_ENTER_TITLE {
        if !running
            return
        if !TEST_MODE && FileExist(FLAG_FILE)
            return
        Sleep(1000)
    }

    ; ── Fermeture des popups Rare de début de jeu ──────────────────────────
    WaitChecked(1000)
}