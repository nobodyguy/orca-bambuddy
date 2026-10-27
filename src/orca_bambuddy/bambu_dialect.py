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
    return value if isinstance(value, (int, float)) else default


def _ams(status: dict) -> dict | None:
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
        ams_list.append(
            {"id": str(uid), "humidity": str(unit.get("humidity") or ""), "temp": str(unit.get("temp") or ""), "tray": trays}
        )
    return {
        "ams": ams_list,
        "ams_exist_bits": format(exist_bits, "x"),
        "tray_exist_bits": format(tray_bits, "x"),
        "tray_now": str(status.get("tray_now", 255)),
        "version": 1,
    }


def push_status(status: dict, sequence: int) -> str:
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
    ams = _ams(status)
    if ams:
        p["ams"] = ams
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
    mapping = parse_ams_mapping(getattr(params, "ams_mapping", ""))
    if mapping and any(v >= 0 for v in mapping):
        opts["ams_mapping"] = mapping
        opts["use_ams"] = bool(getattr(params, "task_use_ams", True))
    return opts


def parse_ams_mapping(raw: str) -> list[int] | None:
    """OrcaSlicer sends a JSON list of global tray ids per filament (-1 = unused), the same
    format Bambuddy's queue takes."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if isinstance(value, list) and value and all(isinstance(v, int) for v in value):
        return value
    return None
