"""Wi-Fi and Bluetooth on/off, through Windows' own radio switches (the ones in the Action Center).
Learned plugins can't do this — they have no access to the OS — so it lives here as a core tool.
Uses the Windows.Devices.Radios API from PowerShell: no admin rights, and it toggles the radio,
not the network adapter, so Wi-Fi reconnects to your usual network by itself."""
import base64
import re
import subprocess

# what speech recognition makes of the names: "blue jute", "blue tooth", "why fi", "wi fi"
_BT = r"blue[\s-]?(?:tooth|jute|tube|teeth)|blu ?tooth"
_WIFI = r"wi[\s-]?fi|why[\s-]?fi|wifi|wireless|wlan"
_ON = r"on|enable|start|activate|connect"
_OFF = r"off|disable|stop|deactivate|disconnect|kill"
# "turn on bluetooth", "switch wifi off", "disable blue jute", "turn off wifi and bluetooth" — a command,
# never a question like "is bluetooth on?"
ASK = re.compile(r"^\s*(?:(?:hey|ok(?:ay)?|so|jarvis|please|can you|could you)[\s,]*)*"
                 r"(?:(?:turn|switch|put|flip|toggle)\s+)?(?:the\s+|my\s+)?"
                 rf"(?:(?P<s1>{_ON}|{_OFF})\b.*?\b(?:{_BT}|{_WIFI})|(?:{_BT}|{_WIFI})\b.*?\b(?P<s2>{_ON}|{_OFF}))"
                 r"\b[\s,.!?]*(?:please|now|jarvis)?[\s,.!?]*$", re.I)
_IS_BT = re.compile(rf"\b(?:{_BT})\b", re.I)
_IS_WIFI = re.compile(rf"\b(?:{_WIFI})\b", re.I)
_IS_ON = re.compile(rf"^(?:{_ON})$", re.I)

_KINDS = {"wifi": "WiFi", "bluetooth": "Bluetooth"}
_NAMES = {"WiFi": "Wi-Fi", "Bluetooth": "Bluetooth"}

# only values from _KINDS and On/Off are ever put into this script, never the user's words
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) { $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result }
[Windows.Devices.Radios.Radio, Windows.System.Devices, ContentType = WindowsRuntime] | Out-Null
$access = Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus])
if ($access -ne 'Allowed') { "ACCESS|$access"; exit }
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
foreach ($kind in @(%KINDS%)) {
    $found = @($radios | Where-Object { $_.Kind -eq $kind })
    if (-not $found) { "$kind|missing"; continue }
    foreach ($r in $found) {
        $result = Await ($r.SetStateAsync('%STATE%')) ([Windows.Devices.Radios.RadioAccessStatus])
        "$kind|$result"
    }
}
"""


def _powershell(script: str):
    """The finished process, or a sentence saying why PowerShell couldn't be run."""
    # -EncodedCommand runs the script as one unit (stdin with -Command - runs it line by line and
    # breaks on multi-line blocks) and, like any inline command, needs no execution-policy change
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return f"I couldn't reach the radio controls: {e}"


def parse(text: str):
    """(which, state) for a spoken "turn on bluetooth", else None. which: wifi, bluetooth or both."""
    m = ASK.match(text)
    if not m:
        return None
    bt, wifi = bool(_IS_BT.search(text)), bool(_IS_WIFI.search(text))
    which = "both" if bt and wifi else "bluetooth" if bt else "wifi"
    return which, "on" if _IS_ON.match(m["s1"] or m["s2"]) else "off"


def set_radios(which: str, state: str) -> str:
    """Turn Wi-Fi, Bluetooth or both on or off; says what actually happened."""
    which, state = str(which).lower().replace("-", "").replace(" ", ""), str(state).lower().strip()
    if state not in ("on", "off"):
        return "Should I turn it on or off?"
    if not which:  # never switch both on a guess: Wi-Fi and Bluetooth are wanted at different times
        return "Wi-Fi or Bluetooth?"
    kinds = list(_KINDS.values()) if which in ("both", "all") else [_KINDS[which]] if which in _KINDS else None
    if not kinds:
        return "I can only switch Wi-Fi and Bluetooth."
    script = (_SCRIPT.replace("%KINDS%", ",".join(f"'{k}'" for k in kinds))
              .replace("%STATE%", "On" if state == "on" else "Off"))
    r = _powershell(script)
    if isinstance(r, str):
        return r
    lines = [ln.strip() for ln in r.stdout.splitlines() if "|" in ln]
    if not lines:
        err = [ln.strip() for ln in (r.stderr or r.stdout).splitlines() if ln.strip()]
        print(f"  [radios] powershell exit {r.returncode}: {(r.stderr or r.stdout).strip()[-800:]}")
        # PowerShell's first error line says what went wrong; the rest is position info
        return f"Switching the radio failed: {err[0][:200] if err else 'no response from Windows'}"
    if lines[0].startswith("ACCESS|"):
        return ("Windows won't let me switch radios. Allow apps to control device radios in "
                "Settings, Privacy and security, Radios.")
    done, failed = [], []
    for line in lines:
        kind, status = line.split("|", 1)
        name = _NAMES.get(kind, kind)
        if status == "Allowed":
            done.append(name)
        elif status == "missing":
            failed.append(f"I couldn't find a {name} radio on this computer")
        else:  # DeniedByUser / DeniedBySystem: e.g. airplane mode, or a hardware switch
            failed.append(f"Windows refused to switch {name} ({status})")
    said = [f"{' and '.join(dict.fromkeys(done))} {'is' if len(set(done)) == 1 else 'are'} {state}"] if done else []
    return ". ".join(s[0].upper() + s[1:] for s in said + failed) + "."
