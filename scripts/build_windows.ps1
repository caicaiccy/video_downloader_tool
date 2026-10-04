$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYINSTALLER_CONFIG_DIR = Join-Path $env:TEMP "bili-downloader-pyinstaller"
$env:PIP_CACHE_DIR = Join-Path $env:TEMP "bili-downloader-pip-cache"

$BuildPython = if ($env:BUILD_PYTHON) { $env:BUILD_PYTHON } else { "python" }
& $BuildPython -c 'import sys; assert sys.version_info >= (3, 10), "Build requires Python 3.10+"'
if ($LASTEXITCODE -ne 0) { throw "Python 3.10+ is required" }
& $BuildPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
& $BuildPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Download tests failed" }
if (Get-Command node -ErrorAction SilentlyContinue) {
  node tests/test_extension.cjs
  if ($LASTEXITCODE -ne 0) { throw "Extension tests failed" }
  node tests/test_background_fetch.cjs
  if ($LASTEXITCODE -ne 0) { throw "Background transfer tests failed" }
}
& $BuildPython -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name BiliDownloader `
  --collect-all yt_dlp `
  --collect-all imageio_ffmpeg `
  --add-data "THIRD_PARTY_NOTICES.md;." `
  app.py
if ($LASTEXITCODE -ne 0) { throw "Desktop build failed" }

& $BuildPython -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --console `
  --name BiliDownloaderHost `
  --distpath dist/host `
  --workpath build/host `
  --collect-all yt_dlp `
  --collect-all imageio_ffmpeg `
  --add-data "THIRD_PARTY_NOTICES.md;." `
  native_host.py
if ($LASTEXITCODE -ne 0) { throw "Native Host build failed" }

$PackageDir = Join-Path (Get-Location) "dist\BiliDownloader-windows-x64"
if (Test-Path $PackageDir) { Remove-Item $PackageDir -Recurse -Force }
New-Item -ItemType Directory -Path $PackageDir -Force | Out-Null
Copy-Item "dist\BiliDownloader.exe" $PackageDir
Copy-Item "dist\host\BiliDownloaderHost.exe" $PackageDir
Copy-Item "browser-extension" $PackageDir -Recurse
Copy-Item "scripts\install_native_host_windows.ps1" $PackageDir
Copy-Item "README.md", "THIRD_PARTY_NOTICES.md" $PackageDir
Copy-Item "browser-extension\README.md" (Join-Path $PackageDir "EXTENSION-README.md")
Copy-Item "docs" $PackageDir -Recurse
$Archive = Join-Path (Get-Location) "dist\BiliDownloader-windows-x64.zip"
if (Test-Path $Archive) { Remove-Item $Archive -Force }
Compress-Archive -Path (Join-Path $PackageDir "*") -DestinationPath $Archive
& $BuildPython scripts/package_source.py
if ($LASTEXITCODE -ne 0) { throw "Source/extension packaging failed" }
Write-Host "Created $Archive"
