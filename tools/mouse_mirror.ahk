; mouse_mirror.ahk — broadcast your clicks to hand-picked windows (AutoHotkey v2)
;
; Install AutoHotkey v2 from https://www.autohotkey.com, then double-click this file.
; A green "H" icon appears in the tray while it runs.
;
; Hotkeys:
;   Ctrl+Alt+T  — tag/untag the window UNDER YOUR MOUSE as a mirror target
;   Ctrl+Alt+L  — show the list of tagged windows
;   Ctrl+Alt+C  — clear all tagged windows
;   Ctrl+Alt+M  — toggle mirroring ON/OFF (starts OFF)
;   Ctrl+Alt+K  — toggle KEYBOARD mirroring ON/OFF (starts OFF; needs mirroring ON too)
;   Ctrl+Alt+Q  — quit
;
; While mirroring is ON, every left-click AND every scroll-wheel notch you make in
; ANY window is replayed at the same client-area coordinates in every tagged window
; (the window you acted in is skipped, so no doubling there). Windows should be the
; SAME SIZE and scrolled the same, or the mirrored actions will land on different
; things. Note: scroll amounts can still drift apart if pages differ in length or a
; window eats a notch — re-sync with Ctrl+Home style jumps (top of page) if needed.
;
; Notes:
;  - ControlClick posts the click without stealing focus, so the target windows
;    don't flash to the front.
;  - Chrome windows of one profile share one login/cart. For independent carts use
;    separate profiles (chrome.exe --profile-directory=...) or different browsers.

#Requires AutoHotkey v2.0
#SingleInstance Force
CoordMode "Mouse", "Screen"

targets := Map()        ; hwnd -> title
mirroring := false
keyMirror := false

Tip(msg) {
    ToolTip msg
    SetTimer () => ToolTip(), -1500
}

^!t:: {
    global targets
    MouseGetPos ,, &hwnd
    hwnd := DllCall("GetAncestor", "ptr", hwnd, "uint", 2, "ptr")  ; GA_ROOT
    if !hwnd
        return
    if targets.Has(hwnd) {
        targets.Delete(hwnd)
        Tip "Untagged: " WinGetTitle(hwnd)
    } else {
        targets[hwnd] := WinGetTitle(hwnd)
        Tip "Tagged (" targets.Count "): " targets[hwnd]
    }
}

^!l:: {
    global targets
    if !targets.Count {
        Tip "No windows tagged"
        return
    }
    list := ""
    for hwnd, title in targets
        list .= (WinExist(hwnd) ? "" : "[CLOSED] ") title "`n"
    MsgBox list, "Mirror targets (" targets.Count ")"
}

^!c:: {
    global targets
    targets := Map()
    Tip "Cleared all targets"
}

^!m:: {
    global mirroring
    mirroring := !mirroring
    Tip "Mirroring " (mirroring ? "ON  (" targets.Count " targets)" : "OFF")
}

^!q:: ExitApp()

; Replay a mouse action ("Left", "WheelUp", "WheelDown") at the cursor's
; window-relative position in every tagged window except the one it happened in.
Broadcast(button) {
    global targets, mirroring
    if !mirroring || !targets.Count
        return
    MouseGetPos &mx, &my, &srcHwnd
    srcHwnd := DllCall("GetAncestor", "ptr", srcHwnd, "uint", 2, "ptr")

    ; screen coords -> client coords of the window the action happened in
    pt := Buffer(8)
    NumPut("int", mx, pt, 0), NumPut("int", my, pt, 4)
    DllCall("ScreenToClient", "ptr", srcHwnd, "ptr", pt)
    cx := NumGet(pt, 0, "int"), cy := NumGet(pt, 4, "int")

    dead := []
    for hwnd in targets {
        if hwnd = srcHwnd
            continue
        if !WinExist(hwnd) {
            dead.Push(hwnd)
            continue
        }
        try ControlClick "x" cx " y" cy, hwnd,, button,, "NA Pos"
    }
    for hwnd in dead
        targets.Delete(hwnd)
}

; ~ = let the real action through to the window it happened in
~LButton::   Broadcast("Left")
~WheelUp::   Broadcast("WheelUp")
~WheelDown:: Broadcast("WheelDown")

^!k:: {
    global keyMirror
    keyMirror := !keyMirror
    Tip "Keyboard mirroring " (keyMirror ? "ON" : "OFF")
}

; ---- keyboard mirroring -------------------------------------------------
; Replays keystrokes (with Shift/Ctrl held state) into every tagged window
; except the ACTIVE one (where the real keystroke already lands).
BroadcastKey(key, *) {
    global targets, mirroring, keyMirror
    if !mirroring || !keyMirror || !targets.Count
        return
    ; never mirror our own Ctrl+Alt hotkeys / AltGr combos
    if GetKeyState("Alt", "P")
        return
    mods := (GetKeyState("Ctrl", "P") ? "^" : "") . (GetKeyState("Shift", "P") ? "+" : "")
    srcHwnd := WinExist("A")
    dead := []
    for hwnd in targets {
        if hwnd = srcHwnd
            continue
        if !WinExist(hwnd) {
            dead.Push(hwnd)
            continue
        }
        try ControlSend mods "{" key "}",, hwnd
    }
    for hwnd in dead
        targets.Delete(hwnd)
}

; Register a pass-through wildcard hotkey for every key we mirror.
mirrorKeys := []
Loop 26                     ; a-z
    mirrorKeys.Push(Chr(Ord("a") + A_Index - 1))
Loop 10                     ; 0-9
    mirrorKeys.Push(Chr(Ord("0") + A_Index - 1))
for k in StrSplit("`` - = [ ] \ `; ' , . /", " ")   ; punctuation
    mirrorKeys.Push(k)
for k in ["Space", "Enter", "Backspace", "Tab", "Delete",
          "Up", "Down", "Left", "Right", "Home", "End", "PgUp", "PgDn"]
    mirrorKeys.Push(k)
for k in mirrorKeys
    Hotkey "~*" k, BroadcastKey.Bind(k)
