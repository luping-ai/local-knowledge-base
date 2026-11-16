# LocalKB 打包脚本：生成可分发的安装包
#
# ## 用法
#
#   powershell -ExecutionPolicy Bypass -File tools/package-localkb.ps1
#   powershell -ExecutionPolicy Bypass -File tools/package-localkb.ps1 -Version 0.1.0
#
# ## 产物
#
#   dist-localkb/LocalKB/                      PyInstaller onedir 输出（调试用）
#   output/LocalKB-Setup-<ver>.exe             安装包（单文件）
#   output/LocalKB-<ver>-manifest.json         版本清单（版本锁定用）
#
# ## 为什么安装包不含 Runtime
#
# 方案 §3.1：桌面端**复用已装的 LocalAIHub**，不装第二份 Runtime。
# 所以包里只有：LocalKB.exe + Python 运行时 + 解析库 + 一个
# 引导用的 LocalAIHub 检测逻辑。
#
# 未装 LocalAIHub 时，安装器会提示"请先安装 Local AI Hub"并给下载入口，
# 而不是自己装一份（那会与已装实例争单实例锁，见方案 §3.1）。
#
# ## 为什么用 PowerShell 而不是 Python 脚本
#
# 与仓库既有的 package.ps1 / package-all.ps1 保持一致——同一个人维护
# 同类脚本时，语言统一比"选更顺手的"更重要。

[CmdletBinding()]
param(
    [string]$Version = "",
    [switch]$SkipBuild,       # 跳过 PyInstaller（复用已有 dist-localkb/LocalKB）
    [switch]$NoTrim,          # 不裁剪（排查"删了某个文件导致功能异常"时用）
    [switch]$NoInstaller,     # 只出 zip，不编译 Inno Setup 安装包
    [string]$SignSubject = "" # 代码签名证书主题（如 "CN=Your Company"）
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# ---------------------------------------------------------------- 版本号
#
# ## 为什么不再读仓库根的 VERSION（迁移后改的）
#
# 这个脚本原本从 `$root\VERSION` 读版本号。那个文件是 **Runtime** 的版本
# （0.2.7），因为当时两个产品在同一个仓库里。
#
# 2026-11-17 桌面端迁到独立仓库 `luping-ai/local-knowledge-base` 之后，
# 仓库里不再有 Runtime 的版本信息。若保留原来的读法，`VERSION` 不存在
# 时会回落到 "0.1.0" —— 于是清单里写着 `runtimeVersion: 0.1.0`，
# 而实际用的 Runtime 是 0.2.7。**一个错误的版本号比没有更糟**：出问题时
# 会照着它去排查错误的版本。
#
# 改成从注册表探测**本机已装的** Runtime 版本。这是唯一真实可得的来源：
#   · 注册表 DisplayVersion → 安装器写入，准确
#   · runtime.json 的 version → 是协议版本（"0.1.0-dev"），不是产品版本
#
# 探测不到就写 "unknown" 并在打包时告警——不猜。

if ($Version -eq "") {
    $Version = "unknown"
    $uninstKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\LocalAIHub"
    $detected = (Get-ItemProperty $uninstKey -ErrorAction SilentlyContinue).DisplayVersion
    if ($detected) {
        $Version = $detected
    } else {
        Write-Warning "未检测到本机安装的 Local AI Hub，清单里的 runtimeVersion 记为 unknown"
    }
}
$appVersion = "0.1.0"   # 桌面端自身版本（与 Runtime 版本独立演进）
Write-Host "LocalKB $appVersion（本机 Runtime $Version）" -ForegroundColor Cyan
Write-Host ""

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    throw "找不到虚拟环境：$py`n请先按 localkb\README.md 建环境"
}

# ---------------------------------------------------------------- 1. 构建

$distDir = Join-Path $root "dist-localkb\LocalKB"
if ($SkipBuild) {
    if (-not (Test-Path $distDir)) {
        throw "-SkipBuild 但 $distDir 不存在"
    }
    Write-Host "[1/5] 跳过 PyInstaller（-SkipBuild）" -ForegroundColor DarkYellow
} else {
    Write-Host "[1/5] PyInstaller 打包..." -ForegroundColor Cyan
    # --clean：清掉 build-localkb 缓存。不 clean 时改了 spec 可能不生效，
    # 表现是"改了配置但产物没变"——实测踩过一次。
    & $py -m PyInstaller --noconfirm --clean `
        --distpath (Join-Path $root "dist-localkb") `
        --workpath (Join-Path $root "build-localkb") `
        (Join-Path $root "tools\localkb.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（exit=$LASTEXITCODE）" }
}

# ---------------------------------------------------------------- 2. 裁剪

# ## 为什么要裁剪（实测体积 225 MB → 157 MB）
#
# 两个大件都是"随依赖二进制落盘的数据文件"，不是 Python 模块，所以
# excludes 删不掉：
#
#   opengl32sw.dll (19.7 MB)  Qt 的软件 OpenGL 兜底。只用 QWidget
#                             （不用 QOpenGLWidget / QQuick）时不需要。
#   *.onnx        (48 MB)     PyMuPDF 的版面分析/表格识别模型。我们只用
#                             `get_text("dict")` 取文本与坐标，不做版面
#                             识别（方案 §16 P7 明确 v0.1 不做表格结构化）。
#
# 风险：删错会让某个功能"悄无声息地不能用"。所以：
#   - 每条都写明判据（见上面）
#   - -NoTrim 可以跳过（排查"删了某个文件导致异常"时用）
#   - 删完后下面会跑一次冒烟测试（启动 exe 并等窗口出现）

if ($NoTrim) {
    Write-Host "[2/5] 跳过裁剪（-NoTrim）" -ForegroundColor DarkYellow
} else {
    Write-Host "[2/5] 裁剪无用文件..." -ForegroundColor Cyan
    $internal = Join-Path $distDir "_internal"
    $saved = 0

    # ## 为什么用递归查找而不是写死相对路径（实测踩到）
    #
    # 第一版按 `_internal/opengl32sw.dll` 与 `_internal/*.onnx` 找，结果
    # 一个都没删到、只打印"共节省 0.0 MB"。而**"删了 0 个"与"删成功"
    # 在日志里长得一样**，很容易误以为没事。
    #
    # 实际布局是：
    #   _internal/PySide6/opengl32sw.dll
    #   _internal/pymupdf/layout/resources/onnx/*.onnx
    #
    # 依赖升级可能再改布局，所以按文件名递归找更稳。

    $opengl = Get-ChildItem -Path $internal -Filter "opengl32sw.dll" `
        -Recurse -File -ErrorAction SilentlyContinue
    foreach ($f in $opengl) {
        $saved += $f.Length
        Remove-Item $f.FullName -Force
        Write-Host "  删除 $($f.Name)（只用 QWidget，不需要软件 OpenGL）" -ForegroundColor DarkGray
    }

    $onnx = Get-ChildItem -Path $internal -Filter "*.onnx" `
        -Recurse -File -ErrorAction SilentlyContinue
    if ($onnx) {
        $n = ($onnx | Measure-Object).Count
        foreach ($f in $onnx) { $saved += $f.Length }
        $onnx | Remove-Item -Force
        Write-Host "  删除 $n 个 .onnx 模型（不做版面/表格识别）" -ForegroundColor DarkGray
    }

    if ($saved -eq 0) {
        # 明确报"没删到"，而不是打印 0.0 MB 让人以为正常。
        Write-Warning "裁剪没找到任何可删文件——依赖布局可能变了，请检查 $internal"
    } else {
        Write-Host ("  共节省 {0:N1} MB" -f ($saved / 1MB)) -ForegroundColor Green
    }
}

# ---------------------------------------------------------------- 2b. 代码签名
#
# ## 为什么签名在"冒烟"和"清单"之前
#
# 签名会**改变 exe 的哈希**。第一版把签名放在最后，于是清单里的
# entrySha256 与实际文件对不上，而 Setup.exe 里打包的还是未签名的 exe
# （编译时就已经读走了）——两处都错，且都不会报错。
#
# 所以顺序必须是：裁剪 → 签名 exe → 冒烟（验签名没弄坏它）→ 清单（记录
# 签名后的哈希）→ 编译 Setup.exe（自然带的是已签名 exe）→ 签 Setup.exe。

$exe = Join-Path $distDir "LocalKB.exe"
if ($SignSubject -ne "") {
    Write-Host "[2b] 代码签名（$SignSubject）..." -ForegroundColor Cyan
    $cert = Get-ChildItem Cert:\CurrentUser\My |
        Where-Object { $_.Subject -like "*$SignSubject*" } |
        Select-Object -First 1
    if (-not $cert) {
        throw "找不到证书 '$SignSubject'。`n" +
              "  创建自签证书：`n" +
              "    New-SelfSignedCertificate -Type CodeSigningCert ``n" +
              "      -Subject '$SignSubject' -KeyUsage DigitalSignature ``n" +
              "      -CertStoreLocation Cert:\CurrentUser\My"
    }
    # ## 为什么用时间戳服务
    #
    # 不盖时间戳的签名在证书过期后失效——用户第二年打开会被 SmartScreen
    # 拦。时间戳把"签名有效"绑到签名发生的那一刻。
    $sig = Set-AuthenticodeSignature -FilePath $exe -Certificate $cert `
        -TimestampServer "http://timestamp.digicert.com"
    if ($sig.Status -ne "Valid") {
        Write-Warning "签名状态：$($sig.Status)（$($sig.StatusMessage)）"
    } else {
        Write-Host "  已签名" -ForegroundColor Green
    }
} else {
    Write-Host "[2b] 跳过签名（未指定 -SignSubject）" -ForegroundColor DarkYellow
    Write-Host "  未签名时 SmartScreen 会拦首次运行（用户需点「更多信息 → 仍要运行」）" -ForegroundColor DarkGray
}

# ------------------------------------------------------- 2c. 合规文件

# ## 为什么打包流程里必须有这一步
#
# Apache-2.0 第 4 条：分发时必须附带 LICENSE 与 NOTICE。
# LGPL（PySide6）：必须附带其许可副本或获取途径。
#
# 这不是可选项。实测发现第一版的安装包里只有 install-manifest.json ——
# 一个合规文件都没有，而打包"成功"、自检"全过"。这类缺失不会报错，
# 只在被追问时才暴露。
#
# 三步：
#   1. 生成 THIRD-PARTY-NOTICES.txt（从已安装包的元数据读，不手写）
#   2. 把它连同 LICENSE / NOTICE 复制进 dist 目录
#   3. 复制后**校验存在**——不校验的话，下一步的 ISCC 用了
#      skipifsourcedoesntexist，缺文件也照样编译成功

Write-Host "[2c] 生成合规文件..." -ForegroundColor Cyan

& $py (Join-Path $root "tools\gen-third-party-notices.py")
if ($LASTEXITCODE -ne 0) { throw "生成第三方许可清单失败" }

$compliance = @("LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.txt")
foreach ($name in $compliance) {
    $srcFile = Join-Path $root $name
    if (-not (Test-Path $srcFile)) { throw "缺少合规文件：$name" }
    Copy-Item $srcFile (Join-Path $distDir $name) -Force
}
foreach ($name in $compliance) {
    if (-not (Test-Path (Join-Path $distDir $name))) {
        throw "合规文件没有进 dist 目录：$name"
    }
}
Write-Host "  LICENSE / NOTICE / THIRD-PARTY-NOTICES.txt 已就位" -ForegroundColor Green

# ---------------------------------------------------------------- 3. 冒烟

Write-Host "[3/5] 冒烟测试（启动 → 等窗口 → 关闭）..." -ForegroundColor Cyan
if (-not (Test-Path $exe)) { throw "找不到 $exe" }

$proc = Start-Process -FilePath $exe -PassThru
try {
    $deadline = (Get-Date).AddSeconds(20)
    $up = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 400
        $proc.Refresh()
        if ($proc.HasExited) {
            throw "LocalKB.exe 启动后立刻退出（exit=$($proc.ExitCode)）。`n" +
                  "常见原因：缺 DLL（在干净机器上首次运行时）、PyInstaller 漏了模块。"
        }
        if ($proc.MainWindowHandle -ne 0) { $up = $true; break }
    }
    if (-not $up) {
        throw "20 秒内没出现主窗口。可能是启动极慢（缺依赖反复重试）或初始化卡住。"
    }
    Write-Host "  窗口已出现（PID $($proc.Id)）" -ForegroundColor Green
} finally {
    if (-not $proc.HasExited) {
        $proc.Kill()
        $proc.WaitForExit(5000) | Out-Null
    }
}

# ---------------------------------------------------------------- 4. 清单

Write-Host "[4/5] 生成版本清单..." -ForegroundColor Cyan

function Get-Sha256([string]$path) {
    (Get-FileHash -Path $path -Algorithm SHA256).Hash.ToLower()
}

$exeHash = Get-Sha256 $exe
$sizeMB = [math]::Round(
    (Get-ChildItem $distDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)

$manifest = [ordered]@{
    product       = "LocalKB"
    version       = $appVersion
    builtAt       = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
    runtimeVersion = $Version
    # ## 为什么锁 SDK 范围而不是精确版本
    #
    # SDK 是独立仓库、独立发版。锁死精确版本会让"SDK 打了个补丁"变成
    # "必须重发整个安装包"。只锁大版本范围。
    #
    # 上限 0.4.0 与 `localkb/preflight.py` 的 MAX_SDK_VERSION 必须一致
    # ——实测踩到过不一致的后果：清单写 `<0.3.0`（那时上限还是 0.3），
    # 而代码已改成 `<0.4.0`，于是"清单说支持、代码也放行"，但两个文件
    # 互相矛盾，下一个人不知道以哪个为准。
    sdk           = @{ requires = ">=0.3.0,<0.4.0" }
    # 契约版本：知识库存储 schema 的下限。
    # 2 = 含溯源四列与 documents/chunks（Phase 1）；4 = 含相似检测与表格指纹。
    # 桌面端启动时读 /capability 的 schema_version 比对。
    runtimeContract = @{ min = 2 }
    entry         = "LocalKB.exe"
    entrySha256   = $exeHash
    installSizeMB = $sizeMB
}

$manifestPath = Join-Path $root "output\LocalKB-$appVersion-manifest.json"
New-Item -ItemType Directory -Force -Path (Split-Path $manifestPath) | Out-Null
$manifest | ConvertTo-Json -Depth 5 | Set-Content -Path $manifestPath -Encoding utf8
Write-Host "  $manifestPath" -ForegroundColor DarkGray

# 清单也放进安装目录：桌面端启动时自己校验
Copy-Item $manifestPath (Join-Path $distDir "install-manifest.json") -Force

# ---------------------------------------------------------------- 5. 打包

Write-Host "[5/5] 生成安装包..." -ForegroundColor Cyan

# ## 为什么用 zip 而不是 mksfx 自解压
#
# 仓库既有 tools/mksfx 是为 Runtime 做的（Go 写的 stub + 内嵌 payload）。
# 对桌面端，同样的做法需要再写一个 Go stub —— 而为"解压一个文件夹"
# 多维护一个二进制不值得。
#
# zip 保留为"绿色版"：内测时给同事临时试用、或 CI 里做冒烟都更方便
# （解压即用，不用过安装向导）。正式分发用的是下面的 Setup.exe。

$zipName = "LocalKB-$appVersion-win64.zip"
$zipPath = Join-Path $root "output\$zipName"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

Compress-Archive -Path (Join-Path $distDir "*") -DestinationPath $zipPath -CompressionLevel Optimal
$zipMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
$zipHash = Get-Sha256 $zipPath

# ---------------------------------------------------------------- 5b. 安装向导

# ## 为什么安装包不含 Runtime（重申，因为这是最容易做错的一步）
#
# 方案 §3.1 / A15：桌面端**复用已装的 LocalAIHub**。同一台机器只能有一份
# Runtime（单实例锁 + 端口 + token + 数据目录）。所以这里调 ISCC 时**不传
# 任何 Runtime 的 payload**，安装向导只做：
#   1. 复制 LocalKB.exe + _internal
#   2. 读注册表检测 LocalAIHub，把结果写进欢迎页之后的说明页
#   3. 注册卸载项与开始菜单
#
# A15 的判据是"装完后 Run 键仍只有一条 LocalAIHub、launch-spec.json 未变、
# 桌面端连的是同一个 PID"。这三条都不是靠"我写了不装 Runtime 的代码"能
# 保证的，只有装完对比基线才算数。

$setupPath = ""
if ($NoInstaller) {
    Write-Host "[5b] 跳过安装向导（-NoInstaller）" -ForegroundColor DarkYellow
} else {
    # ## 为什么在脚本里找 ISCC 而不是写死路径
    #
    # Inno Setup 默认装在 {localappdata}\Programs\Inno Setup 6（每用户），
    # 但也可能装在 Program Files（x86）（全员）。两种都要能找到。
    $isccCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )
    $iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) {
        throw "找不到 ISCC.exe（Inno Setup 6 未安装）。`n" +
              "  安装：winget install --id JRSoftware.InnoSetup`n" +
              "  或加 -NoInstaller 只出 zip"
    }
    Write-Host "[5b] 编译安装向导（Inno Setup）..." -ForegroundColor Cyan

    $issPath = Join-Path $root "tools\localkb-installer.iss"
    # ## 为什么 .iss 与 .isl 必须是带 BOM 的 UTF-8
    #
    # Inno Setup 只在有 BOM 时才按 UTF-8 解析，否则按系统 ANSI 读——中文
    # 界面会变成乱码，而且**编译不报错**，只在装的时候才看出来。
    # 这里每次都检查，不依赖"上次已经加过了"。
    foreach ($f in @($issPath, (Join-Path $root "tools\ChineseSimplified.isl"))) {
        if (-not (Test-Path $f)) { throw "缺文件：$f" }
        $bytes = [System.IO.File]::ReadAllBytes($f)
        if ($bytes.Length -lt 3 -or $bytes[0] -ne 0xEF -or $bytes[1] -ne 0xBB -or $bytes[2] -ne 0xBF) {
            $utf8Bom = [byte[]](0xEF, 0xBB, 0xBF)
            [System.IO.File]::WriteAllBytes($f, $utf8Bom + $bytes)
            Write-Host "  已为 $(Split-Path -Leaf $f) 补 UTF-8 BOM" -ForegroundColor DarkYellow
        }
    }

    $isccArgs = @(
        "/Qp"                                  # 安静模式，但仍输出错误
        "/DAppVersion=$appVersion"
        "/DRuntimeVersion=$Version"
        "/DSourceDir=$(Join-Path $root 'dist-localkb\LocalKB')"
        "/DOutputDir=$(Join-Path $root 'output')"
    )
    if ($SignSubject -ne "") {
        # ISCC 的 SignTool 是 [Setup] 里的具名工具（在 Inno 的 IDE 里配置），
        # 命令行无法直接传证书主题。带签名时改用下面的 Set-AuthenticodeSignature
        # 对 Setup.exe 补签，效果等价。
        Write-Host "  （Setup.exe 将在编译后单独签名）" -ForegroundColor DarkGray
    }
    & $iscc @isccArgs $issPath
    if ($LASTEXITCODE -ne 0) { throw "ISCC 失败（exit=$LASTEXITCODE）" }

    $setupPath = Join-Path $root "output\LocalKB-$appVersion-Setup.exe"
    if (-not (Test-Path $setupPath)) {
        throw "ISCC 报成功但没有产物：$setupPath"
    }
    Write-Host ("  {0}  {1:N1} MB" -f (Split-Path -Leaf $setupPath),
        ((Get-Item $setupPath).Length / 1MB)) -ForegroundColor Green
}

# ---------------------------------------------------------------- 5c. 签 Setup.exe

# ## 为什么 Setup.exe 也要签
#
# 用户双击的是 Setup.exe。未签名的 Setup.exe 会被 SmartScreen 直接拦下，
# 里面那个已签名的 LocalKB.exe 根本没机会被看到——拦截发生在"运行安装程序"
# 这一步，不是"运行程序"那一步。

$setupHash = ""
if ($setupPath -ne "") {
    $setupHash = Get-Sha256 $setupPath
    if ($SignSubject -ne "") {
        $cert = Get-ChildItem Cert:\CurrentUser\My |
            Where-Object { $_.Subject -like "*$SignSubject*" } |
            Select-Object -First 1
        $sig = Set-AuthenticodeSignature -FilePath $setupPath -Certificate $cert `
            -TimestampServer "http://timestamp.digicert.com"
        if ($sig.Status -ne "Valid") {
            Write-Warning "Setup.exe 签名状态：$($sig.Status)"
        } else {
            # 签名会改哈希，签完必须重算——否则下面打印的 sha256 是错的
            $setupHash = Get-Sha256 $setupPath
            Write-Host "  Setup.exe 已签名" -ForegroundColor Green
        }
    }
}

# ---------------------------------------------------------------- 汇总

Write-Host ""
Write-Host "完成" -ForegroundColor Green
Write-Host "  解压目录  dist-localkb\LocalKB\        $sizeMB MB（未压缩）"
if ($setupPath -ne "") {
    Write-Host ("  安装包    output\LocalKB-$appVersion-Setup.exe  " +
        ("{0:N1} MB" -f ((Get-Item $setupPath).Length / 1MB)))
    Write-Host "  setup sha256 $setupHash"
}
Write-Host "  绿色版    output\$zipName             $zipMB MB"
Write-Host "  清单      output\LocalKB-$appVersion-manifest.json"
Write-Host "  exe sha256 $exeHash"
Write-Host "  zip sha256 $zipHash"
Write-Host ""
Write-Host "手动验证：" -ForegroundColor Cyan
Write-Host "  安装版：双击 output\LocalKB-$appVersion-Setup.exe → 完成 → 开始菜单启动"
Write-Host "  绿色版：解压 $zipName，双击 LocalKB.exe"
Write-Host "  两者都要求本机 Local AI Hub 在运行（LocalKB 复用它，不自带 Runtime）"
Write-Host "  启动后状态栏应显示模型名与可用内存（不是「—」）"
