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
;   Ctrl+Alt+Q  — quit
;
; While mirroring is ON, every left-click you make in ANY window is replayed at the
; same client-area coordinates in every tagged window (the window you clicked in is
; skipped, so no double-click there). Windows should be the SAME SIZE and scrolled
; the same, or the mirrored clicks will land on different things.
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

; ~ = let the real click through to the window you clicked in
~LButton:: {
    global targets, mirroring
    if !mirroring || !targets.Count
        return
    MouseGetPos &mx, &my, &srcHwnd
    srcHwnd := DllCall("GetAncestor", "ptr", srcHwnd, "uint", 2, "ptr")

    ; screen coords -> client coords of the window that was actually clicked
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
        try ControlClick "x" cx " y" cy, hwnd,,,, "NA Pos"
    }
    for hwnd in dead
        targets.Delete(hwnd)
}
