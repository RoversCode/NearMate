#ifndef Profile
#define Profile "cuda"
#endif
#ifndef PackageRoot
#define PackageRoot AddBackslash(SourcePath) + "..\desktop\dist-package\" + Profile + "-unpacked\win-unpacked"
#endif

[Setup]
; 沿用历史 AppId，Windows 才能把本次安装识别为原 NearMate 的升级。
AppId={{8D5D75A7-8E4F-4F7D-9E1D-5A7E1A7E9D01}
AppName=NearMate
AppVersion=0.3.0
AppPublisher=NearMate
DefaultDirName={localappdata}\Programs\NearMate
DefaultGroupName=NearMate
OutputBaseFilename=NearMate-{#Profile}-setup
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
Compression=lzma2/max
SolidCompression=yes
DiskSpanning=yes
DiskSliceSize=2000000000
SlicesPerDisk=1
Uninstallable=yes
CloseApplications=yes
RestartApplications=no
VersionInfoProductName=NearMate
VersionInfoDescription=NearMate Installer
SetupIconFile=..\desktop\dist-brand\icon.ico
UninstallDisplayIcon={app}\resources\icon.ico

[Files]
Source: "{#PackageRoot}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[UninstallDelete]
Type: files; Name: "{app}\repair-origin.ini"

[Icons]
Name: "{autoprograms}\NearMate"; Filename: "{app}\NearMate.exe"; IconFilename: "{app}\resources\icon.ico"
Name: "{autodesktop}\NearMate"; Filename: "{app}\NearMate.exe"; IconFilename: "{app}\resources\icon.ico"

[Run]
Filename: "{app}\NearMate.exe"; Description: "启动 NearMate"; Flags: nowait postinstall skipifsilent

[Code]
#include "..\desktop\installer\releaseCleanup.iss"

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    CleanPreviousNearMateRelease();
  if CurStep = ssPostInstall then
    SetIniString('installer', 'sha256', GetSHA256OfFile(ExpandConstant('{srcexe}')), ExpandConstant('{app}\repair-origin.ini'));
end;
