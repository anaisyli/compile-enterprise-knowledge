param(
    [switch]$SelfTest
)

# One-time MinerU setup for a computer. The token is read from the clipboard, saved as a
# Windows user environment variable, and never printed, logged, or written to a project.

$ErrorActionPreference = "Stop"
$TokenName = "MINERU_API_TOKEN"
$ExpiryName = "MINERU_API_EXPIRES_ON"
$Client = Join-Path $PSScriptRoot "mineru_client.py"

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

Write-Host "MinerU 首次设置" -ForegroundColor Cyan
Write-Host "Token 只保存在本机 Windows 用户环境变量里，不会写进 Skill、客户项目或日志。"
Write-Host ""

if (-not (Test-Path -LiteralPath $Client -PathType Leaf)) {
    throw "找不到 mineru_client.py。请从完整的 Skill 目录运行本脚本。"
}
$python = Find-Python
if (-not $python) {
    throw "找不到 Python。请先安装 Python 3.10 以上版本，或先启动一次 Codex / Claude Code，再重新运行。"
}

if ($SelfTest) {
    $saved = @($env:MINERU_API_TOKEN, $env:MINERU_API_EXPIRES_ON)
    try {
        $env:MINERU_API_TOKEN = "setup-self-test-token-000000"
        $env:MINERU_API_EXPIRES_ON = "2099-12-31"
        if ((Invoke-Client $python @("probe")) -ne 0) { throw "自检失败：probe 没有通过。" }
    }
    finally {
        $env:MINERU_API_TOKEN = $saved[0]
        $env:MINERU_API_EXPIRES_ON = $saved[1]
    }
    Write-Host "脚本结构自检通过。Python: $python" -ForegroundColor Green
    exit 0
}

Write-Host "第 1 步：打开 https://mineru.net/apiManage/token ，登录后创建 Token，并记下到期日。"
Write-Host "第 2 步：复制整行 Token（只复制 Token 本身，不要带 Bearer、引号或空格）。"
[void](Read-Host "复制好后回到这里按 Enter（不要把 Token 粘贴进这个窗口）")
$clipboard = Get-Clipboard -Raw -ErrorAction Stop
try { Set-Clipboard -Value " " -ErrorAction Stop } catch {
    Write-Host "提醒：剪贴板没能清空，设置完成后请随便复制一段普通文字覆盖它。" -ForegroundColor Yellow
}
if ($null -eq $clipboard) { throw "剪贴板里没有文字。复制 Token 后重新运行。" }
$token = $clipboard.TrimEnd([char[]]"`r`n")
$clipboard = $null
if ([string]::IsNullOrWhiteSpace($token)) { throw "剪贴板是空的。复制 Token 后重新运行。" }
if ($token.Contains("`r") -or $token.Contains("`n")) { throw "剪贴板里有多行文字。只复制 Token 那一行。" }
if ($token -ne $token.Trim()) { throw "Token 前后有空格。只复制 Token 本身。" }
if ($token.StartsWith("Bearer ", [StringComparison]::OrdinalIgnoreCase)) { throw "不要带 'Bearer ' 前缀，只复制 Token 本身。" }
if (($token.StartsWith('"') -and $token.EndsWith('"')) -or ($token.StartsWith("'") -and $token.EndsWith("'"))) { throw "不要带引号，只复制 Token 本身。" }
if ($token.Length -lt 20) { throw "只收到 $($token.Length) 个字符，这不是完整的 MinerU Token。" }
Write-Host "已收到 Token：$($token.Length) 个字符（内容不显示）。" -ForegroundColor Green

do {
    $expiry = Read-Host "第 3 步：输入 Token 到期日（格式 YYYY-MM-DD，例如 2026-12-31）"
    $parsed = [datetime]::MinValue
    $valid = [datetime]::TryParseExact($expiry, "yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::None, [ref]$parsed)
    if (-not $valid) { Write-Host "日期格式不对，例如 2026-12-31。" -ForegroundColor Yellow }
} until ($valid)

[Environment]::SetEnvironmentVariable($TokenName, $token, "User")
[Environment]::SetEnvironmentVariable($ExpiryName, $expiry, "User")
$env:MINERU_API_TOKEN = $token
$env:MINERU_API_EXPIRES_ON = $expiry
$token = $null

Write-Host ""
Write-Host "检查 1/2：本机配置..."
if ((Invoke-Client $python @("probe")) -ne 0) { throw "本机检查没通过。Token 已保存，但还不能用。" }

Write-Host ""
Write-Host "检查 2/2：上传一个不含客户信息的测试 PDF，验证 Token、上传、解析、下载..."
$health = Invoke-Client $python @("health-check", "--force")
if ($health -eq 3) {
    Write-Host ""
    Write-Host "本机设置已完成，但在线验证暂时没通过（网络或服务临时问题）。" -ForegroundColor Yellow
    Write-Host "请完全退出并重新打开 AI 工具，之后需要时会自动重试。不要因此重新输入 Token。"
    exit 0
}
if ($health -ne 0) { throw "在线检查失败。看上面的 failure_label 和 next_action 判断是 Token、网络还是额度问题。" }

Write-Host ""
Write-Host "设置完成。请完全退出并重新打开 Codex / Claude Code，新的环境变量才会生效。" -ForegroundColor Green
Write-Host "之后同事只要把 PDF 交给 AI 并说“用 MinerU 解析”，不需要再接触 Token 或命令。"
