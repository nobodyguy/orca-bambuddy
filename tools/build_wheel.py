"""Build the plugin wheel with the standard library only: python3 tools/build_wheel.py

Writes dist/orca_bambuddy_any.whl, the file users install through OrcaSlicer's Plugins
dialog (Install local plugin) and the file published to OrcaCloud. OrcaSlicer reads the
plugin's name, version and description from METADATA and imports the package named in
top_level.txt; it does not parse the file name.

The file name deliberately carries no version: OrcaSlicer keys a locally installed plugin by
its file name, and that key labels the saved settings, the storage folder and the granted
permissions, so a versioned name would lose them on every update. "_any" is the target-OS
suffix OrcaCloud requires on uploads (any OS: the wheel is pure Python).
"""

import base64
import hashlib
import pathlib
import re
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "orca_bambuddy"
VERSION = re.search(r'__version__ = "([^"]+)"', (PKG / "orca_adapter.py").read_text()).group(1)
DIST = f"orca_bambuddy-{VERSION}"

METADATA = f"""Metadata-Version: 2.1
Name: orca-bambuddy
Version: {VERSION}
Summary: Send sliced plates from OrcaSlicer to a Bambuddy server (print now, queue or store) and watch your printers.
Author: Jan Gnip
License: MIT
Requires-Python: >=3.10
"""

WHEEL = """Wheel-Version: 1.0
Generator: orca-bambuddy build_wheel.py
Root-Is-Purelib: true
Tag: py3-none-any
"""


def record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


def main() -> pathlib.Path:
    sources = sorted(PKG.glob("*.py")) + sorted(PKG.glob("assets/*"))
    files = {f"orca_bambuddy/{p.relative_to(PKG).as_posix()}": p.read_bytes() for p in sources}
    files[f"{DIST}.dist-info/METADATA"] = METADATA.encode()
    files[f"{DIST}.dist-info/WHEEL"] = WHEEL.encode()
    files[f"{DIST}.dist-info/top_level.txt"] = b"orca_bambuddy\n"
    record_name = f"{DIST}.dist-info/RECORD"
    record = "\n".join(record_line(n, d) for n, d in files.items()) + f"\n{record_name},,\n"
    out = ROOT / "dist" / "orca_bambuddy_any.whl"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        zf.writestr(record_name, record)
    print(out)
    return out


if __name__ == "__main__":
    main()
