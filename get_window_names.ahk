#Requires AutoHotkey v2.0

PYTHON_PATH   := "C:\miniconda\envs\sot\python.exe"
DETECTOR_PATH := "C:\Users\Alexandre\fof_detector\v8.4_compatible.py"

; Lancer Python
cmd := PYTHON_PATH . " " . DETECTOR_PATH . " --listen 45"
Run(cmd, , , &pid)

Sleep(2000)

; Récupérer le titre de la fenêtre par PID
title := WinGetTitle("ahk_pid " . pid)
MsgBox("PID: " . pid . "`nTitre fenêtre: " . title)
