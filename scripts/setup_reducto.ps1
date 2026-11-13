param(
    [switch]$SelfTest
)

# One-time Reducto setup for a computer. The API key is read from the clipboard, saved as a
# Windows user environment variable, and never printed, logged, or written to a project.

$ErrorActionPreference = "Stop"
$KeyName = "REDUCTO_API_KEY"
$Client = Join-Path $PSScriptRoot "reducto_client.py"

function Find-Python {
    $candidates = [System.Collections.Generic.List[string]]::new()
    foreach ($name in @("python.exe", "python3.exe", "py.exe")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and $command.Source -notlike "*WindowsApps*") { $candidates.Add($command.Source) }
    }
    $runtimeRoot = Join-Path $env:USERPROFILE ".cache\codex-runtimes"
    if (Test-Path -LiteralPath $runtimeRoot) {
        Get-ChildItem -LiteralPath $runtimeRoot -Recurse -Filter python.exe -File -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like "*\dependencies\python\python.exe" } |
            ForEach-Object { $candidates.Add($_.FullName) }
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $candidate }
    }
    return $null
}

function Invoke-Client([string]$Python, [string[]]$Arguments) {
    # Show the JSON on screen; only the exit code is returned, or it would mix into the result.
    & $Python $Client @Arguments | Out-Host
    return [int]$LASTEXITCODE
}

Write-Host "Reducto 首次设置" -ForegroundColor Cyan
Write-Host "API Key 只保存在本机 Windows 用户环境变量里，不会写进 Skill、客户项目或日志。"
Write-Host ""

if (-not (Test-Path -LiteralPath $Client -PathType Leaf)) {
    throw "找不到 reducto_client.py。请从完整的 Skill 目录运行本脚本。"
}
$python = Find-Python
if (-not $python) {
    throw "找不到 Python。请先安装 Python 3.10 以上版本，或先启动一次 Codex / Claude Code，再重新运行。"
}

if ($SelfTest) {
    $saved = $env:REDUCTO_API_KEY
    try {
        $env:REDUCTO_API_KEY = "setup-self-test-key-000000000000"
        if ((Invoke-Client $python @("probe")) -ne 0) { throw "自检失败：probe 没有通过。" }
    }
    finally { $env:REDUCTO_API_KEY = $saved }
    Write-Host "脚本结构自检通过。Python: $python" -ForegroundColor Green
    exit 0
}

Write-Host "第 1 步：打开 https://studio.reducto.ai ，用你自己的账号登录（新账号送 15,000 credits），左侧 API Keys → Create new API key。"
Write-Host "第 2 步：复制整串 Key（只复制 Key 本身，不要带 Bearer、引号、空格或 REDUCTO_API_KEY=）。"
[void](Read-Host "复制好后回到这里按 Enter（不要把 Key 粘贴进这个窗口）")
$clipboard = Get-Clipboard -Raw -ErrorAction Stop
try { Set-Clipboard -Value " " -ErrorAction Stop } catch {
    Write-Host "提醒：剪贴板没能清空，设置完成后请随便复制一段普通文字覆盖它。" -ForegroundColor Yellow
}
if ($null -eq $clipboard) { throw "剪贴板里没有文字。复制 Key 后重新运行。" }
$key = $clipboard.TrimEnd([char[]]"`r`n")
$clipboard = $null
if ([string]::IsNullOrWhiteSpace($key)) { throw "剪贴板是空的。复制 Key 后重新运行。" }
if ($key.Contains("`r") -or $key.Contains("`n")) { throw "剪贴板里有多行文字。只复制 Key 那一串。" }
if ($key -ne $key.Trim()) { throw "Key 前后有空格。只复制 Key 本身。" }
if ($key.StartsWith("Bearer ", [StringComparison]::OrdinalIgnoreCase)) { throw "不要带 'Bearer ' 前缀，只复制 Key 本身。" }
if ($key.StartsWith("REDUCTO_API_KEY", [StringComparison]::OrdinalIgnoreCase)) { throw "不要带 'REDUCTO_API_KEY=' 前缀，只复制 Key 本身。" }
if (($key.StartsWith('"') -and $key.EndsWith('"')) -or ($key.StartsWith("'") -and $key.EndsWith("'"))) { throw "不要带引号，只复制 Key 本身。" }
if ($key.Length -lt 20) { throw "只收到 $($key.Length) 个字符，这不是完整的 Reducto API Key。" }
Write-Host "已收到 Key：$($key.Length) 个字符（内容不显示）。" -ForegroundColor Green

[Environment]::SetEnvironmentVariable($KeyName, $key, "User")
$env:REDUCTO_API_KEY = $key
$key = $null

Write-Host ""
Write-Host "检查 1/2：本机配置..."
if ((Invoke-Client $python @("probe")) -ne 0) { throw "本机检查没通过。Key 已保存，但还不能用。" }

Write-Host ""
Write-Host "检查 2/2：联网验证 Key（只查任务列表，不上传文件、不消耗额度）..."
$health = Invoke-Client $python @("health-check")
if ($health -eq 3) {
    Write-Host ""
    Write-Host "本机设置已完成，但在线验证暂时没通过（网络或服务临时问题）。" -ForegroundColor Yellow
    Write-Host "请完全退出并重新打开 AI 工具，之后需要时会自动重试。不要因此重新输入 Key。"
    exit 0
}
if ($health -ne 0) { throw "在线检查失败。看上面的 failure_label 和 next_action 判断是 Key、网络还是额度问题。" }

Write-Host ""
Write-Host "设置完成。请完全退出并重新打开 Codex / Claude Code，新的环境变量才会生效。" -ForegroundColor Green
Write-Host "之后 AI 会先列出要交给 Reducto 的难页，等你确认后才上传。"
