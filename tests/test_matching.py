import unittest

import fakes  # noqa: F401 - puts src/ on sys.path

from orca_bambuddy import matching
from orca_bambuddy.matching import check_target, choose, models_compatible, normalize_model


def printer(pid, name, model, ip="", active=True):
    return {"id": pid, "name": name, "model": model, "ip_address": ip, "location": "", "is_active": active}


def status(state="IDLE", online=True):
    return {"connected": online, "state": state}


class NormalizeTest(unittest.TestCase):
    def test_spellings(self):
        self.assertEqual(normalize_model("Bambu Lab X1 Carbon"), "X1C")
        self.assertEqual(normalize_model("BL-P001"), "X1C")
        self.assertEqual(normalize_model("Bambu Lab A1 mini"), "A1 Mini")
        self.assertEqual(normalize_model("N1"), "A1 Mini")
        self.assertEqual(normalize_model("O1D"), "H2D")
        self.assertEqual(normalize_model("Bambu Lab Q9 Future"), "Q9 Future")
        self.assertIsNone(normalize_model(""))
        self.assertIsNone(normalize_model(None))

    def test_compatibility_matches_bambuddy(self):
        self.assertTrue(models_compatible("X1C", "Bambu Lab X1 Carbon"))
        self.assertTrue(models_compatible("X1C", "P1S"))  # Bambuddy's X1/P1 family
        self.assertTrue(models_compatible("C12", "X1E"))
        self.assertFalse(models_compatible("X1C", "A1 Mini"))
        self.assertFalse(models_compatible("A1", "A1 Mini"))
        self.assertFalse(models_compatible("H2D", "H2D Pro"))
        self.assertIsNone(models_compatible(None, "X1C"))


class ChooseTest(unittest.TestCase):
    def test_single_matching_printer(self):
        ps = [printer(1, "X1C Office", "X1C"), printer(2, "A1 Desk", "A1 Mini")]
        m = choose(ps, {1: status(), 2: status()}, "X1C")
        self.assertEqual(m.selected, "p:1")
        self.assertEqual([t.id for t in m.targets][:1], ["p:1"])
        self.assertFalse(next(t for t in m.targets if t.id == "p:2").compatible)

    def test_prefers_idle_printer_of_several(self):
        ps = [printer(1, "X1C A", "X1C"), printer(2, "X1C B", "X1C")]
        m = choose(ps, {1: status("RUNNING"), 2: status("IDLE")}, "X1C")
        self.assertEqual(m.selected, "p:2")
        self.assertIn("idle", m.reason)
        self.assertIn("m:X1C", [t.id for t in m.targets])

    def test_all_busy_targets_any_printer_of_the_model(self):
        ps = [printer(1, "X1C A", "X1C"), printer(2, "X1C B", "X1C")]
        m = choose(ps, {1: status("RUNNING"), 2: status("PAUSE")}, "X1C")
        self.assertEqual(m.selected, "m:X1C")
        any_target = next(t for t in m.targets if t.id == "m:X1C")
        self.assertEqual((any_target.target_model, any_target.printer_id), ("X1C", None))

    def test_offline_printer_is_ranked_last(self):
        ps = [printer(1, "X1C A", "X1C"), printer(2, "X1C B", "X1C")]
        m = choose(ps, {1: status(online=False), 2: status("RUNNING")}, "X1C")
        printers_only = [t.id for t in m.targets if t.printer_id]
        self.assertEqual(printers_only, ["p:2", "p:1"])

    def test_remembered_choice_wins(self):
        ps = [printer(1, "X1C A", "X1C"), printer(2, "X1C B", "X1C")]
        m = choose(ps, {1: status(), 2: status("RUNNING")}, "X1C", remembered="p:2")
        self.assertEqual(m.selected, "p:2")

    def test_remembered_incompatible_printer_is_ignored(self):
        ps = [printer(1, "X1C A", "X1C"), printer(2, "A1 Desk", "A1 Mini")]
        m = choose(ps, {1: status(), 2: status()}, "X1C", remembered="p:2")
        self.assertEqual(m.selected, "p:1")

    def test_print_host_address_identifies_the_printer(self):
        ps = [printer(1, "X1C A", "X1C", ip="10.0.0.5"), printer(2, "X1C B", "X1C", ip="10.0.0.6")]
        m = choose(ps, {1: status(), 2: status()}, "X1C", host_hint="http://10.0.0.6:80")
        self.assertEqual(m.selected, "p:2")

    def test_no_compatible_printer(self):
        ps = [printer(1, "A1 Desk", "A1 Mini")]
        m = choose(ps, {1: status()}, "X1C")
        self.assertIsNone(m.selected)
        self.assertIn("X1C", m.reason)

    def test_unknown_model_with_one_printer(self):
        m = choose([printer(1, "Only", "X1C")], {1: status()}, None)
        self.assertEqual(m.selected, "p:1")

    def test_inactive_printers_are_hidden(self):
        m = choose([printer(1, "Old", "X1C", active=False)], {}, "X1C")
        self.assertEqual(m.targets, [])


class CheckTargetTest(unittest.TestCase):
    def target(self, **kw):
        base = {"id": "p:1", "label": "X1C A", "printer_id": 1, "model": "X1C", "online": True, "state": "IDLE", "compatible": True}
        base.update(kw)
        return matching.Target(**base)

    def test_blocks_wrong_model(self):
        level, msg = check_target("X1C", self.target(model="A1 Mini", compatible=False), "print")
        self.assertEqual(level, "block")
        self.assertIn("sliced for X1C", msg)

    def test_store_only_never_blocks(self):
        self.assertIsNone(check_target("X1C", self.target(compatible=False), "store"))

    def test_warns_for_offline_print_now(self):
        level, msg = check_target("X1C", self.target(online=False), "print")
        self.assertEqual(level, "warn")
        self.assertIn("offline", msg)
        self.assertIsNone(check_target("X1C", self.target(online=False), "queue"))

    def test_warns_when_model_unknown(self):
        self.assertEqual(check_target(None, self.target(compatible=None), "print")[0], "warn")


if __name__ == "__main__":
    unittest.main()
