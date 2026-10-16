const
  ReleaseReparsePoint = $400;
  ReleaseDirectory = $10;
  ReleaseInvalidAttributes = $FFFFFFFF;

function GetReleaseFileAttributes(FileName: String): Cardinal;
  external 'GetFileAttributesW@kernel32.dll stdcall';

{ 拒绝目录联接，避免安装目录或旧发行文件实际指向用户的其它目录。
  Args:
    FileName: 即将检查或清理的绝对路径。
  Returns:
    不返回值；发现重解析路径时停止安装。 }
procedure CheckReleasePathAncestors(FileName: String);
var
  ParentName: String;
  Attributes: Cardinal;
begin
  repeat
    Attributes := GetReleaseFileAttributes(FileName);
    if (Attributes <> ReleaseInvalidAttributes) and
       ((Attributes and ReleaseReparsePoint) <> 0) then
      RaiseException('安装路径包含目录联接或符号链接，未清理文件：' + FileName);
    ParentName := ExtractFileDir(FileName);
    if CompareText(ParentName, FileName) = 0 then
      Break;
    FileName := ParentName;
  until FileName = '';
end;

{ 先独占检查旧文件，全部通过后再按同一白名单逐项删除。
  Args:
    FileName: 安装根内已明确属于发行包的文件或目录。
    DeleteFiles: 是否进入删除阶段；检查阶段不会改变文件。
  Returns:
    不返回值；文件使用中、无法删除或出现重解析路径时停止安装。 }
procedure VisitOwnedReleasePath(FileName: String; DeleteFiles: Boolean);
var
  Attributes: Cardinal;
  Found: TFindRec;
  Stream: TFileStream;
begin
  Attributes := GetReleaseFileAttributes(FileName);
  if Attributes = ReleaseInvalidAttributes then
    Exit;
  CheckReleasePathAncestors(FileName);

  if (Attributes and ReleaseDirectory) <> 0 then
  begin
    if FindFirst(AddBackslash(FileName) + '*', Found) then
    begin
      try
        repeat
          if (Found.Name <> '.') and (Found.Name <> '..') then
            VisitOwnedReleasePath(AddBackslash(FileName) + Found.Name, DeleteFiles);
        until not FindNext(Found);
      finally
        FindClose(Found);
      end;
    end;
    if DeleteFiles and not RemoveDir(FileName) then
      RaiseException('旧发行目录无法删除，请关闭 NearMate 后重试：' + FileName);
    Exit;
  end;

  { 映射中的程序和 DLL 不能以独占读写方式打开；不按名称终止其它进程。 }
  try
    Stream := TFileStream.Create(FileName, fmOpenReadWrite or fmShareExclusive);
    Stream.Free;
  except
    RaiseException('旧发行文件仍被使用或无法写入，请关闭 NearMate 后重试：' + FileName);
  end;
  if DeleteFiles and not DeleteFile(FileName) then
    RaiseException('旧发行文件无法删除，安装已停止：' + FileName);
end;

{ 检查运行时清单是否明确属于 NearMate Windows 发行。
  Args:
    FileName: 待识别的 runtime-manifest.json 绝对路径。
  Returns:
    同时包含 schema v3、NearMate 产品、Windows x64 平台和必需文件清单时返回 True。 }
function IsNearMateRuntimeManifest(FileName: String): Boolean;
var
  Lines: TArrayOfString;
  Index: Integer;
  NormalizedLine: String;
  HasProduct: Boolean;
  HasPlatform: Boolean;
  HasSchemaVersion: Boolean;
  HasRequiredFiles: Boolean;
begin
  Result := False;
  if not LoadStringsFromFile(FileName, Lines) then
    Exit;

  HasProduct := False;
  HasPlatform := False;
  HasSchemaVersion := False;
  HasRequiredFiles := False;
  for Index := 0 to GetArrayLength(Lines) - 1 do
  begin
    NormalizedLine := Trim(Lines[Index]);
    StringChangeEx(NormalizedLine, ' ', '', True);
    StringChangeEx(NormalizedLine, #9, '', True);
    if Pos('"product":"NearMate"', NormalizedLine) = 1 then
      HasProduct := True;
    if Pos('"platform":"windows-x64"', NormalizedLine) = 1 then
      HasPlatform := True;
    if Pos('"schema_version":3', NormalizedLine) = 1 then
      HasSchemaVersion := True;
    if Pos('"required_files":', NormalizedLine) = 1 then
      HasRequiredFiles := True;
  end;
  Result := HasProduct and HasPlatform and HasSchemaVersion and HasRequiredFiles;
end;

function IsSafeReleaseRootEntry(Entry: String): Boolean; forward;
function LoadOwnedReleasePaths(FileName: String; var OwnedPaths: TArrayOfString): Boolean; forward;

{ 判断安装目录是否具有可升级的 NearMate 发行身份。
  Args:
    AppRoot: 带末尾反斜杠的安装根目录。
  Returns:
    主程序、通过身份检查的运行时清单和发行所有权清单都存在时返回 True。 }
function IsPreviousNearMateRelease(AppRoot: String): Boolean;
var
  ManifestPath: String;
  OwnershipPath: String;
  OwnedPaths: TArrayOfString;
  Index: Integer;
  HasMainExecutable: Boolean;
  HasResourcesDirectory: Boolean;
  HasOwnershipFile: Boolean;
begin
  ManifestPath := AppRoot + 'resources\runtime\runtime-manifest.json';
  OwnershipPath := AppRoot + 'release-root-files.txt';
  HasMainExecutable := False;
  HasResourcesDirectory := False;
  HasOwnershipFile := False;
  if LoadOwnedReleasePaths(OwnershipPath, OwnedPaths) then
    for Index := 0 to GetArrayLength(OwnedPaths) - 1 do
    begin
      HasMainExecutable := HasMainExecutable or
        (CompareText(OwnedPaths[Index], 'NearMate.exe') = 0);
      HasResourcesDirectory := HasResourcesDirectory or
        (CompareText(OwnedPaths[Index], 'resources') = 0);
      HasOwnershipFile := HasOwnershipFile or
        (CompareText(OwnedPaths[Index], 'release-root-files.txt') = 0);
    end;
  Result := FileExists(AppRoot + 'NearMate.exe') and
    FileExists(ManifestPath) and
    IsNearMateRuntimeManifest(ManifestPath) and
    HasMainExecutable and HasResourcesDirectory and HasOwnershipFile;
end;

{ 读取构建阶段从 Electron unpacked 目录生成的根级所有权清单。
  Args:
    FileName: 安装根下的 release-root-files.txt。
    OwnedPaths: 输出每个发行根级文件或目录名。
  Returns:
    清单存在、非空且每一行都是安全的根级名称时返回 True。 }
function LoadOwnedReleasePaths(FileName: String; var OwnedPaths: TArrayOfString): Boolean;
var
  Lines: TArrayOfString;
  Index: Integer;
  Entry: String;
  RawEntry: String;
begin
  Result := False;
  SetArrayLength(OwnedPaths, 0);
  if not LoadStringsFromFile(FileName, Lines) then
    Exit;

  for Index := 0 to GetArrayLength(Lines) - 1 do
  begin
    RawEntry := Lines[Index];
    Entry := Trim(RawEntry);
    if Entry = '' then
      Continue;
    { 根级清单只允许 Windows 的普通单段名称；任何非法字符或设备名都说明清单已损坏。 }
    if (Entry <> RawEntry) or not IsSafeReleaseRootEntry(Entry) then
      Exit;
    SetArrayLength(OwnedPaths, GetArrayLength(OwnedPaths) + 1);
    OwnedPaths[GetArrayLength(OwnedPaths) - 1] := Entry;
  end;

  Result := GetArrayLength(OwnedPaths) > 0;
end;

{ 校验发行清单中的根级名称，防止损坏清单借助通配符或设备名逃过清理。
  Args:
    Entry: 清单中的单个根级文件或目录名。
  Returns:
    名称是安全的 Windows 普通单段路径时返回 True。 }
function IsSafeReleaseRootEntry(Entry: String): Boolean;
var
  Index: Integer;
  CharacterCode: Integer;
  BaseName: String;
  DotPosition: Integer;
begin
  Result := False;
  if Entry = '' then
    Exit;
  { Windows 单个路径段最长 255 个字符；超长项不可能来自本次 unpacked 发行。 }
  if Length(Entry) > 255 then
    Exit;
  if (Entry = '.') or (Entry = '..') or
     (Entry[1] = '.') or
     (Entry[Length(Entry)] = '.') or
     (Entry[Length(Entry)] = ' ') then
    Exit;

  for Index := 1 to Length(Entry) do
  begin
    CharacterCode := Ord(Entry[Index]);
    if (CharacterCode < 32) or
       (Pos(Entry[Index], '<>:"/\|?*') > 0) then
      Exit;
  end;

  DotPosition := Pos('.', Entry);
  if DotPosition > 0 then
    BaseName := Copy(Entry, 1, DotPosition - 1)
  else
    BaseName := Entry;
  if (CompareText(BaseName, 'CON') = 0) or
     (CompareText(BaseName, 'PRN') = 0) or
     (CompareText(BaseName, 'AUX') = 0) or
     (CompareText(BaseName, 'NUL') = 0) or
     ((Length(BaseName) = 4) and
      ((CompareText(Copy(BaseName, 1, 3), 'COM') = 0) or
       (CompareText(Copy(BaseName, 1, 3), 'LPT') = 0)) and
      (BaseName[4] >= '1') and (BaseName[4] <= '9')) then
    Exit;

  Result := True;
end;

{ 检查安装根下所有现存项目，避免用户新增的联接在升级复制时把文件导向安装目录外。
  Args:
    AppRoot: 带末尾反斜杠的安装根目录。
  Returns:
    不返回值；发现重解析点或无法读取的根级项目时停止安装。 }
procedure CheckReleaseRootEntries(AppRoot: String);
var
  RootEntry: TFindRec;
  RootPath: String;
  Attributes: Cardinal;
begin
  if not DirExists(AppRoot) then
    Exit;
  if not FindFirst(AppRoot + '*', RootEntry) then
    Exit;
  try
    repeat
      if (RootEntry.Name <> '.') and (RootEntry.Name <> '..') then
      begin
        RootPath := AppRoot + RootEntry.Name;
        Attributes := GetReleaseFileAttributes(RootPath);
        if Attributes = ReleaseInvalidAttributes then
          RaiseException('无法读取安装根项目，安装已停止：' + RootPath);
        if (Attributes and ReleaseReparsePoint) <> 0 then
          RaiseException('安装根包含目录联接或符号链接，未进行升级：' + RootPath);
      end;
    until not FindNext(RootEntry);
  finally
    FindClose(RootEntry);
  end;
end;

{ 只更换应用拥有的发行目录与固定程序文件，用户配置和模型不参与清理。
  Args:
    无。
  Returns:
    不返回值；所有旧文件清理成功后，安装器才开始复制新发行。 }
procedure CleanPreviousNearMateRelease();
var
  AppRoot: String;
  Target: String;
  OwnedPaths: TArrayOfString;
  Index: Integer;
  HasPreviousRelease: Boolean;
  RootEntry: TFindRec;
  HasUnownedRootEntry: Boolean;
begin
  AppRoot := AddBackslash(ExpandFileName(ExpandConstant('{app}')));
  CheckReleasePathAncestors(RemoveBackslashUnlessRoot(AppRoot));
  CheckReleaseRootEntries(AppRoot);
  { 只有主程序、运行时清单和所有权清单身份都匹配时才可被升级接管。 }
  HasPreviousRelease := IsPreviousNearMateRelease(AppRoot);

  { 没有旧发行身份时，只允许接管空目录。否则用户误选一个普通非空目录，
    后续复制会把不属于 NearMate 的文件留在安装根，升级边界就失效了。 }
  HasUnownedRootEntry := False;
  if not HasPreviousRelease and DirExists(AppRoot) and
     FindFirst(AppRoot + '*', RootEntry) then
  begin
    try
      repeat
        if (RootEntry.Name <> '.') and (RootEntry.Name <> '..') then
        begin
          HasUnownedRootEntry := True;
          Break;
        end;
      until not FindNext(RootEntry);
    finally
      FindClose(RootEntry);
    end;
  end;
  if HasUnownedRootEntry then
    RaiseException('目标安装目录不是空目录，且没有 NearMate 安装身份；请选择独立的安装目录：' + AppRoot);

  if not HasPreviousRelease then
    Exit;
  if not LoadOwnedReleasePaths(AppRoot + 'release-root-files.txt', OwnedPaths) then
    RaiseException('旧 NearMate 缺少有效的发行所有权清单，安装已停止：' + AppRoot);

  { 在删除第一项之前检查全部目标，任何占用都保留原安装供用户退出后重试。 }
  for Index := 0 to GetArrayLength(OwnedPaths) - 1 do
  begin
    Target := ExpandFileName(AppRoot + OwnedPaths[Index]);
    if CompareText(Copy(Target, 1, Length(AppRoot)), AppRoot) <> 0 then
      RaiseException('发行清理路径超出安装目录，安装已停止。');
    VisitOwnedReleasePath(Target, False);
  end;
  for Index := 0 to GetArrayLength(OwnedPaths) - 1 do
    VisitOwnedReleasePath(AppRoot + OwnedPaths[Index], True);
end;
