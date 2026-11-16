# 启动 LocalKB 开发版（不弹控制台）
#
# ## 为什么用 pythonw 而不是 python
#
# python 是控制台子系统，双击 .bat 会留一个黑框。pythonw 没有控制台，
# 更接近最终 exe 的形态。代价是 `print` 输出看不到——调试时改用
# `python -m localkb.app` 直接跑。
#
# ## 为什么先检查 venv
#
# 缺依赖时的默认报错是 "ModuleNotFoundError: No module named 'PySide6'"
# 弹在一个即将关闭的窗口里，用户什么都看不到。这里提前给出可读提示。

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
$pyCheck = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "✗ 没有找到虚拟环境：$py" -ForegroundColor Red
    Write-Host ""
    Write-Host "请先建环境（见 localkb\README.md）：" -ForegroundColor Yellow
    Write-Host '  & "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv'
    Write-Host "  & .venv\Scripts\python.exe -m pip install PySide6"
    Write-Host "  & .venv\Scripts\python.exe -m pip install <sdk>\dist\luping_localai-0.3.0-py3-none-any.whl"
    exit 1
}

# 快速自检：缺依赖时给出可读提示而不是静默退出
& $pyCheck -c "import PySide6, localai" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "✗ 依赖不完整（PySide6 或 localai 缺失）" -ForegroundColor Red
    Write-Host "  先跑：& .venv\Scripts\python.exe -m pip install PySide6 luping-localai" -ForegroundColor Yellow
    exit 1
}

Start-Process -FilePath $py -ArgumentList "-m", "localkb.app" -WorkingDirectory $PSScriptRoot
Write-Host "已启动 LocalKB。" -ForegroundColor Green
Write-Host "若窗口没有出现，用这条看报错：" -ForegroundColor DarkGray
Write-Host "  & .venv\Scripts\python.exe -m localkb.app" -ForegroundColor DarkGray
