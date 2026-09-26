"""缓升计划：2~4 个湿润阶段的**联合**整数配水。

业务
----
在既有管路草稿（水源/分区/分流节点/管路/上下限/优选量）之上录入：
- 2~4 个按顺序排列的湿润阶段，每阶段给出水源总量与各分区精确需求；
- 每条既有管路一个非负整数 `max_adjust`：相邻阶段该管流量之差
  绝对值的统一上限。

服务端把**全部阶段放进同一个整数规划联合求解**（绝不逐阶段独立配平后
拼接）：

    变量 f[k][i] ∈ [min_i, max_i]，整数
    每阶段 k：
        水源流出 = 该阶段水源总量
        分流节点流入 = 流出
        分区流入 = 该阶段该分区需求
    相邻阶段：|f[k][i] - f[k-1][i]| ≤ max_adjust_i

目标
----
Σ_k Σ_i |f[k][i] - preferred_i| 最小；并列时按"阶段顺序优先、
阶段内管路录入顺序次之"展平的流量序列取字典序最小。沿用 balance.py 的
分层大权编码：每单位主偏差权重 P，录入第 i 管的决胜权重 q_i，
目标即 Σ(P·|f-pref| + q·(f-pref))。

不可行诊断
----------
按阶段前缀（0..k 联合）逐个试解，定位**最早**无法与前序阶段同时
满足调节约束的阶段 k：
- k=0：单阶段草稿自身不守恒，直接给出逐点收支缺口；
- k≥1：以前 k 个阶段的联合最优末段流量为锚，放松第 k 阶段调节约束，
  最小化各管越限总量；越限 >0 的管路即"受限管路"，
  报告其锚定流量、所需变化量、允许调节量与超出量（缺口）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .balance import ValidationError, solve as solve_single, validate_and_build
from .milp import Model, solve_milp

MIN_STAGES, MAX_STAGES = 2, 4


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #

def validate_plan(payload: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]],
                                        List[int]]:
    """校验缓升计划请求。

    返回 (基础草稿模型, 阶段列表[{'total','demands':{zone:demand}}],
    各管调节上限)。草稿部分复用 /api/balance 的全部既有校验。
    """
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")

    model = validate_and_build(payload)  # 水源/分区/节点/管路/上下限/优选量
    pipes = model["pipes"]
    zone_ids = [z["id"] for z in model["zones"]]

    raw_stages = payload.get("stages")
    if not isinstance(raw_stages, list):
        raise ValidationError("stages 必须是数组")
    if not (MIN_STAGES <= len(raw_stages) <= MAX_STAGES):
        raise ValidationError(
            f"湿润阶段数量必须在 {MIN_STAGES}~{MAX_STAGES} 个之间")

    stages: List[Dict[str, Any]] = []
    for k, st in enumerate(raw_stages):
        label = f"第 {k + 1} 阶段"
        if not isinstance(st, dict):
            raise ValidationError(f"{label}必须是对象")
        if "source_total" not in st:
            raise ValidationError(f"{label}缺少水源总量 source_total")
        total = st["source_total"]
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise ValidationError(f"{label}水源总量必须是非负整数")

        raw_zs = st.get("zones")
        if not isinstance(raw_zs, list):
            raise ValidationError(f"{label}的 zones 必须是数组")
        seen_zone = set()
        demands: Dict[str, int] = {}
        for z in raw_zs:
            if not isinstance(z, dict):
                raise ValidationError(f"{label}每个分区必须是对象")
            zid = z.get("id")
            if zid not in zone_ids:
                raise ValidationError(
                    f"{label}分区 {zid} 不在草稿分区名单内"
                    "（各阶段分区必须与草稿一致）")
            if zid in seen_zone:
                raise ValidationError(f"{label}分区 {zid} 重复填写")
            seen_zone.add(zid)
            d = z.get("demand")
            if not isinstance(d, int) or isinstance(d, bool) or d < 0:
                raise ValidationError(f"{label}分区 {zid} 的需求必须是非负整数")
            demands[zid] = d
        if seen_zone != set(zone_ids):
            missing = [z for z in zone_ids if z not in seen_zone]
            raise ValidationError(f"{label}缺少分区需求：{', '.join(missing)}")
        stages.append({"index": k, "total": total, "demands": demands})

    # 每条既有管路随草稿一起提交相邻阶段最大调节量 max_adjust
    # （validate_and_build 只保留既有字段，按录入顺序与原始管路配对读取）
    raw_pipes = payload.get("pipes")
    limits: List[int] = []
    for i, (p, raw) in enumerate(zip(pipes, raw_pipes)):
        c = raw.get("max_adjust")
        if not isinstance(c, int) or isinstance(c, bool):
            raise ValidationError(
                f"第 {i + 1} 条管路（{p['id']}）必须填写整数最大调节量"
                " max_adjust")
        if c < 0:
            raise ValidationError(
                f"第 {i + 1} 条管路（{p['id']}）最大调节量不能为负数")
        limits.append(c)

    return model, stages, limits


# --------------------------------------------------------------------------- #
# 权重与建模
# --------------------------------------------------------------------------- #

def _weights(pipes: List[Dict[str, Any]], t_count: int
             ) -> Tuple[List[List[int]], int]:
    """分层权重（按"阶段优先、阶段内管序次之"展平）。

    q[k][i] 对应展平位置 k·m+i：早位权重压倒其后**所有阶段**的管路，
    故最小费用等价于先最小化总绝对偏差、再取展平序列字典序最小。
    P = 1 + 全部线性决胜项的最大摆幅：1 单位主偏差压倒一切决胜差异。
    """
    m = len(pipes)
    ranges = [p["max"] - p["min"] for p in pipes]
    q: List[List[int]] = [[0] * m for _ in range(t_count)]
    weight = 0
    for k in range(t_count - 1, -1, -1):
        for i in range(m - 1, -1, -1):
            q[k][i] = weight + 1
            weight += q[k][i] * ranges[i]
    primary = weight + 1
    return q, primary


def _build_joint(model: Dict[str, Any], stages: List[Dict[str, Any]],
                 limits: List[int], with_objective: bool = True
                 ) -> Tuple[Model, List[List[int]], int]:
    """构造全部阶段联合的整数规划。

    返回 (mip, flow_vars[t][i], objective_constant)。
    with_objective=False 时仅用于可行性判定（目标系数全 0）。
    """
    pipes = model["pipes"]
    nodes = model["nodes"]
    source_id = model["source_id"]
    zone_ids = [z["id"] for z in model["zones"]]
    m = len(pipes)
    t_count = len(stages)

    q, primary = _weights(pipes, t_count)
    mip = Model()
    flows: List[List[int]] = []
    const = 0

    for k in range(t_count):
        row: List[int] = []
        for i, p in enumerate(pipes):
            coef = q[k][i] if with_objective else 0
            row.append(mip.var(
                p["min"], p["max"], coef=coef,
                name=f"f_{k}_{p['id']}"))
            if with_objective:
                const -= q[k][i] * p["preferred"]
        flows.append(row)

    # 偏差 epigraph：t[k][i] ≥ |f - pref|，目标 P·t
    devs: List[List[int]] = [[0] * m for _ in range(t_count)]
    if with_objective:
        for k in range(t_count):
            for i, p in enumerate(pipes):
                lo, hi, pref = p["min"], p["max"], p["preferred"]
                t = mip.var(0, hi - lo, coef=primary,
                            name=f"dev_{k}_{p['id']}", branchable=False)
                devs[k][i] = t
                f = flows[k][i]
                mip.ge([(t, 1), (f, -1)], -pref)   # t - f ≥ -pref
                mip.ge([(t, 1), (f, 1)], pref)     # t + f ≥ pref

    # 每阶段守恒
    for k, st in enumerate(stages):
        out_terms: Dict[str, List[Tuple[int, int]]] = {}
        in_terms: Dict[str, List[Tuple[int, int]]] = {}
        for i, p in enumerate(pipes):
            f = flows[k][i]
            out_terms.setdefault(p["from"], []).append((f, 1))
            in_terms.setdefault(p["to"], []).append((f, 1))
        mip.eq(out_terms.get(source_id, []), st["total"])
        for nid in nodes:
            terms = [(j, a) for j, a in in_terms.get(nid, [])] + [
                (j, -a) for j, a in out_terms.get(nid, [])]
            mip.eq(terms, 0)
        for zid in zone_ids:
            mip.eq(in_terms.get(zid, []), st["demands"][zid])

    # 相邻阶段调节：d[k-1][i] ∈ [0, c]，d ≥ |f_k - f_{k-1}|
    for k in range(1, t_count):
        for i, p in enumerate(pipes):
            cap = min(limits[i], p["max"] - p["min"])
            d = mip.var(0, cap, coef=0,
                        name=f"adj_{k}_{p['id']}", branchable=False)
            fa, fb = flows[k - 1][i], flows[k][i]
            mip.ge([(d, 1), (fb, -1), (fa, 1)], 0)  # d ≥ f_k - f_{k-1}
            mip.ge([(d, 1), (fa, -1), (fb, 1)], 0)  # d ≥ f_{k-1} - f_k

    return mip, flows, const


# --------------------------------------------------------------------------- #
# 结果组装
# --------------------------------------------------------------------------- #

def _stage_report(model: Dict[str, Any], stage: Dict[str, Any],
                  flows_k: List[int], changes: List[Optional[int]],
                  limits: List[int]) -> Dict[str, Any]:
    pipes = model["pipes"]
    inflow: Dict[str, int] = {v: 0 for v in _vertices(model)}
    outflow: Dict[str, int] = {v: 0 for v in _vertices(model)}
    flow_rows = []
    deviation = 0
    for i, (p, f) in enumerate(zip(pipes, flows_k)):
        dev = abs(f - p["preferred"])
        deviation += dev
        outflow[p["from"]] += f
        inflow[p["to"]] += f
        change = changes[i]
        flow_rows.append({
            "pipe_id": p["id"], "order": p["order"],
            "from": p["from"], "to": p["to"],
            "min": p["min"], "max": p["max"],
            "preferred": p["preferred"], "flow": f, "deviation": dev,
            "change": change,
            "change_limit": None if change is None else limits[i],
            "within_limit": None if change is None else change <= limits[i],
        })

    sid = model["source_id"]
    return {
        "index": stage["index"],
        "source_total": stage["total"],
        "zone_demands": [
            {"id": z["id"], "demand": stage["demands"][z["id"]]}
            for z in model["zones"]],
        "objective": deviation,
        "flows": flow_rows,
        "balances": {
            "source": {
                "id": sid, "outflow": outflow[sid],
                "total": stage["total"],
                "difference": outflow[sid] - stage["total"],
            },
            "nodes": [{
                "id": nid,
                "inflow": inflow[nid], "outflow": outflow[nid],
                "difference": inflow[nid] - outflow[nid],
            } for nid in model["nodes"]],
            "zones": [{
                "id": z["id"], "demand": stage["demands"][z["id"]],
                "inflow": inflow[z["id"]],
                "difference": inflow[z["id"]] - stage["demands"][z["id"]],
            } for z in model["zones"]],
        },
    }


def _vertices(model: Dict[str, Any]) -> List[str]:
    return ([model["source_id"]] + model["nodes"]
            + [z["id"] for z in model["zones"]])


def _adjustments_report(pipes: List[Dict[str, Any]],
                        all_flows: List[List[int]],
                        limits: List[int]) -> List[Dict[str, Any]]:
    reps = []
    for k in range(1, len(all_flows)):
        rows = []
        all_within = True
        for i, p in enumerate(pipes):
            a, b = all_flows[k - 1][i], all_flows[k][i]
            change = abs(b - a)
            within = change <= limits[i]
            all_within = all_within and within
            rows.append({
                "pipe_id": p["id"], "order": p["order"],
                "from_flow": a, "to_flow": b,
                "change": change, "limit": limits[i],
                "within_limit": within,
            })
        reps.append({
            "from_stage": k - 1, "to_stage": k,
            "within_limit": all_within, "pipes": rows,
        })
    return reps


# --------------------------------------------------------------------------- #
# 不可行诊断
# --------------------------------------------------------------------------- #

def _single_stage_payload(model: Dict[str, Any],
                          stage: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": {"id": model["source_id"]},
        "source_total": stage["total"],
        "zones": [{"id": z["id"], "demand": stage["demands"][z["id"]]}
                  for z in model["zones"]],
        "nodes": [{"id": n} for n in model["nodes"]],
        "pipes": [{
            "id": p["id"], "from": p["from"], "to": p["to"],
            "min": p["min"], "max": p["max"], "preferred": p["preferred"],
        } for p in model["pipes"]],
    }


def _prefix_optimal(model: Dict[str, Any], stages: List[Dict[str, Any]],
                    limits: List[int]):
    """联合求 stages 前缀的最优整数解；不可行返回 None。"""
    mip, flows, _const = _build_joint(model, stages, limits)
    status, x, _obj = solve_milp(mip)
    if status != "optimal":
        return None
    return [[x[flows[k][i]] for i in range(len(model["pipes"]))]
            for k in range(len(stages))]


def _diagnose_blocked(model: Dict[str, Any], prev_flows: List[int],
                      stage: Dict[str, Any], limits: List[int]
                      ) -> Tuple[List[Dict[str, Any]], int, List[Dict[str, Any]]]:
    """放松调节约束解第 k 阶段：最小化各管相对锚定流量的越限总量。

    返回 (受限管路明细, 越限缺口合计, 放松后该阶段全部管路流量明细)。
    """
    pipes = model["pipes"]
    nodes = model["nodes"]
    source_id = model["source_id"]
    zone_ids = [z["id"] for z in model["zones"]]
    m = len(pipes)
    q0, primary = _weights(pipes, 1)
    q = q0[0]
    range_sum = sum(p["max"] - p["min"] for p in pipes)
    big = 1 + primary * range_sum + sum(
        q[i] * (pipes[i]["max"] - pipes[i]["min"]) for i in range(m))

    mip = Model()
    f = [mip.var(p["min"], p["max"], coef=q[i], name=f"f_{pipes[i]['id']}")
         for i, p in enumerate(pipes)]
    dev = []
    for i, p in enumerate(pipes):
        t = mip.var(0, p["max"] - p["min"], coef=primary,
                    name=f"dev_{pipes[i]['id']}", branchable=False)
        dev.append(t)
        mip.ge([(t, 1), (f[i], -1)], -p["preferred"])
        mip.ge([(t, 1), (f[i], 1)], p["preferred"])
    # w_i ≥ 越限量；主目标 BIG·Σw 压过一切偏好/决胜项
    excess = []
    for i, p in enumerate(pipes):
        c = limits[i]
        anchor = prev_flows[i]
        w = mip.var(0, p["max"] - p["min"], coef=big,
                    name=f"over_{pipes[i]['id']}", branchable=False)
        excess.append(w)
        # w ≥ f - anchor - c  ⟺  w - f ≥ -(anchor + c)
        mip.ge([(w, 1), (f[i], -1)], -(anchor + c))
        # w ≥ anchor - f - c  ⟺  w + f ≥ anchor - c
        mip.ge([(w, 1), (f[i], 1)], anchor - c)

    out_terms: Dict[str, List[Tuple[int, int]]] = {}
    in_terms: Dict[str, List[Tuple[int, int]]] = {}
    for i, p in enumerate(pipes):
        out_terms.setdefault(p["from"], []).append((f[i], 1))
        in_terms.setdefault(p["to"], []).append((f[i], 1))
    mip.eq(out_terms.get(source_id, []), stage["total"])
    for nid in nodes:
        terms = in_terms.get(nid, []) + [(j, -a)
                                         for j, a in out_terms.get(nid, [])]
        mip.eq(terms, 0)
    for zid in zone_ids:
        mip.eq(in_terms.get(zid, []), stage["demands"][zid])

    status, x, _ = solve_milp(mip)
    if status != "optimal":
        return [], 0, []  # 放松后仍不可行，由上层改按阶段内部不可行另诊

    blocked = []
    total_gap = 0
    for i, p in enumerate(pipes):
        w = x[excess[i]]
        if w > 0:
            req = abs(x[f[i]] - prev_flows[i])
            blocked.append({
                "pipe_id": p["id"], "order": p["order"],
                "from": p["from"], "to": p["to"],
                "anchor_flow": prev_flows[i],
                "required_flow": x[f[i]],
                "required_change": req,
                "allowed_change": limits[i],
                "excess": w,
            })
            total_gap += w
    blocked.sort(key=lambda r: (-(r["excess"]), r["order"]))
    relaxed_rows = [{
        "pipe_id": p["id"], "order": p["order"],
        "from": p["from"], "to": p["to"],
        "anchor_flow": prev_flows[i],
        "flow": x[f[i]],
        "change": abs(x[f[i]] - prev_flows[i]),
        "change_limit": limits[i],
        "within_limit": abs(x[f[i]] - prev_flows[i]) <= limits[i],
    } for i, p in enumerate(pipes)]
    return blocked, total_gap, relaxed_rows


def _fail(stage_count: int, failing_stage: Optional[int], kind: str,
          gap_total: int, blocked: List[Dict[str, Any]],
          reasons: List[str],
          deficits: Optional[List[Dict[str, Any]]] = None,
          surpluses: Optional[List[Dict[str, Any]]] = None,
          relaxed: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "feasible": False,
        "stage_count": stage_count,
        "stages": None,
        "objective": None,
        "tie_sequence": None,
        "adjustments": None,
        "infeasibility": {
            "failing_stage": failing_stage,
            "kind": kind,
            "gap_total": gap_total,
            "blocked_pipes": blocked,
            "relaxed_stage_flows": relaxed,
            "deficits": deficits or [],
            "surpluses": surpluses or [],
            "reasons": reasons,
        },
    }


def _infeasible(model: Dict[str, Any], stages: List[Dict[str, Any]],
                limits: List[int]) -> Dict[str, Any]:
    """定位最早失败阶段并给出缺口与受限管路。"""
    pipes = model["pipes"]
    t_count = len(stages)

    # 阶段 0 单独是否成立（也是长度 1 的前缀）
    prefix_flows = _prefix_optimal(model, stages[:1], limits)
    if prefix_flows is None:
        single = solve_single(_single_stage_payload(model, stages[0]))
        info = single["infeasibility"] or {}
        return _fail(
            t_count, 0, "stage_infeasible",
            info.get("shortfall_flow", 0), [],
            ["第 1 阶段自身不满足水源/节点/分区守恒或管路上下限，"
             "尚未进入相邻阶段调节就已无解："] + info.get("reasons", []),
            info.get("deficits", []), info.get("surpluses", []))

    # 逐前缀定位最早的调节性失败（前缀均为联合求解）
    failing = -1
    for k in range(1, t_count):
        flows_k = _prefix_optimal(model, stages[:k + 1], limits)
        if flows_k is None:
            failing = k
            break
        prefix_flows = flows_k
    if failing < 0:
        # 理论上不应发生（联合已判不可行却找不出失败前缀）
        return _fail(t_count, None, "unknown", 0, [],
                     ["联合模型不可行，但未能定位失败阶段（内部错误）"])

    anchor = prefix_flows[-1]  # 前 failing 个阶段联合最优解的末段流量
    blocked, gap_total, relaxed = _diagnose_blocked(
        model, anchor, stages[failing], limits)

    if not blocked:
        # 放松调节仍不可行：该阶段自身收支不成立
        single = solve_single(
            _single_stage_payload(model, stages[failing]))
        info = single["infeasibility"] or {}
        return _fail(
            t_count, failing, "stage_infeasible",
            info.get("shortfall_flow", 0), [],
            [f"第 {failing + 1} 阶段即使不考虑与前序阶段的调节限制，"
             "自身也不满足守恒或管路上下限："] + info.get("reasons", []),
            info.get("deficits", []), info.get("surpluses", []))

    reasons = [
        f"第 {failing + 1} 阶段无法与前 {failing} 个阶段同时满足相邻调节约束"
        f"（最早失败阶段）：以下 {len(blocked)} 条管路按前序联合最优配水，"
        f"进入该阶段所需变化量超过允许的最大调节量，越限缺口合计 {gap_total}。"
        "（relaxed_stage_flows 给出放松调节限制后该阶段的最小越限配水，"
        "供定位缺口参考；它不构成可行计划。）"
    ]
    for b in blocked:
        reasons.append(
            f"管路 {b['pipe_id']}（{b['from']}→{b['to']}）："
            f"前序末段流量 {b['anchor_flow']}，本阶段至少需调到 "
            f"{b['required_flow']}（变化 {b['required_change']}），"
            f"允许最大调节量仅 {b['allowed_change']}，缺口 {b['excess']}")

    return _fail(t_count, failing, "adjustment", gap_total, blocked,
                 reasons, relaxed=relaxed)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def solve_plan(payload: Any) -> Dict[str, Any]:
    """缓升计划联合求解。校验失败由调用方转 400。"""
    model, stages, limits = validate_plan(payload)
    pipes = model["pipes"]

    mip, flows, _const = _build_joint(model, stages, limits)
    status, x, _obj = solve_milp(mip)

    if status != "optimal":
        return _infeasible(model, stages, limits)

    all_flows = [[x[flows[k][i]] for i in range(len(pipes))]
                 for k in range(len(stages))]

    stage_reports = []
    total_dev = 0
    for k, st in enumerate(stages):
        changes: List[Optional[int]] = [None] * len(pipes)
        if k > 0:
            changes = [abs(all_flows[k][i] - all_flows[k - 1][i])
                       for i in range(len(pipes))]
        rep = _stage_report(model, st, all_flows[k], changes, limits)
        total_dev += rep["objective"]
        stage_reports.append(rep)

    tie_sequence = [v for k in range(len(stages)) for v in all_flows[k]]
    return {
        "feasible": True,
        "stage_count": len(stages),
        "stages": stage_reports,
        "objective": total_dev,
        "tie_sequence": tie_sequence,
        "adjustments": _adjustments_report(pipes, all_flows, limits),
        "infeasibility": None,
    }
