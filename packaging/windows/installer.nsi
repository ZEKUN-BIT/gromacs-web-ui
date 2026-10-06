Unicode True
!include "MUI2.nsh"
!include "x64.nsh"
!include "WinVer.nsh"

!ifndef APP_VERSION
  !define APP_VERSION "0.1.0"
!endif
Name "GROMACS Console"
OutFile "${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\GromacsConsole"
InstallDirRegKey HKCU "Software\GromacsConsole" "InstallDir"
RequestExecutionLevel user
ManifestDPIAware true
ManifestDPIAwareness System
ManifestSupportedOS Win10
SetFont "Segoe UI" 10
SetFont /LANG=2052 "Microsoft YaHei UI" 10
BrandingText "GROMACS Console"
SetCompressor /SOLID lzma
VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey /LANG=1033 "ProductName" "GROMACS Console"
VIAddVersionKey /LANG=1033 "FileDescription" "Lightweight Windows launcher and WSL2 installer"
VIAddVersionKey /LANG=1033 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "LegalCopyright" "Copyright (c) 2026 yzk"

!define MUI_ICON "${__FILEDIR__}\GromacsConsole.ico"
!define MUI_UNICON "${__FILEDIR__}\GromacsConsole.ico"
!define MUI_BGCOLOR "FFFEF8"
!define MUI_TEXTCOLOR "18211E"
!define MUI_WELCOMEFINISHPAGE_BITMAP "${__FILEDIR__}\wizard-sidebar.bmp"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_RIGHT
!define MUI_HEADERIMAGE_BITMAP "${__FILEDIR__}\wizard-header.bmp"
!define MUI_WELCOMEPAGE_TITLE "$(WelcomeTitle)"
!define MUI_WELCOMEPAGE_TEXT "$(WelcomeText)"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\Start.cmd"
!define MUI_FINISHPAGE_TITLE "$(FinishTitle)"
!define MUI_FINISHPAGE_TEXT "$(FinishText)"
!define MUI_FINISHPAGE_RUN_TEXT "$(RunText)"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "SimpChinese"

LangString WelcomeTitle ${LANG_ENGLISH} "Welcome to$\r$\nGROMACS Console"
LangString WelcomeTitle ${LANG_SIMPCHINESE} "欢迎安装$\r$\nGROMACS Console"
LangString WelcomeText ${LANG_ENGLISH} "Your local molecular simulation workspace.$\r$\n$\r$\nRun GROMACS from your browser, with automatic GPU detection and ligand preparation tools.$\r$\n$\r$\nThe first launch prepares Ubuntu on WSL2 and the scientific tools. Existing compatible environments are reused.$\r$\n$\r$\nPlease keep an Internet connection and at least 8 GB of free disk space for the first setup."
LangString WelcomeText ${LANG_SIMPCHINESE} "本地分子动力学工作台。$\r$\n$\r$\n在浏览器中配置和运行 GROMACS，自动检测 GPU，并提供配体参数化工具。$\r$\n$\r$\n首次启动时准备 WSL2 Ubuntu 和科学计算环境。已有兼容环境会直接复用。$\r$\n$\r$\n首次配置需要联网，请预留至少 8 GB 磁盘空间。"
LangString FinishTitle ${LANG_ENGLISH} "Ready to get started"
LangString FinishTitle ${LANG_SIMPCHINESE} "安装完成"
LangString FinishText ${LANG_ENGLISH} "The launcher and desktop shortcut are ready.$\r$\n$\r$\nSelect the option below to prepare the computing environment and open your workspace. First-time setup may take a few minutes."
LangString FinishText ${LANG_SIMPCHINESE} "启动器和桌面快捷方式已准备好。$\r$\n$\r$\n勾选下方选项，将配置计算环境并打开工作台。首次配置需要等待一段时间。"
LangString RunText ${LANG_ENGLISH} "Set up and open GROMACS Console"
LangString RunText ${LANG_SIMPCHINESE} "配置计算环境并打开 GROMACS Console"
LangString KeepEnvironment ${LANG_ENGLISH} "Windows shortcuts will be removed. WSL2, the computation environment, and simulation results are kept.$\r$\nTo reclaim environment space, use Remove Computation Environment before uninstalling."
LangString KeepEnvironment ${LANG_SIMPCHINESE} "将移除 Windows 快捷方式，并保留 WSL2、计算环境和模拟结果。$\r$\n如需释放计算环境占用，请先运行 Remove Computation Environment。"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "64-bit Windows on an Intel/AMD CPU is required."
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "Windows 10 or Windows 11 with WSL2 is required."
    Abort
  ${EndIf}
  !insertmacro MUI_LANGDLL_DISPLAY
FunctionEnd

Section "GROMACS Console"
  SetOutPath "$INSTDIR"
  File "${PAYLOAD_DIR}\application.tar.gz"
  File "${PAYLOAD_DIR}\application.sha256"
  File "${PAYLOAD_DIR}\Console.ps1"
  File "${PAYLOAD_DIR}\WslSetup.ps1"
  File "${PAYLOAD_DIR}\WslPrerequisites.ps1"
  File "${PAYLOAD_DIR}\PowerShellHost.ps1"
  File "${PAYLOAD_DIR}\ubuntu-rootfs.json"
  File "${PAYLOAD_DIR}\Start.cmd"
  File "${PAYLOAD_DIR}\GromacsConsole.ico"
  File "${PAYLOAD_DIR}\linux-install.sh"
  File "${PAYLOAD_DIR}\application_update.py"
  File "${PAYLOAD_DIR}\linux-system.sh"
  File "${PAYLOAD_DIR}\desktop_service.py"
  File "${PAYLOAD_DIR}\gpu_setup.py"
  File "${PAYLOAD_DIR}\dependency_setup.py"
  File "${PAYLOAD_DIR}\science-tools.py"
  File "${PAYLOAD_DIR}\science-requirements.txt"
  File "${PAYLOAD_DIR}\science-bootstrap-requirements.txt"
  File "${PAYLOAD_DIR}\windows-python-requirements.lock"
  File "${PAYLOAD_DIR}\LICENSE"
  File "${PAYLOAD_DIR}\THIRD_PARTY_NOTICES.md"
  File "${PAYLOAD_DIR}\release.json"
  File "${PAYLOAD_DIR}\README.md"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\GromacsConsole" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GromacsConsole" "DisplayName" "GROMACS Console"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GromacsConsole" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GromacsConsole" "DisplayIcon" "$INSTDIR\GromacsConsole.ico"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GromacsConsole" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GromacsConsole" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GromacsConsole" "NoRepair" 1
  CreateDirectory "$SMPROGRAMS\GROMACS Console"
  CreateShortcut "$DESKTOP\GROMACS Console.lnk" "$INSTDIR\Start.cmd" "" "$INSTDIR\GromacsConsole.ico" 0
  CreateShortcut "$SMPROGRAMS\GROMACS Console\GROMACS Console.lnk" "$INSTDIR\Start.cmd" "" "$INSTDIR\GromacsConsole.ico" 0
  CreateShortcut "$SMPROGRAMS\GROMACS Console\Configure or Update.lnk" "$INSTDIR\Start.cmd" "Install" "$INSTDIR\GromacsConsole.ico" 0
  CreateShortcut "$SMPROGRAMS\GROMACS Console\Stop Service.lnk" "$INSTDIR\Start.cmd" "Stop" "$INSTDIR\GromacsConsole.ico" 0
  CreateShortcut "$SMPROGRAMS\GROMACS Console\Open Results.lnk" "$INSTDIR\Start.cmd" "Files" "$INSTDIR\GromacsConsole.ico" 0
  CreateShortcut "$SMPROGRAMS\GROMACS Console\Diagnose WSL.lnk" "$INSTDIR\Start.cmd" "Diagnose" "$INSTDIR\GromacsConsole.ico" 0
  CreateShortcut "$SMPROGRAMS\GROMACS Console\Remove Computation Environment.lnk" "$INSTDIR\Start.cmd" "RemoveEnvironment" "$INSTDIR\GromacsConsole.ico" 0
  CreateShortcut "$SMPROGRAMS\GROMACS Console\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
  MessageBox MB_OK "$(KeepEnvironment)"
  Delete "$DESKTOP\GROMACS Console.lnk"
  RMDir /r "$SMPROGRAMS\GROMACS Console"
  ; Only delete known distribution files; never recursively delete an installation directory.
  Delete "$INSTDIR\application.tar.gz"
  Delete "$INSTDIR\application.sha256"
  Delete "$INSTDIR\Console.ps1"
  Delete "$INSTDIR\WslSetup.ps1"
  Delete "$INSTDIR\WslPrerequisites.ps1"
  Delete "$INSTDIR\PowerShellHost.ps1"
  Delete "$INSTDIR\ubuntu-rootfs.json"
  Delete "$INSTDIR\Start.cmd"
  Delete "$INSTDIR\GromacsConsole.ico"
  Delete "$INSTDIR\linux-install.sh"
  Delete "$INSTDIR\application_update.py"
  Delete "$INSTDIR\linux-system.sh"
  Delete "$INSTDIR\desktop_service.py"
  Delete "$INSTDIR\gpu_setup.py"
  Delete "$INSTDIR\dependency_setup.py"
  Delete "$INSTDIR\science-tools.py"
  Delete "$INSTDIR\science-requirements.txt"
  Delete "$INSTDIR\science-bootstrap-requirements.txt"
  Delete "$INSTDIR\windows-python-requirements.lock"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\THIRD_PARTY_NOTICES.md"
  Delete "$INSTDIR\release.json"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\wsl-settings.json"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\GromacsConsole"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GromacsConsole"
SectionEnd
