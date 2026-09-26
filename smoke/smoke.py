"""API 业务冒烟脚本（仅标准库）：在真实 HTTP 服务上端到端验证。

检查项：
1. GET /healthz 返回 200 且 status=ok；
2. 可行草稿返回 feasible=true，守恒/范围/目标值全部成立，流量为整数；
3. 不可行草稿返回 feasible=false 且给出收支诊断；
4. 非法草稿返回 HTTP 400；
5. 缓升计划 /api/plan：跨阶段可行（联合守恒+相邻调节全部成立）；
6. 缓升计划 /api/plan：调节超限判不可行，定位最早失败阶段与受限管路；
7. 旧接口 /api/balance 行为保持不变（含携带新字段的请求仍按旧契约响应）。

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


def main():
    print(f"[smoke] 目标服务 {BASE}")

    status, body = request("GET", "/healthz")
    check(status == 200 and body.get("status") == "ok",
          f"健康检查 200/status=ok（实际 {status}, {body}）")

    feasible_payload = {
        "source": {"id": "S"},
        "source_total": 10,
        "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}],
        "nodes": [{"id": "N"}],
        "pipes": [
            {"id": "p1", "from": "S", "to": "N",
             "min": 0, "max": 10, "preferred": 5},
            {"id": "p2", "from": "N", "to": "A",
             "min": 0, "max": 10, "preferred": 3},
            {"id": "p3", "from": "N", "to": "B",
             "min": 0, "max": 10, "preferred": 7},
            {"id": "p4", "from": "S", "to": "A",
             "min": 0, "max": 0, "preferred": 0},
        ],
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

    # ---- 5. 缓升计划：跨阶段可行（必须联合满足守恒与相邻调节） ----
    plan_payload = {
        "source": {"id": "S"}, "source_total": 10,
        "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}],
        "nodes": [{"id": "N"}],
        "pipes": [
            {"id": "p1", "from": "S", "to": "N",
             "min": 0, "max": 10, "preferred": 5, "max_adjust": 2},
            {"id": "p2", "from": "N", "to": "A",
             "min": 0, "max": 10, "preferred": 3, "max_adjust": 2},
            {"id": "p3", "from": "N", "to": "B",
             "min": 0, "max": 10, "preferred": 7, "max_adjust": 2},
            {"id": "p4", "from": "S", "to": "A",
             "min": 0, "max": 0, "preferred": 0, "max_adjust": 2},
        ],
        "stages": [
            {"source_total": 10,
             "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}]},
            {"source_total": 10,
             "zones": [{"id": "A", "demand": 8}, {"id": "B", "demand": 2}]},
        ],
    }
    status, r = request("POST", "/api/plan", plan_payload)
    check(status == 200 and r.get("feasible") is True,
          f"缓升计划可行 HTTP 200/feasible=true（实际 {status}, {r.get('feasible') if status==200 else r}）")
    check(r["stage_count"] == 2 and len(r["stages"]) == 2,
          "返回 2 个阶段的逐阶段结果")
    check(all(isinstance(v, int) for v in r["tie_sequence"]),
          f"全部阶段流量均为整数（{r['tie_sequence']}）")
    check(r["tie_sequence"] == [10, 6, 4, 0, 10, 8, 2, 0],
          f"展平决胜序列符合联合最优（实际 {r['tie_sequence']}）")
    for si, s in enumerate(r["stages"]):
        check(s["balances"]["source"]["difference"] == 0,
              f"第 {si + 1} 阶段水源守恒差额为 0")
        check(all(n["difference"] == 0 for n in s["balances"]["nodes"]),
              f"第 {si + 1} 阶段分流节点守恒差额为 0")
        check(all(z["difference"] == 0 for z in s["balances"]["zones"]),
              f"第 {si + 1} 阶段分区精确满足需求")
        for f in s["flows"]:
            check(f["min"] <= f["flow"] <= f["max"],
                  f"第 {si + 1} 阶段管路 {f['pipe_id']} 流量在上下限内")
    for a in r["adjustments"]:
        check(a["within_limit"],
              f"阶段 {a['from_stage'] + 1}→{a['to_stage'] + 1} 整体未越调节限")
        for p in a["pipes"]:
            check(p["change"] == abs(p["to_flow"] - p["from_flow"]),
                  f"管路 {p['pipe_id']} 调整量与两阶段流量差一致")
            check(p["change"] <= p["limit"],
                  f"管路 {p['pipe_id']} 调整量 {p['change']} 未越过上限 {p['limit']}")
    # 相邻阶段流量由后端联合求出，逐阶段独立拼接无法做到平滑过渡
    check(r["stages"][1]["flows"][1]["change"] == 2,
          "管路 p2 从第 1 阶段 6 缓升至第 2 阶段 8（联合配平，非拼接）")

    # ---- 6. 缓升计划：调节超限不可行，定位最早失败阶段与受限管路 ----
    over_payload = json.loads(json.dumps(plan_payload))
    for p in over_payload["pipes"]:
        p["max_adjust"] = 1
    status, r = request("POST", "/api/plan", over_payload)
    check(status == 200 and r.get("feasible") is False,
          "调节超限判为业务不可行（HTTP 200 feasible=false）")
    info = r["infeasibility"]
    check(info["failing_stage"] == 1 and info["kind"] == "adjustment",
          f"最早失败为第 2 阶段且属调节约束（实际 {info['failing_stage']}, {info['kind']}）")
    check(info["gap_total"] == 2,
          f"越限缺口合计为 2（实际 {info['gap_total']}）")
    blocked = {b["pipe_id"]: b for b in info["blocked_pipes"]}
    check(set(blocked) == {"p2", "p3"},
          f"受限管路为 p2、p3（实际 {sorted(blocked)}）")
    for pid, (anchor, required, excess) in {
            "p2": (6, 8, 1), "p3": (4, 2, 1)}.items():
        b = blocked[pid]
        check(b["anchor_flow"] == anchor and b["required_flow"] == required
              and b["excess"] == excess,
              f"受限管路 {pid} 锚定/所需/缺口 = {anchor}/{required}/{excess}"
              f"（实际 {b['anchor_flow']}/{b['required_flow']}/{b['excess']}）")
    check(bool(info["reasons"]), "给出可读的中文不可行原因")
    check(len(info.get("relaxed_stage_flows") or []) == 4,
          "附带放松调节后的该阶段全管配水供定位缺口")

    # ---- 7. 旧接口兼容：携带新字段的请求走 /api/balance 仍按旧契约 ----
    compat_payload = json.loads(json.dumps(plan_payload))
    status, r = request("POST", "/api/balance", compat_payload)
    check(status == 200 and r.get("feasible") is True,
          "/api/balance 忽略 stages/max_adjust 仍返回单阶段可行结论")
    check(r["tie_sequence"] == [10, 6, 4, 0],
          f"/api/balance 决胜序列保持旧契约（实际 {r['tie_sequence']}）")
    check("stages" not in r and "stage_count" not in r,
          "/api/balance 响应体不混入多阶段字段")
    # 同一旧样例的既有断言（与 smoke 第 2 项相同契约）
    status, r = request("POST", "/api/balance", feasible_payload)
    check(status == 200 and r["tie_sequence"] == [10, 6, 4, 0]
          and r["objective"] == 11,
          "/api/balance 原样例序列 [10,6,4,0]、偏差 11 保持不变")

    print("[smoke] 全部冒烟断言通过")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke] 失败：{exc}", file=sys.stderr)
        sys.exit(1)
