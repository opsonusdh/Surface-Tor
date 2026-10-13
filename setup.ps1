$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Pinned third-party versions: what this project was tested with.
#
# Bump these deliberately (and re-verify) rather than tracking "latest". A new
# release can regress or be withdrawn, and a download of "whatever is newest"
# can't be checked against anything.
# ---------------------------------------------------------------------------
$ReplicaCommit = "f21df1031b686c467533e48806f60c8ce1dce762"

$PinnedCloudflaredVersion = "2026.9.3"
# SHA-256 of the official cloudflared-windows-amd64.exe release asset for the
# version above, as published in Cloudflare's own release notes.
$Sha256WindowsAmd64 = "f096265ec2fcbe9bb6e2d64268db167ced3fcbb83d894bdb9e2fcdb26f2ea7e2"

# Override with e.g.  $env:CLOUDFLARED_VERSION = "2026.10.0"  or  "latest".
# Anything other than the pinned version is downloaded WITHOUT verification.
$CloudflaredVersion = if ($env:CLOUDFLARED_VERSION) { $env:CLOUDFLARED_VERSION } else { $PinnedCloudflaredVersion }


# ---------------------------------------------------------------------------
# Replica (the reverse-proxy engine each chain node runs)
# ---------------------------------------------------------------------------
$ReplicaDir = Join-Path $PSScriptRoot "Replica"

if (-not (Test-Path $ReplicaDir)) {
    Write-Host "[*] Cloning Replica, pinned to commit $($ReplicaCommit.Substring(0, 7))..."
    git clone https://github.com/sarperavci/Replica.git $ReplicaDir
    if ($LASTEXITCODE -ne 0) { throw "git clone failed." }
    git -C $ReplicaDir checkout --quiet $ReplicaCommit
    if ($LASTEXITCODE -ne 0) { throw "git checkout of the pinned Replica commit failed." }
} else {
    Write-Host "[*] Replica directory already exists. Leaving your checkout as it is."
    $CurrentCommit = (git -C $ReplicaDir rev-parse HEAD 2>$null)
    if ($CurrentCommit -and $CurrentCommit -ne $ReplicaCommit) {
        Write-Host "[NOTE] Replica is at $CurrentCommit; this project was tested with $ReplicaCommit."
    }
}

Write-Host "[*] Applying local patch (adds -u/--user-agent support to Replica)..."
$PatchScript = Join-Path $PSScriptRoot "scripts\patch_replica.py"
$PatchOk = $false
if (Get-Command "py" -ErrorAction SilentlyContinue) {
    py $PatchScript
    $PatchOk = ($LASTEXITCODE -eq 0)
} elseif (Get-Command "python" -ErrorAction SilentlyContinue) {
    python $PatchScript
    $PatchOk = ($LASTEXITCODE -eq 0)
} else {
    Write-Warning "No python found to apply scripts\patch_replica.py."
}
if (-not $PatchOk) {
    Write-Warning "Could not apply the Replica patch; -u/--user-agent will refuse to run until scripts\patch_replica.py succeeds."
}


# ---------------------------------------------------------------------------
# cloudflared
# ---------------------------------------------------------------------------
$ArchRaw = $env:PROCESSOR_ARCHITECTURE
$Arch = ""

if ($ArchRaw -eq "AMD64") {
    $Arch = "amd64"
} else {
    $Arch = "386"
}

Write-Host "[*] Target Environment Identified: Windows | Architecture=$Arch"

$ExpectedSha = ""
if (($CloudflaredVersion -eq $PinnedCloudflaredVersion) -and ($Arch -eq "amd64")) {
    $ExpectedSha = $Sha256WindowsAmd64
}

if ($CloudflaredVersion -eq "latest") {
    $DownloadUrl = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-$Arch.exe"
} else {
    $DownloadUrl = "https://github.com/cloudflare/cloudflared/releases/download/$CloudflaredVersion/cloudflared-windows-$Arch.exe"
}

$BinaryName = "cloudflared.exe"
$DestinationPath = Join-Path $PSScriptRoot $BinaryName
$TempPath = "$DestinationPath.download"

function Get-Sha256Lower($Path) {
    return (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLower()
}

if ($ExpectedSha -and (Test-Path $DestinationPath) -and ((Get-Sha256Lower $DestinationPath) -eq $ExpectedSha)) {
    Write-Host "[*] cloudflared $CloudflaredVersion is already present and its checksum matches. Skipping download."
} else {
    Write-Host "[*] Downloading cloudflared $CloudflaredVersion from the official Cloudflare releases..."
    if (Test-Path $TempPath) { Remove-Item $TempPath -Force }
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $TempPath

    if ($ExpectedSha) {
        $ActualSha = Get-Sha256Lower $TempPath
        if ($ActualSha -ne $ExpectedSha) {
            Remove-Item $TempPath -Force
            throw "Checksum mismatch for the downloaded cloudflared (expected $ExpectedSha, got $ActualSha). Refusing to install it."
        }
        Write-Host "[*] Checksum verified against Cloudflare's published SHA-256."
    } else {
        Write-Warning "No pinned checksum for cloudflared $CloudflaredVersion on $Arch; the download was NOT verified."
    }

    Move-Item -Path $TempPath -Destination $DestinationPath -Force
}

Write-Host "[SUCCESS] cloudflared deployed inside the application directory."


# ---------------------------------------------------------------------------
# Python dependencies
# ---------------------------------------------------------------------------
Write-Host "[*] Installing Python dependencies..."

$Requirements = Join-Path $PSScriptRoot "requirements.txt"
if (Get-Command "py" -ErrorAction SilentlyContinue) {
    py -m pip install -r $Requirements
} elseif (Get-Command "python" -ErrorAction SilentlyContinue) {
    python -m pip install -r $Requirements
} else {
    Write-Warning "No 'python' found. Please install the dependencies in requirements.txt manually."
    Exit
}

Write-Host ""
Write-Host "========================================================================="
Write-Host "[*] SETUP COMPLETED."
Write-Host " -> Proxy engine (Replica): ready."
Write-Host " -> Tunnel client: ready (.\$BinaryName)."
Write-Host "========================================================================="
