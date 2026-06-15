<#
.SYNOPSIS
  Build and package MMFileTransfer for air-gapped deployment.

.DESCRIPTION
  Produces two files under .\dist for hand-off to the infra team:
    - mmftp-images.tar     (all Docker images: app + postgres + redis + minio + mc + nginx)
    - mmftp-deploy.tar.gz  (compose file, env template, nginx configs, README, install script)

  The single application image `mmftp_app` runs web, worker, and beat. Run this
  from the repo root on a machine WITH internet so the third-party base images
  can be pulled.

.EXAMPLE
  pwsh -File .\scripts\build-deploy-package.ps1
  pwsh -File .\scripts\build-deploy-package.ps1 -Tag v1.0.0
  pwsh -File .\scripts\build-deploy-package.ps1 -AppOnly   # code-only update: ships just mmftp_app
#>

[CmdletBinding()]
param(
  [string]$Tag = "latest",
  [string]$OutDir = "dist",
  # -AppOnly: skip third-party pulls + deploy bundle. Produces mmftp-app-image.tar
  #   with only mmftp_app. Use for code-only updates (postgres/redis/minio/nginx
  #   already on the server).
  [switch]$AppOnly
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

function Step($msg) {
  Write-Host ""
  Write-Host "==> $msg" -ForegroundColor Cyan
}

# ---------- 1. Sanity checks ----------
Step "Checking prerequisites"
docker --version | Out-Host
docker compose version | Out-Host
tar --version | Select-Object -First 1 | Out-Host

# ---------- 2. Prepare output directory ----------
$outPath = Join-Path $repoRoot $OutDir
if (-not (Test-Path $outPath)) { New-Item -ItemType Directory -Path $outPath | Out-Null }

# ---------- 3. Build the application image ----------
Step "Building mmftp_app:$Tag"
docker build -t "mmftp_app:$Tag" $repoRoot
if ($LASTEXITCODE -ne 0) { throw "app build failed" }
if ($Tag -ne "latest") { docker tag "mmftp_app:$Tag" mmftp_app:latest }

# ---------- 4. Pull third-party images ----------
$thirdParty = @(
  "postgres:16-alpine",
  "redis:7-alpine",
  "minio/minio:latest",
  "minio/mc:latest",
  "nginx:1.27-alpine"
)
if ($AppOnly) {
  Step "App-only build: skipping postgres/redis/minio/mc/nginx pulls"
  $thirdParty = @()
}

function Test-LocalImage($name) {
  $id = docker images -q $name
  return -not [string]::IsNullOrWhiteSpace($id)
}

function Pull-WithRetry($img, [int]$maxAttempts = 4) {
  for ($i = 1; $i -le $maxAttempts; $i++) {
    docker pull $img
    if ($LASTEXITCODE -eq 0) { return $true }
    if ($i -lt $maxAttempts) {
      $delay = [math]::Min(30, [math]::Pow(2, $i))
      Write-Host "    pull failed (attempt $i/$maxAttempts), retrying in $delay s..." -ForegroundColor Yellow
      Start-Sleep -Seconds $delay
    }
  }
  return $false
}

foreach ($img in $thirdParty) {
  if (Test-LocalImage $img) {
    Step "$img already present locally - skipping pull"
    continue
  }
  Step "Pulling $img"
  if (-not (Pull-WithRetry $img)) {
    throw ("pull failed after retries: " + $img + ". Transient Docker Hub error - just re-run; already-built images are reused.")
  }
}

# ---------- 5. Save all images to a single tar ----------
$tarName = if ($AppOnly) { "mmftp-app-image.tar" } else { "mmftp-images.tar" }
$imagesTar = Join-Path $outPath $tarName
Step "Saving images to $imagesTar"

$appImages = @("mmftp_app:$Tag", "mmftp_app:latest")
$allImages = @( ($appImages + $thirdParty) | Select-Object -Unique )

Write-Host "    Images to save: $($allImages -join ', ')"
docker save -o $imagesTar $allImages
if ($LASTEXITCODE -ne 0) { throw "docker save failed" }

$sizeMb = [math]::Round((Get-Item $imagesTar).Length / 1MB, 1)
Write-Host "    -> $imagesTar ($sizeMb MB)"

# ---------- 6. App-only short-circuit ----------
if ($AppOnly) {
  Step "Done (app-only)"
  Write-Host "Hand this single file to the infra team:" -ForegroundColor Green
  Write-Host "  $imagesTar"
  Write-Host ""
  Write-Host "On the server (/opt/mmftp), run:" -ForegroundColor Green
  Write-Host "  docker load -i $tarName"
  Write-Host "  docker compose --env-file .env.production -f docker-compose.airgap.yml up -d"
  return
}

# ---------- 7. Stage deploy bundle ----------
$stage = Join-Path $outPath "_stage"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Path $stage | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stage "deploy") | Out-Null

Step "Staging deploy bundle in $stage"

Copy-Item "deploy\docker-compose.airgap.yml" -Destination (Join-Path $stage "docker-compose.airgap.yml")
Copy-Item "README_DEPLOY.md"                  -Destination (Join-Path $stage "README_DEPLOY.md")
Copy-Item "deploy\.env.production.template"   -Destination (Join-Path $stage ".env.production.template")
Copy-Item "deploy\nginx.compose.conf"         -Destination (Join-Path $stage "deploy\nginx.compose.conf")
Copy-Item "deploy\nginx.host.conf"            -Destination (Join-Path $stage "deploy\nginx.host.conf")
Copy-Item "deploy\install.sh"                 -Destination (Join-Path $stage "deploy\install.sh")

# ---------- 8. Tar.gz the staged tree ----------
$deployTgz = Join-Path $outPath "mmftp-deploy.tar.gz"
if (Test-Path $deployTgz) { Remove-Item $deployTgz }

Step "Creating $deployTgz"
Push-Location $stage
try {
  tar -czf $deployTgz .
  if ($LASTEXITCODE -ne 0) { throw "tar failed" }
} finally {
  Pop-Location
}

Remove-Item -Recurse -Force $stage

$tgzKb = [math]::Round((Get-Item $deployTgz).Length / 1KB, 1)
Write-Host "    -> $deployTgz ($tgzKb KB)"

# ---------- 9. Done ----------
Step "Done"
Write-Host "Hand these two files to the infra team:" -ForegroundColor Green
Write-Host "  $imagesTar"
Write-Host "  $deployTgz"
Write-Host ""
Write-Host "On the target server, follow README_DEPLOY.md (also inside the tar.gz)."
