#Requires AutoHotkey v2.0
#SingleInstance Force

; ─── CONFIGURATION DE CHEMINS (Ajuste si nécessaire) ─────────────────────────
DETECTOR_PATH := "C:\Users\Alexandre\fof_detector\test.py"
PYTHON_PATH   := "C:\miniconda\envs\sot\python.exe"
FLAG_FILE     := "C:\Users\Alexandre\fof_detector\fort_detected.txt"
BOAT_TOGGLE   := false

; ─── CONFIGURATION MODE TEST (DIAGNOSTIC) ─────────────────────────────────────
TEST_MODE     := True   ; true = Mode test AHK uniquement, false = Mode réel

; ─── TIMINGS INTER-SAISIES (ms) ────────────────────────────────────────────────
T_CURSOR         := 500    ; entre Q Q E
T_MENU_NAV       := 1000   ; entre Enter jouer/aventure/haute mer
T_AFTER_HAUTEMER := 4000   ; attente chargement avant sélection guilde
T_ARROW          := 500    ; entre flèches guilde
T_AFTER_GUILDE   := 1000   ; après Enter guilde
T_BOAT_ARROW     := 500    ; entre flèches choix bateau
T_BEFORE_DEPART  := 1000   ; avant confirmer départ
T_AFTER_DEPART   := 1000   ; après confirmer départ
T_GUILDE_OUVERTE := 500    ; entre Down et Enter mode guilde ouvert
T_AFTER_CONFIRM  := 1000   ; après confirmation finale
T_IN_GAME        := 25     ; Secondes d'écoute à la taverne
T_QUIT_ARROW     := 500    ; entre flèches menu quitter
T_TITLE_SCREEN   := 15     ; secondes attente écran titre
T_AFTER_ENTER_TITLE := 10  ; secondes attente après Enter sur écran titre
T_POPUP_ESC      := 500    ; entre les deux Escape popup

; ─── RACCOURCIS CLAVIER D'ORIGINE ─────────────────────────────────────────────
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

StartDetector() {
    global detector_pid, PYTHON_PATH, DETECTOR_PATH, FLAG_FILE, TEST_MODE
    
    if TEST_MODE
        return
        
    ; Protection : on nettoie les restes s'il y en a
    if detector_pid != 0 {
        try ProcessClose(detector_pid)
        detector_pid := 0
    }
    
    if FileExist(FLAG_FILE)
        try FileDelete(FLAG_FILE)
        
    ; Commande pour lancer Python en arrière-plan masqué ("Hide")
    cmd := PYTHON_PATH . " " . DETECTOR_PATH
    Run(cmd, , "Hide", &detector_pid)
    
    ToolTip("🚀 Moteur Python lancé en arrière-plan (PID: " . detector_pid . ")")
    SetTimer(() => ToolTip(), -3000)
}

StopLoop() {
    global running, detector_pid
    running := false
    
    ; On laisse un petit battement puis on coupe proprement le processus Python
    Sleep(1000)
    if detector_pid != 0 {
        try {
            ProcessClose(detector_pid)
        }
        detector_pid := 0
    }
    
    if FileExist(FLAG_FILE)
        try FileDelete(FLAG_FILE)
        
    ToolTip("🛑 Macro et Python COMPLÈTEMENT FERMÉS")
    SetTimer(() => ToolTip(), -5000)
    
    ExitApp
}

StartLoop() {
    global running, FLAG_FILE, TEST_MODE
    if running {
        ToolTip("⚠️ Boucle déjà active")
        SetTimer(() => ToolTip(), -2000)
        return
    }
    
    running := true
    
    ; Lancement AUTOMATIQUE de Python au tout début du script (une seule fois !)
    if !TEST_MODE {
        StartDetector()
        Sleep(1500) ; Laisse le temps à Python de charger le template audio
    }
    
    ToolTip("🏴‍☠️ Boucle lancée | Ctrl+Shift+Q pour tout couper")
    
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
    global running, BOAT_TOGGLE, TEST_MODE
    global T_CURSOR, T_MENU_NAV, T_AFTER_HAUTEMER, T_ARROW, T_AFTER_GUILDE
    global T_BOAT_ARROW, T_BEFORE_DEPART, T_AFTER_DEPART, T_GUILDE_OUVERTE
    global T_AFTER_CONFIRM, T_IN_GAME, T_QUIT_ARROW, T_TITLE_SCREEN
    global T_AFTER_ENTER_TITLE, T_POPUP_ESC

    ; ── Alignement curseur principal ──────────────────────────────────────
    Send("q")
    WaitChecked(T_CURSOR)
    Send("q")
    WaitChecked(T_CURSOR)
    Send("e")
    WaitChecked(T_CURSOR)

    ; ── Navigation de session (Jouer → Aventure → Haute Mer) ──────────────
    Send("{Enter}")
    if !WaitChecked(T_MENU_NAV)
        return
    Send("{Enter}")
    if !WaitChecked(T_MENU_NAV)
        return
    Send("{Enter}")
    if !WaitChecked(T_AFTER_HAUTEMER)
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
    if !WaitChecked(2500)
        return
    Send("{Enter}")
    if !WaitChecked(T_AFTER_CONFIRM)
        return

    ; ── Attente de l'arrivée en jeu et réveil du détecteur ─────────────────
    if !TEST_MODE {
        ToolTip("⚓ Arrivée Taverne : Réveil du détecteur (F10)...")
        Send("{F10}")
        WaitChecked(500)
        ToolTip()
    } else {
        ToolTip("🧪 TEST MACRO : Simulation en jeu (10s)...")
    }

    ; ── Fenêtre d'écoute en jeu (25 secondes) ──────────────────────────────
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

    
    ; ── Sortie de session (MODIFIÉE AVEC TON INPUT PHYSIQUE 200MS) ─────────
    if !WaitChecked(1000)
        return

    ; Appui physique sur Echap (Down), maintien pendant 200ms, puis relâchement (Up)
    Send("{Escape Down}")
    Sleep(200) ; Maintien de 200 ms
    Send("{Escape Up}")

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
    Send("{Escape}")
    WaitChecked(T_POPUP_ESC)
    Send("{Escape}")
    WaitChecked(1000)
}