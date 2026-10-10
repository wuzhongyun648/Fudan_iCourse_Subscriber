param(
    [switch]$Download,
    [switch]$Login,
    [string]$CookieFile,
    [string]$CourseId = '39173',
    [string]$Output = 'D:\desktop\26秋\课堂录制',
    [string]$FFprobe,
    [string]$FFmpeg,
    [string]$Proxy,
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$venvRoot = Join-Path $PSScriptRoot '.venv-download'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host '正在准备仅供下载使用的环境……'
    & $Python -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw '无法创建 Python 环境。请确认已安装 Python 3.10 或更高版本。' }
}
$dependenciesReady = Join-Path $venvRoot 'download-dependencies.ready'
if (-not (Test-Path -LiteralPath $dependenciesReady)) {
    & $venvPython -m pip install -r (Join-Path $PSScriptRoot 'requirements-download.txt') --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw '下载依赖安装失败，重跑即可重试。' }
    Set-Content -LiteralPath $dependenciesReady -Value 'ready'
}
$arguments = @((Join-Path $PSScriptRoot 'download_videos.py'), '--course-id', $CourseId, '--output', $Output)
if ($Download) { $arguments += '--download' }
if ($Login) { $arguments += '--login' }
if ($CookieFile) { $arguments += @('--cookie-file', $CookieFile) }
if ($FFprobe) { $arguments += @('--ffprobe', $FFprobe) }
if ($FFmpeg) { $arguments += @('--ffmpeg', $FFmpeg) }
if ($Proxy) { $arguments += @('--proxy', $Proxy) }
& $venvPython @arguments
exit $LASTEXITCODE
