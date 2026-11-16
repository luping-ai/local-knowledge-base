; LocalKB 安装向导（Inno Setup 6）
;
; ## 为什么用 Inno Setup 而不是自研 stub
;
; 仓库既有 tools/mksfx 是为 Runtime 做的（Go stub + 内嵌 payload）。但"解压
; 并运行一个 exe"和"安装向导"是两件事：后者要处理
;   · 安装目录选择与权限
;   · 卸载项注册（设置 → 应用）
;   · 开始菜单 / 桌面快捷方式
;   · 静默安装参数（供别的安装器调用）
;   · 文件占用检测（程序正在运行时提示关闭）
; 这些 Inno Setup 都现成且经过大量验证，自研要几百行代码且必然踩坑。
;
; ## 为什么安装包不含 Runtime
;
; 方案 §3.1：桌面端**复用已装的 LocalAIHub**。同一台机器上只能有一份 Runtime
; （单实例锁），装第二份会让两者互相争用端口、token 与数据目录。
;
; 所以本安装脚本只做三件事：
;   1. 装 LocalKB 本体（exe + _internal）
;   2. 检测 LocalAIHub，未装时明确告知并提供下载入口
;   3. 注册卸载项与开始菜单

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef RuntimeVersion
  #define RuntimeVersion "unknown"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist-localkb\LocalKB"
#endif
#ifndef OutputDir
  #define OutputDir "..\output"
#endif

[Setup]
; AppId 一旦发布就不能改：它决定"同一产品"的身份。
; 改动会让新版本被当成另一个软件（并存安装 + 两个卸载项）。
AppId={{8F3A5C21-7B4D-4E9A-9C1F-5D6E7A8B9C0D}
AppName=LocalKB
AppVersion={#AppVersion}
AppVerName=LocalKB {#AppVersion}
AppPublisher=Local AI Hub
DefaultDirName={localappdata}\Programs\LocalKB
; 每用户安装（与 ADR 0002 的"每用户实例"一致）：不需要管理员权限，
; 办公机用户通常没有提权。
PrivilegesRequired=lowest
DefaultGroupName=LocalKB
AllowNoIcons=yes
OutputDir={#OutputDir}
OutputBaseFilename=LocalKB-{#AppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=LocalKB {#AppVersion}
DisableProgramGroupPage=yes
#ifdef SignTool
SignTool={#SignTool}
SignedUninstaller=yes
#endif

[Languages]
; ## 为什么中文语言包是仓库里的文件而不是 compiler:Languages
;
; Inno Setup 6 的官方安装包**只带 30 种语言，不含简体中文**（简体中文是
; 官方仓库里的 user-contributed 翻译，不在 Languages 目录）。写
; `compiler:Languages\ChineseSimplified.isl` 会在编译时直接失败：
;   Could not read "Languages\ChineseSimplified.isl"
;
; 所以把 .isl 放进 tools/ 一起入库，用脚本所在目录的绝对路径引用。
; 这样换一台机器、换个 Inno 版本都能编译。
Name: "chinesesimplified"; MessagesFile: "{#SourcePath}\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
; 整个 onedir 产物。Flags 说明：
;   ignoreversion —— PyInstaller 的 DLL 没有版本资源，不忽略会报错
;   recursesubdirs / createallsubdirs —— _internal 下有多层目录
Source: "{#SourceDir}\LocalKB.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\install-manifest.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\LocalKB"; Filename: "{app}\LocalKB.exe"
Name: "{group}\LocalKB 自检"; Filename: "{cmd}"; \
    Parameters: "/c ""{app}\LocalKB.exe"" --self-check & pause"; \
    Comment: "启动异常时用它查看诊断结果"
Name: "{group}\卸载 LocalKB"; Filename: "{uninstallexe}"
Name: "{autodesktop}\LocalKB"; Filename: "{app}\LocalKB.exe"; Tasks: desktopicon

[Run]
; 装完直接启动。skipifsilent：静默安装时不自动启动，否则会突然弹窗。
Filename: "{app}\LocalKB.exe"; Description: "启动 LocalKB"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

[Code]
var
  RuntimePage: TOutputMsgWizardPage;
  RuntimeFound: Boolean;
  RuntimeVersion: String;

// CheckRuntimeInstalled 读注册表判断 LocalAIHub 是否已装。
//
// ## 为什么读注册表而不是探测 discovery 文件
//
// discovery 文件只在 Runtime **运行时**存在。用户装了但没启动时文件不在，
// 按文件判断会误报"未安装"，然后在安装过程中引导他去装第二份。
//
// 注册表项由 LocalAIHub 安装器写入，装了就一直在。
function CheckRuntimeInstalled(): Boolean;
var
  InstallLocation, DisplayVersion: String;
begin
  Result := False;
  RuntimeVersion := '';
  if RegQueryStringValue(HKCU,
       'Software\Microsoft\Windows\CurrentVersion\Uninstall\LocalAIHub',
       'InstallLocation', InstallLocation) then
  begin
    // 注册表说装了，但要确认 exe 真的在（用户可能手工删过目录）
    if FileExists(AddBackslash(InstallLocation) + 'localai-cli.exe') then
    begin
      Result := True;
      RegQueryStringValue(HKCU,
        'Software\Microsoft\Windows\CurrentVersion\Uninstall\LocalAIHub',
        'DisplayVersion', RuntimeVersion);
    end;
  end;
end;

procedure InitializeWizard();
begin
  RuntimeFound := CheckRuntimeInstalled();
  RuntimePage := CreateOutputMsgPage(wpWelcome,
    '运行环境检查',
    'LocalKB 需要 Local AI Hub 提供推理能力',
    '正在检查…');
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Msg: String;
begin
  if CurPageID = RuntimePage.ID then
  begin
    if RuntimeFound then
      Msg := '已检测到 Local AI Hub（版本 ' + RuntimeVersion + '）。' + #13#10 + #13#10 +
             'LocalKB 将复用它来提供向量化与模型推理。'
    else
      Msg := '未检测到 Local AI Hub。' + #13#10 + #13#10 +
             'LocalKB 是一个客户端：它复用 Local AI Hub 提供推理能力，' +
             '自己不带运行环境。' + #13#10 + #13#10 +
             '请先安装 Local AI Hub，再启动 LocalKB。' + #13#10 +
             '（安装可以继续，但启动后会提示缺少运行环境。）' + #13#10 + #13#10 +
             '为什么这样设计：同一台机器上只能有一份 Runtime（单实例锁）。' +
             '如果本程序自带一份，两者会互相争用端口、token 与数据目录，' +
             '而这类问题在现场几乎无法排查。';
    RuntimePage.MsgLabel.Caption := Msg;
  end;
end;

// 检测目标目录里的程序是否正在运行。
//
// Inno Setup 会自动处理"安装中替换正在使用的文件"，但那个提示很难理解
// （"文件正在使用，是否重试"）。提前检测并让用户自己关掉更友好。
function IsAppRunning(): Boolean;
var
  ResultCode: Integer;
begin
  Result := False;
  if Exec(ExpandConstant('{cmd}'),
          '/c tasklist /FI "IMAGENAME eq LocalKB.exe" /NH | find /I "LocalKB.exe"',
          '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := (ResultCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if IsAppRunning() then
    Result := 'LocalKB 正在运行，请先关闭它再继续安装。';
end;

// 卸载时询问是否删除用户数据（state.db、对话历史）。
//
// ## 为什么默认不删
//
// 用户重装后不该重建 5000 篇文档的索引（那要重新解析几小时）。
// 而模型与 Runtime 数据属于 LocalAIHub，本程序**无权处置**。
//
// ## 为什么用 UninstallSilent() 而不是 WizardSilent()（实测踩到的缺陷）
//
// 第一版写的是 `WizardSilent()`。看起来自然（"静默吗"），但卸载阶段调用会
// 直接抛异常：
//   CurUninstallStepChanged raised an exception.
//   内部错误：Cannot call "WizardSilent" function during Uninstall.
//
// 更麻烦的是**异常让"数据被删"变成"数据没被删"——但这是巧合，不是正确**：
// 抛异常提前结束了这个事件处理函数，恰好跳过了 DelTree。换个 Inno 版本、
// 或异常被处理掉，就会退化成"静默卸载删光用户数据"。
//
// 正确的函数是 UninstallSilent()，它是卸载专用，静默卸载时返回 True
// （已用 tools/_probe_silent.iss 实测：/VERYSILENT 下输出 UninstallSilent=YES，
// 无异常）。
//
// ## 关于 /SUPPRESSMSGBOXES 的坑（第一版就错在这里）
//
// Inno 文档：消息框被抑制时，**需要选择的场景默认响应是 Yes**。
// MB_DEFBUTTON2 只影响用户看得见按钮时的高亮位置，对抑制后的返回值无效。
// 所以只靠 MsgBox 的默认按钮无法保证静默卸载不删数据——必须显式分支。
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  DataDir := ExpandConstant('{localappdata}\LocalKB');
  if not DirExists(DataDir) then
    Exit;

  if CurUninstallStep = usPostUninstall then
  begin
    if UninstallSilent() then
    begin
      Log('静默卸载：保留用户数据 ' + DataDir);
      Exit;
    end;

    if MsgBox('是否同时删除 LocalKB 的本地数据？' + #13#10 + #13#10 +
              '包括：扫描目录配置、文件索引记录、对话历史。' + #13#10 +
              '删除后重新安装需要重新扫描所有目录（可能要几小时）。' + #13#10 + #13#10 +
              '注意：知识库内容与模型属于 Local AI Hub，不会被删除，' +
              '请用 Local AI Hub 的卸载程序处理。' + #13#10 + #13#10 +
              '要删除本地数据吗？', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      DelTree(DataDir, True, True, True);
      Log('用户选择删除本地数据：' + DataDir);
    end else
      Log('用户选择保留本地数据：' + DataDir);
  end;
end;
