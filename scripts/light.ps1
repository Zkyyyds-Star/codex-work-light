# 保持普通脚本参数转发，避免 PowerShell 将 --turn-id 等参数当作自身参数解析。
$ErrorActionPreference = 'Stop'
$settingsPath = Join-Path $PSScriptRoot '..\runtime.json'
if (-not (Test-Path -LiteralPath $settingsPath -PathType Leaf)) {
    throw 'aaa无敌尚未安装；请先运行项目根目录的 install.ps1。'
}
$settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding utf8 | ConvertFrom-Json
if (-not $settings.python -or -not (Test-Path -LiteralPath $settings.python -PathType Leaf)) {
    throw '记录的 Python 环境不可用；请重新运行 install.ps1 修复安装。'
}
& $settings.python -X utf8 -B (Join-Path $PSScriptRoot 'status_light.py') @args
if ($LASTEXITCODE -ne 0) { throw "aaa无敌控制命令未被接受（退出码 $LASTEXITCODE）；请检查上方诊断。" }
