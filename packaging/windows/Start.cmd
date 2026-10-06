@echo off
setlocal
set "GROMACS_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "GROMACS_POWERSHELL=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
set "GROMACS_ACTION=Start"
if /i "%~1"=="Install" set "GROMACS_ACTION=Install"
if /i "%~1"=="Stop" set "GROMACS_ACTION=Stop"
if /i "%~1"=="Files" set "GROMACS_ACTION=Files"
if /i "%~1"=="Diagnose" set "GROMACS_ACTION=Diagnose"
if /i "%~1"=="RemoveEnvironment" set "GROMACS_ACTION=RemoveEnvironment"
"%GROMACS_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0PowerShellHost.ps1" -LaunchAction %GROMACS_ACTION%
set "GROMACS_EXIT=%ERRORLEVEL%"
if not "%GROMACS_EXIT%"=="0" (pause) else if /i not "%GROMACS_ACTION%"=="Start" if /i not "%GROMACS_ACTION%"=="Files" pause
exit /b %GROMACS_EXIT%
