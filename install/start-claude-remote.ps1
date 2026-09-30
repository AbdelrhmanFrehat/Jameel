<#
.SYNOPSIS
  Starts the Claude Hardware Remote bridge daemon (python -m bridge).

.DESCRIPTION
  Run from anywhere:   powershell -ExecutionPolicy Bypass -File install\start-claude-remote.ps1
  Options:
    -Port COM7        explicit serial port (default: auto-detect the Espressif USB device)
    -Verbose          DEBUG logging (every wire line, secrets redacted)
    -Background       start minimized in its own window and return immediately
    -Autostart        register a Startup-folder shortcut so the bridge starts at logon
    -RemoveAutostart  remove that shortcut

  The bridge only listens on 127.0.0.1 and is the only process that opens the
  ESP32's COM port. Leave it running while you use Claude Code.
#>
[CmdletBinding()]
param(
    [string]$Port = "",
    [switch]$Background,
    [switch]$Autostart,
    [switch]$RemoveAutostart
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = "python"
if (Test-Path "C:\Python314\python.exe") { $python = "C:\Python314\python.exe" }

$startupDir = [Environment]::GetFolderPath("Startup")
$cmdPath = Join-Path $startupDir "claude-hardware-remote.cmd"

if ($RemoveAutostart) {
    if (Test-Path $cmdPath) { Remove-Item $cmdPath -Force; Write-Host "Removed $cmdPath" }
    else { Write-Host "No autostart entry found." }
    return
}

if ($Autostart) {
    $portArg = ""
    if ($Port) { $portArg = " --port $Port" }
    $content = "@echo off`r`ncd /d `"$root`"`r`nstart `"Claude Remote Bridge`" /min `"$python`" -m bridge$portArg`r`n"
    Set-Content -Path $cmdPath -Value $content -Encoding ASCII
    Write-Host "Autostart entry written: $cmdPath"
    Write-Host "The bridge will start minimized at every logon. Run with -RemoveAutostart to undo."
    return
}

$args = @("-m", "bridge")
if ($Port) { $args += @("--port", $Port) }
if ($VerbosePreference -eq "Continue") { $args += "--verbose" }

Set-Location $root
if ($Background) {
    Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $root -WindowStyle Minimized
    Write-Host "Bridge started in a minimized window. Health: http://127.0.0.1:8765/health"
} else {
    Write-Host "Starting bridge from $root (Ctrl+C to stop)"
    & $python @args
}
