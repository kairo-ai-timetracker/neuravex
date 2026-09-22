@echo off
REM NEURAVEX launcher — double-click this file instead of start.ps1
REM directly. A .bat file isn't subject to Windows' "not digitally
REM signed" PowerShell script block, so it can unblock start.ps1 for you
REM automatically every time, instead of you running Unblock-File by hand.

powershell -NoProfile -ExecutionPolicy Bypass -Command "Unblock-File -Path '%~dp0start.ps1'; & '%~dp0start.ps1'"
