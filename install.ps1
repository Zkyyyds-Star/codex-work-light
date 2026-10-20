[CmdletBinding()]
param(
    [string]$Python,
    [string]$DeviceAddress,
    [string]$CodexHome,
    [string]$DataDir,
    [switch]$SkipDependencies
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

function Read-Settings([string]$Path) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        try {
            $settings = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($null -eq $settings -or $settings -isnot [PSCustomObject]) { throw '配置必须为 JSON 对象。' }
            return $settings
        } catch {
            throw "无法读取现有配置 '$Path'；请保留原文件并修复 JSON 后重试：$($_.Exception.Message)"
        }
    }
    return $null
}
function Setting($Settings, [string]$Name) {
    if ($null -ne $Settings -and $null -ne $Settings.PSObject.Properties[$Name]) {
        return [string]$Settings.$Name
    }
    return $null
}
function Full-Path([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    if ($resolved -eq [IO.Path]::GetPathRoot($resolved)) { return $resolved }
    return $resolved.TrimEnd('\', '/')
}
function Is-Within([string]$Path, [string]$Root) {
    return $Path.Equals($Root, [StringComparison]::OrdinalIgnoreCase) -or
        $Path.StartsWith($Root.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}
function Run-Python([string]$Executable, [string[]]$Arguments) {
    $output = & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python 命令失败，退出码 $LASTEXITCODE。" }
    return $output
}
function Copy-Payload([string]$From, [string]$To) {
    $null = New-Item -ItemType Directory -Path $To -Force
    # 只复制发布文件，避免把备份、运行数据或虚拟环境再复制进自身。
    foreach ($name in @('SKILL.md', 'README.md', 'CHANGES.md', 'LICENSE', 'requirements.txt', 'install.ps1')) {
        $item = Join-Path $From $name
        if (Test-Path -LiteralPath $item -PathType Leaf) {
            Copy-Item -LiteralPath $item -Destination (Join-Path $To $name) -Force
        }
    }
    foreach ($name in @('scripts', 'agents')) {
        $directory = Join-Path $From $name
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) { continue }
        foreach ($file in Get-ChildItem -LiteralPath $directory -File -Recurse) {
            if ($file.FullName -match '[\\/]__pycache__[\\/]|\.(pyc|pyo)$') { continue }
            $relative = $file.FullName.Substring($From.Length).TrimStart('\', '/')
            $destination = Join-Path $To $relative
            $null = New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force
            Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
        }
    }
}

try {
    $source = Full-Path $PSScriptRoot
    foreach ($required in @('SKILL.md', 'requirements.txt', 'scripts\status_light.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $source $required) -PathType Leaf)) {
            throw "发布文件缺失：$required。请从完整仓库重新安装。"
        }
    }
    if (-not $CodexHome) {
        $CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
    }
    $CodexHome = Full-Path $CodexHome
    $target = Full-Path (Join-Path $CodexHome 'skills\aaa-wudi')
    if (-not $source.Equals($target, [StringComparison]::OrdinalIgnoreCase) -and (Is-Within $target $source)) {
        throw '-CodexHome 不能使安装目录位于发布源目录内部，避免文件复制进自身。'
    }
    $existing = Read-Settings (Join-Path $target 'runtime.json')
    $local = Read-Settings (Join-Path $source 'runtime.json')
    if (-not $Python) { $Python = Setting $existing 'python' }
    if (-not $Python) { $Python = Setting $local 'python' }
    if (-not $DeviceAddress) { $DeviceAddress = Setting $existing 'device_address' }
    if (-not $DeviceAddress) { $DeviceAddress = Setting $local 'device_address' }
    if (-not $DeviceAddress) { throw '首次安装必须提供 -DeviceAddress；安装器不会扫描或自动选择蓝牙设备。' }
    if ($DeviceAddress -notmatch '^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$') {
        throw '-DeviceAddress 必须是六组十六进制字节，例如 AA:BB:CC:DD:EE:FF。'
    }
    if (-not $Python) { throw '请通过 -Python 指定 Python 3.12 或更新版本的 python.exe。' }
    $Python = Full-Path $Python
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw '指定的 Python 不存在。' }
    if (-not $DataDir) { $DataDir = Setting $existing 'data_dir' }
    if (-not $DataDir) { $DataDir = Setting $local 'data_dir' }
    if (-not $DataDir) { $DataDir = Join-Path $env:LOCALAPPDATA 'aaa-wudi' }
    $DataDir = Full-Path $DataDir
    if ((Is-Within $DataDir $source) -or (Is-Within $DataDir $target)) {
        throw '-DataDir 必须位于技能目录之外，避免运行数据进入发布文件或递归备份。'
    }
    $info = (Run-Python $Python @('-X', 'utf8', '-B', '-c', 'import json,sys; print(json.dumps(dict(version=list(sys.version_info[:2]),venv=sys.prefix!=sys.base_prefix)))')) | ConvertFrom-Json
    if ($info.version[0] -lt 3 -or ($info.version[0] -eq 3 -and $info.version[1] -lt 12)) {
        throw 'aaa无敌需要 Python 3.12 或更新版本。'
    }
    $null = New-Item -ItemType Directory -Path $DataDir -Force
    if (-not $info.venv) {
        $venv = Join-Path $DataDir 'venv'
        $venvPython = Join-Path $venv 'Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
            $null = Run-Python $Python @('-X', 'utf8', '-B', '-m', 'venv', $venv)
        }
        $Python = $venvPython
        $null = Run-Python $Python @('-X', 'utf8', '-B', '-c', 'import sys; assert sys.version_info >= (3,12) and sys.prefix != sys.base_prefix')
    }
    if (-not $SkipDependencies) {
        $dependencyProbe = 'import bleak, importlib.metadata; assert importlib.metadata.version(''bleak'') == ''3.0.2'''
        $dependencyReady = $false
        try {
            $null = & $Python -X utf8 -B -c $dependencyProbe 2>$null
            $dependencyReady = $LASTEXITCODE -eq 0
        } catch { $dependencyReady = $false }
        if (-not $dependencyReady) {
            $null = Run-Python $Python @('-X', 'utf8', '-B', '-m', 'pip', 'install', '--disable-pip-version-check', '-r', (Join-Path $source 'requirements.txt'))
        }
        $null = Run-Python $Python @('-X', 'utf8', '-B', '-c', $dependencyProbe)
    }

    $backup = $null
    if (Test-Path -LiteralPath $target -PathType Container) {
        $backup = Join-Path $DataDir ('backups\skill-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
        Copy-Payload $target $backup
        $previousRuntime = Join-Path $target 'runtime.json'
        if (Test-Path -LiteralPath $previousRuntime -PathType Leaf) {
            Copy-Item -LiteralPath $previousRuntime -Destination (Join-Path $backup 'runtime.json')
        }
        $hooksFile = Join-Path $CodexHome 'hooks.json'
        if (Test-Path -LiteralPath $hooksFile -PathType Leaf) {
            Copy-Item -LiteralPath $hooksFile -Destination (Join-Path $backup 'hooks.json')
        }
    }
    if (-not $source.Equals($target, [StringComparison]::OrdinalIgnoreCase)) { Copy-Payload $source $target }
    $runtimePath = Join-Path $target 'runtime.json'
    $temporary = $runtimePath + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    $settings = @{ python = $Python; data_dir = $DataDir; device_address = $DeviceAddress.ToUpperInvariant() }
    try {
        [IO.File]::WriteAllText($temporary, (($settings | ConvertTo-Json) + [Environment]::NewLine), [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $runtimePath -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
    # 仅注册定义；Codex 自己负责审核。安装不启动服务，也不连接或点亮设备。
    $previousCodexHome = $env:CODEX_HOME
    try {
        $env:CODEX_HOME = $CodexHome
        $hookResult = (Run-Python $Python @('-X', 'utf8', '-B', (Join-Path $target 'scripts\status_light.py'), 'install-hooks')) | ConvertFrom-Json
    } finally {
        $env:CODEX_HOME = $previousCodexHome
    }
    @{
        installed = $true; skill_dir = $target; runtime_python = $Python; data_dir = $DataDir
        backup_dir = $backup; hooks = $hookResult; dependencies_skipped = [bool]$SkipDependencies
        notice = '安装完成，默认未启用。仅新增或变更的 Hook 定义需要在 Codex 正常审核。'
    } | ConvertTo-Json -Depth 10
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
