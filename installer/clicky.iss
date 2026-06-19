; Inno Setup script for Peep.
;
; Wraps the PyInstaller --onedir output (dist/Peep/) into a single
; Setup.exe for distribution via GitHub Releases.
;
; Pattern cribbed from doug-101/TreeLine v3.2.1 (PyQt6 + Inno Setup
; reference). Per-user install — no UAC prompt, lower friction for
; portfolio-tier users on locked-down corporate machines.
;
; Build:
;     iscc installer\clicky.iss
;
; Output: installer\Output\Peep-Setup-v0.1.0.exe (~80-150 MB
; after Inno's LZMA2 compresses the 275 MB onedir bundle).
;
; Inno Setup 6+ required: https://jrsoftware.org/isdl.php (free).

#define AppName "Peep"
#define AppVersion "0.2.1"
#define AppPublisher "Monish Gosar"
#define AppURL "https://github.com/MonishGosar/peep"
#define AppExeName "Peep.exe"

[Setup]
AppId={{C9A8F1B3-7D2E-4A6F-9E8C-3B1D5F2A4C8D}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}/releases
; Per-user install — no admin/UAC required. {userpf} = %LOCALAPPDATA%\Programs
DefaultDirName={userpf}\{#AppName}
DefaultGroupName={#AppName}
; PrivilegesRequired=lowest avoids the elevation prompt; auto means
; per-user-or-system based on whether the user is admin (we want
; per-user always for portfolio scope).
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=Peep-Setup-v{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
SetupLogging=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Bundle the entire PyInstaller --onedir output. recursesubdirs grabs
; the _internal/ tree (Qt plugins, Python stdlib, all bundled deps).
Source: "..\dist\Peep\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Open Knowledge Folder"; Filename: "{userdocs}\Peep Wiki"; Comment: "Drop per-app .md files here"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Don't delete user data on uninstall — preserve memory + KB folders.
; If a user wants a clean wipe, they delete ~/.peep/ +
; ~/Documents/Peep Wiki/ manually. This matches the "transparency
; contract" UX: their data is theirs.
Type: filesandordirs; Name: "{app}\_internal\__pycache__"
