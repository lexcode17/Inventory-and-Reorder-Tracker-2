"""Automated tests for inventory_tracker. Every test uses a temporary directory."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

os.environ.setdefault("MPLBACKEND", "Agg")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import inventory_tracker as it  # noqa: E402

FIXED = datetime(2026, 3, 15, 10, 30, 0)


def make_inventory() -> it.Inventory:
    inv = it.Inventory(clock=lambda: FIXED)
    inv.add_product("p1", "Pen", "Stationery", "10.50", 20, 5)
    inv.add_product("P2", "Chair", "Furniture", 100, 3, 2)
    return inv


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.storage = it.Storage(self.dir)


class TestValidation(unittest.TestCase):
    def test_valid_product_is_normalised(self):
        p = it.Product(" p1 ", "  Blue   Pen ", "stationery", "10.505", "7", "2")
        self.assertEqual((p.product_id, p.name, p.unit_price, p.quantity), ("P1", "Blue Pen", 10.51, 7))

    def test_invalid_fields_rejected(self):
        bad = [("", "N", "C", 1, 1, 1), ("P 1", "N", "C", 1, 1, 1), ("P1", "  ", "C", 1, 1, 1),
               ("P1", "N", "", 1, 1, 1), ("P1", "N", "C", -1, 1, 1), ("P1", "N", "C", "abc", 1, 1),
               ("P1", "N", "C", "nan", 1, 1), ("P1", "N", "C", 1, -1, 1), ("P1", "N", "C", 1, "2.5", 1),
               ("P1", "N", "C", 1, 1, -3), ("P1", "N", "C", 1, "x", 1)]
        for args in bad:
            with self.subTest(args=args), self.assertRaises(it.ValidationError):
                it.Product(*args)

    def test_zero_price_and_zero_quantity_allowed(self):
        p = it.Product("P1", "Free", "Promo", 0, 0, 0)
        self.assertEqual(p.value, 0.0)

    def test_parsers(self):
        self.assertEqual(it.parse_quantity("5", minimum=1), 5)
        for text in ("0", "-2", "1.5", "", "abc"):
            with self.subTest(text=text), self.assertRaises(it.ValidationError):
                it.parse_quantity(text, minimum=1)
        self.assertEqual(it.parse_price("1,234.567"), 1234.57)


class TestInventoryRules(unittest.TestCase):
    def test_duplicate_id_rejected_case_insensitive(self):
        inv = make_inventory()
        with self.assertRaises(it.DuplicateProductError):
            inv.add_product("P1", "Other", "X", 1, 1, 1)
        self.assertEqual(len(inv.products), 2)

    def test_failed_add_leaves_no_trace(self):
        inv = make_inventory()
        before = len(inv.transactions)
        with self.assertRaises(it.ValidationError):
            inv.add_product("P9", "Bad", "X", -5, 10, 1)
        self.assertFalse(inv.has_product("P9"))
        self.assertEqual(len(inv.transactions), before)

    def test_stock_in(self):
        inv = make_inventory()
        txn = inv.stock_in("p1", "10")
        self.assertEqual(inv.get_product("P1").quantity, 30)
        self.assertEqual((txn.type, txn.quantity, txn.unit_price, txn.value), ("IN", 10, 10.5, 105.0))

    def test_stock_out(self):
        inv = make_inventory()
        txn = inv.stock_out("P1", 8)
        self.assertEqual(inv.get_product("P1").quantity, 12)
        self.assertEqual((txn.type, txn.value), ("OUT", 84.0))

    def test_stock_out_exact_quantity_allowed(self):
        inv = make_inventory()
        inv.stock_out("P2", 3)
        self.assertEqual(inv.get_product("P2").quantity, 0)

    def test_insufficient_stock_rejected_and_state_unchanged(self):
        inv = make_inventory()
        before = len(inv.transactions)
        with self.assertRaises(it.InsufficientStockError):
            inv.stock_out("P2", 4)
        self.assertEqual(inv.get_product("P2").quantity, 3)
        self.assertEqual(len(inv.transactions), before)

    def test_invalid_movements(self):
        inv = make_inventory()
        for qty in (0, -1, "x", "2.5", ""):
            with self.subTest(qty=qty):
                with self.assertRaises(it.ValidationError):
                    inv.stock_in("P1", qty)
                with self.assertRaises(it.ValidationError):
                    inv.stock_out("P1", qty)
        with self.assertRaises(it.ProductNotFoundError):
            inv.stock_in("NOPE", 1)
        with self.assertRaises(it.ProductNotFoundError):
            inv.stock_out("NOPE", 1)

    def test_update_does_not_change_stock(self):
        inv = make_inventory()
        inv.update_product("P1", name="Gel Pen", unit_price="12", reorder_level=8)
        p = inv.get_product("P1")
        self.assertEqual((p.name, p.unit_price, p.reorder_level, p.quantity), ("Gel Pen", 12.0, 8, 20))

    def test_invalid_update_is_atomic(self):
        inv = make_inventory()
        with self.assertRaises(it.ValidationError):
            inv.update_product("P1", name="New Name", unit_price=-1)
        self.assertEqual(inv.get_product("P1").name, "Pen")

    def test_category_spelling_is_reused(self):
        inv = make_inventory()
        inv.add_product("P3", "Pencil", "STATIONERY", 2, 1, 1)
        self.assertEqual(inv.get_product("P3").category, "Stationery")

    def test_transaction_ids_unique_and_sequential(self):
        inv = make_inventory()
        inv.stock_in("P1", 1)
        inv.stock_out("P1", 1)
        ids = [t.transaction_id for t in inv.transactions]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, ["T000001", "T000002", "T000003", "T000004"])

    def test_stock_matches_transaction_log(self):
        inv = make_inventory()
        inv.stock_in("P1", 7)
        inv.stock_out("P1", 4)
        net = sum(t.quantity if t.type == "IN" else -t.quantity
                  for t in inv.transactions if t.product_id == "P1")
        self.assertEqual(net, inv.get_product("P1").quantity)

    def test_search(self):
        inv = make_inventory()
        self.assertEqual([p.product_id for p in inv.search("p1")], ["P1"])
        self.assertEqual([p.product_id for p in inv.search("CHAIR")], ["P2"])
        self.assertEqual([p.product_id for p in inv.search("furn")], ["P2"])
        self.assertEqual(inv.search("zzz"), [])
        with self.assertRaises(it.ValidationError):
            inv.search("   ")


class TestAlertsAndValuation(unittest.TestCase):
    def test_threshold_boundaries(self):
        cases = [(11, 10, None), (10, 10, it.NORMAL), (6, 10, it.NORMAL), (5, 10, it.URGENT),
                 (1, 10, it.URGENT), (0, 10, it.URGENT), (0, 0, it.URGENT), (1, 0, None),
                 (1, 1, it.NORMAL), (2, 3, it.NORMAL), (1, 2, it.URGENT)]
        for qty, reorder, expected in cases:
            with self.subTest(qty=qty, reorder=reorder):
                self.assertEqual(it.alert_level(qty, reorder), expected)

    def test_alert_ordering(self):
        inv = it.Inventory(clock=lambda: FIXED)
        inv.add_product("A", "A", "X", 1, 8, 10)   # normal
        inv.add_product("B", "B", "X", 1, 2, 10)   # urgent
        inv.add_product("C", "C", "X", 1, 50, 10)  # fine
        self.assertEqual([(p.product_id, lvl) for p, lvl in inv.alerts()], [("B", "URGENT"), ("A", "NORMAL")])

    def test_valuation_and_rounding(self):
        inv = it.Inventory(clock=lambda: FIXED)
        inv.add_product("A", "A", "X", "19.99", 3, 1)
        inv.add_product("B", "B", "X", "0.005", 3, 1)  # price rounds to 0.01
        self.assertEqual(inv.get_product("A").value, 59.97)
        self.assertEqual(inv.get_product("B").value, 0.03)
        self.assertEqual(inv.total_value(), 60.0)
        self.assertEqual(it.numpy_summary(inv)["total_value"], 60.0)


class TestReports(unittest.TestCase):
    def test_numpy_summary(self):
        s = it.numpy_summary(make_inventory())
        self.assertEqual((s["total_units"], s["mean_units"], s["median_units"], s["min_units"], s["max_units"]),
                         (23, 11.5, 11.5, 3, 20))
        self.assertEqual(s["total_value"], 510.0)

    def test_category_report(self):
        report = it.category_report(make_inventory())
        row = report.set_index("category").loc["Furniture"]
        self.assertEqual((row.products, row.units, row.stock_value, row.low_stock_items), (1, 3, 300.0, 0))  # qty 3 > reorder level 2 -> not low

    def test_monthly_report(self):
        inv = make_inventory()
        inv.stock_out("P1", 5)
        report = it.monthly_report(inv)
        self.assertEqual(len(report), 1)
        row = report.iloc[0]
        self.assertEqual((row.month, row.units_in, row.units_out, row.value_out), ("2026-03", 23, 5, 52.5))

    def test_monthly_report_with_only_stock_in(self):
        report = it.monthly_report(make_inventory())
        self.assertEqual(int(report.iloc[0].units_out), 0)

    def test_empty_reports(self):
        inv = it.Inventory()
        self.assertIsNone(it.numpy_summary(inv))
        self.assertTrue(it.category_report(inv).empty)
        self.assertTrue(it.monthly_report(inv).empty)
        self.assertTrue(it.daily_stock_out_value(inv).empty)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(it.stock_chart(inv, Path(tmp) / "a.png"))
            self.assertIsNone(it.sales_chart(inv, Path(tmp) / "b.png"))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_charts_created(self):
        inv = make_inventory()
        inv.stock_out("P1", 2)
        with tempfile.TemporaryDirectory() as tmp:
            a = it.stock_chart(inv, Path(tmp) / "charts" / "a.png")
            b = it.sales_chart(inv, Path(tmp) / "charts" / "b.png")
            self.assertTrue(a.exists() and a.stat().st_size > 1000)
            self.assertTrue(b.exists() and b.stat().st_size > 1000)

    def test_sales_chart_needs_stock_out_and_is_labelled_estimate(self):
        inv = make_inventory()  # only stock-in so far
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(it.sales_chart(inv, Path(tmp) / "b.png"))
        self.assertIn("Estimate", it.ESTIMATE_NOTE)
        inv.stock_out("P2", 1)
        self.assertEqual(it.daily_stock_out_value(inv).iloc[0].estimated_value, 100.0)


class TestStorage(TempDirCase):
    def test_round_trip(self):
        inv = make_inventory()
        inv.stock_out("P1", 3)
        self.storage.save(inv)
        loaded, notes = self.storage.load()
        self.assertEqual(notes, [])
        self.assertEqual(loaded.products, inv.products)
        self.assertEqual(loaded.transactions, inv.transactions)

    def test_next_transaction_id_continues_after_reload(self):
        inv = make_inventory()
        self.storage.save(inv)
        loaded, _ = self.storage.load()
        txn = loaded.stock_in("P1", 1)
        self.assertEqual(txn.transaction_id, "T000003")

    def test_missing_files_give_empty_inventory(self):
        inv, notes = it.Storage(self.dir / "does_not_exist").load()
        self.assertEqual((inv.products, notes), ([], []))

    def test_empty_inventory_saves_and_reloads(self):
        self.storage.save(it.Inventory())
        inv, notes = self.storage.load()
        self.assertEqual((inv.products, inv.transactions, notes), ([], [], []))

    def test_malformed_rows_skipped_and_original_preserved(self):
        (self.dir / "products.csv").write_text(
            "product_id,name,category,unit_price,quantity,reorder_level\n"
            "P1,Pen,Stationery,10,5,2\n"
            "P1,Dup,Stationery,10,5,2\n"
            "P2,Bad,Stationery,abc,5,2\n"
            "P3,Short\n"
            "P4,Neg,Stationery,5,-1,2\n"
            "P5,Ok,Stationery,5,1,2\n", encoding="utf-8")
        (self.dir / "transactions.csv").write_text(
            "transaction_id,timestamp,product_id,type,quantity,unit_price,value\n"
            "T000001,2026-01-01 10:00:00,P1,IN,5,10,50\n"
            "T000001,2026-01-01 10:00:00,P1,IN,5,10,50\n"
            "T000002,not-a-date,P1,IN,5,10,50\n"
            "T000003,2026-01-01 10:00:00,P1,SWAP,5,10,50\n", encoding="utf-8")
        inv, notes = self.storage.load()
        self.assertEqual([p.product_id for p in inv.products], ["P1", "P5"])
        self.assertEqual(len(inv.transactions), 1)
        self.assertEqual(len(notes), 7)
        self.assertTrue(list(self.dir.glob("products.csv.corrupt_*")))

    def test_missing_header_is_reported_not_fatal(self):
        (self.dir / "products.csv").write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        inv, notes = self.storage.load()
        self.assertEqual(inv.products, [])
        self.assertIn("missing column", notes[0])

    def test_permission_error_on_load_and_save(self):
        self.storage.save(make_inventory())
        with mock.patch("pathlib.Path.open", side_effect=PermissionError):
            with self.assertRaises(it.StorageError):
                self.storage.load()
            with self.assertRaises(it.StorageError):
                self.storage.save(make_inventory())

    def test_failed_save_keeps_previous_data(self):
        inv = make_inventory()
        self.storage.save(inv)
        original = (self.dir / "products.csv").read_text(encoding="utf-8")
        inv.stock_in("P1", 5)
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(it.StorageError):
                self.storage.save(inv)
        self.assertEqual((self.dir / "products.csv").read_text(encoding="utf-8"), original)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_backup_created_on_second_save(self):
        self.storage.save(make_inventory())
        self.storage.save(make_inventory())
        self.assertTrue((self.dir / "products.csv.bak").exists())


class TestJsonExport(TempDirCase):
    def test_export_is_valid_json(self):
        inv = make_inventory()
        inv.stock_out("P1", 2)
        path = self.storage.export_json(inv)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["inventory"]), 2)
        self.assertEqual(len(data["transactions"]), 3)
        self.assertEqual(data["inventory"][0]["stock_value"], 189.0)

    def test_empty_export_is_valid(self):
        path = self.storage.export_json(it.Inventory(), self.dir / "empty.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((data["inventory"], data["transactions"]), ([], []))

    def test_export_permission_error(self):
        with mock.patch("pathlib.Path.open", side_effect=PermissionError):
            with self.assertRaises(it.StorageError):
                self.storage.export_json(make_inventory())


class TestDemoData(TempDirCase):
    def test_demo_adds_consistent_data(self):
        inv = it.Inventory(clock=lambda: FIXED)
        self.assertEqual(inv.seed_demo(), 12)
        for p in inv.products:
            net = sum(t.quantity if t.type == "IN" else -t.quantity
                      for t in inv.transactions if t.product_id == p.product_id)
            self.assertEqual(net, p.quantity, p.product_id)
        self.assertTrue(inv.alerts())

    def test_demo_does_not_overwrite_existing(self):
        inv = it.Inventory(clock=lambda: FIXED)
        inv.add_product("P001", "My Own Item", "Custom", 5, 9, 1)
        self.assertEqual(inv.seed_demo(), 11)
        self.assertEqual(inv.get_product("P001").name, "My Own Item")
        self.assertEqual(inv.get_product("P001").quantity, 9)
        self.assertEqual(inv.seed_demo(), 0)


class TestConsoleApp(TempDirCase):
    def run_app(self, inputs, inventory=None):
        feed = iter(inputs)

        def fake_input(prompt):
            try:
                return next(feed)
            except StopIteration:
                raise EOFError from None

        out: list[str] = []
        inv = inventory or it.Inventory()
        app = it.App(self.storage, inv, fake_input, out.append, color=False, show_charts=False)
        app.run()
        return "\n".join(out), inv

    def test_invalid_menu_choice(self):
        out, _ = self.run_app(["abc", "99", "", "0"])
        self.assertEqual(out.count("not a valid menu option"), 3)
        self.assertIn("Data saved", out)

    def test_add_product_with_invalid_inputs_then_valid(self):
        out, inv = self.run_app([
            "1", "", "P 1", "p1", "   ", "Pen", "", "Stationery", "-5", "abc", "10.50", "-1", "2.5", "20",
            "x", "5", "0"])
        self.assertIn("Product ID cannot be blank", out)
        self.assertIn("Product ID must be", out)
        self.assertIn("Product name cannot be blank", out)
        self.assertIn("Unit price cannot be negative", out)
        self.assertIn("Unit price must be a number", out)
        self.assertIn("Quantity cannot be negative", out)
        self.assertIn("Quantity must be a whole number", out)
        self.assertIn("Reorder level must be a whole number", out)
        self.assertEqual(inv.get_product("P1").quantity, 20)
        loaded, _ = self.storage.load()
        self.assertEqual(loaded.get_product("P1").unit_price, 10.5)

    def test_duplicate_and_unknown_id_prompts(self):
        inv = make_inventory()
        out, _ = self.run_app(["1", "P1", "cancel", "5", "NOPE", "cancel", "0"], inv)
        self.assertIn("already exists", out)
        self.assertIn("No product with ID 'NOPE'", out)
        self.assertEqual(out.count("Cancelled"), 2)

    def test_stock_out_over_limit_then_valid(self):
        inv = make_inventory()
        out, _ = self.run_app(["6", "P2", "10", "-1", "3", "0"], inv)
        self.assertIn("Only 3 unit(s)", out)
        self.assertIn("Quantity must be at least 1", out)
        self.assertEqual(inv.get_product("P2").quantity, 0)
        self.assertIn("URGENT reorder alert", out)

    def test_update_blank_keeps_values(self):
        inv = make_inventory()
        out, _ = self.run_app(["4", "P1", "", "", "", "", "0"], inv)
        self.assertIn("No changes entered", out)
        self.assertEqual(inv.get_product("P1").name, "Pen")

    def test_reports_on_empty_inventory(self):
        out, _ = self.run_app(["2", "3", "zzz", "7", "8", "9", "10", "11", "12", "13", "14", "0"])
        self.assertIn("Inventory is empty", out)
        self.assertIn("No transactions recorded yet", out)
        self.assertIn("All products are above their reorder levels", out)
        self.assertIn("no stock chart", out)
        self.assertIn("no sales-value chart", out)

    def test_full_menu_with_demo_data(self):
        out, inv = self.run_app(["16", "2", "3", "bolt", "3", "stat", "7", "8", "9", "10", "11", "5", "12",
                                 "13", "14", "15", "0"])
        for expected in ("Added 12 demo", "Wireless Mouse", "Reorder alerts", "Inventory valuation",
                         "NumPy summary", "Category report", "Transaction history", "Monthly report",
                         "Chart saved", "Exported"):
            self.assertIn(expected, out)
        self.assertEqual(len(inv.products), 12)
        self.assertTrue((self.dir / "charts" / "stock_by_product.png").exists())
        self.assertTrue(list((self.dir / "exports").glob("*.json")))

    def test_eof_saves_cleanly(self):
        out, _ = self.run_app(["16"])  # input ends abruptly
        self.assertIn("Data saved", out)
        loaded, _ = self.storage.load()
        self.assertEqual(len(loaded.products), 12)

    def test_save_failure_is_reported_not_raised(self):
        with mock.patch("os.replace", side_effect=PermissionError):
            out, _ = self.run_app(["16", "0"])
        self.assertIn("Permission denied", out)

    def test_render_table_alignment(self):
        text = it.render_table(["A", "B"], [["x", "1"], ["yy", "22"]], ["l", "r"])
        widths = {len(line) for line in text.splitlines()}
        self.assertEqual(len(widths), 1)


if __name__ == "__main__":
    unittest.main()
