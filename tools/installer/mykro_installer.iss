[Setup]
AppName=MYKRO
AppVersion=0.1.0
DefaultDirName={localappdata}\MYKRO
DefaultGroupName=MYKRO
OutputBaseFilename=MYKRO-Setup
Compression=lzma
SolidCompression=yes

[Files]
Source: "dist\MYKRO\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs
Source: "..\mykro_icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Additional icons:"; Flags: unchecked

[Icons]
Name: "{group}\MYKRO"; Filename: "{app}\MYKRO.exe"; WorkingDir: "{app}"; IconFilename: "{app}\mykro_icon.ico"
Name: "{userdesktop}\MYKRO.lnk"; Filename: "{app}\MYKRO.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\MYKRO.exe"; Description: "Launch MYKRO"; Flags: nowait postinstall skipifsilent
