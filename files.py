"""The only way a learned ability can touch the disk: files and folders inside one workspace
folder (config.WORKSPACE_DIR). Every path is resolved and checked to stay inside it, so a plugin
asking for '..\\..\\Windows' or 'C:\\anything' is refused. Each call returns the full path it
used, so JARVIS can always say where something is. `created` lists what this object made or
wrote, which the self-coding test uses to check that an ability really did its job."""
from pathlib import Path

MAX_BYTES = 1_000_000  # one text file; plenty for notes, small enough that a runaway loop can't fill the disk


class Workspace:
    def __init__(self, root, confirm=lambda question: False):
        self.root = Path(root).resolve()
        self.confirm = confirm
        self.created = []

    def path(self, name: str = "") -> str:
        """Full path of a name inside the workspace (the workspace itself for '')."""
        return str(self._resolve(name))

    def make_folder(self, name: str) -> str:
        p = self._resolve(name, need_name=True)
        if p.is_file():
            raise ValueError(f"there's already a file called '{p.name}' there")
        if not p.exists():
            p.mkdir(parents=True)
            self.created.append(str(p))
        return str(p)

    def write(self, name: str, text: str) -> str:
        """Create or replace a text file; replacing an existing one asks the user first."""
        p = self._writable(name, text)
        if p.exists() and not self.confirm(f"'{p.name}' already exists. Replace it?"):
            raise PermissionError(f"the user chose not to replace '{p.name}'")
        p.write_text(str(text), encoding="utf-8")
        self.created.append(str(p))
        return str(p)

    def append(self, name: str, text: str) -> str:
        """Add to the end of a text file, creating it if needed."""
        p = self._writable(name, text)
        if p.exists() and p.stat().st_size + len(str(text).encode("utf-8")) > MAX_BYTES:
            raise ValueError("that file would get too big")
        with p.open("a", encoding="utf-8") as f:
            f.write(str(text))
        self.created.append(str(p))
        return str(p)

    def read(self, name: str) -> str:
        p = self._resolve(name, need_name=True)
        if not p.is_file():
            raise FileNotFoundError(f"there's no file called '{name}' in the workspace")
        return p.read_text(encoding="utf-8", errors="replace")[:MAX_BYTES]

    def exists(self, name: str) -> bool:
        return self._resolve(name).exists()

    def list(self, folder: str = "") -> list[str]:
        """Names in a workspace folder; folders end with a slash."""
        p = self._resolve(folder)
        if not p.is_dir():
            raise FileNotFoundError(f"there's no folder called '{folder}' in the workspace")
        return sorted(c.name + ("/" if c.is_dir() else "") for c in p.iterdir())

    def _writable(self, name: str, text) -> Path:
        p = self._resolve(name, need_name=True)
        if p.is_dir():
            raise ValueError(f"'{p.name}' is a folder, not a file")
        if len(str(text).encode("utf-8")) > MAX_BYTES:
            raise ValueError("that's too much text for one file")
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _resolve(self, name: str, need_name=False) -> Path:
        name = str(name or "").strip().strip("\"'")
        if need_name and not name:
            raise ValueError("a file or folder name is needed")
        if Path(name).is_absolute() or Path(name).drive:
            raise PermissionError(f"I can only work inside {self.root}, not at '{name}'")
        self.root.mkdir(parents=True, exist_ok=True)
        p = (self.root / name).resolve()
        if p != self.root and self.root not in p.parents:
            raise PermissionError(f"'{name}' is outside the workspace {self.root}")
        return p
