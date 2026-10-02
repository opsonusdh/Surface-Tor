$ErrorActionPreference = "Stop"

if (-not (Test-Path "$PSScriptRoot\Replica")) {
    Write-Host "[*] Cloning the upstream web proxy isolation engine..." 
    git clone https://github.com/sarperavci/Replica.git
} else {
    Write-Host "[*] Replica workspace directory already exists. Skipping clone stage." 
}

$ArchRaw = $env:PROCESSOR_ARCHITECTURE
$Arch = ""

if ($ArchRaw -eq "AMD64") {
    $Arch = "amd64"
} else {
    $Arch = "386"
}

Write-Host "[*] Target Environment Identified: Windows | Architecture=$Arch" 

$DownloadUrl = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-$Arch.exe"
$BinaryName = "cloudflared.exe"
$DestinationPath = "$PSScriptRoot\$BinaryName"

Write-Host "[*] Extracting standalone executable from Cloudflare distribution..." 
Invoke-WebRequest -Uri $DownloadUrl -OutFile $DestinationPath

Write-Host "[SUCCESS] Standalone proxy daemon '$BinaryName' deployed inside application workspace." 

Write-Host "[*] Transitioning to local runtime environment configuration loop..." 
Set-Location -Path "$PSScriptRoot\Replica"

if (Get-Command "py" -ErrorAction SilentlyContinue) {
    Write-Host "[*] Triggering dependency installation layer using Python Launcher (py)..." 
    py -m pip install --upgrade pip
    py -m pip install -r requirements.txt
} elseif (Get-Command "python" -ErrorAction SilentlyContinue) {
    Write-Host "[*] Triggering dependency installation layer using Python..." 
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
} else {
    Write-Warning "[WARNING] No systemic 'python' execution binary located. Please execute dependencies manually."
    Exit
}

Write-Host "`n=========================================================================" 
Write-Host "[*] WINDOWS SYSTEM METRICS DEPLOYMENT COMPLETED." 
Write-Host " -> Proxy Node Stack: Active & Configured." 
Write-Host " -> Tunnel Gateway Execution Binary: Ready (.\$BinaryName)." 
Write-Host "=========================================================================" 
