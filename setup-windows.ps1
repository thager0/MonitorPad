<#
.SYNOPSIS
  One-time Windows setup for MonitorPad: firewall rule and/or auto-start.

.DESCRIPTION
  The firewall rule lets your phone reach the agent over the LAN and needs an
  elevated PowerShell. Auto-start does not need elevation -- it drops a
  shortcut in your Startup folder so the agent comes back after a reboot.

  MonitorPad has to run inside your interactive desktop session (that is what
  owns the display configuration), so it is deliberately a Startup shortcut
  rather than a Windows service.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File setup-windows.ps1 -Firewall -AutoStart
#>

[CmdletBinding()]
param(
  [switch]$Firewall,
  [switch]$AutoStart,
  [switch]$Remove,
  [int]$Port = 8777
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$agent = Join-Path $root 'agent'
$serverPy = Join-Path $agent 'server.py'
$ruleName = "MonitorPad ($Port)"
$startupDir = [Environment]::GetFolderPath('Startup')
$shortcut = Join-Path $startupDir 'MonitorPad.lnk'

function Test-Admin {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = New-Object Security.Principal.WindowsPrincipal($identity)
  return $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not $Firewall -and -not $AutoStart -and -not $Remove) {
  Write-Host ''
  Write-Host '  MonitorPad setup' -ForegroundColor Cyan
  Write-Host ''
  Write-Host '  Pick what you want:'
  Write-Host '    -Firewall    allow your phone through Windows Firewall'
  Write-Host '                 (run this window as Administrator)'
  Write-Host '    -AutoStart   start MonitorPad when you sign in, via a'
  Write-Host '                 scheduled task (no elevation needed)'
  Write-Host '    -Remove      undo both of the above'
  Write-Host ''
  Write-Host '  Example:'
  Write-Host '    powershell -ExecutionPolicy Bypass -File setup-windows.ps1 -Firewall -AutoStart'
  Write-Host ''
  return
}

if ($Remove) {
  if (Test-Admin) {
    $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if ($existing) {
      Remove-NetFirewallRule -DisplayName $ruleName
      Write-Host "Removed firewall rule '$ruleName'." -ForegroundColor Green
    } else {
      Write-Host "No firewall rule named '$ruleName'." -ForegroundColor Yellow
    }
  } else {
    Write-Host 'Skipping the firewall rule: needs an elevated PowerShell.' -ForegroundColor Yellow
  }
  $python = (Get-Command python.exe -ErrorAction SilentlyContinue)
  if ($python) {
    Push-Location $agent
    try { & $python.Source 'autostart.py' 'disable' } finally { Pop-Location }
  }
  if (Test-Path $shortcut) {
    Remove-Item $shortcut
    Write-Host 'Removed the legacy Startup shortcut too.' -ForegroundColor Green
  }
  return
}

if ($Firewall) {
  if (-not (Test-Admin)) {
    throw "The firewall rule needs an elevated PowerShell. Right-click PowerShell, Run as administrator, then run this again with -Firewall."
  }
  $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
  if ($existing) {
    Write-Host "Firewall rule '$ruleName' already exists." -ForegroundColor Yellow
  } else {
    # Private profile only: this should never be reachable from a public
    # network such as cafe or hotel Wi-Fi.
    New-NetFirewallRule -DisplayName $ruleName `
      -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
      -Profile Private -Description 'MonitorPad display agent (LAN only)' | Out-Null
    Write-Host "Added firewall rule '$ruleName' for private networks." -ForegroundColor Green
  }

  $net = Get-NetConnectionProfile | Where-Object { $_.IPv4Connectivity -ne 'Disconnected' }
  foreach ($profile in $net) {
    if ($profile.NetworkCategory -eq 'Public') {
      Write-Host ''
      Write-Host "  Heads up: '$($profile.Name)' is set to Public, so the rule" -ForegroundColor Yellow
      Write-Host '  will not apply there. Set it to Private in Settings >' -ForegroundColor Yellow
      Write-Host '  Network & internet if that is your home Wi-Fi.' -ForegroundColor Yellow
    }
  }
}

if ($AutoStart) {
  $python = (Get-Command python.exe -ErrorAction SilentlyContinue)
  if (-not $python) { throw 'Could not find python on PATH.' }

  Push-Location $agent
  try {
    & $python.Source 'autostart.py' 'enable' $Port
    if ($LASTEXITCODE -ne 0) { throw 'Registering the scheduled task failed.' }
  } finally {
    Pop-Location
  }

  # A shortcut left over from an earlier version would start a second copy.
  if (Test-Path $shortcut) {
    Remove-Item $shortcut
    Write-Host 'Removed the old Startup shortcut it replaces.' -ForegroundColor Yellow
  }

  Write-Host ''
  Write-Host '  Note: this is a scheduled task, not a Windows service.' -ForegroundColor Cyan
  Write-Host '  Display configuration belongs to your interactive desktop' -ForegroundColor Cyan
  Write-Host '  session; a service runs in session 0 and cannot touch it.' -ForegroundColor Cyan
}

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
