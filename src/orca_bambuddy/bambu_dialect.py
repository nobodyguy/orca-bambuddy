"""Translate Bambuddy printer data into the Bambu-style JSON OrcaSlicer's device layer reads.

OrcaSlicer 2.5's printer-agent contract is Bambu-shaped: DeviceManager parses LAN
messages as Bambu MQTT payloads ({"print": {"command": "push_status", ...}}) and
discovery as Bambu SSDP records. Field names below were taken from the OrcaSlicer
2.5.0-alpha parsers (DeviceManager.cpp, DeviceCore/Dev*.cpp) and the built-in
Moonraker agent, which does the same translation for Klipper printers.
"""

from __future__ import annotations

import json

from .matching import normalize_model

AGENT_ID = "bambuddy"
DEV_ID_PREFIX = "bambuddy-"

# Bambuddy short model name -> model_id of OrcaSlicer's Bambu Lab printer profiles
# (resources/profiles/BBL/machine/*.json). DeviceManager compares it with the preset.
PROFILE_MODEL_IDS = {
    "X1C": "BL-P001",
    "X1": "BL-P002",
    "X1E": "C13",
    "P1P": "C11",
    "P1S": "C12",
    "P2S": "N7",
    "A1": "N2S",
    "A1 Mini": "N1",
    "A2L": "N9",
    "H2C": "O1C2",
    "H2D": "O1D",
    "H2D Pro": "O1E",
    "H2S": "O1S",
    "X2D": "N6",
}

# OrcaSlicer's SendingPrintJobStage (slic3r/Utils/bambu_networking.hpp).
STAGE_CREATE, STAGE_UPLOAD, STAGE_WAITING, STAGE_SENDING, STAGE_RECORD, STAGE_WAIT_PRINTER, STAGE_FINISHED, STAGE_ERROR = range(8)

# Status codes the host understands (bambu_networking.hpp / IPrinterAgent).
OK = 0
ERR_FAILED = -1
ERR_CANCELED = -18
ERR_UPLOAD_FAILED = -4020  # BAMBU_NETWORK_ERR_PRINT_LP_UPLOAD_FTP_FAILED
ERR_START_FAILED = -4030  # BAMBU_NETWORK_ERR_PRINT_LP_PUBLISH_MSG_FAILED
ERR_SEND_UPLOAD_FAILED = -5010  # BAMBU_NETWORK_ERR_PRINT_SG_UPLOAD_FTP_FAILED
ERR_NOT_SUPPORTED = -7010


def dev_id(printer: dict) -> str:
    """OrcaSlicer's device id for a Bambuddy printer. The serial number keeps it stable and
    unique across Bambuddy servers; OrcaSlicer stores the access code per device id."""
    serial = (printer.get("serial_number") or "").strip()
    return DEV_ID_PREFIX + (serial or f"id{int(printer['id'])}")


def find_printer(printers: list[dict], dev: str) -> dict | None:
    return next((p for p in printers if dev_id(p) == dev), None)


def model_code(model: str | None) -> str:
    short = normalize_model(model)
    return PROFILE_MODEL_IDS.get(short or "", short or "")


def ssdp_record(printer: dict, host: str) -> str:
    """A discovery record for one Bambuddy printer (GUI_App's on_ssdp_msg format)."""
    return json.dumps(
        {
            "dev_id": dev_id(printer),
            "dev_name": printer.get("name") or f"Printer {printer['id']}",
            # The address the device list shows and connect_printer() receives. The agent
            # never talks to the printer itself, only to Bambuddy.
            "dev_ip": host,
            "dev_type": model_code(printer.get("model")),
            "dev_signal": "0",
            "connect_type": "lan",
            "bind_state": "free",
            "sec_link": "secure",
            "ssdp_version": "v1",
            "connection_name": "Bambuddy",
        }
    )


def version_info(server_version: str) -> str:
    return json.dumps(
        {
            "info": {
                "command": "get_version",
                "result": "success",
                "module": [{"name": "ota", "sw_ver": server_version, "product_name": "Bambuddy"}],
            }
        }
    )


def access_code_reply(code: str) -> str:
    return json.dumps({"system": {"command": "get_access_code", "access_code": code}})


def _num(value, default=None):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def is_dual(printer: dict | None, status: dict | None) -> bool:
    """A two-hotend printer (H2D, H2D Pro, H2C). Bambuddy's printer list has nozzle_count;
    the status names the slot each hotend is fed from."""
    if _num((printer or {}).get("nozzle_count"), 1) >= 2:
        return True
    return len((status or {}).get("extruder_slots") or {}) >= 2


# OrcaSlicer's AMS type ids (DevFilaSystem.cpp): 1 AMS, 3 AMS 2 Pro (n3f), 4 AMS HT (n3s).
AMS_TYPES = {"ams": 1, "n3f": 3, "n3s": 4}


def _ams_info(unit: dict, extruder_map: dict) -> str:
    """ams[].info of a dual-hotend printer: the type in bits 0-3, the hotend it feeds in
    bits 8-11. OrcaSlicer drops a unit whose hotend is 0xE, so it is never sent."""
    kind = AMS_TYPES.get(unit.get("module_type") or "") or (4 if unit.get("is_ams_ht") else 1)
    extruder = extruder_map.get(str(unit.get("id", 0)))
    extruder = extruder if extruder in (0, 1) else 0
    return format(kind | extruder << 8, "x")


def _ams(status: dict, dual: bool = False) -> dict | None:
    units = status.get("ams") or []
    if not units:
        return None
    ams_list, exist_bits, tray_bits = [], 0, 0
    for unit in units:
        uid = int(unit.get("id", 0))
        trays = []
        for tray in unit.get("tray") or []:
            tid = int(tray.get("id", 0))
            loaded = bool(tray.get("tray_type"))
            if loaded and uid < 4:
                tray_bits |= 1 << (uid * 4 + tid)
            entry = {"id": str(tid)}
            if loaded:
                entry.update(
                    {
                        "tray_type": tray.get("tray_type") or "",
                        "tray_color": (tray.get("tray_color") or "FFFFFFFF").upper(),
                        "tray_info_idx": tray.get("tray_info_idx") or "",
                        "tray_sub_brands": tray.get("tray_sub_brands") or "",
                        "tray_id_name": tray.get("tray_id_name") or "",
                        "remain": int(tray.get("remain") or 0),
                        "nozzle_temp_min": str(tray.get("nozzle_temp_min") or ""),
                        "nozzle_temp_max": str(tray.get("nozzle_temp_max") or ""),
                    }
                )
            trays.append(entry)
        if uid < 4:
            exist_bits |= 1 << uid
        entry = {"id": str(uid), "humidity": str(unit.get("humidity") or ""), "temp": str(unit.get("temp") or ""), "tray": trays}
        if dual:
            entry["info"] = _ams_info(unit, status.get("ams_extruder_map") or {})
        ams_list.append(entry)
    return {
        "ams": ams_list,
        "ams_exist_bits": format(exist_bits, "x"),
        "tray_exist_bits": format(tray_bits, "x"),
        "tray_now": str(status.get("tray_now", 255)),
        "version": 1,
    }


def _packed_temp(current, target) -> int:
    return max(0, round(_num(target, 0))) << 16 | max(0, round(_num(current, 0)))


def _slot_code(slot: dict) -> int:
    """(ams_id << 8) | slot_id, 0xFFFF for a hotend not fed from any slot (Bambu's encoding)."""
    ams_id, slot_id = slot.get("ams_id"), slot.get("slot_id")
    if _num(ams_id) is None or _num(slot_id) is None:
        return 0xFFFF
    return (int(ams_id) & 0xFF) << 8 | int(slot_id) & 0xFF


def _nozzle(status: dict, hotend: int) -> dict:
    """device.nozzle.info entry. Bambuddy's nozzle_rack holds the printer's own type code
    (e.g. "HS01": high flow, hardened steel), from which OrcaSlicer reads the flow type;
    nozzles[] may hold a plain material name from the legacy field instead."""
    rack = next((n for n in status.get("nozzle_rack") or [] if n.get("id") == hotend), None)
    nozzles = status.get("nozzles") or []
    src = rack or (nozzles[hotend] if hotend < len(nozzles) else {}) or {}
    try:
        diameter = float(src.get("nozzle_diameter") or 0)
    except (TypeError, ValueError):
        diameter = 0.0
    kind = str(src.get("nozzle_type") or "")
    # "N/A" means an unknown flow type, which blocks printing in OrcaSlicer's print dialog.
    return {"id": hotend, "diameter": diameter, "type": "" if kind == "N/A" else kind}


def _extruder(status: dict, hotend: int) -> dict:
    """device.extruder.info entry. Bambuddy names the right hotend (id 0) nozzle_2 and the
    left one (id 1) nozzle. OrcaSlicer reads every key, so none may be missing."""
    temps = status.get("temperatures") or {}
    prefix = "nozzle_2" if hotend == 0 else "nozzle"
    slot = (status.get("extruder_slots") or {}).get(str(hotend)) or {}
    code = _slot_code(slot)
    return {
        "id": hotend,
        "filam_bak": [],
        "info": 1 << 3 | (1 << 1 if slot.get("has_filament") else 0),  # nozzle present, filament loaded
        "temp": _packed_temp(temps.get(prefix), temps.get(prefix + "_target")),
        "spre": code,
        "snow": code,
        "star": code,
        "stat": 0,
        "hnow": hotend,
    }


def _external_spool(status: dict, tray_id: int) -> dict:
    """vir_slot entry: 255 feeds the right hotend, 254 the left one."""
    tray = next((t for t in status.get("vt_tray") or [] if _num(t.get("id")) == tray_id), None) or {}
    entry = {"id": str(tray_id)}
    if tray.get("tray_type"):
        entry.update(
            {
                "tray_type": tray.get("tray_type") or "",
                "tray_color": (tray.get("tray_color") or "FFFFFFFF").upper(),
                "tray_info_idx": tray.get("tray_info_idx") or "",
                "tray_sub_brands": tray.get("tray_sub_brands") or "",
                "nozzle_temp_min": str(tray.get("nozzle_temp_min") or ""),
                "nozzle_temp_max": str(tray.get("nozzle_temp_max") or ""),
            }
        )
    return entry


def dual_fields(status: dict) -> dict:
    """What makes OrcaSlicer show a printer with two hotends (DeviceManager parse_new_info).

    OrcaSlicer reads print.device only when cfg, fun, aux and stat are all present. Empty
    strings leave its defaults alone; aux bits 12-13 = 1 report the storage as present, so
    LAN printing is not blocked. The extruder count is bits 0-3 of device.extruder.state.
    Both external spools must be listed, or the AMS panel of a two-hotend printer fails.
    """
    active = 1 if status.get("active_extruder") == 1 else 0
    return {
        "cfg": "",
        "fun": "",
        "aux": "1000",
        "stat": "",
        "device": {
            "extruder": {"state": 2 | active << 4 | active << 8, "info": [_extruder(status, 0), _extruder(status, 1)]},
            "nozzle": {"info": [_nozzle(status, 0), _nozzle(status, 1)]},
        },
        "vir_slot": [_external_spool(status, 255), _external_spool(status, 254)],
    }


def push_status(status: dict, sequence: int, dual: bool = False) -> str:
    """A full push_status message from Bambuddy's GET /printers/{id}/status."""
    temps = status.get("temperatures") or {}
    state = status.get("state") if status.get("connected") else None
    p: dict = {
        "command": "push_status",
        "msg": 0,
        "sequence_id": str(sequence),
        "gcode_state": state or "OFFLINE",
        "print_type": "local",
        # The job goes to Bambuddy, which stores it and uploads it to the printer itself,
        # so OrcaSlicer's "storage present" check is about Bambuddy, not the SD card.
        "sdcard": True,
        # Without it OrcaSlicer's Send dialog refuses: "does not support sending to printer storage".
        "support_send_to_sd": True,
        "mc_print_error_code": "0",
        "print_error": 0,
        "mc_percent": int(_num(status.get("progress"), 0)),
        "mc_remaining_time": int(_num(status.get("remaining_time"), 0)),
        "layer_num": int(_num(status.get("layer_num"), 0)),
        "total_layer_num": int(_num(status.get("total_layers"), 0)),
        "subtask_name": status.get("subtask_name") or "",
        "gcode_file": status.get("gcode_file") or "",
        "stg_cur": int(_num(status.get("stg_cur"), -1)),
        "wifi_signal": f"{status['wifi_signal']}dBm" if _num(status.get("wifi_signal")) is not None else "",
    }
    for src, dst in (
        ("nozzle", "nozzle_temper"),
        ("nozzle_target", "nozzle_target_temper"),
        ("bed", "bed_temper"),
        ("bed_target", "bed_target_temper"),
        ("chamber", "chamber_temper"),
    ):
        if _num(temps.get(src)) is not None:
            p[dst] = float(temps[src])
    nozzles = status.get("nozzles") or []
    if nozzles and nozzles[0].get("nozzle_diameter"):
        p["nozzle_diameter"] = str(nozzles[0]["nozzle_diameter"])
        p["nozzle_type"] = nozzles[0].get("nozzle_type") or "stainless_steel"
    if not dual:
        # The extruder's filament sensor. Without it OrcaSlicer draws the AMS path as unloaded
        # and marks no slot (StatusPanel update_ams, DeviceManager.cpp hw_switch_state; two
        # hotends report it in device.extruder instead). Bambuddy does not pass the sensor on,
        # but tray_now is 255 exactly when nothing is loaded.
        p["hw_switch_state"] = 0 if _num(status.get("tray_now"), 255) == 255 else 1
        # One external spool, which OrcaSlicer reads from print.vt_tray (DeviceManager.cpp,
        # parse_vt_tray). Bambuddy lists it as id 254, like the printer reports it.
        ids = [t.get("id") for t in status.get("vt_tray") or []]
        if ids:
            p["vt_tray"] = _external_spool(status, 254 if 254 in ids else ids[0])
    ams = _ams(status, dual)
    if ams:
        p["ams"] = ams
    if dual:
        p.update(dual_fields(status))
    return json.dumps({"print": p})


def command_of(json_str: str) -> tuple[str, str]:
    """(section, command) of a Bambu-style command, e.g. ("info", "get_version")."""
    try:
        msg = json.loads(json_str)
    except ValueError:
        return ("", "")
    for section in ("info", "system", "print", "pushing", "camera", "xcam"):
        body = msg.get(section)
        if isinstance(body, dict) and isinstance(body.get("command"), str):
            return (section, body["command"])
    return ("", "")


def _tristate(value: int | None, flag: bool | None) -> str:
    # OrcaSlicer: auto_* ints are 0 = off, 1 = on, 2 = auto; older paths only set the bool.
    if value in (0, 1, 2):
        return {0: "off", 1: "on", 2: "auto"}[value]
    return "on" if flag else "off"


def queue_options(params) -> dict:
    """Bambuddy queue options from OrcaSlicer's PrintParams (print dialog choices)."""
    opts = {
        "bed_levelling": _tristate(getattr(params, "auto_bed_leveling", None), getattr(params, "task_bed_leveling", None)),
        "flow_cali": _tristate(getattr(params, "auto_flow_cali", None), getattr(params, "task_flow_cali", None)),
        "nozzle_offset_cali": _tristate(getattr(params, "auto_offset_cali", None), None),
        "vibration_cali": bool(getattr(params, "task_vibration_cali", True)),
        "layer_inspect": bool(getattr(params, "task_layer_inspect", False)),
        "timelapse": bool(getattr(params, "task_record_timelapse", False)),
    }
    # Only a mapping onto real trays is worth keeping. When OrcaSlicer could not see the
    # AMS (printer offline or busy), Bambuddy maps the filaments itself at dispatch.
    mapping = parse_ams_mapping(getattr(params, "ams_mapping", ""), getattr(params, "ams_mapping2", ""))
    if mapping and any(v >= 0 for v in mapping):
        opts["ams_mapping"] = mapping
        opts["use_ams"] = bool(getattr(params, "task_use_ams", True))
    return opts


EXTERNAL_SPOOLS = (254, 255)


def parse_ams_mapping(raw: str, raw2: str = "") -> list[int] | None:
    """OrcaSlicer sends a JSON list of global tray ids per filament (-1 = unused), the same
    format Bambuddy's queue takes. An external spool is -1 there too; ams_mapping2 (a list of
    {ams_id, slot_id}) names it as ams_id 255 (right/only spool) or 254 (left spool) with
    slot_id 0, which is the id Bambuddy's queue expects. An unmapped filament is 255/255."""
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not (isinstance(value, list) and value and all(isinstance(v, int) for v in value)):
        return None
    try:
        detail = json.loads(raw2) if raw2 else []
    except ValueError:
        detail = []
    if isinstance(detail, list) and len(detail) == len(value):
        for i, item in enumerate(detail):
            if value[i] == -1 and isinstance(item, dict) and item.get("ams_id") in EXTERNAL_SPOOLS and item.get("slot_id") == 0:
                value[i] = item["ams_id"]
    return value
