# clsw kurulumu (Windows PowerShell):  irm https://github.com/orjinalp/clsw/releases/latest/download/install.ps1 | iex
# Belirli sürüm: $env:CLSW_VERSION = "1.2.3"; irm https://github.com/orjinalp/clsw/releases/latest/download/install.ps1 | iex
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repo = "orjinalp/clsw"
$asset = "clsw-windows-x64.zip"
if ($env:CLSW_DOWNLOAD_URL) {
    $base = $env:CLSW_DOWNLOAD_URL.TrimEnd('/')
} elseif ($env:CLSW_VERSION) {
    $base = "https://github.com/$repo/releases/download/v$($env:CLSW_VERSION.TrimStart('v'))"
} else {
    $base = "https://github.com/$repo/releases/latest/download"
}
if (-not [Environment]::Is64BitOperatingSystem) { throw "clsw yalnız 64 bit Windows için derleniyor." }

$tmp = Join-Path $env:TEMP ("clsw-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    Write-Host "clsw indiriliyor ($asset)..."
    $zip = Join-Path $tmp $asset
    Invoke-WebRequest -UseBasicParsing -Uri "$base/$asset" -OutFile $zip
    $sums = (Invoke-WebRequest -UseBasicParsing -Uri "$base/SHA256SUMS").Content
    if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }
    $want = ($sums -split "`n" | ForEach-Object { $p = $_.Trim() -split '\s+'; if ($p.Count -eq 2 -and $p[1].TrimStart('*') -eq $asset) { $p[0] } } | Select-Object -First 1)
    if (-not $want) { throw "SHA256SUMS icinde $asset yok" }
    $got = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
    if ($got -ne $want.ToLower()) { throw "SHA-256 tutmuyor (beklenen $want, gelen $got)" }

    Expand-Archive -Path $zip -DestinationPath (Join-Path $tmp "x")
    $env:CLSW_QUIET_PATH = "1"
    & (Join-Path $tmp "x\clsw\clsw.exe") install
    if ($LASTEXITCODE) { throw "clsw install basarisiz ($LASTEXITCODE)" }

    # Program %USERPROFILE%\.local\share\clsw altinda; "current" kullanilan surumu gosterir.
    $bin = Join-Path $HOME ".local\share\clsw\current"
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($userPath -split ';' | Where-Object { $_ })
    if ($parts -notcontains $bin) {
        [Environment]::SetEnvironmentVariable("Path", (($parts + $bin) -join ';'), "User")
        Write-Host "PATH'e $bin eklendi (kullanici ortam degiskenleri)."
        Write-Host "Terminali yeniden baslatman gerekiyor; sonra her yerden clsw yazilabilir."
    }
    if (($env:Path -split ';') -notcontains $bin) { $env:Path = "$env:Path;$bin" }
    Write-Host "Hazir. Baslamak icin: clsw list"
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
