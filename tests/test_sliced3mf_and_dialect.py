import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from fakes import FIXTURES, make_sliced_3mf

from orca_bambuddy import bambu_dialect as bd
from orca_bambuddy import sliced3mf
from orca_bambuddy.service import job_name_from_params


class SlicedFileTest(unittest.TestCase):
    def test_orcaslicer_export(self):
        info = sliced3mf.inspect(os.path.join(FIXTURES, "cube_x1c.gcode.3mf"))
        self.assertEqual([p.index for p in info.plates], [1])
        self.assertEqual(info.sliced_model, "X1C")
        self.assertEqual(info.printer_preset, "Bambu Lab X1 Carbon 0.4 nozzle")
        self.assertGreater(info.plates[0].prediction_s, 0)
        info = sliced3mf.inspect(os.path.join(FIXTURES, "cube_a1m.gcode.3mf"))
        self.assertEqual(info.sliced_model, "A1 Mini")

    def test_model_id_wins_over_model_name(self):
        with tempfile.TemporaryDirectory() as d:
            info = sliced3mf.inspect(make_sliced_3mf(os.path.join(d, "a.gcode.3mf"), printer_model="Bambu Lab X1 Carbon", model_id="C12"))
        self.assertEqual(info.sliced_model, "P1S")

    def test_all_plates_export(self):
        with tempfile.TemporaryDirectory() as d:
            info = sliced3mf.inspect(make_sliced_3mf(os.path.join(d, "all.gcode.3mf"), plates=(1, 2, 4)))
        self.assertEqual([p.index for p in info.plates], [1, 2, 4])
        self.assertEqual(info.summary()["prediction_s"], 600 + 1200 + 2400)

    def test_rejects_unsliced_and_non_zip(self):
        with tempfile.TemporaryDirectory() as d:
            project = os.path.join(d, "project.3mf")
            with __import__("zipfile").ZipFile(project, "w") as zf:
                zf.writestr("3D/3dmodel.model", "<model/>")
            with self.assertRaises(sliced3mf.NotASlicedFile) as e:
                sliced3mf.inspect(project)
            self.assertIn("Export plate sliced file", str(e.exception))
            text = os.path.join(d, "x.gcode.3mf")
            with open(text, "w") as f:
                f.write("G28")
            with self.assertRaises(sliced3mf.NotASlicedFile):
                sliced3mf.inspect(text)

    def test_filename_rules(self):
        self.assertIsNone(sliced3mf.filename_problem("Benchy v2.gcode.3mf"))
        self.assertIn(":", sliced3mf.filename_problem("a:b.gcode.3mf"))
        self.assertIsNotNone(sliced3mf.filename_problem("name."))

    def test_recent_exports(self):
        with tempfile.TemporaryDirectory() as d:
            old = make_sliced_3mf(os.path.join(d, "old.gcode.3mf"))
            new = make_sliced_3mf(os.path.join(d, "new.gcode.3mf"))
            os.utime(old, (1, 1))
            open(os.path.join(d, "notes.txt"), "w").close()
            self.assertEqual([p for p, _ in sliced3mf.recent_exports(d)], [new, old])
        self.assertEqual(sliced3mf.recent_exports("/no/such/folder"), [])


class DialectTest(unittest.TestCase):
    P = {"id": 7, "name": "X1C Office", "model": "X1C", "serial_number": "00M09A350100123"}

    def test_dev_id_uses_serial(self):
        self.assertEqual(bd.dev_id(self.P), "bambuddy-00M09A350100123")
        self.assertEqual(bd.dev_id({"id": 7, "serial_number": ""}), "bambuddy-id7")
        self.assertIs(bd.find_printer([self.P], "bambuddy-00M09A350100123"), self.P)
        self.assertIsNone(bd.find_printer([self.P], "bambuddy-other"))

    def test_ssdp_record(self):
        rec = json.loads(bd.ssdp_record(self.P, "bambuddy.local:8000"))
        self.assertEqual(rec["dev_type"], "BL-P001")  # what OrcaSlicer's X1C profile calls model_id
        self.assertEqual((rec["connect_type"], rec["bind_state"]), ("lan", "free"))
        self.assertEqual(rec["dev_ip"], "bambuddy.local:8000")

    def test_model_codes_match_orca_profiles(self):
        self.assertEqual(bd.model_code("A1 Mini"), "N1")
        self.assertEqual(bd.model_code("P1S"), "C12")
        self.assertEqual(bd.model_code("H2D"), "O1D")
        self.assertEqual(bd.model_code("O1D"), "O1D")

    def test_push_status_from_bambuddy_status(self):
        st = {
            "connected": True, "state": "RUNNING", "progress": 42.5, "remaining_time": 37, "layer_num": 12,
            "total_layers": 200, "subtask_name": "Benchy", "temperatures": {"nozzle": 220.4, "nozzle_target": 220, "bed": 60, "bed_target": 60},
            "nozzles": [{"nozzle_type": "hardened_steel", "nozzle_diameter": "0.4"}], "tray_now": 2,
            "ams": [{"id": 0, "humidity": 3, "temp": 25.1, "tray": [
                {"id": 0, "tray_type": "PLA", "tray_color": "ff0000ff", "tray_info_idx": "GFA00", "remain": 80},
                {"id": 1, "tray_type": None}, {"id": 2, "tray_type": "PETG", "tray_color": "00FF00FF"}]}],
        }
        p = json.loads(bd.push_status(st, 9))["print"]
        self.assertEqual((p["command"], p["msg"], p["sequence_id"]), ("push_status", 0, "9"))
        self.assertEqual((p["gcode_state"], p["mc_percent"], p["mc_remaining_time"]), ("RUNNING", 42, 37))
        self.assertEqual((p["layer_num"], p["total_layer_num"]), (12, 200))
        self.assertEqual((p["nozzle_temper"], p["bed_target_temper"]), (220.4, 60.0))
        self.assertEqual((p["nozzle_diameter"], p["nozzle_type"]), ("0.4", "hardened_steel"))
        self.assertTrue(p["sdcard"])
        ams = p["ams"]
        self.assertEqual((ams["ams_exist_bits"], ams["tray_exist_bits"], ams["tray_now"]), ("1", "5", "2"))
        trays = ams["ams"][0]["tray"]
        self.assertEqual(trays[0]["tray_color"], "FF0000FF")
        self.assertEqual(trays[1], {"id": "1"})  # empty slot

    def test_offline_printer(self):
        p = json.loads(bd.push_status({"connected": False, "state": "IDLE"}, 1))["print"]
        self.assertEqual(p["gcode_state"], "OFFLINE")

    def test_command_of(self):
        self.assertEqual(bd.command_of('{"info": {"command": "get_version"}}'), ("info", "get_version"))
        self.assertEqual(bd.command_of('{"print": {"command": "pause", "sequence_id": "1"}}'), ("print", "pause"))
        self.assertEqual(bd.command_of("not json"), ("", ""))

    def test_queue_options_from_print_dialog(self):
        params = SimpleNamespace(auto_bed_leveling=2, task_bed_leveling=False, auto_flow_cali=1, task_flow_cali=True,
                                 auto_offset_cali=0, task_vibration_cali=False, task_layer_inspect=True,
                                 task_record_timelapse=True, task_use_ams=True, ams_mapping="[2,-1,0]")
        self.assertEqual(bd.queue_options(params), {
            "bed_levelling": "auto", "flow_cali": "on", "nozzle_offset_cali": "off", "vibration_cali": False,
            "layer_inspect": True, "timelapse": True, "ams_mapping": [2, -1, 0], "use_ams": True})

    def test_unmapped_ams_is_left_to_bambuddy(self):
        params = SimpleNamespace(ams_mapping="[-1]", task_use_ams=False)
        opts = bd.queue_options(params)
        self.assertNotIn("ams_mapping", opts)
        self.assertNotIn("use_ams", opts)

    def test_job_name(self):
        self.assertEqual(job_name_from_params(SimpleNamespace(project_name="cube", filename="/tmp/x/.123.0.3mf")), "cube.gcode.3mf")
        self.assertEqual(job_name_from_params(SimpleNamespace(project_name="a:b/c?", filename="")), "a_b_c_.gcode.3mf")
        self.assertEqual(job_name_from_params(SimpleNamespace(project_name="", task_name="", filename="/t/plate.gcode.3mf")), "plate.gcode.3mf")


if __name__ == "__main__":
    unittest.main()
