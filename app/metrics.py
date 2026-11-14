"""Pure window-metric semantics.

All timestamps are integer epoch milliseconds (UTC). All money is integer cents.

Definitions (also surfaced in the UI and README):

* Rolling 7-day amount at an evaluation instant T is the sum of **active** order
  amounts whose ``occurred_at`` lies in the closed interval
  ``[T - 7 days, T]``. Both boundaries are inclusive, so orders at the same
  instant as T are included, and an order exactly 7 days old is still counted.
* Cumulative amount at T is the sum of active orders with ``occurred_at <= T``.
* Last-three average at T uses active orders with ``occurred_at <= T`` ordered
  by ``(occurred_at ASC, order_id ASC)``; it averages the final up-to-three
  orders. Same-instant orders are ordered by their stable, lexicographic order
  id; the larger id is treated as later. Averages are reported as the exact
  integer sum plus the count, never a rounded intermediate.
* Voided orders keep their event instant as a timeline anchor but contribute
  nothing to any metric.
* The metric timeline is the set of all event instants for a customer, including
  instants that only carry a voided order. No other instants are materialized,
  so "every affected cell" means every timeline anchor at or after the first
  changed instant.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Iterable, Sequence

ROLLING_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
LAST_N = 3


@dataclass(frozen=True)
class OrderRow:
    order_id: str
    customer_id: str
    amount_cents: int
    occurred_at_ms: int
    status: str

    @property
    def active(self) -> bool:
        return self.status == "active"


def sort_key(order: OrderRow) -> tuple[int, str]:
    return (order.occurred_at_ms, order.order_id)


def active_sorted(orders: Iterable[OrderRow]) -> list[OrderRow]:
    return sorted((o for o in orders if o.active), key=sort_key)


def evaluation_instants(orders: Iterable[OrderRow]) -> list[int]:
    """Every event instant is a timeline anchor, including voided orders."""
    return sorted({o.occurred_at_ms for o in orders})


@dataclass
class WindowIndex:
    """Sorted active orders plus prefix sums; immutable after construction."""

    active: list[OrderRow]
    times: list[int]
    prefix: list[int]

    @classmethod
    def build(cls, active: Sequence[OrderRow]) -> "WindowIndex":
        times = [o.occurred_at_ms for o in active]
        prefix = [0]
        total = 0
        for order in active:
            total += order.amount_cents
            prefix.append(total)
        return cls(active=list(active), times=times, prefix=prefix)

    def cell(self, as_of_ms: int) -> dict:
        end = bisect.bisect_right(self.times, as_of_ms)
        cumulative = self.prefix[end]
        start = bisect.bisect_left(self.times, as_of_ms - ROLLING_WINDOW_MS)
        rolling = self.prefix[end] - self.prefix[start]
        tail_start = max(0, end - LAST_N)
        return {
            "rolling_7d_cents": rolling,
            "cumulative_cents": cumulative,
            "last3_sum_cents": self.prefix[end] - self.prefix[tail_start],
            "last3_count": end - tail_start,
            "last3_ids": [o.order_id for o in self.active[tail_start:end]],
        }

    def snapshot(self, as_of_ms: int) -> dict:
        end = bisect.bisect_right(self.times, as_of_ms)
        start = bisect.bisect_left(self.times, as_of_ms - ROLLING_WINDOW_MS)
        tail_start = max(0, end - LAST_N)
        return {
            "rolling_ids": [o.order_id for o in self.active[start:end]],
            "rolling_sum_cents": self.prefix[end] - self.prefix[start],
            "cumulative_ids": [o.order_id for o in self.active[:end]],
            "last3_ids": [o.order_id for o in self.active[tail_start:end]],
            "last3_sum_cents": self.prefix[end] - self.prefix[tail_start],
            "active_order_count": end,
        }


def compute_cell(active: Sequence[OrderRow], as_of_ms: int) -> dict:
    """Compute one metric cell from an already-sorted active order sequence."""
    return WindowIndex.build(active).cell(as_of_ms)


def cell_sort_key(cell: dict) -> tuple:
    return (
        cell["rolling_7d_cents"],
        cell["cumulative_cents"],
        cell["last3_sum_cents"],
        cell["last3_count"],
        tuple(cell["last3_ids"]),
    )


def compute_customer_cells(
    orders: Sequence[OrderRow], eval_instants: Sequence[int]
) -> dict[int, dict]:
    index = WindowIndex.build(active_sorted(orders))
    return {t: index.cell(t) for t in eval_instants}


def compute_all_cells(orders: Sequence[OrderRow]) -> dict[tuple[str, int], dict]:
    by_customer: dict[str, list[OrderRow]] = {}
    for order in orders:
        by_customer.setdefault(order.customer_id, []).append(order)
    result: dict[tuple[str, int], dict] = {}
    for customer_id, rows in by_customer.items():
        index = WindowIndex.build(active_sorted(rows))
        for instant in evaluation_instants(rows):
            result[(customer_id, instant)] = index.cell(instant)
    return result


def window_snapshot(orders: Sequence[OrderRow], as_of_ms: int) -> dict:
    """Return the exact orders that make up a cell, for impact explanations."""
    return WindowIndex.build(active_sorted(orders)).snapshot(as_of_ms)


def merged_ranges(
    ranges: Iterable[tuple[int, int]], latest_ms: int | None = None
) -> list[tuple[int, int]]:
    """Merge overlapping or immediately adjacent closed intervals.

    Ends are clipped to ``latest_ms`` when supplied, because no metric cell can
    change beyond the final evaluation instant.
    """
    clipped: list[tuple[int, int]] = []
    for start, end in ranges:
        if latest_ms is not None:
            end = min(end, latest_ms)
        if end < start:
            continue
        clipped.append((start, end))
    if not clipped:
        return []
    clipped.sort()
    merged = [list(clipped[0])]
    for start, end in clipped[1:]:
        last = merged[-1]
        # +1 ms makes closed intervals that merely touch merge into one range.
        if start <= last[1] + 1:
            if end > last[1]:
                last[1] = end
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def rolling_impact_ranges(
    changed_instants: Iterable[int], latest_ms: int | None = None
) -> list[tuple[int, int]]:
    return merged_ranges(
        [(t, t + ROLLING_WINDOW_MS) for t in changed_instants], latest_ms
    )


def downstream_impact_ranges(
    changed_instants: Iterable[int], latest_ms: int | None = None
) -> list[tuple[int, int]]:
    return merged_ranges(
        [(t, latest_ms if latest_ms is not None else t) for t in changed_instants],
        latest_ms,
    )
