"""Tests for window semantics, impact ranges, atomic publication and the API."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from app import metrics
from app.db import BatchError, Database
from app.seed import CUSTOMERS, DEMO_BATCHES, seed_orders
from app.server import make_server
from app.timeutil import iso, parse_timestamp

T0 = parse_timestamp("2026-11-01T00:00:00Z")
DAY = 24 * 60 * 60 * 1000


def row(order_id, at, cents, customer="C-1", status="active"):
    return metrics.OrderRow(
        order_id=order_id,
        customer_id=customer,
        amount_cents=cents,
        occurred_at_ms=at,
        status=status,
    )


class WindowSemanticsTest(unittest.TestCase):
    def test_rolling_window_includes_both_boundaries(self):
        orders = [row("A", T0, 100), row("B", T0 + 7 * DAY, 250)]
        index = metrics.WindowIndex.build(metrics.active_sorted(orders))
        # T at the later order: A sits exactly 7 days back and is included.
        cell = index.cell(T0 + 7 * DAY)
        self.assertEqual(cell["rolling_7d_cents"], 350)
        # 1 ms later, A has fallen out; B stays.
        cell = index.cell(T0 + 7 * DAY + 1)
        self.assertEqual(cell["rolling_7d_cents"], 250)
        self.assertEqual(cell["cumulative_cents"], 350)

    def test_order_at_same_instant_is_included(self):
        orders = [row("A", T0, 100), row("B", T0, 50)]
        index = metrics.WindowIndex.build(metrics.active_sorted(orders))
        cell = index.cell(T0)
        self.assertEqual(cell["rolling_7d_cents"], 150)
        self.assertEqual(cell["cumulative_cents"], 150)

    def test_last_three_stable_order_with_same_instant(self):
        orders = [
            row("O-9", T0, 900),
            row("O-2", T0, 200),  # same instant, smaller id sorts first
            row("O-5", T0 + DAY, 500),
            row("O-1", T0 + 2 * DAY, 100),
        ]
        index = metrics.WindowIndex.build(metrics.active_sorted(orders))
        cell = index.cell(T0)
        self.assertEqual(cell["last3_ids"], ["O-2", "O-9"])
        self.assertEqual(cell["last3_count"], 2)
        cell = index.cell(T0 + DAY)
        self.assertEqual(cell["last3_ids"], ["O-2", "O-9", "O-5"])
        self.assertEqual(cell["last3_sum_cents"], 1600)
        cell = index.cell(T0 + 2 * DAY)
        self.assertEqual(cell["last3_ids"], ["O-9", "O-5", "O-1"])
        self.assertEqual(cell["last3_sum_cents"], 1500)

    def test_void_keeps_anchor_and_drops_amounts(self):
        orders = [row("A", T0, 100), row("B", T0 + DAY, 50, status="voided")]
        instants = metrics.evaluation_instants(orders)
        self.assertEqual(instants, [T0, T0 + DAY])
        cell = metrics.compute_cell(metrics.active_sorted(orders), T0 + DAY)
        self.assertEqual(cell["cumulative_cents"], 100)
        self.assertEqual(cell["last3_count"], 1)

    def test_merged_ranges(self):
        self.assertEqual(
            metrics.merged_ranges([(0, 10), (11, 20), (30, 40)]),
            [(0, 20), (30, 40)],
        )
        self.assertEqual(
            metrics.merged_ranges([(0, 100), (5, 8)]), [(0, 100)]
        )
        self.assertEqual(
            metrics.rolling_impact_ranges([T0, T0 + DAY], T0 + 2 * DAY),
            [(T0, T0 + 2 * DAY)],
        )


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "test.db")
        self.db = Database(self.path)
        self.db.seed(CUSTOMERS, seed_orders())

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_cache_matches_facts(self):
        self.assertTrue(self.db.verify()["ok"])

    def test_preview_is_read_only(self):
        before = self.db.state()
        result = self.db.preview(DEMO_BATCHES["late-correction"]["payload"])
        after = self.db.state()
        self.assertFalse(result["committed"])
        self.assertEqual(before["summary"], after["summary"])
        self.assertEqual(before["batches"], after["batches"])
        self.assertEqual(self.db.verify()["checked_cells"], result["all_cells"])

    def test_late_correction_affects_later_anchors_only(self):
        result = self.db.preview(DEMO_BATCHES["late-correction"]["payload"])
        customer = result["customers"][0]
        self.assertEqual(customer["customer_id"], "C-1001")
        self.assertEqual(customer["first_affected"], "2026-11-03T09:00:00Z")
        self.assertEqual(customer["skipped_cells"], 1)
        self.assertEqual(customer["changed_cells"], customer["recomputed_cells"])
        self.assertLess(customer["recomputed_cells"], customer["total_customer_cells"] + 1)
        # The first anchor, 11-01, is untouched.
        self.assertTrue(
            all(c["as_of"] >= customer["first_affected"] for c in customer["cells"])
        )

    def test_void_propagates_to_latest(self):
        result = self.db.preview(DEMO_BATCHES["void-old"]["payload"])
        customer = result["customers"][0]
        self.assertEqual(customer["changed_cells"], customer["recomputed_cells"])
        self.assertEqual(customer["last_affected"], customer["cells"][-1]["as_of"])
        last = customer["cells"][-1]
        self.assertEqual(
            last["after"]["cumulative_cents"],
            last["before"]["cumulative_cents"] - 120000,
        )
        self.assertNotIn("O-1001", str(last["after"]["last3_ids"]))

    def test_adjacent_batch_merges_ranges(self):
        result = self.db.preview(DEMO_BATCHES["adjacent-batch"]["payload"])
        customer = result["customers"][0]
        self.assertEqual(len(customer["rolling_ranges"]), 1)
        self.assertEqual(len(customer["recompute_ranges"]), 1)
        self.assertEqual(
            customer["rolling_ranges"][0]["end"], "2026-11-16T09:00:00Z"
        )
        # One recompute pass, not one per operation.
        self.assertEqual(customer["recomputed_cells"], 4)

    def test_same_instant_import_creates_new_anchor_only_once(self):
        result = self.db.preview(DEMO_BATCHES["same-instant"]["payload"])
        customer = result["customers"][0]
        self.assertEqual(customer["new_cells"], 0)
        anchor = [
            c for c in customer["cells"] if c["as_of"] == "2026-11-09T09:00:00Z"
        ][0]
        self.assertEqual(
            anchor["after"]["rolling_7d_cents"],
            anchor["before"]["rolling_7d_cents"] + 31000,
        )
        self.assertEqual(anchor["after"]["last3_ids"][-1], "O-1007")

    def test_cross_window_void_and_refill(self):
        result = self.db.preview(DEMO_BATCHES["cross-window-void"]["payload"])
        customer = result["customers"][0]
        self.assertEqual(len(customer["rolling_ranges"]), 2)
        self.assertEqual(customer["new_cells"], 1)
        latest = customer["cells"][-1]
        self.assertEqual(latest["as_of"], "2026-11-18T08:00:00Z")
        self.assertEqual(latest["after"]["rolling_7d_cents"], 12000 + 26000)

    def test_commit_then_verify(self):
        result = self.db.commit(DEMO_BATCHES["late-correction"]["payload"])
        self.assertTrue(result["committed"])
        self.assertIsNotNone(result["batch_id"])
        self.assertTrue(self.db.verify()["ok"])
        state = self.db.state()
        self.assertEqual(state["summary"]["batch_count"], 1)
        detail = self.db.batch_detail(result["batch_id"])
        self.assertEqual(len(detail["changes"]), 1)
        self.assertEqual(len(detail["recalc"]), result["recomputed_cells"])
        self.assertTrue(any(r["changed"] for r in detail["recalc"]))

    def test_failed_commit_rolls_back_everything(self):
        before_state = self.db.state()
        before_verify = self.db.verify()
        result = self.db.commit(
            DEMO_BATCHES["void-old"]["payload"], simulate_failure=True
        )
        self.assertFalse(result["committed"])
        self.assertTrue(result["rolled_back"])
        after_state = self.db.state()
        self.assertEqual(before_state["summary"], after_state["summary"])
        self.assertEqual(before_state["customers"], after_state["customers"])
        self.assertEqual(before_state["batches"], after_state["batches"])
        self.assertEqual(before_verify, self.db.verify())
        self.assertEqual(self.db.state()["summary"]["batch_count"], 0)

    def test_import_before_first_anchor_recomputes_from_new_instant(self):
        payload = {
            "operations": [
                {
                    "op": "import",
                    "order_id": "O-0999",
                    "customer_id": "C-1002",
                    "amount": "500.00",
                    "occurred_at": "2026-10-25T10:00:00Z",
                }
            ]
        }
        result = self.db.preview(payload)
        customer = result["customers"][0]
        self.assertEqual(customer["new_cells"], 1)
        self.assertEqual(customer["first_affected"], "2026-10-25T10:00:00Z")
        self.assertEqual(customer["skipped_cells"], 0)
        self.assertEqual(customer["recomputed_cells"], customer["total_customer_cells"])
        first = customer["cells"][0]
        self.assertEqual(first["after"]["cumulative_cents"], 50000)

    def test_same_instant_tie_break_is_visible_in_preview(self):
        # O-1007 sorts after O-1004/O-1005 at the same instant, so it becomes
        # the newest of the three and pushes O-1004 out of last-three.
        result = self.db.preview(DEMO_BATCHES["same-instant"]["payload"])
        anchor = [
            c for c in result["customers"][0]["cells"]
            if c["as_of"] == "2026-11-09T09:00:00Z"
        ][0]
        self.assertEqual(anchor["before"]["last3_ids"], ["O-1003", "O-1004", "O-1005"])
        # O-1007 has the largest id at that instant, so it sorts last and
        # displaces O-1003 from the last-three set.
        self.assertEqual(anchor["after"]["last3_ids"], ["O-1004", "O-1005", "O-1007"])
        self.assertNotIn("O-1003", anchor["after"]["last3_ids"])

    def test_unaffected_customers_are_not_recomputed(self):
        result = self.db.preview(DEMO_BATCHES["late-correction"]["payload"])
        self.assertEqual(result["affected_customer_count"], 1)
        self.assertLess(result["recomputed_cells"], result["all_cells"])
        self.assertEqual(
            [c["customer_id"] for c in result["customers"]], ["C-1001"]
        )

    def test_validation_collects_all_errors(self):
        payload = {
            "operations": [
                {"op": "import", "order_id": "O-1001", "customer_id": "C-1001", "amount": "1", "occurred_at": "2026-11-01T00:00:00Z"},
                {"op": "void", "order_id": "NOPE"},
                {"op": "correct", "order_id": "O-1002", "amount": "-3"},
                {"op": "correct", "order_id": "O-1003", "amount": "1.234"},
                {"op": "banana", "order_id": "X"},
                {"op": "void", "order_id": "O-1002"},
                {"op": "void", "order_id": "O-1002"},
            ]
        }
        with self.assertRaises(BatchError) as ctx:
            self.db.preview(payload)
        self.assertGreaterEqual(len(ctx.exception.errors), 5)

    def test_voided_order_cannot_be_operated_again(self):
        self.assertTrue(
            self.db.commit({"operations": [{"op": "void", "order_id": "O-1001"}]})[
                "committed"
            ]
        )
        with self.assertRaises(BatchError):
            self.db.preview({"operations": [{"op": "void", "order_id": "O-1001"}]})

    def test_new_customer_requires_name_and_is_created(self):
        with self.assertRaises(BatchError):
            self.db.preview(
                {
                    "operations": [
                        {
                            "op": "import",
                            "order_id": "N-1",
                            "customer_id": "C-9000",
                            "amount": "10",
                            "occurred_at": "2026-11-20T00:00:00Z",
                        }
                    ]
                }
            )
        result = self.db.commit(
            {
                "operations": [
                    {
                        "op": "import",
                        "order_id": "N-1",
                        "customer_id": "C-9000",
                        "customer_name": "新客户",
                        "amount": "10",
                        "occurred_at": "2026-11-20T00:00:00Z",
                    }
                ]
            }
        )
        self.assertTrue(result["committed"])
        names = {c["customer_id"]: c["name"] for c in self.db.state()["customers"]}
        self.assertEqual(names["C-9000"], "新客户")
        self.assertEqual(result["customers"][0]["new_cells"], 1)

    def test_multiple_customers_in_one_batch(self):
        payload = {
            "operations": [
                {"op": "void", "order_id": "O-1001"},
                {"op": "correct", "order_id": "O-2001", "amount": "2500"},
                {"op": "void", "order_id": "O-4001"},
            ]
        }
        result = self.db.preview(payload)
        self.assertEqual(
            sorted(c["customer_id"] for c in result["customers"]),
            ["C-1001", "C-1002", "C-1004"],
        )
        self.assertTrue(self.db.commit(payload)["committed"])
        self.assertTrue(self.db.verify()["ok"])

    def test_reseeding_resets_everything(self):
        self.db.commit(DEMO_BATCHES["late-correction"]["payload"])
        self.db.seed(CUSTOMERS, seed_orders(), rebuild=True)
        state = self.db.state()
        self.assertEqual(state["summary"]["batch_count"], 0)
        self.assertEqual(state["summary"]["order_count"], 16)
        self.assertEqual(state["summary"]["active_order_count"], 16)
        self.assertTrue(self.db.verify()["ok"])

    def test_customer_detail_snapshot_matches_cell(self):
        detail = self.db.customer_detail("C-1001")
        self.assertEqual(
            detail["metrics"]["rolling_7d_cents"],
            detail["snapshot"]["rolling_sum_cents"],
        )
        self.assertEqual(
            detail["metrics"]["last3_sum_cents"],
            detail["snapshot"]["last3_sum_cents"],
        )
        self.assertEqual(
            detail["metrics"]["last3_ids"], detail["snapshot"]["last3_ids"]
        )

    def test_audit_records_commit_and_seed(self):
        self.db.commit(DEMO_BATCHES["late-correction"]["payload"])
        kinds = [event["kind"] for event in self.db.audit()]
        self.assertIn("batch_committed", kinds)
        self.assertIn("seed", kinds)


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server(
            "127.0.0.1", 0, os.path.join(self.tmp.name, "api.db")
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, path, payload=None, method=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_state_and_static(self):
        status, body = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"]["customer_count"], 4)
        with urllib.request.urlopen(self.base + "/") as resp:
            html = resp.read().decode()
        self.assertIn("订单", html)

    def test_preview_and_commit(self):
        status, body = self.request(
            "/api/preview", DEMO_BATCHES["void-old"]["payload"]
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["committed"])
        status, body = self.request(
            "/api/commit", DEMO_BATCHES["void-old"]["payload"]
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["committed"])
        status, body = self.request(f"/api/batches/{body['batch_id']}")
        self.assertEqual(status, 200)
        self.assertTrue(body["recalc"])

    def test_validation_error_shape(self):
        status, body = self.request(
            "/api/preview",
            {"operations": [{"op": "void", "order_id": "missing"}]},
        )
        self.assertEqual(status, 422)
        self.assertIn("errors", body)

    def test_failed_commit_is_409_and_state_unchanged(self):
        _, before = self.request("/api/state")
        status, body = self.request(
            "/api/commit",
            {**DEMO_BATCHES["void-old"]["payload"], "simulate_failure": True},
        )
        self.assertEqual(status, 409)
        self.assertFalse(body["committed"])
        _, after = self.request("/api/state")
        self.assertEqual(before["summary"], after["summary"])

    def test_demo_catalogue(self):
        status, body = self.request("/api/demo")
        self.assertEqual(status, 200)
        keys = {d["key"] for d in body["demos"]}
        self.assertTrue({"same-instant", "late-correction", "cross-window-void"} <= keys)


if __name__ == "__main__":
    unittest.main()
