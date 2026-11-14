"""SQLite persistence, batch validation, impact forecasting and atomic publication.

Money is stored as integer cents. API ``amount`` values are yuan (decimal
string or number) and are converted exactly; floats never touch the database.

A batch is analysed in full before anything is written. The write phase runs in
one immediate SQLite transaction covering order changes, metric-cell updates,
the batch journal, the recalculation log and the audit row, so a failure cannot
publish a subset of metrics.
"""

from __future__ import annotations

import json
import random
import sqlite3
import string
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator, Sequence

from . import metrics
from .timeutil import iso, parse_timestamp

MAX_OPERATIONS = 500
MAX_AMOUNT_CENTS = 10**12

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id       TEXT PRIMARY KEY,
    customer_id    TEXT NOT NULL REFERENCES customers(customer_id),
    amount_cents   INTEGER NOT NULL CHECK (amount_cents >= 0),
    occurred_at_ms INTEGER NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('active', 'voided')),
    revision       INTEGER NOT NULL DEFAULT 1,
    updated_at_ms  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_customer_time
    ON orders (customer_id, occurred_at_ms, order_id);

CREATE TABLE IF NOT EXISTS metric_cells (
    customer_id      TEXT NOT NULL,
    as_of_ms         INTEGER NOT NULL,
    rolling_7d_cents INTEGER NOT NULL,
    cumulative_cents INTEGER NOT NULL,
    last3_sum_cents  INTEGER NOT NULL,
    last3_count      INTEGER NOT NULL,
    last3_ids        TEXT NOT NULL,
    updated_batch_id TEXT,
    PRIMARY KEY (customer_id, as_of_ms)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id                 TEXT PRIMARY KEY,
    created_at_ms            INTEGER NOT NULL,
    operation_count          INTEGER NOT NULL,
    affected_customers       INTEGER NOT NULL,
    recomputed_cells         INTEGER NOT NULL,
    changed_cells            INTEGER NOT NULL,
    affected_customer_cells  INTEGER NOT NULL,
    all_cells                INTEGER NOT NULL,
    request_json             TEXT NOT NULL,
    summary_json             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_changes (
    batch_id    TEXT NOT NULL REFERENCES batches(batch_id),
    seq         INTEGER NOT NULL,
    op          TEXT NOT NULL,
    order_id    TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    before_json TEXT,
    after_json  TEXT,
    PRIMARY KEY (batch_id, seq)
);

CREATE TABLE IF NOT EXISTS cell_recalc_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL REFERENCES batches(batch_id),
    customer_id TEXT NOT NULL,
    as_of_ms    INTEGER NOT NULL,
    before_json TEXT,
    after_json  TEXT NOT NULL,
    changed     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recalc_batch
    ON cell_recalc_log (batch_id, customer_id, as_of_ms);

CREATE TABLE IF NOT EXISTS audit_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ms     INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    detail    TEXT NOT NULL
);
"""

VALID_OPS = ("import", "void", "correct")


class BatchError(Exception):
    """A user-facing batch error. ``errors`` lists every problem found."""

    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__("；".join(self.errors))


class FaultInjected(Exception):
    """Demo-only failure raised mid-commit to prove the rollback path."""


@dataclass(frozen=True)
class ParsedOperation:
    seq: int
    op: str
    order_id: str
    customer_id: str | None
    customer_name: str | None
    amount_cents: int | None
    occurred_at_ms: int | None


def parse_amount(value) -> int:
    """Parse a yuan amount into integer cents without binary-float arithmetic."""
    if isinstance(value, bool) or value is None:
        raise ValueError("金额不能为空")
    if isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace("¥", "").replace("￥", "")
    else:
        raise ValueError("金额必须是字符串或数字")
    if not text:
        raise ValueError("金额不能为空")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"金额格式无效：{value}") from exc
    if not amount.is_finite():
        raise ValueError("金额必须是有限数")
    if amount < 0:
        raise ValueError("金额不能为负数")
    if amount.as_tuple().exponent < -2:
        raise ValueError("金额最多两位小数")
    cents = int(amount * 100)
    if cents > MAX_AMOUNT_CENTS:
        raise ValueError("金额超出允许范围")
    return cents


def _parse_operations(raw) -> list[ParsedOperation]:
    if not isinstance(raw, list):
        raise BatchError(["operations 必须是数组"])
    if not raw:
        raise BatchError(["operations 不能为空"])
    if len(raw) > MAX_OPERATIONS:
        raise BatchError([f"单批最多 {MAX_OPERATIONS} 条操作"])
    parsed: list[ParsedOperation] = []
    errors: list[str] = []
    seen_order_ids: dict[str, int] = {}
    for index, item in enumerate(raw):
        seq = index + 1
        if not isinstance(item, dict):
            errors.append(f"第 {seq} 条：操作必须是对象")
            continue
        op = item.get("op")
        order_id = item.get("order_id")
        if op not in VALID_OPS:
            errors.append(f"第 {seq} 条：op 必须是 import、void 或 correct")
            continue
        if not isinstance(order_id, str) or not order_id.strip():
            errors.append(f"第 {seq} 条：order_id 不能为空")
            continue
        order_id = order_id.strip()
        if len(order_id) > 64:
            errors.append(f"第 {seq} 条：order_id 最长 64 个字符")
            continue
        if order_id in seen_order_ids:
            errors.append(
                f"第 {seq} 条：order_id {order_id} 与第 {seen_order_ids[order_id]} 条重复；"
                "同一批次内不能重复操作同一订单"
            )
            continue
        seen_order_ids[order_id] = seq

        customer_id = item.get("customer_id")
        customer_name = item.get("customer_name")
        amount_cents = None
        occurred_at_ms = None

        if op == "import":
            if not isinstance(customer_id, str) or not customer_id.strip():
                errors.append(f"第 {seq} 条：导入新订单必须提供 customer_id")
                continue
            customer_id = customer_id.strip()
            if len(customer_id) > 64:
                errors.append(f"第 {seq} 条：customer_id 最长 64 个字符")
                continue
            if customer_name is not None:
                if not isinstance(customer_name, str) or not customer_name.strip():
                    errors.append(f"第 {seq} 条：customer_name 不能为空")
                    continue
                customer_name = customer_name.strip()
                if len(customer_name) > 80:
                    errors.append(f"第 {seq} 条：customer_name 最长 80 个字符")
                    continue
            try:
                amount_cents = parse_amount(item.get("amount"))
            except ValueError as exc:
                errors.append(f"第 {seq} 条：{exc}")
                continue
            try:
                occurred_at_ms = parse_timestamp(item.get("occurred_at"))
            except ValueError as exc:
                errors.append(f"第 {seq} 条：{exc}")
                continue
        elif op == "correct":
            customer_id = None
            customer_name = None
            try:
                amount_cents = parse_amount(item.get("amount"))
            except ValueError as exc:
                errors.append(f"第 {seq} 条：{exc}")
                continue
        else:
            customer_id = None
            customer_name = None

        parsed.append(
            ParsedOperation(
                seq=seq,
                op=op,
                order_id=order_id,
                customer_id=customer_id,
                customer_name=customer_name,
                amount_cents=amount_cents,
                occurred_at_ms=occurred_at_ms,
            )
        )
    if errors:
        raise BatchError(errors)
    return parsed


def order_from_row(row: sqlite3.Row) -> metrics.OrderRow:
    return metrics.OrderRow(
        order_id=row["order_id"],
        customer_id=row["customer_id"],
        amount_cents=row["amount_cents"],
        occurred_at_ms=row["occurred_at_ms"],
        status=row["status"],
    )


def cell_json(cell: dict) -> dict:
    return {
        "rolling_7d_cents": cell["rolling_7d_cents"],
        "cumulative_cents": cell["cumulative_cents"],
        "last3_sum_cents": cell["last3_sum_cents"],
        "last3_count": cell["last3_count"],
        "last3_ids": list(cell["last3_ids"]),
    }


def order_json(order: metrics.OrderRow) -> dict:
    return {
        "order_id": order.order_id,
        "customer_id": order.customer_id,
        "amount_cents": order.amount_cents,
        "occurred_at_ms": order.occurred_at_ms,
        "occurred_at": iso(order.occurred_at_ms),
        "status": order.status,
    }


def cell_signature(cell: dict | None) -> tuple | None:
    if cell is None:
        return None
    return (
        cell["rolling_7d_cents"],
        cell["cumulative_cents"],
        cell["last3_sum_cents"],
        cell["last3_count"],
        tuple(cell["last3_ids"]),
    )


def _new_batch_id() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    suffix = "".join(
        random.choice(string.ascii_lowercase + string.digits) for _ in range(6)
    )
    return f"B-{stamp}-{suffix}"


def _now_ms() -> int:
    return int(time.time() * 1000)


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            yield conn
        finally:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            conn.close()

    # ------------------------------------------------------------------ seed
    def seed(self, customers, orders, *, rebuild: bool = False) -> None:
        """Insert seed data and materialize every metric cell from order facts."""
        with self.transaction() as conn:
            if rebuild:
                for table in (
                    "cell_recalc_log",
                    "batch_changes",
                    "batches",
                    "metric_cells",
                    "orders",
                    "customers",
                    "audit_events",
                ):
                    conn.execute(f"DELETE FROM {table}")
            if conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]:
                return
            now = _now_ms()
            for customer_id, name in customers:
                conn.execute(
                    "INSERT INTO customers (customer_id, name, created_at_ms) VALUES (?, ?, ?)",
                    (customer_id, name, now),
                )
            for order in orders:
                conn.execute(
                    """INSERT INTO orders
                       (order_id, customer_id, amount_cents, occurred_at_ms,
                        status, revision, updated_at_ms)
                       VALUES (?, ?, ?, ?, 'active', 1, ?)""",
                    (
                        order.order_id,
                        order.customer_id,
                        order.amount_cents,
                        order.occurred_at_ms,
                        now,
                    ),
                )
            all_orders = [
                order_from_row(r)
                for r in conn.execute("SELECT * FROM orders").fetchall()
            ]
            for (customer_id, instant), cell in metrics.compute_all_cells(
                all_orders
            ).items():
                self._upsert_cell(conn, customer_id, instant, cell, None)
            conn.execute(
                "INSERT INTO audit_events (at_ms, kind, detail) VALUES (?, 'seed', ?)",
                (now, json.dumps({"orders": len(orders)}, ensure_ascii=False)),
            )

    @staticmethod
    def _upsert_cell(
        conn: sqlite3.Connection,
        customer_id: str,
        instant: int,
        cell: dict,
        batch_id: str | None,
    ) -> None:
        conn.execute(
            """INSERT INTO metric_cells
               (customer_id, as_of_ms, rolling_7d_cents, cumulative_cents,
                last3_sum_cents, last3_count, last3_ids, updated_batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(customer_id, as_of_ms) DO UPDATE SET
                 rolling_7d_cents = excluded.rolling_7d_cents,
                 cumulative_cents = excluded.cumulative_cents,
                 last3_sum_cents  = excluded.last3_sum_cents,
                 last3_count      = excluded.last3_count,
                 last3_ids        = excluded.last3_ids,
                 updated_batch_id = excluded.updated_batch_id""",
            (
                customer_id,
                instant,
                cell["rolling_7d_cents"],
                cell["cumulative_cents"],
                cell["last3_sum_cents"],
                cell["last3_count"],
                json.dumps(cell["last3_ids"], ensure_ascii=False),
                batch_id,
            ),
        )

    # -------------------------------------------------------------- analysis
    def preview(self, payload: dict) -> dict:
        """Validate and forecast a batch without writing anything."""
        with self.read() as conn:
            result = self._analyze(conn, payload)
        result["committed"] = False
        result["rolled_back"] = False
        result["batch_id"] = None
        return result

    def commit(self, payload: dict, *, simulate_failure: bool = False) -> dict:
        """Apply a batch atomically.

        With ``simulate_failure`` the transaction is rolled back after orders and
        cells have been written, demonstrating that no partial result is visible.
        """
        reason = None
        try:
            with self.transaction() as conn:
                result = self._analyze(conn, payload)
                self._apply_orders(conn, result["operations"])
                self._apply_metrics(conn, result, result["batch"]["batch_id"])
                if simulate_failure:
                    # Fail after orders and cells are written but before the
                    # batch journal, so a partial publish would be observable
                    # if the transaction did not roll back.
                    raise FaultInjected(
                        "演示故障：订单与指标已写入，但提交前中止；事务已全部回滚。"
                    )
                self._persist_batch(conn, payload, result)
        except FaultInjected as exc:
            # Let the context manager see the exception so it ROLLBACKs orders,
            # metric cells, batch journal, recalculation log and audit row.
            # Returning from inside ``with`` would count as a normal exit and
            # COMMIT the partial writes -- exactly what must never happen.
            reason = str(exc)
            result = {
                "customers": [],
                "recomputed_cells": 0,
                "changed_cells": 0,
                "new_cells": 0,
                "affected_customer_count": 0,
                "all_customer_count": 0,
                "affected_customer_cells": 0,
                "all_cells": 0,
                "explanation": {"headline": "提交已回滚", "sentences": [], "ops_by_kind": {}},
                "operations": [],
            }
        result["committed"] = reason is None
        result["rolled_back"] = reason is not None
        result["error"] = reason
        result["batch_id"] = result["batch"]["batch_id"] if reason is None else None
        return result

    def _analyze(self, conn: sqlite3.Connection, payload: dict) -> dict:
        raw_operations = payload.get("operations") if isinstance(payload, dict) else None
        parsed = _parse_operations(raw_operations)

        existing = {
            r["order_id"]: order_from_row(r)
            for r in conn.execute("SELECT * FROM orders").fetchall()
        }
        customers = {
            r["customer_id"]: r["name"]
            for r in conn.execute("SELECT customer_id, name FROM customers").fetchall()
        }

        errors: list[str] = []
        changes: list[dict] = []
        working: dict[str, metrics.OrderRow] = dict(existing)

        for item in parsed:
            if item.op == "import":
                if item.order_id in working:
                    errors.append(
                        f"第 {item.seq} 条：订单 {item.order_id} 已存在，不能重复导入"
                    )
                    continue
                if item.customer_id not in customers:
                    if item.customer_name is None:
                        errors.append(
                            f"第 {item.seq} 条：客户 {item.customer_id} 不存在；"
                            "导入该客户的首笔订单时必须提供 customer_name"
                        )
                        continue
                    customers[item.customer_id] = item.customer_name
                order = metrics.OrderRow(
                    order_id=item.order_id,
                    customer_id=item.customer_id,
                    amount_cents=item.amount_cents,
                    occurred_at_ms=item.occurred_at_ms,
                    status="active",
                )
                working[item.order_id] = order
                changes.append(
                    {
                        "seq": item.seq,
                        "op": "import",
                        "order_id": item.order_id,
                        "customer_id": item.customer_id,
                        "customer_name": item.customer_name,
                        "before": None,
                        "after": order_json(order),
                    }
                )
            else:
                current = existing.get(item.order_id)
                if current is None:
                    errors.append(f"第 {item.seq} 条：订单 {item.order_id} 不存在")
                    continue
                if current.status == "voided":
                    errors.append(
                        f"第 {item.seq} 条：订单 {item.order_id} 已撤销，不能再操作"
                    )
                    continue
                if item.op == "void":
                    voided = metrics.OrderRow(
                        order_id=current.order_id,
                        customer_id=current.customer_id,
                        amount_cents=current.amount_cents,
                        occurred_at_ms=current.occurred_at_ms,
                        status="voided",
                    )
                    working[item.order_id] = voided
                    changes.append(
                        {
                            "seq": item.seq,
                            "op": "void",
                            "order_id": item.order_id,
                            "customer_id": current.customer_id,
                            "customer_name": None,
                            "before": order_json(current),
                            "after": order_json(voided),
                        }
                    )
                else:
                    if item.amount_cents == current.amount_cents:
                        errors.append(
                            f"第 {item.seq} 条：订单 {item.order_id} 金额未变化"
                            "（与原金额相同），无需更正"
                        )
                        continue
                    corrected = metrics.OrderRow(
                        order_id=current.order_id,
                        customer_id=current.customer_id,
                        amount_cents=item.amount_cents,
                        occurred_at_ms=current.occurred_at_ms,
                        status="active",
                    )
                    working[item.order_id] = corrected
                    changes.append(
                        {
                            "seq": item.seq,
                            "op": "correct",
                            "order_id": item.order_id,
                            "customer_id": current.customer_id,
                            "customer_name": None,
                            "before": order_json(current),
                            "after": order_json(corrected),
                        }
                    )

        if errors:
            raise BatchError(errors)
        if not changes:
            raise BatchError(["批次没有任何可执行操作"])

        affected_ids = sorted({c["customer_id"] for c in changes})
        forecast = self._forecast(existing, working, customers, affected_ids, changes)
        forecast["operations"] = changes
        forecast["batch"] = {
            "batch_id": _new_batch_id(),
            "created_at_ms": _now_ms(),
            "operation_count": len(changes),
            "effective_operations": len(changes),
            "affected_customers": len(forecast["customers"]),
            "recomputed_cells": forecast["recomputed_cells"],
            "changed_cells": forecast["changed_cells"],
            "affected_customer_cells": forecast["affected_customer_cells"],
            "all_cells": forecast["all_cells"],
        }
        return forecast

    def _forecast(
        self,
        existing: dict[str, metrics.OrderRow],
        working: dict[str, metrics.OrderRow],
        customers: dict[str, str],
        affected_ids: list[str],
        changes: list[dict],
    ) -> dict:
        old_by_customer: dict[str, list[metrics.OrderRow]] = {}
        new_by_customer: dict[str, list[metrics.OrderRow]] = {}
        for order in existing.values():
            old_by_customer.setdefault(order.customer_id, []).append(order)
        for order in working.values():
            new_by_customer.setdefault(order.customer_id, []).append(order)

        customers_out: list[dict] = []
        total_recomputed = 0
        total_changed = 0
        total_new = 0
        total_affected_customer_cells = 0
        all_cells = sum(
            len(metrics.evaluation_instants(rows)) for rows in new_by_customer.values()
        )

        for customer_id in affected_ids:
            old_rows = old_by_customer.get(customer_id, [])
            new_rows = new_by_customer.get(customer_id, [])
            customer_changes = [c for c in changes if c["customer_id"] == customer_id]
            changed_instants = sorted(
                {c["after"]["occurred_at_ms"] for c in customer_changes}
            )
            old_instants = metrics.evaluation_instants(old_rows)
            new_instants = metrics.evaluation_instants(new_rows)
            old_set = set(old_instants)
            first = changed_instants[0]
            latest = new_instants[-1] if new_instants else first

            old_index = metrics.WindowIndex.build(metrics.active_sorted(old_rows))
            new_index = metrics.WindowIndex.build(metrics.active_sorted(new_rows))

            cells_out = []
            for instant in new_instants:
                if instant < first:
                    continue
                after = new_index.cell(instant)
                before = old_index.cell(instant) if instant in old_set else None
                changed = cell_signature(before) != cell_signature(after)
                entry = {
                    "as_of_ms": instant,
                    "as_of": iso(instant),
                    "before": cell_json(before) if before is not None else None,
                    "after": cell_json(after),
                    "changed": changed,
                    "new_cell": before is None,
                }
                if changed:
                    entry["detail"] = {
                        "before": old_index.snapshot(instant)
                        if instant in old_set
                        else None,
                        "after": new_index.snapshot(instant),
                    }
                cells_out.append(entry)

            changed_cells = sum(1 for c in cells_out if c["changed"])
            new_cells = sum(1 for c in cells_out if c["new_cell"])
            total_recomputed += len(cells_out)
            total_changed += changed_cells
            total_new += new_cells
            total_affected_customer_cells += len(new_instants)

            # Rolling cells are affected only inside each changed order's closed
            # [t, t+7d] window; overlapping windows are merged. Cumulative and
            # last-three are affected from the earliest change onward (no cell
            # exists beyond the latest anchor, so recomputation stops there).
            rolling_ranges = metrics.rolling_impact_ranges(changed_instants)
            recompute_ranges = metrics.downstream_impact_ranges(
                changed_instants, latest
            )
            customers_out.append(
                {
                    "customer_id": customer_id,
                    "customer_name": customers.get(customer_id, customer_id),
                    "operations": [c["seq"] for c in customer_changes],
                    "changed_order_ids": [c["order_id"] for c in customer_changes],
                    "first_affected_ms": first,
                    "last_affected_ms": latest,
                    "first_affected": iso(first),
                    "last_affected": iso(latest),
                    "rolling_ranges": [
                        {"start": iso(a), "end": iso(b), "start_ms": a, "end_ms": b}
                        for a, b in rolling_ranges
                    ],
                    "recompute_ranges": [
                        {"start": iso(a), "end": iso(b), "start_ms": a, "end_ms": b}
                        for a, b in recompute_ranges
                    ],
                    "total_customer_cells": len(new_instants),
                    "recomputed_cells": len(cells_out),
                    "changed_cells": changed_cells,
                    "new_cells": new_cells,
                    "unchanged_cells": len(cells_out) - changed_cells,
                    "skipped_cells": len(new_instants) - len(cells_out),
                    "cells": cells_out,
                }
            )

        return {
            "customers": customers_out,
            "recomputed_cells": total_recomputed,
            "changed_cells": total_changed,
            "new_cells": total_new,
            "affected_customer_count": len(customers_out),
            "all_customer_count": len(new_by_customer),
            "affected_customer_cells": total_affected_customer_cells,
            "all_cells": all_cells,
            "explanation": self._explain(customers_out, changes),
        }

    @staticmethod
    def _explain(customers_out: list[dict], changes: list[dict]) -> dict:
        ops_by_kind = {"import": 0, "void": 0, "correct": 0}
        for change in changes:
            ops_by_kind[change["op"]] += 1
        names = "、".join(
            f"{c['customer_name']}（{c['customer_id']}）" for c in customers_out
        )
        sentences = [
            f"本批次共 {len(changes)} 条操作（导入 {ops_by_kind['import']}、"
            f"撤销 {ops_by_kind['void']}、更正 {ops_by_kind['correct']}），"
            f"影响 {len(customers_out)} 位客户：{names}。其他客户的数据与指标不变。"
        ]
        if ops_by_kind["void"]:
            sentences.append(
                "撤销不会只抹掉一笔金额：它保留该订单的时间锚点，但会改变撤销时刻之后"
                "每个锚点的累计金额，并可能改变最近三笔的构成，因此这些锚点必须一路重算到最新。"
            )
        for customer in customers_out:
            rolling_text = "、".join(
                f"{r['start']} → {r['end']}" for r in customer["rolling_ranges"]
            )
            sentences.append(
                f"{customer['customer_name']}：滚动七日金额受影响的时间窗为 {rolling_text}"
                "（闭区间，两端都计入；同一时刻的订单也计入）。"
            )
            recompute_text = "、".join(
                f"{r['start']} → {r['end']}" for r in customer["recompute_ranges"]
            )
            sentences.append(
                f"{customer['customer_name']}：累计金额与最近三笔理论上自 "
                f"{customer['first_affected']} 起持续受影响；本次实际重算 "
                f"{Database._format_recompute_range(customer, recompute_text)}，"
                f"共 {customer['recomputed_cells']} 个锚点，"
                f"其中 {customer['changed_cells']} 个结果变化"
                + (
                    f"（新增锚点 {customer['new_cells']} 个）"
                    if customer["new_cells"]
                    else ""
                )
                + f"，更早的 {customer['skipped_cells']} 个锚点无需重算。"
            )
            if customer["changed_cells"] == 0:
                sentences.append(
                    f"{customer['customer_name']}：重算结果与修改前完全一致，"
                    "没有发布任何数值变化。"
                )
            if len(customer["rolling_ranges"]) > 1:
                sentences.append(
                    f"{customer['customer_name']}：多条操作窗口不连续，已合并为 "
                    f"{len(customer['rolling_ranges'])} 段滚动影响区间；相邻窗口会合并成一段，"
                    "不会重复计算。"
                )
            elif len(changes) > 1:
                sentences.append(
                    f"{customer['customer_name']}：多条相邻操作的影响区间已合并为 1 段，"
                    "重算不会遗漏撤销带来的后续变化。"
                )
        return {
            "headline": (
                f"影响 {len(customers_out)} 位客户，重算 "
                f"{sum(c['recomputed_cells'] for c in customers_out)} 个锚点，"
                f"其中 {sum(c['changed_cells'] for c in customers_out)} 个结果变化。"
            ),
            "sentences": sentences,
            "ops_by_kind": ops_by_kind,
        }

    @staticmethod
    def _format_recompute_range(customer: dict, text: str) -> str:
        if not customer["recompute_ranges"]:
            return "零个锚点"
        if len(customer["recompute_ranges"]) == 1:
            return f"{customer['first_affected']} → {customer['last_affected']}"
        return f"{customer['first_affected']} → {customer['last_affected']}（{text}）"

    def _apply_orders(
        self, conn: sqlite3.Connection, changes: list[dict]
    ) -> None:
        now = _now_ms()
        for change in changes:
            after = change["after"]
            if change["op"] == "import":
                conn.execute(
                    """INSERT INTO customers (customer_id, name, created_at_ms)
                       VALUES (?, ?, ?)
                       ON CONFLICT(customer_id) DO NOTHING""",
                    (
                        after["customer_id"],
                        change.get("customer_name") or after["customer_id"],
                        now,
                    ),
                )
                conn.execute(
                    """INSERT INTO orders
                       (order_id, customer_id, amount_cents, occurred_at_ms,
                        status, revision, updated_at_ms)
                       VALUES (?, ?, ?, ?, 'active', 1, ?)""",
                    (
                        after["order_id"],
                        after["customer_id"],
                        after["amount_cents"],
                        after["occurred_at_ms"],
                        now,
                    ),
                )
            elif change["op"] == "void":
                updated = conn.execute(
                    """UPDATE orders
                       SET status = 'voided', revision = revision + 1, updated_at_ms = ?
                       WHERE order_id = ? AND status = 'active'""",
                    (now, after["order_id"]),
                ).rowcount
                if updated != 1:
                    raise RuntimeError(f"撤销订单 {after['order_id']} 时状态已变化")
            else:
                updated = conn.execute(
                    """UPDATE orders
                       SET amount_cents = ?, revision = revision + 1, updated_at_ms = ?
                       WHERE order_id = ? AND status = 'active'""",
                    (after["amount_cents"], now, after["order_id"]),
                ).rowcount
                if updated != 1:
                    raise RuntimeError(f"更正订单 {after['order_id']} 时状态已变化")

    def _apply_metrics(
        self, conn: sqlite3.Connection, result: dict, batch_id: str | None
    ) -> None:
        for customer in result["customers"]:
            for cell in customer["cells"]:
                self._upsert_cell(
                    conn,
                    customer["customer_id"],
                    cell["as_of_ms"],
                    cell["after"],
                    batch_id,
                )

    def _persist_batch(
        self, conn: sqlite3.Connection, payload: dict, result: dict
    ) -> None:
        batch = result["batch"]
        batch_id = batch["batch_id"]
        conn.execute(
            """INSERT INTO batches
               (batch_id, created_at_ms, operation_count, affected_customers,
                recomputed_cells, changed_cells, affected_customer_cells,
                all_cells, request_json, summary_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                batch_id,
                batch["created_at_ms"],
                batch["operation_count"],
                batch["affected_customers"],
                batch["recomputed_cells"],
                batch["changed_cells"],
                batch["affected_customer_cells"],
                batch["all_cells"],
                json.dumps(payload, ensure_ascii=False),
                json.dumps(
                    {
                        "explanation": result["explanation"],
                        "customers": [
                            {
                                "customer_id": c["customer_id"],
                                "customer_name": c["customer_name"],
                                "first_affected": c["first_affected"],
                                "last_affected": c["last_affected"],
                                "rolling_ranges": c["rolling_ranges"],
                                "recompute_ranges": c["recompute_ranges"],
                                "recomputed_cells": c["recomputed_cells"],
                                "changed_cells": c["changed_cells"],
                                "new_cells": c["new_cells"],
                                "skipped_cells": c["skipped_cells"],
                                "total_customer_cells": c["total_customer_cells"],
                            }
                            for c in result["customers"]
                        ],
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        for change in result["operations"]:
            conn.execute(
                """INSERT INTO batch_changes
                   (batch_id, seq, op, order_id, customer_id, before_json, after_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_id,
                    change["seq"],
                    change["op"],
                    change["order_id"],
                    change["customer_id"],
                    json.dumps(change["before"], ensure_ascii=False)
                    if change["before"] is not None
                    else None,
                    json.dumps(change["after"], ensure_ascii=False),
                ),
            )
        for customer in result["customers"]:
            for cell in customer["cells"]:
                conn.execute(
                    """INSERT INTO cell_recalc_log
                       (batch_id, customer_id, as_of_ms, before_json, after_json, changed)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        batch_id,
                        customer["customer_id"],
                        cell["as_of_ms"],
                        json.dumps(cell["before"], ensure_ascii=False)
                        if cell["before"] is not None
                        else None,
                        json.dumps(cell["after"], ensure_ascii=False),
                        1 if cell["changed"] else 0,
                    ),
                )
        conn.execute(
            "INSERT INTO audit_events (at_ms, kind, detail) VALUES (?, 'batch_committed', ?)",
            (
                _now_ms(),
                json.dumps(
                    {
                        "batch_id": batch_id,
                        "changed_cells": result["changed_cells"],
                        "recomputed_cells": result["recomputed_cells"],
                    },
                    ensure_ascii=False,
                ),
            ),
        )

    # ------------------------------------------------------------------ reads
    def state(self, as_of_ms: int | None = None) -> dict:
        with self.read() as conn:
            customers = [
                dict(r)
                for r in conn.execute(
                    "SELECT customer_id, name FROM customers ORDER BY customer_id"
                ).fetchall()
            ]
            orders = [
                order_from_row(r)
                for r in conn.execute("SELECT * FROM orders").fetchall()
            ]
            by_customer: dict[str, list[metrics.OrderRow]] = {}
            for order in orders:
                by_customer.setdefault(order.customer_id, []).append(order)
            all_instants = metrics.evaluation_instants(orders)
            latest = all_instants[-1] if all_instants else _now_ms()
            selected = as_of_ms if as_of_ms is not None else latest
            rows = []
            rolling_total = 0
            active_count = 0
            for customer in customers:
                customer_orders = by_customer.get(customer["customer_id"], [])
                active = metrics.active_sorted(customer_orders)
                cell = metrics.WindowIndex.build(active).cell(selected)
                rolling_total += cell["rolling_7d_cents"]
                active_count += len(active)
                customer_instants = metrics.evaluation_instants(customer_orders)
                rows.append(
                    {
                        **customer,
                        "metrics": cell_json(cell),
                        "active_orders": len(active),
                        "voided_orders": len(customer_orders) - len(active),
                        "all_orders": len(customer_orders),
                        "latest_event_ms": customer_instants[-1]
                        if customer_instants
                        else None,
                        "latest_event": iso(customer_instants[-1])
                        if customer_instants
                        else None,
                    }
                )
            batches = [
                dict(r)
                for r in conn.execute(
                    """SELECT * FROM batches
                       ORDER BY created_at_ms DESC, batch_id DESC LIMIT 20"""
                ).fetchall()
            ]
            for batch in batches:
                batch["created_at"] = iso(batch["created_at_ms"])
            total_batches = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
            return {
                "as_of_ms": selected,
                "as_of": iso(selected),
                "latest_event_ms": latest,
                "latest_event": iso(latest),
                "evaluation_instants": [
                    {"ms": t, "iso": iso(t)} for t in all_instants
                ],
                "customers": rows,
                "batches": batches,
                "summary": {
                    "customer_count": len(customers),
                    "order_count": len(orders),
                    "active_order_count": active_count,
                    "voided_order_count": len(orders) - active_count,
                    "rolling_total_cents": rolling_total,
                    "batch_count": total_batches,
                },
                "definition": {
                    "rolling_window": "闭区间 [T−7天, T]；T 当刻与恰好 7 天前的订单都计入",
                    "cumulative": "occurred_at ≤ T 的全部有效订单",
                    "last3": "occurred_at ≤ T 的有效订单按 (occurred_at, order_id) 升序排列，取最后 3 笔求均值；同一时刻按订单号字典序，订单号大者视为更晚",
                    "voided": "已撤销订单保留时间锚点，但不计入任何金额；撤销会影响该时刻之后所有锚点的累计与最近三笔",
                    "timeline": "客户时间轴由该客户全部事件时刻组成（含仅撤销订单的时刻），每个锚点物化一行指标",
                },
            }

    def customer_detail(
        self, customer_id: str, as_of_ms: int | None = None
    ) -> dict:
        with self.read() as conn:
            customer = conn.execute(
                "SELECT customer_id, name FROM customers WHERE customer_id = ?",
                (customer_id,),
            ).fetchone()
            if customer is None:
                raise KeyError(customer_id)
            orders = [
                order_from_row(r)
                for r in conn.execute(
                    """SELECT * FROM orders WHERE customer_id = ?
                       ORDER BY occurred_at_ms, order_id""",
                    (customer_id,),
                ).fetchall()
            ]
            index = metrics.WindowIndex.build(metrics.active_sorted(orders))
            instants = metrics.evaluation_instants(orders)
            selected = (
                as_of_ms
                if as_of_ms is not None
                else (instants[-1] if instants else _now_ms())
            )
            cell = index.cell(selected)
            snapshot = index.snapshot(selected)
            by_id = {o.order_id: o for o in orders}
            return {
                "customer": dict(customer),
                "as_of_ms": selected,
                "as_of": iso(selected),
                "metrics": cell_json(cell),
                "snapshot": {
                    **snapshot,
                    "rolling_orders": [
                        order_json(by_id[i]) for i in snapshot["rolling_ids"]
                    ],
                    "last3_orders": [
                        order_json(by_id[i]) for i in snapshot["last3_ids"]
                    ],
                },
                "orders": [order_json(o) for o in orders],
                "timeline": [
                    {"as_of_ms": t, "as_of": iso(t), **cell_json(index.cell(t))}
                    for t in instants
                ],
                "evaluation_instants": [
                    {"ms": t, "iso": iso(t)} for t in instants
                ],
            }

    def batch_detail(self, batch_id: str) -> dict:
        with self.read() as conn:
            batch = conn.execute(
                "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            changes = []
            for r in conn.execute(
                "SELECT * FROM batch_changes WHERE batch_id = ? ORDER BY seq",
                (batch_id,),
            ).fetchall():
                changes.append(
                    {
                        "seq": r["seq"],
                        "op": r["op"],
                        "order_id": r["order_id"],
                        "customer_id": r["customer_id"],
                        "before": json.loads(r["before_json"])
                        if r["before_json"]
                        else None,
                        "after": json.loads(r["after_json"]),
                    }
                )
            recalc = [
                {
                    "customer_id": r["customer_id"],
                    "as_of_ms": r["as_of_ms"],
                    "as_of": iso(r["as_of_ms"]),
                    "before": json.loads(r["before_json"])
                    if r["before_json"]
                    else None,
                    "after": json.loads(r["after_json"]),
                    "changed": bool(r["changed"]),
                }
                for r in conn.execute(
                    """SELECT * FROM cell_recalc_log
                       WHERE batch_id = ? ORDER BY customer_id, as_of_ms""",
                    (batch_id,),
                ).fetchall()
            ]
            summary = json.loads(batch["summary_json"])
            return {
                "batch": {
                    **dict(batch),
                    "created_at": iso(batch["created_at_ms"]),
                    "request": json.loads(batch["request_json"]),
                },
                "summary": summary,
                "changes": changes,
                "recalc": recalc,
                "recalc_changed": sum(1 for row in recalc if row["changed"]),
            }

    def audit(self, limit: int = 50) -> list[dict]:
        with self.read() as conn:
            return [
                {
                    "id": r["id"],
                    "at": iso(r["at_ms"]),
                    "kind": r["kind"],
                    "detail": json.loads(r["detail"]),
                }
                for r in conn.execute(
                    "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    def verify(self) -> dict:
        """Recompute every cell from order facts and compare with the cache."""
        with self.read() as conn:
            orders = [
                order_from_row(r)
                for r in conn.execute("SELECT * FROM orders").fetchall()
            ]
            expected = metrics.compute_all_cells(orders)
            cached = {
                (r["customer_id"], r["as_of_ms"]): {
                    "rolling_7d_cents": r["rolling_7d_cents"],
                    "cumulative_cents": r["cumulative_cents"],
                    "last3_sum_cents": r["last3_sum_cents"],
                    "last3_count": r["last3_count"],
                    "last3_ids": json.loads(r["last3_ids"]),
                }
                for r in conn.execute("SELECT * FROM metric_cells").fetchall()
            }
            mismatches = []
            for key, cell in expected.items():
                if key not in cached:
                    mismatches.append({"key": list(key), "issue": "missing"})
                elif cell_signature(cached[key]) != cell_signature(cell):
                    mismatches.append(
                        {
                            "key": list(key),
                            "issue": "value",
                            "cached": cached[key],
                            "expected": cell,
                        }
                    )
            for key in cached:
                if key not in expected:
                    mismatches.append({"key": list(key), "issue": "stale"})
            batch_count = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
            return {
                "ok": not mismatches,
                "checked_cells": len(expected),
                "mismatches": mismatches,
                "batch_count": batch_count,
            }
