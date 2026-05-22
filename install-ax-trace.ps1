# Installer for ax-trace on Windows.
#
# Usage:
#   irm https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install-ax-trace.ps1 | iex

$ErrorActionPreference = "Stop"

$Repo = "Arize-ai/coding-harness-tracing"
$InstallDir = if ($env:AX_TRACE_INSTALL_DIR) { $env:AX_TRACE_INSTALL_DIR } else { "$env:LOCALAPPDATA\Programs\ax-trace" }
$Version = $env:AX_TRACE_VERSION

if (-not $Version) {
    $api = "https://api.github.com/repos/$Repo/releases"
    $releases = Invoke-RestMethod -Uri $api
    $tag = ($releases | Where-Object { $_.tag_name -like "ax-trace-v*" } | Select-Object -First 1).tag_name
    if (-not $tag) { throw "Could not resolve latest ax-trace version" }
    $Version = $tag -replace "^ax-trace-", ""
}

Write-Host "[ax-trace] Installing ax-trace $Version for windows_amd64"

$base = "https://github.com/$Repo/releases/download/ax-trace-$Version"
$archive = "ax-trace_$($Version -replace '^v','')_windows_amd64.zip"
$checksums = "checksums.txt"

$tmp = New-Item -ItemType Directory -Path "$env:TEMP\ax-trace-install-$(Get-Random)"
try {
    Invoke-WebRequest -Uri "$base/$archive" -OutFile "$tmp\$archive"
    Invoke-WebRequest -Uri "$base/$checksums" -OutFile "$tmp\$checksums"

    # Verify SHA256
    $expected = (Get-Content "$tmp\$checksums" | Where-Object { $_ -match $archive }).Split(" ")[0]
    $actual = (Get-FileHash "$tmp\$archive" -Algorithm SHA256).Hash.ToLower()
    if ($expected -ne $actual) { throw "SHA256 verification failed" }

    Expand-Archive -Path "$tmp\$archive" -DestinationPath $tmp -Force
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    Copy-Item -Path "$tmp\ax-trace.exe" -Destination "$InstallDir\ax-trace.exe" -Force
    Write-Host "[ax-trace] Installed to $InstallDir\ax-trace.exe"

    # Add to user PATH if absent
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$InstallDir*") {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$InstallDir", "User")
        Write-Host "[ax-trace] Added $InstallDir to user PATH (restart shell to take effect)"
    }
}
finally {
    Remove-Item -Recurse -Force $tmp
}

Write-Host ""
Write-Host "[ax-trace] Run 'ax-trace claude' to get started"
