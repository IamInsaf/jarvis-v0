"""The gate every tool call passes before it runs: safe (just run it), confirm (ask the user out loud first),
or block (never, whatever the model or the user's words say). A small model writes the arguments, so this is
where "delete my documents folder" turned into Remove-Item -Recurse gets stopped, not in the prompt."""
import re

# shell commands that destroy data, change the system, or fetch and run code from the internet
_BLOCK_SHELL = [
    (r"\b(?:remove-item|rm|del|erase|rd|rmdir|ri)\b.*(?:-r(?:ecurse)?\b|/s\b)", "deleting folders recursively"),
    (r"\b(?:remove-item|rm|del|erase|ri)\b.*(?:\$env:|~|\\users\\|\\windows|system32|program ?files|"
     r"documents|desktop|downloads|pictures|onedrive|[a-z]:\\\s*$|[a-z]:\\\*)", "deleting files in personal or system folders"),
    (r"\b(?:format(?:-volume)?|diskpart|clear-disk|initialize-disk|remove-partition|cipher\s+/w)\b", "wiping disks"),
    (r"\b(?:stop-computer|restart-computer|shutdown(?:\.exe)?|logoff)\b", "shutting down or restarting"),
    (r"\b(?:bcdedit|reg(?:\.exe)?\s+(?:delete|add)|remove-itemproperty|set-itemproperty\s+.*hklm)", "editing boot or registry settings"),
    (r"\b(?:set-executionpolicy|takeown|icacls|net\s+user|net\s+localgroup|add-localgroupmember|new-localuser)\b",
     "changing security settings or accounts"),
    (r"\b(?:disable-computerrestore|vssadmin|wbadmin|remove-computerrestorepoint)\b", "removing backups or restore points"),
    (r"(?:invoke-expression|\biex\b|invoke-webrequest|\biwr\b|\bcurl\b|\bwget\b|downloadstring|downloadfile|"
     r"start-bitstransfer)", "downloading or running code from the internet"),
    (r"-encodedcommand|\bfrombase64string\b", "hidden (encoded) commands"),
    (r"\b(?:set-mppreference|add-mppreference)\b|disablerealtimemonitoring", "turning off antivirus protection"),
    (r"\b(?:stop-process|kill|taskkill)\b.*(?:-name\s+\*|/im\s+\*|\b(?:csrss|winlogon|lsass|svchost|explorer)\b)",
     "killing system processes"),
]
_BLOCK_SHELL = [(re.compile(p, re.I | re.S), why) for p, why in _BLOCK_SHELL]
# a pipeline only of these just looks things up, so it runs without asking
_READ_ONLY = re.compile(r"^(?:get-\w+|test-connection|test-path|select-object|select|sort-object|sort|"
                        r"where-object|where|measure-object|measure|format-table|ft|format-list|fl|out-string|"
                        r"ipconfig|hostname|whoami|systeminfo|ping|tasklist|dir|ls|gci|resolve-dnsname|"
                        r"\$psversiontable|\[environment\]::\w+)\b", re.I)
_CHAINED = re.compile(r";|&&|\|\||`|\$\(|>|<")
# key combinations that close or quit things
_RISKY_KEYS = re.compile(r"\balt\s*[+ ]\s*f4\b|\bctrl\s*[+ ]\s*w\b|\bctrl\s*[+ ]\s*shift\s*[+ ]\s*(?:esc|delete|del)\b|"
                         r"\bwin(?:dows)?\s*[+ ]\s*l\b", re.I)


def check(name: str, args: dict) -> tuple[str, str]:
    """("safe" | "confirm" | "block", why). For confirm, `why` is the yes/no question to ask."""
    if name == "run_shell":
        return _shell(str(args.get("command", "")))
    if name == "press_keys" and _RISKY_KEYS.search(str(args.get("keys", ""))):
        return "confirm", f"That shortcut, {args.get('keys')}, closes things. Shall I press it?"
    return "safe", ""


def _shell(command: str) -> tuple[str, str]:
    cmd = command.strip()
    if not cmd:
        return "safe", ""  # run_shell itself asks what to run
    for pattern, why in _BLOCK_SHELL:
        if pattern.search(cmd):
            return "block", why
    stages = [s.strip() for s in cmd.split("|")]
    if not _CHAINED.search(cmd) and all(_READ_ONLY.match(s) for s in stages):
        return "safe", ""
    return "confirm", f"Shall I run this command: {cmd}?"
