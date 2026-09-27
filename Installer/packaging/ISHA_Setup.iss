; Inno Setup script -> ISHA_Setup.exe (small online installer, ~1 MB).
; Build on Windows:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\ISHA_Setup.iss
; The .exe only unpacks the ISHA setup payload to a temp folder and starts the ISHA
; bootstrapper, which runs the graphical installer (location, Python, packages,
; llama.cpp, models, shortcuts, uninstaller). Models are NOT bundled.
#define AppVersion "0.5.0"

[Setup]
AppId={{6B7C1E3A-5A0E-4B8B-9E61-1A2B3C4D5E6F}
AppName=ISHA Setup
AppVersion={#AppVersion}
AppPublisher=ISHA Project
CreateAppDir=no
Uninstallable=no
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ISHA_Setup
Compression=lzma2
SolidCompression=yes
DisableWelcomePage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
#if FileExists(AddBackslash(SourcePath) + "..\icon\isha.ico")
SetupIconFile=..\icon\isha.ico
#endif
WizardStyle=modern

[Files]
Source: "..\mani.py";               DestDir: "{tmp}\isha"; Flags: ignoreversion
Source: "..\isha_core\*";           DestDir: "{tmp}\isha\isha_core"; Excludes: "__pycache__\*"; Flags: recursesubdirs ignoreversion
Source: "..\installer\*";           DestDir: "{tmp}\isha\installer"; Excludes: "__pycache__\*"; Flags: recursesubdirs ignoreversion
Source: "..\icon\*";                DestDir: "{tmp}\isha\icon"; Flags: recursesubdirs ignoreversion skipifsourcedoesntexist
Source: "..\*.md";                  DestDir: "{tmp}\isha"; Flags: ignoreversion
Source: "..\requirements.txt";      DestDir: "{tmp}\isha"; Flags: ignoreversion
Source: "..\isha_config.example.json"; DestDir: "{tmp}\isha"; Flags: ignoreversion
Source: "..\ISHA_Setup.ps1";        DestDir: "{tmp}\isha"; Flags: ignoreversion
Source: "..\ISHA_Setup.cmd";        DestDir: "{tmp}\isha"; Flags: ignoreversion
; Offline full installer: uncomment to embed an offline\ bundle made by build_offline_bundle.py
;Source: "..\offline\*";            DestDir: "{tmp}\isha\offline"; Flags: recursesubdirs ignoreversion

[Run]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{tmp}\isha\ISHA_Setup.ps1"""; \
  WorkingDir: "{tmp}\isha"; Flags: waituntilterminated
