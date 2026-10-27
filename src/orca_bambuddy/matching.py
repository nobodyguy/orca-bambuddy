"""Match a sliced file to a Bambuddy printer.

Signals, in order of trust:
  1. the model the file was sliced for (slice_info printer_model_id, else the
     printer_model setting) - always present in a Bambu/Orca .gcode.3mf;
  2. the user's earlier choice for the same printer preset;
  3. the printer preset's print host address equal to a Bambuddy printer's IP.

The model tables mirror Bambuddy 1.2.5.7 backend/app/utils/printer_models.py so the
plugin agrees with the server's own cross-model gate (is_gcode_compatible).
"""

from __future__ import annotations

from dataclasses import dataclass

MODEL_NAMES = {
    "Bambu Lab X1 Carbon": "X1C",
    "Bambu Lab X1": "X1",
    "Bambu Lab X1E": "X1E",
    "Bambu Lab P1S": "P1S",
    "Bambu Lab P1P": "P1P",
    "Bambu Lab P2S": "P2S",
    "Bambu Lab A1": "A1",
    "Bambu Lab A1 Mini": "A1 Mini",
    "Bambu Lab A1 mini": "A1 Mini",
    "Bambu Lab A1M": "A1 Mini",
    "Bambu Lab H2D": "H2D",
    "Bambu Lab H2D Pro": "H2D Pro",
    "Bambu Lab H2C": "H2C",
    "Bambu Lab H2S": "H2S",
    "Bambu Lab X2D": "X2D",
    "Bambu Lab A2L": "A2L",
}

MODEL_IDS = {
    "BL-P001": "X1C",
    "BL-P002": "X1",
    "C13": "X1E",
    "C11": "P1P",
    "C12": "P1S",
    "P1P": "P1P",
    "P1S": "P1S",
    "N7": "P2S",
    "P2S": "P2S",
    "N6": "X2D",
    "N9": "A2L",
    "A11": "A1",
    "A12": "A1 Mini",
    "N1": "A1 Mini",
    "N2S": "A1",
    "A04": "A1 Mini",
    "O1D": "H2D",
    "O1E": "H2D Pro",
    "O2D": "H2D Pro",
    "O1C": "H2C",
    "O1C2": "H2C",
    "O1S": "H2S",
}

# G-code sliced for one member runs on the others (Bambuddy GCODE_COMPAT_FAMILIES).
COMPAT_FAMILIES = (frozenset(["X1", "X1C", "X1E", "P1P", "P1S"]),)

BUSY_STATES = {"RUNNING", "PAUSE", "PREPARE", "SLICING"}


def normalize_model(raw: str | None) -> str | None:
    """Any spelling of a Bambu model (name, internal code, short name) to its short name."""
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    if raw in MODEL_IDS:
        return MODEL_IDS[raw]
    if raw in MODEL_NAMES:
        return MODEL_NAMES[raw]
    return raw.replace("Bambu Lab ", "").strip() or None


def _key(model: str) -> str:
    return model.upper().replace(" ", "").replace("-", "")


def models_compatible(sliced_for: str | None, printer_model: str | None) -> bool | None:
    """True/False, or None when either side is unknown."""
    a, b = normalize_model(sliced_for), normalize_model(printer_model)
    if not a or not b:
        return None
    a, b = _key(a), _key(b)
    return a == b or any(a in fam and b in fam for fam in COMPAT_FAMILIES)


@dataclass
class Target:
    """One entry of the printer chooser."""

    id: str  # "p:<printer id>" or "m:<model>"
    label: str
    printer_id: int | None = None
    target_model: str | None = None
    model: str | None = None
    location: str | None = None
    online: bool | None = None
    state: str | None = None
    compatible: bool | None = None

    @property
    def idle(self) -> bool:
        return bool(self.online) and (self.state or "IDLE") not in BUSY_STATES

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "printer_id": self.printer_id,
            "target_model": self.target_model,
            "model": self.model,
            "location": self.location,
            "online": self.online,
            "state": self.state,
            "compatible": self.compatible,
        }


@dataclass
class Match:
    targets: list[Target]
    selected: str | None  # Target.id
    reason: str


def _host_of(address: str | None) -> str:
    if not address:
        return ""
    host = address.split("://", 1)[-1].split("/", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def build_targets(printers: list[dict], statuses: dict[int, dict], sliced_model: str | None) -> list[Target]:
    targets: list[Target] = []
    by_model: dict[str, list[Target]] = {}
    for p in printers:
        if not p.get("is_active", True):
            continue
        status = statuses.get(p["id"]) or {}
        t = Target(
            id=f"p:{p['id']}",
            label=p.get("name") or f"Printer {p['id']}",
            printer_id=p["id"],
            model=p.get("model"),
            location=p.get("location"),
            online=status.get("connected") if status else None,
            state=status.get("state"),
            compatible=models_compatible(sliced_model, p.get("model")),
        )
        targets.append(t)
        if t.compatible and p.get("model"):
            by_model.setdefault(p["model"], []).append(t)

    # Bambuddy can target "any printer of this model": the scheduler hands the job to the
    # first one that becomes idle. Offer it when two or more compatible printers share a
    # model. The raw stored model string is sent; the server matches it exactly.
    for model, group in by_model.items():
        if len(group) >= 2 and normalize_model(model) == model:
            targets.append(
                Target(
                    id=f"m:{model}",
                    label=f"Any {model} (first free)",
                    target_model=model,
                    model=model,
                    online=any(t.online for t in group),
                    state="IDLE" if any(t.idle for t in group) else "RUNNING",
                    compatible=True,
                )
            )

    rank = {True: 0, None: 1, False: 2}
    targets.sort(key=lambda t: (rank[t.compatible], not t.idle, not t.online, t.target_model is not None, t.label.lower()))
    return targets


def choose(
    printers: list[dict],
    statuses: dict[int, dict],
    sliced_model: str | None,
    *,
    remembered: str | None = None,
    host_hint: str | None = None,
) -> Match:
    """Rank the printers and preselect the most likely target."""
    targets = build_targets(printers, statuses, sliced_model)
    usable = [t for t in targets if t.compatible is not False]

    if remembered and any(t.id == remembered for t in usable):
        return Match(targets, remembered, "Last printer used with this printer preset.")

    host = _host_of(host_hint)
    if host:
        for p in printers:
            if p.get("ip_address") == host:
                t = next((t for t in usable if t.printer_id == p["id"]), None)
                if t:
                    return Match(targets, t.id, "The printer preset's address matches this printer.")

    compatible = [t for t in targets if t.compatible and t.printer_id is not None]
    if len(compatible) == 1:
        return Match(targets, compatible[0].id, f"The only {sliced_model} printer in Bambuddy.")
    if len(compatible) > 1:
        idle = [t for t in compatible if t.idle]
        if idle:
            return Match(targets, idle[0].id, f"{len(compatible)} matching printers; this one is idle.")
        any_model = next((t for t in targets if t.target_model and t.compatible), None)
        if any_model:
            return Match(targets, any_model.id, f"{len(compatible)} matching printers, all busy: the first free one.")
        return Match(targets, compatible[0].id, f"{len(compatible)} matching printers.")
    if sliced_model is None and len(usable) == 1:
        return Match(targets, usable[0].id, "The only printer in Bambuddy.")
    if sliced_model:
        return Match(targets, None, f"No printer in Bambuddy matches the sliced model ({sliced_model}).")
    return Match(targets, None, "The file does not say which printer it was sliced for; choose one.")


def check_target(sliced_model: str | None, target: Target | None, action: str) -> tuple[str, str] | None:
    """('block'|'warn', message) for an obvious problem with sending to ``target``."""
    if action == "store" or target is None:
        return None
    if target.compatible is False:
        return (
            "block",
            f"This file was sliced for {sliced_model}, but {target.label} is a {target.model}. "
            "Select the matching printer preset in OrcaSlicer and slice again.",
        )
    if target.compatible is None and target.target_model is None:
        return ("warn", f"Cannot verify that this file fits {target.label}; its model is unknown.")
    if target.online is False and action == "print":
        return ("warn", f"{target.label} is offline. The job waits in the Bambuddy queue until it is back.")
    return None
