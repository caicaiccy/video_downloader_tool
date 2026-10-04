param(
  [Parameter(Mandatory = $true)]
  [string]$HostPath,
  [string]$ChromeExtensionId = "",
  [string]$EdgeExtensionId = ""
)

$ErrorActionPreference = "Stop"
$HostPath = (Resolve-Path $HostPath).Path

function Test-ExtensionId([string]$Id) {
  return $Id -match '^[a-p]{32}$'
}

$Origins = @()
if ($ChromeExtensionId) {
  if (-not (Test-ExtensionId $ChromeExtensionId)) { throw "Chrome 扩展 ID 格式无效。" }
  $Origins += "chrome-extension://$ChromeExtensionId/"
}
if ($EdgeExtensionId) {
  if (-not (Test-ExtensionId $EdgeExtensionId)) { throw "Edge 扩展 ID 格式无效。" }
  $Origins += "chrome-extension://$EdgeExtensionId/"
}
if ($Origins.Count -eq 0) { throw "至少要提供 Chrome 或 Edge 扩展 ID。" }

$InstallDir = Join-Path $env:LOCALAPPDATA "BiliDownloader"
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$InstalledHost = Join-Path $InstallDir "BiliDownloaderHost.exe"
Copy-Item $HostPath $InstalledHost -Force
$HostPath = $InstalledHost
$ManifestPath = Join-Path $InstallDir "com.codex.bili_downloader.json"
$Manifest = @{
  name = "com.codex.bili_downloader"
  description = "Bilibili 本地下载器 Native Messaging Host"
  path = $HostPath
  type = "stdio"
  allowed_origins = $Origins
} | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText(
  $ManifestPath,
  $Manifest,
  (New-Object System.Text.UTF8Encoding($false))
)

if ($ChromeExtensionId) {
  $Key = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.codex.bili_downloader"
  New-Item -Path $Key -Force | Out-Null
  Set-Item -Path $Key -Value $ManifestPath
}
if ($EdgeExtensionId) {
  $Key = "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.codex.bili_downloader"
  New-Item -Path $Key -Force | Out-Null
  Set-Item -Path $Key -Value $ManifestPath
}

Write-Host "Native Host 已注册：$ManifestPath"
Write-Host "请重新加载 Edge/Chrome 扩展。默认保存到系统下载目录，可在弹窗中更改。"
