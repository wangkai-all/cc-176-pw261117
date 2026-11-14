#!/usr/bin/env python3
"""Preview and optionally commit a batch JSON file against a running server.

Usage:
    python3 samples/run_batch.py samples/void-old.json
    python3 samples/run_batch.py samples/void-old.json --commit
    python3 samples/run_batch.py samples/void-old.json --commit --simulate-failure

The script prints the impact explanation and exits non-zero if the batch was
rejected or rolled back, so it works as a smoke test in scripts.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def call(base: str, path: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def print_impact(result: dict) -> None:
    print(result["explanation"]["headline"])
    for sentence in result["explanation"]["sentences"]:
        print(f"  · {sentence}")
    for customer in result.get("customers", []):
        print(
            f"\n{customer['customer_id']} {customer['customer_name']}: "
            f"重算 {customer['recomputed_cells']} / 客户锚点 {customer['total_customer_cells']}，"
            f"变化 {customer['changed_cells']}，新增 {customer['new_cells']}，"
            f"更早未重算 {customer['skipped_cells']}"
        )
        for cell in customer["cells"]:
            if not cell["changed"]:
                continue
            before = cell["before"]
            after = cell["after"]
            before_text = (
                f"滚动 {before['rolling_7d_cents']/100:.2f} 累计 {before['cumulative_cents']/100:.2f}"
                if before else "（无旧值）"
            )
            after_text = (
                f"滚动 {after['rolling_7d_cents']/100:.2f} 累计 {after['cumulative_cents']/100:.2f}"
            )
            print(f"    {cell['as_of']}  {before_text}  →  {after_text}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("file")
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--simulate-failure", action="store_true")
    args = parser.parse_args()

    with open(args.file, encoding="utf-8") as handle:
        payload = json.load(handle)
    if args.simulate_failure:
        payload["simulate_failure"] = True

    if not 0 <= len(payload.get("operations", [])) <= 500:
        print("operations 数量不合法", file=sys.stderr)
        return 2
    path = "/api/commit" if args.commit else "/api/preview"
    status, result = call(args.base, path, payload)
    if status == 422:
        print("批次校验失败：", file=sys.stderr)
        for error in result.get("errors", []):
            print(f"  · {error}", file=sys.stderr)
        return 2
    if status >= 400 and not result.get("rolled_back"):
        print(f"HTTP {status}: {result.get('error')}", file=sys.stderr)
        return 1

    print_impact(result)
    if result.get("rolled_back"):
        print(f"\n已回滚：{result['error']}", file=sys.stderr)
        return 3
    if args.commit:
        print(f"\n已提交批次 {result['batch_id']}")
    else:
        print("\n（预览未写入；加 --commit 提交）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
