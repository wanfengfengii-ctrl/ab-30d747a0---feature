"""经 Web（nginx 反代）做联调冒烟：静态健康端点 + 透传业务 API。

BASE_URL 默认 http://web（compose 网络内的 web 服务）。
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("WEB_URL", "http://web").rstrip("/")


def fail(msg):
    raise AssertionError(msg)


def main():
    print(f"[web-smoke] 目标 {BASE}")

    with urllib.request.urlopen(BASE + "/healthz", timeout=10) as resp:
        text = resp.read().decode("utf-8")
        if resp.status != 200 or "ok" not in text:
            fail(f"Web 健康检查异常：{resp.status} {text!r}")
    print("  ✓ GET /healthz 经 nginx 返回 200/ok")

    payload = {
        "source": {"id": "S"}, "source_total": 10,
        "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}],
        "nodes": [{"id": "N"}],
        "pipes": [
            {"id": "p1", "from": "S", "to": "N", "min": 0, "max": 10, "preferred": 5},
            {"id": "p2", "from": "N", "to": "A", "min": 0, "max": 10, "preferred": 3},
            {"id": "p3", "from": "N", "to": "B", "min": 0, "max": 10, "preferred": 7},
            {"id": "p4", "from": "S", "to": "A", "min": 0, "max": 0, "preferred": 0},
        ],
    }
    req = urllib.request.Request(
        BASE + "/api/balance",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if resp.status != 200 or not data.get("feasible"):
        fail(f"经 nginx 的业务配平失败：{data}")
    print(f"  ✓ POST /api/balance 经 nginx 透传成功，序列 {data['tie_sequence']}")

    # 缓升计划经同一反代透传
    plan_payload = json.loads(json.dumps(payload))
    for p in plan_payload["pipes"]:
        p["max_adjust"] = 2
    plan_payload["stages"] = [
        {"source_total": 10,
         "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}]},
        {"source_total": 10,
         "zones": [{"id": "A", "demand": 8}, {"id": "B", "demand": 2}]},
    ]
    req = urllib.request.Request(
        BASE + "/api/plan",
        data=json.dumps(plan_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if resp.status != 200 or not data.get("feasible") or data.get("stage_count") != 2:
        fail(f"经 nginx 的缓升计划失败：{data}")
    if data["tie_sequence"] != [10, 6, 4, 0, 10, 8, 2, 0]:
        fail(f"缓升计划展平序列异常：{data['tie_sequence']}")
    print(f"  ✓ POST /api/plan 经 nginx 透传成功，展平序列 {data['tie_sequence']}")
    print("[web-smoke] Web/API 联调全部通过")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[web-smoke] 失败：{exc}", file=sys.stderr)
        sys.exit(1)
