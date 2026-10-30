# Starts JARVIS when you sign in to Windows: puts a shortcut in your Startup folder that runs it with
# pythonw (no console window; output goes to data\jarvis.log). The desktop orb shows it's running.
#   .\install_startup.ps1          add the shortcut
#   .\install_startup.ps1 -Remove  take it out again
param([switch]$Remove)

$here = $PSScriptRoot
$link = Join-Path ([Environment]::GetFolderPath("Startup")) "JARVIS.lnk"

if ($Remove) {
    if (Test-Path $link) { Remove-Item $link; Write-Host "Removed $link" } else { Write-Host "No startup shortcut to remove." }
    exit
}

$pythonw = Join-Path $here ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { Write-Error "No $pythonw - create the .venv first (see README)."; exit 1 }

$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "`"$(Join-Path $here 'jarvis.py')`""
$shortcut.WorkingDirectory = $here
$shortcut.Description = "JARVIS voice assistant"
$shortcut.Save()
Write-Host "JARVIS will start when you sign in ($link). Log: $(Join-Path $here 'data\jarvis.log')"
