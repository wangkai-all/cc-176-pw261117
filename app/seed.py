"""Seed data and demo batches.

The seed exercises the edge cases the brief calls out:

* ``C-1001`` has two orders at the same instant, so same-instant ordering and
  window boundaries are visible.
* ``C-1002`` has an order exactly 7 days before the latest anchor, so the
  inclusive window edge matters.
* ``C-1003`` has a gap of more than 7 days, so the rolling window empties and
  refills.
* ``C-1004`` is a smaller account, useful for checking that unaffected customers
  stay untouched.

Demo batches are plain API payloads, so the same JSON can be posted from the
page, the CLI or a test:

* ``late-correction`` corrects an order dated in the past (late correction that
  shifts every later anchor, not the current one).
* ``void-old`` voids an old order, which changes cumulative and last-three all
  the way to the latest anchor.
* ``same-instant`` imports a same-instant order next to an existing one.
* ``cross-window-void`` voids an order and then imports a replacement more than
  seven days later, so the rolling window empties and refills.
* ``adjacent-batch`` corrects several adjacent orders in one batch, exercising
  merged impact ranges.
"""

from __future__ import annotations

from .metrics import OrderRow
from .timeutil import parse_timestamp

CUSTOMERS = [
    ("C-1001", "青禾书店"),
    ("C-1002", "远山咖啡馆"),
    ("C-1003", "北岸花店"),
    ("C-1004", "拾光工作室"),
]

# (order_id, customer_id, amount in yuan as string, occurred_at)
SEED_ORDERS = [
    ("O-1001", "C-1001", "1200.00", "2026-11-01T09:00:00Z"),
    ("O-1002", "C-1001", "860.50", "2026-11-03T09:00:00Z"),
    ("O-1003", "C-1001", "430.00", "2026-11-06T09:00:00Z"),
    ("O-1004", "C-1001", "1580.00", "2026-11-09T09:00:00Z"),
    ("O-1005", "C-1001", "240.00", "2026-11-09T09:00:00Z"),  # same instant as O-1004
    ("O-1006", "C-1001", "990.00", "2026-11-12T09:00:00Z"),
    ("O-2001", "C-1002", "2300.00", "2026-11-02T10:00:00Z"),
    ("O-2002", "C-1002", "410.00", "2026-11-08T10:00:00Z"),
    ("O-2003", "C-1002", "775.00", "2026-11-14T10:00:00Z"),  # 7 days before O-2004
    ("O-2004", "C-1002", "1260.00", "2026-11-21T10:00:00Z"),
    ("O-3001", "C-1003", "330.00", "2026-10-20T08:00:00Z"),
    ("O-3002", "C-1003", "510.00", "2026-10-22T08:00:00Z"),
    ("O-3003", "C-1003", "95.00", "2026-11-05T08:00:00Z"),
    ("O-3004", "C-1003", "120.00", "2026-11-18T08:00:00Z"),
    ("O-4001", "C-1004", "300.00", "2026-11-07T12:00:00Z"),
    ("O-4002", "C-1004", "300.00", "2026-11-13T12:00:00Z"),
]


def seed_orders() -> list[OrderRow]:
    from .db import parse_amount

    return [
        OrderRow(
            order_id=order_id,
            customer_id=customer_id,
            amount_cents=parse_amount(amount),
            occurred_at_ms=parse_timestamp(when),
            status="active",
        )
        for order_id, customer_id, amount, when in SEED_ORDERS
    ]


DEMO_BATCHES = {
    "late-correction": {
        "title": "迟到更正：改一笔旧订单金额",
        "note": "更正 11-03 的订单，后续每个锚点的累计金额都会变化。",
        "payload": {
            "operations": [
                {"op": "correct", "order_id": "O-1002", "amount": "1000.00"}
            ]
        },
    },
    "void-old": {
        "title": "撤销旧订单：影响一路延伸到最新",
        "note": "撤销 11-01 的订单，滚动窗口、累计金额与最近三笔同时受影响。",
        "payload": {
            "operations": [{"op": "void", "order_id": "O-1001"}]
        },
    },
    "same-instant": {
        "title": "同时间订单：同一时刻插入新订单",
        "note": "在 11-09T09:00Z 再插入一笔订单，与 O-1004 同一时刻，按订单号排序。",
        "payload": {
            "operations": [
                {
                    "op": "import",
                    "order_id": "O-1007",
                    "customer_id": "C-1001",
                    "amount": "310.00",
                    "occurred_at": "2026-11-09T09:00:00Z",
                }
            ]
        },
    },
    "cross-window-void": {
        "title": "跨窗口删除：撤销后窗口清空再回填",
        "note": "先撤销 10-20 的订单，再导入一笔 11-15 的替代订单，跨越 7 天窗口。",
        "payload": {
            "operations": [
                {"op": "void", "order_id": "O-3001"},
                {
                    "op": "import",
                    "order_id": "O-3005",
                    "customer_id": "C-1003",
                    "amount": "260.00",
                    "occurred_at": "2026-11-15T08:00:00Z",
                },
            ]
        },
    },
    "adjacent-batch": {
        "title": "相邻批量更正：合并影响范围",
        "note": "一次更正 C-1001 的三笔相邻订单，影响区间应合并而不是重复计算。",
        "payload": {
            "operations": [
                {"op": "correct", "order_id": "O-1002", "amount": "900.00"},
                {"op": "correct", "order_id": "O-1003", "amount": "500.00"},
                {"op": "correct", "order_id": "O-1004", "amount": "1600.00"},
            ]
        },
    },
}
