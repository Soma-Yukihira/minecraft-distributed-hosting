; Inno Setup script of the Windows client. Built by build.ps1 (iscc /DAppVersion=x.y.z).
;
; * Per-user install in %LOCALAPPDATA%\Programs: no administrator rights, nothing system-wide.
; * Refuses to run while the application is open (AppMutex): closing it by force could stop
;   Minecraft without its final world backup. The user quits it from the tray icon first.
; * Uninstall keeps the worlds no matter what; it only offers to delete what can be downloaded
;   again (Java, server JAR, cache) and the logs.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Minecraft Distributed Hosting"
#define AppExe "MinecraftDistributedHosting.exe"
#define DataDir "{localappdata}\MinecraftDistributedHosting"

[Setup]
AppId={{94A88E09-DF04-4920-B4E6-E401F79FCF8A}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Minecraft Distributed Hosting
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AppMutex=MinecraftDistributedHostingRunning
CloseApplications=no
OutputDir=..\..\dist\installer
OutputBaseFilename=MinecraftDistributedHosting-Setup-{#AppVersion}
SetupIconFile=..\..\build\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Messages]
french.SetupAppRunningError=L'application Minecraft Distributed Hosting est ouverte.%n%nQuitte-la depuis son icône près de l'horloge (clic droit > Quitter) : si ce PC héberge le serveur, il sera arrêté proprement et le monde sauvegardé. Clique ensuite sur OK.
french.UninstallAppRunningError=L'application Minecraft Distributed Hosting est ouverte.%n%nQuitte-la depuis son icône près de l'horloge (clic droit > Quitter) : si ce PC héberge le serveur, il sera arrêté proprement et le monde sauvegardé. Clique ensuite sur OK.

[Tasks]
Name: "desktopicon"; Description: "Créer une icône sur le Bureau"; GroupDescription: "Raccourcis :"; Flags: unchecked

[Files]
Source: "..\..\dist\MinecraftDistributedHosting\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Files of an older version that the new one no longer ships (the folder is fully ours).
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Lancer {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  AppMutexName = 'MinecraftDistributedHostingRunning';

{ Automatic updates (mdh_client/system/updates.py): the application starts this installer with
  /VERYSILENT /RELAUNCH=show|tray, then quits. Wait for it to be gone (it may still be uploading
  the world), install, and start it again -- even if the installation failed, so the PC is never
  left without its agent. InitializeSetup runs before the AppMutex check. }
function UpdateMode(): String;
begin
  Result := ExpandConstant('{param:relaunch|}');
end;

function InitializeSetup(): Boolean;
var
  Waited: Integer;
begin
  Result := True;
  if UpdateMode() = '' then
    Exit;
  Waited := 0;
  while CheckForMutexes(AppMutexName) and (Waited < 600) do
  begin
    Sleep(500);
    Waited := Waited + 1;
  end;
end;

procedure DeinitializeSetup();
var
  Exe, Params: String;
  Code: Integer;
begin
  if (UpdateMode() = '') or CheckForMutexes(AppMutexName) then
    Exit;
  try
    Exe := ExpandConstant('{app}\{#AppExe}');
  except
    Exit;  { stopped before the installation folder was known }
  end;
  if not FileExists(Exe) then
    Exit;
  Params := '';
  if UpdateMode() = 'tray' then
    Params := '--minimized';
  Exec(Exe, Params, ExpandConstant('{app}'), SW_SHOWNORMAL, ewNoWait, Code);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    { "Start with Windows" is set by the application itself: remove it with the program. }
    RegDeleteValue(HKEY_CURRENT_USER, RunKey, 'MinecraftDistributedHosting');
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{#DataDir}');
    if DirExists(Data) and not UninstallSilent() then
      if MsgBox('Supprimer aussi les fichiers téléchargés (Java, serveur Minecraft, cache) et les journaux ?'
                + #13#10#13#10 + 'Tes mondes ne sont jamais supprimés : ils restent dans' + #13#10
                + Data + '\servers' + #13#10 + 'et les sauvegardes du groupe restent sur le serveur central.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      begin
        DelTree(Data + '\runtimes', True, True, True);
        DelTree(Data + '\cache', True, True, True);
        DelTree(Data + '\logs', True, True, True);
        DeleteFile(Data + '\credentials.bin');
      end;
  end;
end;
