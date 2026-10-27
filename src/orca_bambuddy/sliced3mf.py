"""Read what the plugin needs from a sliced Bambu/Orca ``.gcode.3mf``.

Layout verified against files written by OrcaSlicer 2.5.0-alpha ("Export plate sliced
file" and the CLI's --export-3mf): Metadata/plate_<N>.gcode per sliced plate,
Metadata/slice_info.config (XML, per-plate metadata) and Metadata/project_settings.config
(JSON, the full print/printer/filament configuration).
"""

from __future__ import annotations

import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .matching import normalize_model

_PLATE_GCODE = re.compile(r"^Metadata/plate_(\d+)\.gcode$")
# Characters Bambu printers' SD cards (FAT32/exFAT) cannot store; Bambuddy rejects them.
INVALID_FILENAME_CHARS = '<>:"/\\|?*'


class NotASlicedFile(Exception):
    pass


@dataclass
class Plate:
    index: int
    prediction_s: int | None = None
    weight_g: float | None = None


@dataclass
class SlicedFile:
    path: str
    filename: str
    size: int
    mtime: float
    plates: list[Plate] = field(default_factory=list)
    printer_model: str | None = None  # e.g. "Bambu Lab X1 Carbon"
    printer_model_id: str | None = None  # e.g. "BL-P001"; empty in some exports
    printer_preset: str | None = None  # printer_settings_id, e.g. "Bambu Lab X1 Carbon 0.4 nozzle"
    nozzle: str | None = None

    @property
    def sliced_model(self) -> str | None:
        """Model short name the way Bambuddy derives sliced_for_model: model id first."""
        return normalize_model(self.printer_model_id) or normalize_model(self.printer_model)

    def summary(self) -> dict:
        return {
            "path": self.path,
            "filename": self.filename,
            "size": self.size,
            "mtime": self.mtime,
            "plates": [p.index for p in self.plates],
            "sliced_model": self.sliced_model,
            "printer_preset": self.printer_preset,
            "nozzle": self.nozzle,
            "prediction_s": sum(p.prediction_s or 0 for p in self.plates) or None,
            "weight_g": round(sum(p.weight_g or 0 for p in self.plates), 2) or None,
        }


def filename_problem(name: str) -> str | None:
    bad = sorted({c for c in name if c in INVALID_FILENAME_CHARS or ord(c) < 0x20})
    if bad:
        return f"The file name contains characters printers cannot store: {' '.join(bad)}"
    if name.endswith((" ", ".")):
        return "The file name must not end with a space or a dot."
    if len(name.encode("utf-8")) > 255:
        return "The file name is longer than 255 bytes."
    return None


def _first(value):
    return value[0] if isinstance(value, list) and value else value


def inspect(path: str) -> SlicedFile:
    """Parse a sliced 3MF. Raises NotASlicedFile with a user-facing reason."""
    try:
        stat = os.stat(path)
        zf = zipfile.ZipFile(path)
    except FileNotFoundError:
        raise NotASlicedFile(f"File not found: {path}") from None
    except (OSError, zipfile.BadZipFile) as e:
        raise NotASlicedFile(f"Not a 3MF file: {os.path.basename(path)} ({e})") from None
    with zf:
        names = zf.namelist()
        plate_ids = sorted(int(m.group(1)) for n in names if (m := _PLATE_GCODE.match(n)))
        if not plate_ids:
            raise NotASlicedFile(
                f"{os.path.basename(path)} contains no G-code. Slice the plate and use "
                "File > Export > Export plate sliced file (.gcode.3mf)."
            )
        info = SlicedFile(path=path, filename=os.path.basename(path), size=stat.st_size, mtime=stat.st_mtime)
        plates = {i: Plate(i) for i in plate_ids}

        if "Metadata/slice_info.config" in names:
            try:
                root = ET.fromstring(zf.read("Metadata/slice_info.config"))
            except ET.ParseError:
                root = None
            for plate_el in root.iter("plate") if root is not None else ():
                meta = {m.get("key"): m.get("value") for m in plate_el.findall("metadata")}
                try:
                    plate = plates.get(int(meta.get("index", "0")))
                except ValueError:
                    plate = None
                if plate is None:
                    continue
                if meta.get("printer_model_id"):
                    info.printer_model_id = meta["printer_model_id"]
                try:
                    plate.prediction_s = int(float(meta["prediction"])) if meta.get("prediction") else None
                    plate.weight_g = float(meta["weight"]) if meta.get("weight") else None
                except ValueError:
                    pass

        if "Metadata/project_settings.config" in names:
            try:
                settings = json.loads(zf.read("Metadata/project_settings.config"))
            except ValueError:
                settings = {}
            info.printer_model = _first(settings.get("printer_model")) or None
            info.printer_preset = _first(settings.get("printer_settings_id")) or None
            info.nozzle = _first(settings.get("nozzle_diameter")) or None
        info.plates = [plates[i] for i in plate_ids]
        return info


def recent_exports(folder: str, limit: int = 20) -> list[tuple[str, float]]:
    """Newest ``*.gcode.3mf`` in ``folder`` as (path, mtime), newest first."""
    found = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if entry.is_file() and entry.name.lower().endswith(".gcode.3mf"):
                    found.append((entry.path, entry.stat().st_mtime))
    except OSError:
        return []
    found.sort(key=lambda x: x[1], reverse=True)
    return found[:limit]
