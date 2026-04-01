[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$Hostname,

    [Parameter(Mandatory = $false)]
    [string]$TunnelName = "fileshare-temp",

    [Parameter(Mandatory = $false)]
    [Nullable[int]]$Port,

    [Parameter(Mandatory = $false)]
    [switch]$SkipMigrate,

    [Parameter(Mandatory = $false)]
    [switch]$InitOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$LocalCloudflaredDir = Join-Path $ProjectRoot ".cloudflared"
$ProjectConfigPath = Join-Path $LocalCloudflaredDir "config.yml"
$SecretPath = Join-Path $LocalCloudflaredDir "django-secret.txt"
$WaitressOutLog = Join-Path $LocalCloudflaredDir "waitress.out.log"
$WaitressErrLog = Join-Path $LocalCloudflaredDir "waitress.err.log"
$UserCloudflaredDir = Join-Path $HOME ".cloudflared"
$UserCertPath = Join-Path $UserCloudflaredDir "cert.pem"

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-PreferredExecutable {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Candidates,

        [Parameter(Mandatory = $true)]
        [string]$ErrorMessage
    )

    foreach ($candidate in $Candidates) {
        if ([System.IO.Path]::IsPathRooted($candidate) -and (Test-Path $candidate)) {
            return $candidate
        }

        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command) {
            return $command.Source
        }
    }

    throw $ErrorMessage
}

function Ensure-Directory {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Invoke-CommandChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $false)]
        [string[]]$Arguments = @(),

        [Parameter(Mandatory = $false)]
        [string]$FailureMessage = "Command failed."
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

function Invoke-CommandCapture {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $false)]
        [string[]]$Arguments = @(),

        [Parameter(Mandatory = $false)]
        [string]$FailureMessage = "Command failed."
    )

    $output = & $FilePath @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        $message = ($output | Out-String).Trim()
        if ($message) {
            throw "$FailureMessage`n$message"
        }
        throw $FailureMessage
    }

    return @($output)
}

function Get-ExistingConfigValues {
    if (-not (Test-Path $ProjectConfigPath)) {
        return $null
    }

    $config = Get-Content $ProjectConfigPath -Raw
    $tunnelMatch = [regex]::Match($config, '(?m)^tunnel:\s*(.+?)\s*$')
    $credentialsMatch = [regex]::Match($config, '(?m)^credentials-file:\s*(.+?)\s*$')
    $hostnameMatch = [regex]::Match($config, '(?m)^  - hostname:\s*(.+?)\s*$')
    $serviceMatch = [regex]::Match($config, '(?m)^    service:\s*http://localhost:(\d+)\s*$')

    if (-not $tunnelMatch.Success -or -not $credentialsMatch.Success) {
        throw "Existing .cloudflared/config.yml is missing required fields."
    }

    [pscustomobject]@{
        TunnelId        = $tunnelMatch.Groups[1].Value.Trim()
        CredentialsFile = $credentialsMatch.Groups[1].Value.Trim()
        Hostname        = if ($hostnameMatch.Success) { $hostnameMatch.Groups[1].Value.Trim() } else { $null }
        Port            = if ($serviceMatch.Success) { [int]$serviceMatch.Groups[1].Value } else { $null }
    }
}

function New-DjangoSecret {
    $bytes = New-Object byte[] 48
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return [Convert]::ToBase64String($bytes)
}

function Ensure-DjangoSecret {
    Ensure-Directory $LocalCloudflaredDir

    if (-not (Test-Path $SecretPath)) {
        Set-Content -Path $SecretPath -Value (New-DjangoSecret) -NoNewline
    }

    return (Get-Content $SecretPath -Raw).Trim()
}

function Ensure-CloudflareLogin {
    if (Test-Path $UserCertPath) {
        return
    }

    Write-Step "No Cloudflare login found. A browser window will open for cloudflared tunnel login."
    Invoke-CommandChecked `
        -FilePath $CloudflaredExe `
        -Arguments @("tunnel", "login") `
        -FailureMessage "cloudflared tunnel login failed."
}

function Create-Or-RefreshConfig {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TunnelId,

        [Parameter(Mandatory = $true)]
        [string]$CredentialsFile,

        [Parameter(Mandatory = $true)]
        [string]$ResolvedHostname,

        [Parameter(Mandatory = $true)]
        [int]$ResolvedPort
    )

    Ensure-Directory $LocalCloudflaredDir

    $configText = @"
tunnel: $TunnelId
credentials-file: $CredentialsFile

ingress:
  - hostname: $ResolvedHostname
    service: http://localhost:$ResolvedPort
  - service: http_status:404
"@

    Set-Content -Path $ProjectConfigPath -Value $configText
}

function Initialize-Tunnel {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ResolvedHostname,

        [Parameter(Mandatory = $true)]
        [int]$ResolvedPort
    )

    Ensure-CloudflareLogin

    $beforeJsonFiles = @{}
    if (Test-Path $UserCloudflaredDir) {
        Get-ChildItem -Path $UserCloudflaredDir -Filter "*.json" -File | ForEach-Object {
            $beforeJsonFiles[$_.FullName] = $true
        }
    }

    Write-Step "Creating Cloudflare tunnel '$TunnelName'"
    $createOutput = Invoke-CommandCapture `
        -FilePath $CloudflaredExe `
        -Arguments @("tunnel", "create", $TunnelName) `
        -FailureMessage "cloudflared tunnel create failed."

    $createText = ($createOutput | Out-String)
    $tunnelIdMatch = [regex]::Match(
        $createText,
        '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
    )
    if (-not $tunnelIdMatch.Success) {
        throw "Tunnel was created, but the tunnel ID could not be parsed from cloudflared output."
    }

    $tunnelId = $tunnelIdMatch.Value
    $credentialsFile = Join-Path $UserCloudflaredDir "$tunnelId.json"
    if (-not (Test-Path $credentialsFile)) {
        $newJsonFile = Get-ChildItem -Path $UserCloudflaredDir -Filter "*.json" -File |
            Where-Object { -not $beforeJsonFiles.ContainsKey($_.FullName) } |
            Select-Object -First 1

        if ($newJsonFile) {
            $credentialsFile = $newJsonFile.FullName
        }
    }

    if (-not (Test-Path $credentialsFile)) {
        throw "Tunnel credentials file was not found after tunnel creation."
    }

    Write-Step "Routing DNS for $ResolvedHostname"
    Invoke-CommandChecked `
        -FilePath $CloudflaredExe `
        -Arguments @("tunnel", "route", "dns", $TunnelName, $ResolvedHostname) `
        -FailureMessage "cloudflared tunnel route dns failed."

    Create-Or-RefreshConfig `
        -TunnelId $tunnelId `
        -CredentialsFile $credentialsFile `
        -ResolvedHostname $ResolvedHostname `
        -ResolvedPort $ResolvedPort
}

$PythonExe = Get-PreferredExecutable `
    -Candidates @(
        (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
        "python"
    ) `
    -ErrorMessage "Python was not found. Create a venv or install Python first."

$WaitressExe = Get-PreferredExecutable `
    -Candidates @(
        (Join-Path $ProjectRoot ".venv\Scripts\waitress-serve.exe"),
        "waitress-serve"
    ) `
    -ErrorMessage "waitress-serve was not found. Install it with: pip install waitress"

$CloudflaredExe = Get-PreferredExecutable `
    -Candidates @("cloudflared") `
    -ErrorMessage "cloudflared was not found. Install it first."

$existingConfig = Get-ExistingConfigValues
$resolvedHostname = if ($PSBoundParameters.ContainsKey("Hostname")) { $Hostname } elseif ($existingConfig) { $existingConfig.Hostname } else { $null }
if (-not $resolvedHostname) {
    throw "Provide -Hostname on first run, for example: .\start-cloudflare-tunnel.ps1 -Hostname share.example.com"
}

$resolvedPort = if ($PSBoundParameters.ContainsKey("Port")) { $Port } elseif ($existingConfig -and $existingConfig.Port) { $existingConfig.Port } else { 8000 }

if (-not $existingConfig) {
    Initialize-Tunnel -ResolvedHostname $resolvedHostname -ResolvedPort $resolvedPort
    $existingConfig = Get-ExistingConfigValues
} else {
    if (-not (Test-Path $existingConfig.CredentialsFile)) {
        throw "The saved tunnel credentials file was not found: $($existingConfig.CredentialsFile)"
    }

    if ($PSBoundParameters.ContainsKey("Hostname") -or ($existingConfig.Port -ne $resolvedPort)) {
        Write-Step "Refreshing local Cloudflare tunnel config"
        Create-Or-RefreshConfig `
            -TunnelId $existingConfig.TunnelId `
            -CredentialsFile $existingConfig.CredentialsFile `
            -ResolvedHostname $resolvedHostname `
            -ResolvedPort $resolvedPort

        if ($PSBoundParameters.ContainsKey("Hostname")) {
            Write-Step "Ensuring DNS route exists for $resolvedHostname"
            Invoke-CommandChecked `
                -FilePath $CloudflaredExe `
                -Arguments @("tunnel", "route", "dns", $existingConfig.TunnelId, $resolvedHostname) `
                -FailureMessage "cloudflared tunnel route dns failed."
        }
    }
}

if ($InitOnly) {
    Write-Host ""
    Write-Host "Tunnel initialized." -ForegroundColor Green
    Write-Host "Config: $ProjectConfigPath"
    Write-Host "Hostname: https://$resolvedHostname"
    exit 0
}

$env:DJANGO_SECRET_KEY = Ensure-DjangoSecret
$env:DJANGO_DEBUG = "false"
$env:DJANGO_ALLOWED_HOSTS = "127.0.0.1,localhost,$resolvedHostname"
$env:DJANGO_CSRF_TRUSTED_ORIGINS = "https://$resolvedHostname"

if (-not $SkipMigrate) {
    Write-Step "Running database migrations"
    Invoke-CommandChecked `
        -FilePath $PythonExe `
        -Arguments @("manage.py", "migrate") `
        -FailureMessage "python manage.py migrate failed."
}

Ensure-Directory $LocalCloudflaredDir
if (Test-Path $WaitressOutLog) {
    Remove-Item $WaitressOutLog -Force
}
if (Test-Path $WaitressErrLog) {
    Remove-Item $WaitressErrLog -Force
}

Write-Step "Starting Waitress on http://127.0.0.1:$resolvedPort"
$waitressProcess = Start-Process `
    -FilePath $WaitressExe `
    -ArgumentList @("--listen=127.0.0.1:$resolvedPort", "config.wsgi:application") `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput $WaitressOutLog `
    -RedirectStandardError $WaitressErrLog `
    -PassThru

Start-Sleep -Seconds 2
if ($waitressProcess.HasExited) {
    $stderr = if (Test-Path $WaitressErrLog) { (Get-Content $WaitressErrLog -Raw).Trim() } else { "" }
    $stdout = if (Test-Path $WaitressOutLog) { (Get-Content $WaitressOutLog -Raw).Trim() } else { "" }
    $details = ($stderr, $stdout | Where-Object { $_ }) -join "`n"
    if (-not $details) {
        $details = "Waitress exited immediately."
    }
    throw $details
}

Write-Host ""
Write-Host "Public URL: https://$resolvedHostname" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop the tunnel and the local server."
Write-Host ""

try {
    Invoke-CommandChecked `
        -FilePath $CloudflaredExe `
        -Arguments @("tunnel", "--config", $ProjectConfigPath, "run", $existingConfig.TunnelId) `
        -FailureMessage "cloudflared tunnel run failed."
}
finally {
    if ($waitressProcess -and -not $waitressProcess.HasExited) {
        Write-Step "Stopping Waitress"
        Stop-Process -Id $waitressProcess.Id -ErrorAction SilentlyContinue
    }
}
