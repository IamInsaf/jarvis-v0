# Shortcuts that start JARVIS with pythonw (no console window; output goes to data\jarvis.log). The desktop
# orb shows it's running, and its power button (or the HUD's SHUT DOWN button) is the kill switch.
#   .\install_startup.ps1                  start JARVIS when you sign in (Startup folder)
#   .\install_startup.ps1 -Desktop         a JARVIS icon on the desktop: click it to start JARVIS
#   .\install_startup.ps1 -Remove          take the sign-in shortcut out again
#   .\install_startup.ps1 -Desktop -Remove take the desktop icon off again
# Clicking either while JARVIS is already running does nothing: there's only ever one.
param([switch]$Desktop, [switch]$Remove)

$here = $PSScriptRoot
$folder = if ($Desktop) { [Environment]::GetFolderPath("Desktop") } else { [Environment]::GetFolderPath("Startup") }
$link = Join-Path $folder "JARVIS.lnk"
$what = if ($Desktop) { "desktop icon" } else { "startup shortcut" }

if ($Remove) {
    if (Test-Path $link) { Remove-Item $link; Write-Host "Removed $link" } else { Write-Host "No $what to remove." }
    exit
}

$pythonw = Join-Path $here ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { Write-Error "No $pythonw - create the .venv first (see README)."; exit 1 }

$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "`"$(Join-Path $here 'jarvis.py')`""
$shortcut.WorkingDirectory = $here
$shortcut.Description = "JARVIS voice assistant"
$icon = Join-Path $here "assets\jarvis.ico"
if (Test-Path $icon) { $shortcut.IconLocation = $icon }
$shortcut.Save()
if ($Desktop) {
    Write-Host "JARVIS icon added to your desktop ($link). Click it to start JARVIS."
} else {
    Write-Host "JARVIS will start when you sign in ($link). Log: $(Join-Path $here 'data\jarvis.log')"
}
