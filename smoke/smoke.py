"""API 业务冒烟脚本（仅标准库）：在真实 HTTP 服务上端到端验证。

检查项：
1. GET /healthz 返回 200 且 status=ok；
2. 【旧接口兼容】/api/balance：可行草稿守恒/范围/目标值全部成立、流量为整数；
   不可行草稿给出收支诊断；非法草稿返回 HTTP 400；
3. 【缓升计划·跨阶段可行】/api/plan：多阶段联合求解可行，逐阶段守恒、
   相邻阶段调整量不越限、流量为整数、目标值与决胜序列正确；
4. 【缓升计划·调节超限不可行】/api/plan：相邻阶段调节量不足时判不可行，
   并返回最早不可行阶段、调节缺口与受限管路。

由容器内 exec 执行，默认访问本机 8000；可用 BASE_URL 覆盖。
任何断言失败即以非零退出码报告。
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def request(method, path, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


BALANCE_PIPES = [
    {"id": "p1", "from": "S", "to": "N",
     "min": 0, "max": 10, "preferred": 5},
    {"id": "p2", "from": "N", "to": "A",
     "min": 0, "max": 10, "preferred": 3},
    {"id": "p3", "from": "N", "to": "B",
     "min": 0, "max": 10, "preferred": 7},
    {"id": "p4", "from": "S", "to": "A",
     "min": 0, "max": 0, "preferred": 0},
]


def smoke_balance_compat():
    """旧接口 /api/balance 行为保持不变。"""
    print("[smoke] 旧接口兼容：/api/balance")
    feasible_payload = {
        "source": {"id": "S"},
        "source_total": 10,
        "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}],
        "nodes": [{"id": "N"}],
        "pipes": BALANCE_PIPES,
    }
    status, r = request("POST", "/api/balance", feasible_payload)
    check(status == 200, f"可行草稿 HTTP 200（实际 {status}）")
    check(r["feasible"] is True, "结论为可行")
    check(r["tie_sequence"] == [10, 6, 4, 0],
          f"流量序列 [10,6,4,0]（实际 {r['tie_sequence']}）")
    check(all(isinstance(x, int) for x in r["tie_sequence"]),
          "所有流量均为整数")
    check(r["objective"] == 11, f"绝对偏差和为 11（实际 {r['objective']}）")
    src = r["balances"]["source"]
    check(src["outflow"] == 10 and src["difference"] == 0,
          "水源流出恰等于总量 10")
    node = r["balances"]["nodes"][0]
    check(node["inflow"] == node["outflow"] == 10,
          "分流节点流入=流出=10")
    for zrow, need in zip(r["balances"]["zones"], (6, 4)):
        check(zrow["inflow"] == need and zrow["difference"] == 0,
              f"分区 {zrow['id']} 流入恰等于需求 {need}")
    for f in r["flows"]:
        check(f["min"] <= f["flow"] <= f["max"],
              f"管路 {f['pipe_id']} 流量 {f['flow']} 在范围内")

    infeasible_payload = json.loads(json.dumps(feasible_payload))
    # A 需求改成 60：总量与需求不等且容量不足，必不可行
    infeasible_payload["zones"][0]["demand"] = 60
    status, r = request("POST", "/api/balance", infeasible_payload)
    check(status == 200 and r["feasible"] is False,
          "超量需求判为不可行（HTTP 200 业务结论）")
    info = r["infeasibility"]
    check(info["total_demand"] == 64 and info["shortfall_flow"] > 0,
          "诊断含需求合计与流量缺口")
    check(bool(info["reasons"]), "给出可读的不可行原因")

    bad_payload = {"source": {"id": "S"}, "source_total": 1,
                   "zones": [{"id": "A", "demand": 1}],
                   "nodes": [], "pipes": []}
    status, r = request("POST", "/api/balance", bad_payload)
    check(status == 400 and "error" in r,
          f"非法草稿返回 400（实际 {status}）")


def plan_pipes(adjusts):
    pipes = json.loads(json.dumps(BALANCE_PIPES))
    for p, a in zip(pipes, adjusts):
        p["max_adjust"] = a
    return pipes


def smoke_plan_feasible():
    """缓升计划：跨阶段联合可行。"""
    print("[smoke] 缓升计划：跨阶段联合可行")
    payload = {
        "source": {"id": "S"},
        "nodes": [{"id": "N"}],
        "zones": [{"id": "A"}, {"id": "B"}],
        "pipes": plan_pipes([3, 2, 2, 0]),
        "stages": [
            {"source_total": 4,
             "zones": [{"id": "A", "demand": 2}, {"id": "B", "demand": 2}]},
            {"source_total": 7,
             "zones": [{"id": "A", "demand": 4}, {"id": "B", "demand": 3}]},
            {"source_total": 10,
             "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}]},
        ],
    }
    status, r = request("POST", "/api/plan", payload)
    check(status == 200, f"缓升计划 HTTP 200（实际 {status}）")
    check(r["feasible"] is True, "跨阶段联合求解结论为可行")
    check(r["tie_sequence"] == [4, 2, 2, 0, 7, 4, 3, 0, 10, 6, 4, 0],
          f"决胜序列按阶段×管路展开（实际 {r['tie_sequence']}）")
    check(all(isinstance(x, int) for x in r["tie_sequence"]),
          "全部阶段流量均为整数")
    check(r["objective"] == 25,
          f"总绝对偏差为 25（实际 {r['objective']}）")
    check(len(r["stages"]) == 3, "返回 3 个阶段的逐阶段流量")
    for st, (total, demands) in zip(r["stages"],
                                    [(4, (2, 2)), (7, (4, 3)), (10, (6, 4))]):
        check(st["balances"]["source"]["outflow"] == total
              and st["balances"]["source"]["difference"] == 0,
              f"{st['label']}水源流出恰等于该阶段总量 {total}")
        for zrow, need in zip(st["balances"]["zones"], demands):
            check(zrow["inflow"] == need and zrow["difference"] == 0,
                  f"{st['label']}分区 {zrow['id']} 流入恰等于需求 {need}")
        node = st["balances"]["nodes"][0]
        check(node["difference"] == 0, f"{st['label']}分流节点收支相等")
        for f in st["flows"]:
            check(f["min"] <= f["flow"] <= f["max"],
                  f"{st['label']}管路 {f['pipe_id']} 流量 {f['flow']} 在范围内")
    check(len(r["adjustments"]) == 2, "返回 2 段相邻阶段调整量")
    for adj in r["adjustments"]:
        for prow in adj["pipes"]:
            check(prow["within_limit"]
                  and prow["abs_change"] <= prow["max_adjust"],
                  f"{adj['label']}管路 {prow['pipe_id']} 调整量 "
                  f"{prow['abs_change']} 未越过 {prow['max_adjust']}")


def smoke_plan_adjustment_infeasible():
    """缓升计划：调节超限不可行，返回最早阶段/缺口/受限管路。"""
    print("[smoke] 缓升计划：调节超限不可行")
    payload = {
        "source": {"id": "S"},
        "nodes": [{"id": "N"}],
        "zones": [{"id": "A"}, {"id": "B"}],
        "pipes": plan_pipes([1, 1, 1, 0]),  # 调节量仅 1，阶段跳变 4→10 不可达
        "stages": [
            {"source_total": 4,
             "zones": [{"id": "A", "demand": 2}, {"id": "B", "demand": 2}]},
            {"source_total": 10,
             "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}]},
        ],
    }
    status, r = request("POST", "/api/plan", payload)
    check(status == 200 and r["feasible"] is False,
          "调节量不足判为不可行（HTTP 200 业务结论）")
    info = r["infeasibility"]
    check(info["stage"] == 1 and info["kind"] == "adjustment",
          f"最早不可行阶段为第 2 阶段且类型为调节超限（实际 {info}）")
    check(info["adjustment"]["gap"] > 0,
          f"返回调节总缺口 {info['adjustment']['gap']}")
    limited = info["adjustment"]["limited_pipes"]
    check(bool(limited), "返回受限管路列表")
    for lp in limited:
        check(lp["needed_adjust"] > lp["max_adjust"]
              and lp["gap"] == lp["needed_adjust"] - lp["max_adjust"],
              f"管路 {lp['pipe_id']} 需要调节 {lp['needed_adjust']} 超过允许 "
              f"{lp['max_adjust']}，缺口 {lp['gap']}")
    check(bool(info["reasons"]), "给出可读的不可行原因")

    # 非法计划（阶段数不足）返回 400
    bad = json.loads(json.dumps(payload))
    bad["stages"] = bad["stages"][:1]
    status, r = request("POST", "/api/plan", bad)
    check(status == 400 and "error" in r,
          f"阶段数不足返回 400（实际 {status}）")


def main():
    print(f"[smoke] 目标服务 {BASE}")

    status, body = request("GET", "/healthz")
    check(status == 200 and body.get("status") == "ok",
          f"健康检查 200/status=ok（实际 {status}, {body}）")

    smoke_balance_compat()
    smoke_plan_feasible()
    smoke_plan_adjustment_infeasible()

    print("[smoke] 全部冒烟断言通过")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke] 失败：{exc}", file=sys.stderr)
        sys.exit(1)
