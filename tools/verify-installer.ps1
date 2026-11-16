# 安装包验收：A15（同机复用 Runtime）+ 卸载不删用户数据
#
# ## 这个脚本为什么必须存在
#
# 这两个缺陷都是"手工装一遍看起来没问题"的类型：
#
#   · 静默卸载删掉 state.db —— 界面上没有任何报错，日志里也只有一行
#     "CurUninstallStepChanged raised an exception"（而且是因为别的错误
#     碰巧挡住的）。不比对 marker 根本发现不了。
#   · 装了第二份 Runtime —— 现象是"偶尔提问失败/端口被占"，现场无法排查。
#     只有对比 Run 键条数与 launch-spec.json 哈希才看得出来。
#
# 所以把它们写成脚本：每次发版跑一遍，改动引入回归立刻暴露。
#
# ## 用法
#
#   powershell -ExecutionPolicy Bypass -File tools\verify-installer.ps1
#   powershell -ExecutionPolicy Bypass -File tools\verify-installer.ps1 -KeepInstalled
#
# ## 前置条件
#
#   1. 已跑过 package-localkb.ps1，output\LocalKB-<ver>-Setup.exe 存在
#   2. 本机已装 LocalAIHub 并在运行（A15 的前提）
#   3. LocalKB 当前未安装（脚本会先检查，已装则报错要求先卸载）

[CmdletBinding()]
param(
    [string]$SetupPath = "",
    [switch]$KeepInstalled   # 验完不卸载（继续手工看界面时用）
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$appDir  = Join-Path $env:LOCALAPPDATA "Programs\LocalKB"
$dataDir = Join-Path $env:LOCALAPPDATA "LocalKB"
$runKey  = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$specPath = Join-Path $env:LOCALAPPDATA "Programs\LocalAIHub\launch-spec.json"
$uninstKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F3A5C21-7B4D-4E9A-9C1F-5D6E7A8B9C0D}_is1"

$failures = @()
$checks = 0

function Check([string]$name, [bool]$ok, [string]$detail = "") {
    $script:checks++
    if ($ok) {
        Write-Host ("  [PASS] {0}{1}" -f $name, $(if ($detail) { "  -> $detail" } else { "" })) -ForegroundColor Green
    } else {
        Write-Host ("  [FAIL] {0}{1}" -f $name, $(if ($detail) { "  -> $detail" } else { "" })) -ForegroundColor Red
        $script:failures += "$name $detail"
    }
}

if ($SetupPath -eq "") {
    $SetupPath = Get-ChildItem (Join-Path $root "output\LocalKB-*-Setup.exe") -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $SetupPath -or -not (Test-Path $SetupPath)) {
    throw "找不到安装包。先跑 tools\package-localkb.ps1"
}
Write-Host "验收 $SetupPath" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------- 前置

# ## 已安装时自动卸载，而不是报错让人手工处理
#
# 第一版是 `if (Test-Path $appDir) { throw "请先卸载" }`。想法是"基线要干净"，
# 但实际用起来每次都要先手工卸载一遍——而"手工步骤"在验收脚本里等于
# "跑几次之后就不跑了"。
#
# 自动卸载是安全的：本脚本在 [4/5] 用 marker 验证"卸载保留用户数据"，
# 这里先跑一次同样的卸载，正好把"装→卸→再装"的真实升级路径也走了一遍。
if (Test-Path $appDir) {
    Write-Host "检测到已安装的 LocalKB，先卸载（模拟升级路径）…" -ForegroundColor DarkYellow
    $oldUnins = Join-Path $appDir "unins000.exe"
    if (Test-Path $oldUnins) {
        $u0 = Start-Process -FilePath $oldUnins -PassThru -Wait -ArgumentList @(
            "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
        if ($u0.ExitCode -ne 0) {
            throw "卸载现有 LocalKB 失败（exit=$($u0.ExitCode)）"
        }
    } else {
        throw "$appDir 存在但没有 unins000.exe——请手工删除这个目录"
    }
    Start-Sleep -Seconds 2
    if (Test-Path $appDir) {
        throw "$appDir 卸载后仍然存在"
    }
}
if (-not (Test-Path $specPath)) {
    throw "找不到 $specPath——本机 LocalAIHub 未运行或未安装（A15 需要它）"
}

# ---------------------------------------------------------------- 基线

Write-Host "[1/5] 记录基线" -ForegroundColor Cyan

$specHashBefore = (Get-FileHash $specPath -Algorithm SHA256).Hash.ToLower()
$specTimeBefore = (Get-Item $specPath).LastWriteTime

# ## 为什么数 Run 键的条数而不是比较整串
#
# 别的软件也在写 Run 键，整串比对会被无关变化干扰。A15 关心的是
# "LocalKB 有没有偷偷加一条启动项"，所以只数本地相关的。
function Get-LocalRunEntries {
    $props = Get-ItemProperty $runKey -ErrorAction SilentlyContinue
    if (-not $props) { return @() }
    $props.PSObject.Properties |
        Where-Object { $_.Name -notlike "PS*" -and $_.Value -like "*localai*" } |
        Select-Object -ExpandProperty Name
}
$runBefore = @(Get-LocalRunEntries)

$runtimeProcsBefore = @(Get-Process localai-cli -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty Id)
Write-Host "  launch-spec.json sha256 = $($specHashBefore.Substring(0,16))…"
Write-Host "  localai 相关 Run 键 = $($runBefore.Count) 条：$($runBefore -join ', ')"
Write-Host "  localai-cli PID = $($runtimeProcsBefore -join ', ')"

# ---------------------------------------------------------------- 安装

Write-Host "[2/5] 静默安装" -ForegroundColor Cyan
$log = Join-Path $env:TEMP "localkb-verify-install.log"
$p = Start-Process -FilePath $SetupPath -PassThru -Wait -ArgumentList @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=$log")
Check "安装进程退出码 0" ($p.ExitCode -eq 0) "exit=$($p.ExitCode)"
Check "安装目录存在" (Test-Path (Join-Path $appDir "LocalKB.exe"))
Check "卸载器已注册" (Test-Path $uninstKey)

# ---------------------------------------------------------------- A15

Write-Host "[3/5] A15：同机复用一个 Runtime" -ForegroundColor Cyan

$specHashAfter = (Get-FileHash $specPath -Algorithm SHA256).Hash.ToLower()
Check "launch-spec.json 未被改写" ($specHashAfter -eq $specHashBefore) `
    "$($specHashAfter.Substring(0,16))…"
Check "launch-spec.json 修改时间未变" `
    ((Get-Item $specPath).LastWriteTime -eq $specTimeBefore)

$runAfter = @(Get-LocalRunEntries)
Check "没有新增 localai 相关 Run 键" ($runAfter.Count -eq $runBefore.Count) `
    "安装后 $($runAfter.Count) 条"

$runtimeProcsAfter = @(Get-Process localai-cli -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty Id)
Check "没有启动第二个 Runtime" `
    (($runtimeProcsAfter -join ',') -eq ($runtimeProcsBefore -join ',')) `
    "PID $($runtimeProcsAfter -join ',')"

# 自检里的"Runtime 连接"是端到端证据：它真的按 discovery 找到并连上了。
# ## 为什么用 --self-check 而不是看窗口
#
# 窗口截图无法自动化判定"连的是哪个 PID"。--self-check 会把结果写进
# %LOCALAPPDATA%\LocalKB\selfcheck.txt，可以逐行断言。
$selfCheck = Join-Path $dataDir "selfcheck.txt"
if (Test-Path $selfCheck) { Remove-Item $selfCheck -Force }
$sc = Start-Process -FilePath (Join-Path $appDir "LocalKB.exe") `
    -ArgumentList "--self-check" -PassThru -Wait
Check "自检退出码 0" ($sc.ExitCode -eq 0) "exit=$($sc.ExitCode)"

$selfText = ""
if (Test-Path $selfCheck) {
    # ## 为什么必须显式指定 UTF8（验收脚本自己踩的坑）
    #
    # selfcheck.txt 是 Python 写的 UTF-8 无 BOM 文件。PowerShell 5.1 的
    # `Get-Content -Raw` 对**无 BOM** 文件按系统 ANSI 解码，中文全变乱码，
    # 于是"Runtime 连接"、"版本检查通过"这些断言全部误判为 FAIL。
    # 而 ASCII 的断言（level=full、[FAIL]）照样通过——最容易让人以为是
    # 产品坏了，去查自检代码。
    #
    # （产品侧已改为 utf-8-sig 写入，但这里仍显式指定，双重保险。）
    $selfText = Get-Content $selfCheck -Raw -Encoding UTF8
}
Check "自检报告已生成" ($selfText.Length -gt 0)
Check "自检：Runtime 连接成功" ($selfText -match "\[OK\s*\]\s*Runtime 连接")
Check "自检：版本检查通过" ($selfText -match "版本检查通过")
Check "自检：无失败项" ($selfText -notmatch "\[FAIL\]")

# ## 能力等级不能断言 full（第一版就错在这里）
#
# 第一版写 `Check "自检：能力等级为 full" ($selfText -match "level=full")`。
# 想法是"能连上就该是全能力"，但 `level` 取决于**当前可用内存**：
#
#   full       内存够加载 3B 模型
#   rule_only  只剩关键词/规则能力（内存紧张时）
#
# 实测触发场景：一次 10 万块的向量化刚跑完，Runtime 占着 125 MB 向量 +
# 索引，系统可用内存降到阈值以下，Runtime 如实报了 `rule_only`。
# 产品行为完全正确（它就该降级），是**断言写错了**。
#
# 所以只断言"有等级信息"，并把实际值打印出来供人判断。
# 这与 `test_no_problem_means_no_banner` 那次是同一类教训：
# **不要把环境相关的状态写成固定断言。**
if ($selfText -match "level=(\w+)") {
    $level = $Matches[1]
    Check "自检：报告了能力等级" $true "level=$level"
    if ($level -ne "full") {
        Write-Host "  （提示：当前 level=$level，通常是可用内存不足导致的降级，不是缺陷）" -ForegroundColor DarkYellow
    }
} else {
    Check "自检：报告了能力等级" $false "没找到 level="
}
if ($selfText -match "全部通过") { Write-Host "  （自检输出：全部通过）" -ForegroundColor DarkGray }

# ---------------------------------------------------------------- 卸载保数据

Write-Host "[4/5] 静默卸载不删用户数据" -ForegroundColor Cyan

# 造一个可辨识的 marker。用真实 state.db 更好，但那要先扫描一次目录，
# 在验收脚本里太慢；这里只验"文件是否被删"，内容用 marker 区分。
$marker = "VERIFY-" + [guid]::NewGuid().ToString("N").Substring(0, 12)
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$markerFile = Join-Path $dataDir "state.db"
Set-Content -Path $markerFile -Value $marker -Encoding utf8

$ulog = Join-Path $env:TEMP "localkb-verify-uninstall.log"
$unins = Join-Path $appDir "unins000.exe"
$u = Start-Process -FilePath $unins -PassThru -Wait -ArgumentList @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=$ulog")
Check "卸载进程退出码 0" ($u.ExitCode -eq 0) "exit=$($u.ExitCode)"
Check "安装目录已删除" (-not (Test-Path $appDir))
Check "卸载注册项已清理" (-not (Test-Path $uninstKey))

# ## 这三条是最关键的
#
# 它们分别对应三种"看起来成功、实际出错"：
#   1. 文件被删（/SUPPRESSMSGBOXES 默认 Yes）
#   2. 内容被改（误删后重建了空库）
#   3. 代码抛异常（WizardSilent 在卸载阶段不可用）
Check "用户数据目录仍在" (Test-Path $dataDir)
Check "用户数据内容未变" `
    ((Test-Path $markerFile) -and ((Get-Content $markerFile -Raw).Trim() -eq $marker))
$ulogText = if (Test-Path $ulog) { Get-Content $ulog -Raw } else { "" }
Check "卸载脚本没有抛异常" ($ulogText -notmatch "raised an exception")
Check "卸载日志记录了「保留用户数据」" ($ulogText -match "保留用户数据")

# ---------------------------------------------------------------- 收尾

Write-Host "[5/5] 收尾" -ForegroundColor Cyan
Remove-Item $dataDir -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "  已清理测试用的用户数据"

if ($KeepInstalled) {
    Write-Host "  按 -KeepInstalled 要求重新安装一份供手工查看…" -ForegroundColor DarkGray
    Start-Process -FilePath $SetupPath -Wait -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
}

Write-Host ""
if ($failures.Count -eq 0) {
    Write-Host "全部通过（$checks 项）" -ForegroundColor Green
    exit 0
} else {
    Write-Host "失败 $($failures.Count)/$checks 项：" -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
