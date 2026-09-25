"""缓升计划：多阶段联合配平（2~4 个按顺序的湿润阶段）。

与 /api/balance 的单阶段配平不同，本模块从完整草稿**联合**求出全部
阶段的整数流量（绝不逐阶段独立配平后再拼接）：
- 任一阶段仍满足：水源流出恰等于该阶段水源总量、分流节点收支相等、
  分区流入恰等于该阶段需求、管路流量落在原有 [min, max] 内；
- 同一管路相邻阶段的流量差绝对值不超过该管的 max_adjust；
- 所有可行计划中，先取相对各管路既有优选量的**总绝对偏差**最小者，
  再按"阶段顺序 × 每阶段管路录入顺序"展开的流量序列字典序稳定决胜。

建模
----
变量：管路 i 阶段 t 的流量 y = min + z1 + z2，其中
z1 ∈ [0, pref-min]、z2 ∈ [0, max-pref]（|y-pref| 的线性化：
偏离优选量 1 单位恰使 -z1+z2 增加 1，最优解自动取 z1 先满）。
约束矩阵（阶段守恒 + 相邻阶段差分 + 变量上界）经检验为全单模，
故精确单纯形（app/lpsolve.py）的顶点解必为整数。
目标按字典序：① 总绝对偏差；② 展开流量序列逐位最小。

不可行诊断
----------
先定位**最早**无法与前序阶段同时满足调节约束的阶段 t*（前缀可行性
单调：前缀 t 不可行则更长前缀必不可行）；再对前缀 0..t* 求最小违约
LP：阶段 t* 守恒软约束优先，其次 t*-1→t* 调节溢出。若守恒违约 > 0，
报告该阶段逐点缺口/积压；否则报告调节缺口与受限管路。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .balance import ValidationError, _is_int
from .lpsolve import solve_lex

# 录入数量约束（与单阶段配平一致；阶段数 2~4）
MIN_ZONES, MAX_ZONES = 2, 4
MAX_NODES = 4
MIN_PIPES, MAX_PIPES = 4, 10
MIN_STAGES, MAX_STAGES = 2, 4

__all__ = ["solve_plan", "validate_plan", "ValidationError"]


def _require_int(obj: Dict[str, Any], key: str, label: str,
                 minimum: int = 0) -> int:
    if key not in obj:
        raise ValidationError(f"{label}缺失（字段 {key}）")
    v = obj[key]
    if not _is_int(v):
        raise ValidationError(f"{label}必须是整数")
    if v < minimum:
        raise ValidationError(f"{label}不能小于 {minimum}")
    return v


def validate_plan(payload: Any) -> Dict[str, Any]:
    """校验缓升计划草稿并整理为内部结构。"""
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")

    src = payload.get("source")
    if not isinstance(src, dict) or not str(src.get("id", "")).strip():
        raise ValidationError("必须指定一处水源（source.id）")
    source_id = str(src["id"]).strip()

    raw_zones = payload.get("zones")
    raw_nodes = payload.get("nodes", [])
    raw_pipes = payload.get("pipes")
    raw_stages = payload.get("stages")
    if not isinstance(raw_zones, list):
        raise ValidationError("zones 必须是数组")
    if not isinstance(raw_nodes, list):
        raise ValidationError("nodes 必须是数组")
    if not isinstance(raw_pipes, list):
        raise ValidationError("pipes 必须是数组")
    if not isinstance(raw_stages, list):
        raise ValidationError("stages 必须是数组（2~4 个按顺序的湿润阶段）")

    if not (MIN_ZONES <= len(raw_zones) <= MAX_ZONES):
        raise ValidationError(f"分区数量必须在 {MIN_ZONES}~{MAX_ZONES} 个之间")
    if len(raw_nodes) > MAX_NODES:
        raise ValidationError(f"分流节点数量不能超过 {MAX_NODES} 个")
    if not (MIN_PIPES <= len(raw_pipes) <= MAX_PIPES):
        raise ValidationError(f"管路数量必须在 {MIN_PIPES}~{MAX_PIPES} 条之间")
    if not (MIN_STAGES <= len(raw_stages) <= MAX_STAGES):
        raise ValidationError(
            f"湿润阶段数量必须在 {MIN_STAGES}~{MAX_STAGES} 个之间")

    zone_ids: List[str] = []
    nodes: List[str] = []
    seen: Dict[str, str] = {source_id: "水源"}

    def check_id(rid: Any, label: str) -> str:
        if not isinstance(rid, str) or not rid.strip():
            raise ValidationError(f"{label}的 id 不能为空")
        rid = rid.strip()
        if rid in seen:
            raise ValidationError(f"id 重复：{rid}（已被{seen[rid]}占用）")
        seen[rid] = label
        return rid

    for z in raw_zones:
        if not isinstance(z, dict):
            raise ValidationError("每个分区必须是对象")
        zone_ids.append(check_id(z.get("id"), "分区"))

    for nd in raw_nodes:
        if not isinstance(nd, dict):
            raise ValidationError("每个分流节点必须是对象")
        nodes.append(check_id(nd.get("id"), "分流节点"))

    pipes: List[Dict[str, Any]] = []
    pipe_ids: Dict[str, str] = {}
    for i, p in enumerate(raw_pipes):
        label = f"第 {i + 1} 条管路"
        if not isinstance(p, dict):
            raise ValidationError(f"{label}必须是对象")
        pid = p.get("id")
        if not isinstance(pid, str) or not pid.strip():
            raise ValidationError(f"{label}缺少 id")
        pid = pid.strip()
        if pid in pipe_ids:
            raise ValidationError(f"管路 id 重复：{pid}")
        pipe_ids[pid] = label

        u = p.get("from")
        v = p.get("to")
        if not isinstance(u, str) or not isinstance(v, str):
            raise ValidationError(f"{label}（{pid}）必须指定起点 from 和终点 to")
        if u not in seen:
            raise ValidationError(f"{label}（{pid}）起点 {u} 不存在")
        if v not in seen:
            raise ValidationError(f"{label}（{pid}）终点 {v} 不存在")
        if seen[u] == "分区":
            raise ValidationError(f"{label}（{pid}）不能从分区 {u} 接出（分区只进水）")
        if seen[v] == "水源":
            raise ValidationError(f"{label}（{pid}）不能接入水源 {v}（水源只出水）")
        if u == v:
            raise ValidationError(f"{label}（{pid}）起点终点不能相同")

        lo = _require_int(p, "min", f"{label}（{pid}）最小量")
        hi = _require_int(p, "max", f"{label}（{pid}）最大量")
        pref = _require_int(p, "preferred", f"{label}（{pid}）优选量")
        adj = _require_int(p, "max_adjust", f"{label}（{pid}）最大调节量")
        if lo > hi:
            raise ValidationError(f"{label}（{pid}）最小量不能大于最大量")
        if not (lo <= pref <= hi):
            raise ValidationError(f"{label}（{pid}）优选量必须位于最小量与最大量之间")

        pipes.append({"id": pid, "from": u, "to": v, "min": lo, "max": hi,
                      "preferred": pref, "max_adjust": adj, "order": i})

    stages: List[Dict[str, Any]] = []
    for t, st in enumerate(raw_stages):
        label = f"第 {t + 1} 阶段"
        if not isinstance(st, dict):
            raise ValidationError(f"{label}必须是对象")
        total = _require_int(st, "source_total", f"{label}水源总量")
        raw_demands = st.get("zones")
        if not isinstance(raw_demands, list):
            raise ValidationError(f"{label}的 zones 必须是数组（各分区精确需求）")
        demands: Dict[str, int] = {}
        for zd in raw_demands:
            if not isinstance(zd, dict):
                raise ValidationError(f"{label}的每个分区需求必须是对象")
            zid = zd.get("id")
            if not isinstance(zid, str) or not zid.strip():
                raise ValidationError(f"{label}存在缺少 id 的分区需求")
            zid = zid.strip()
            if zid not in zone_ids:
                raise ValidationError(f"{label}的分区 {zid} 不在草稿分区列表中")
            if zid in demands:
                raise ValidationError(f"{label}的分区 {zid} 需求重复填写")
            demands[zid] = _require_int(zd, "demand", f"{label}分区 {zid} 的需求")
        missing = [zid for zid in zone_ids if zid not in demands]
        if missing:
            raise ValidationError(
                f"{label}缺少分区 {missing[0]} 的精确需求（每阶段须填写全部"
                f" {len(zone_ids)} 个分区的需求）")
        stages.append({"source_total": total, "demands": demands})

    return {
        "source_id": source_id,
        "nodes": nodes,
        "zone_ids": zone_ids,
        "pipes": pipes,
        "stages": stages,
    }


# ----------------------------------------------------------------------
# LP 建模
# ----------------------------------------------------------------------

def _zvar(t: int, i: int, which: int, m: int) -> int:
    """管路 i 阶段 t 的 z1(which=0)/z2(which=1) 变量下标。"""
    return (t * m + i) * 2 + which


def _network_rows(model: Dict[str, Any], n_stages: int,
                  n_vars: int) -> List[Tuple[list, str, int]]:
    """阶段 0..n_stages-1 的守恒等式行。"""
    pipes = model["pipes"]
    m = len(pipes)
    vertices = [model["source_id"]] + model["nodes"] + model["zone_ids"]
    rows: List[Tuple[list, str, int]] = []
    for t in range(n_stages):
        st = model["stages"][t]
        required = {model["source_id"]: -st["source_total"]}
        for nd in model["nodes"]:
            required[nd] = 0
        for zid in model["zone_ids"]:
            required[zid] = st["demands"][zid]
        for v in vertices:
            row = [0] * n_vars
            rhs = required[v]
            for i, p in enumerate(pipes):
                if p["to"] == v:
                    row[_zvar(t, i, 0, m)] += 1
                    row[_zvar(t, i, 1, m)] += 1
                    rhs -= p["min"]
                if p["from"] == v:
                    row[_zvar(t, i, 0, m)] -= 1
                    row[_zvar(t, i, 1, m)] -= 1
                    rhs += p["min"]
            rows.append((row, "=", rhs))
    return rows


def _diff_rows(model: Dict[str, Any], n_stages: int,
               n_vars: int) -> List[Tuple[list, str, int]]:
    """相邻阶段调节约束 ±(y_t - y_{t-1}) ≤ max_adjust（min 平移后抵消）。"""
    pipes = model["pipes"]
    m = len(pipes)
    rows: List[Tuple[list, str, int]] = []
    for i, p in enumerate(pipes):
        for t in range(1, n_stages):
            row = [0] * n_vars
            row[_zvar(t, i, 0, m)] = 1
            row[_zvar(t, i, 1, m)] = 1
            row[_zvar(t - 1, i, 0, m)] = -1
            row[_zvar(t - 1, i, 1, m)] = -1
            rows.append((row, "<=", p["max_adjust"]))
            rows.append(([-v for v in row], "<=", p["max_adjust"]))
    return rows


def _bound_rows(model: Dict[str, Any], n_stages: int,
                n_vars: int) -> List[Tuple[list, str, int]]:
    """z1 ≤ pref-min、z2 ≤ max-pref（上下界行）。"""
    pipes = model["pipes"]
    m = len(pipes)
    rows: List[Tuple[list, str, int]] = []
    for t in range(n_stages):
        for i, p in enumerate(pipes):
            r1 = [0] * n_vars
            r1[_zvar(t, i, 0, m)] = 1
            rows.append((r1, "<=", p["preferred"] - p["min"]))
            r2 = [0] * n_vars
            r2[_zvar(t, i, 1, m)] = 1
            rows.append((r2, "<=", p["max"] - p["preferred"]))
    return rows


def _main_objectives(model: Dict[str, Any], n_stages: int,
                     n_vars: int) -> List[list]:
    """字典序目标：① 总绝对偏差（-z1+z2）；② 展开流量序列逐位。"""
    pipes = model["pipes"]
    m = len(pipes)
    dev = [0] * n_vars
    for t in range(n_stages):
        for i in range(m):
            dev[_zvar(t, i, 0, m)] = -1
            dev[_zvar(t, i, 1, m)] = 1
    objectives = [dev]
    for t in range(n_stages):
        for i in range(m):
            row = [0] * n_vars
            row[_zvar(t, i, 0, m)] = 1
            row[_zvar(t, i, 1, m)] = 1
            objectives.append(row)
    return objectives


def _extract_flows(model: Dict[str, Any], n_stages: int,
                   x: List[int]) -> List[List[int]]:
    """由 z 变量还原各阶段管路流量 y[t][i]。"""
    pipes = model["pipes"]
    m = len(pipes)
    return [
        [pipes[i]["min"] + x[_zvar(t, i, 0, m)] + x[_zvar(t, i, 1, m)]
         for i in range(m)]
        for t in range(n_stages)
    ]


def _prefix_feasible(model: Dict[str, Any], n_stages: int) -> bool:
    """前缀 0..n_stages-1 是否联合可行。"""
    m = len(model["pipes"])
    n_vars = 2 * m * n_stages
    rows = (_network_rows(model, n_stages, n_vars)
            + _diff_rows(model, n_stages, n_vars)
            + _bound_rows(model, n_stages, n_vars))
    status, _x, _v = solve_lex(n_vars, rows, [[0] * n_vars])
    return status == "optimal"


# ----------------------------------------------------------------------
# 求解与诊断
# ----------------------------------------------------------------------

def solve_plan(payload: Any) -> Dict[str, Any]:
    """求缓升计划。校验失败抛 ValidationError 由调用方转 400。"""
    model = validate_plan(payload)
    n_stages = len(model["stages"])
    m = len(model["pipes"])
    n_vars = 2 * m * n_stages

    rows = (_network_rows(model, n_stages, n_vars)
            + _diff_rows(model, n_stages, n_vars)
            + _bound_rows(model, n_stages, n_vars))
    objectives = _main_objectives(model, n_stages, n_vars)
    status, x, _values = solve_lex(n_vars, rows, objectives)

    if status != "optimal":
        return _infeasible_response(model)

    flows = _extract_flows(model, n_stages, x)
    return _feasible_response(model, flows)


def _feasible_response(model: Dict[str, Any],
                       flows: List[List[int]]) -> Dict[str, Any]:
    pipes = model["pipes"]
    n_stages = len(model["stages"])
    tie_sequence = [f for stage in flows for f in stage]
    objective = sum(abs(f - p["preferred"])
                    for stage in flows for f, p in zip(stage, pipes))

    stage_rows = []
    for t in range(n_stages):
        st = model["stages"][t]
        inflow: Dict[str, int] = {}
        outflow: Dict[str, int] = {}
        flow_rows = []
        for i, p in enumerate(pipes):
            f = flows[t][i]
            dev = abs(f - p["preferred"])
            outflow[p["from"]] = outflow.get(p["from"], 0) + f
            inflow[p["to"]] = inflow.get(p["to"], 0) + f
            flow_rows.append({
                "pipe_id": p["id"], "order": p["order"],
                "from": p["from"], "to": p["to"],
                "min": p["min"], "max": p["max"],
                "preferred": p["preferred"], "flow": f, "deviation": dev,
            })
        sid = model["source_id"]
        node_rows = [{
            "id": nd,
            "inflow": inflow.get(nd, 0), "outflow": outflow.get(nd, 0),
            "difference": inflow.get(nd, 0) - outflow.get(nd, 0),
        } for nd in model["nodes"]]
        zone_rows = [{
            "id": zid, "demand": st["demands"][zid],
            "inflow": inflow.get(zid, 0),
            "difference": inflow.get(zid, 0) - st["demands"][zid],
        } for zid in model["zone_ids"]]
        stage_rows.append({
            "index": t,
            "label": f"第 {t + 1} 阶段",
            "source_total": st["source_total"],
            "flows": flow_rows,
            "balances": {
                "source": {
                    "id": sid, "outflow": outflow.get(sid, 0),
                    "total": st["source_total"],
                    "difference": outflow.get(sid, 0) - st["source_total"],
                },
                "nodes": node_rows,
                "zones": zone_rows,
            },
        })

    adjustments = []
    for t in range(1, n_stages):
        pipe_rows = []
        for i, p in enumerate(pipes):
            prev_f, next_f = flows[t - 1][i], flows[t][i]
            change = next_f - prev_f
            pipe_rows.append({
                "pipe_id": p["id"], "order": p["order"],
                "previous_flow": prev_f, "next_flow": next_f,
                "change": change, "abs_change": abs(change),
                "max_adjust": p["max_adjust"],
                "within_limit": abs(change) <= p["max_adjust"],
            })
        adjustments.append({
            "from_stage": t - 1, "to_stage": t,
            "label": f"第 {t} 阶段 → 第 {t + 1} 阶段",
            "pipes": pipe_rows,
        })

    return {
        "feasible": True,
        "objective": objective,
        "tie_sequence": tie_sequence,
        "stage_count": n_stages,
        "stages": stage_rows,
        "adjustments": adjustments,
        "infeasibility": None,
    }


def _infeasible_response(model: Dict[str, Any]) -> Dict[str, Any]:
    """定位最早不可行前缀阶段，并求最小违约诊断。"""
    n_stages = len(model["stages"])
    # 前缀可行性单调，线性扫描找到最早不可行阶段 t*
    first_bad: Optional[int] = None
    for t in range(1, n_stages + 1):
        if not _prefix_feasible(model, t):
            first_bad = t - 1
            break
    if first_bad is None:
        # 完整 LP 不可行但所有前缀可行，理论上不可达
        first_bad = n_stages - 1

    diag = _diagnose(model, first_bad)
    return {
        "feasible": False,
        "objective": None,
        "tie_sequence": None,
        "stage_count": n_stages,
        "stages": None,
        "adjustments": None,
        "infeasibility": diag,
    }


def _diagnose(model: Dict[str, Any], t_star: int) -> Dict[str, Any]:
    """对前缀 0..t* 求最小违约 LP，给出缺口与受限对象。"""
    pipes = model["pipes"]
    m = len(pipes)
    vertices = [model["source_id"]] + model["nodes"] + model["zone_ids"]
    kind_of = {model["source_id"]: "水源"}
    kind_of.update({nd: "分流节点" for nd in model["nodes"]})
    kind_of.update({zid: "分区" for zid in model["zone_ids"]})

    n_prefix = t_star + 1
    n_main = 2 * m * n_prefix
    # 追加变量：阶段 t* 每顶点 缺口/积压，过渡 t*-1→t* 每管路 调节溢出
    vidx: Dict[str, Tuple[int, int]] = {}
    n_vars = n_main
    for v in vertices:
        vidx[v] = (n_vars, n_vars + 1)  # (缺口 short, 积压 excess)
        n_vars += 2
    oidx: List[Optional[int]] = [None] * m
    if t_star >= 1:
        for i in range(m):
            oidx[i] = n_vars
            n_vars += 1

    rows: List[Tuple[list, str, int]] = []
    # 前序阶段守恒：硬约束
    rows += _network_rows(model, t_star, n_vars)
    # 阶段 t* 守恒：软约束（缺口/积压变量）
    soft = _network_rows(model, n_prefix, n_vars)[-len(vertices):]
    for (row, _sense, rhs), v in zip(soft, vertices):
        short, excess = vidx[v]
        row[short] = 1
        row[excess] = -1
        rows.append((row, "=", rhs))
    # 前序过渡调节：硬约束；末段过渡：软约束（溢出变量）
    rows += _diff_rows(model, t_star, n_vars)
    if t_star >= 1:
        for i, p in enumerate(pipes):
            ov = oidx[i]
            assert ov is not None
            row = [0] * n_vars
            row[_zvar(t_star, i, 0, m)] = 1
            row[_zvar(t_star, i, 1, m)] = 1
            row[_zvar(t_star - 1, i, 0, m)] = -1
            row[_zvar(t_star - 1, i, 1, m)] = -1
            row[ov] = -1
            rows.append((row, "<=", p["max_adjust"]))
            neg = [-v for v in row]
            neg[ov] = -1  # 反向行同样由溢出变量吸收
            rows.append((neg, "<=", p["max_adjust"]))
    rows += _bound_rows(model, n_prefix, n_vars)

    # 字典序目标：① 阶段 t* 守恒违约合计；② 末段调节溢出合计；
    # 之后逐顶点、逐管路违约量，保证诊断输出确定可复现
    objectives: List[list] = []
    l1 = [0] * n_vars
    for v in vertices:
        short, excess = vidx[v]
        l1[short] = 1
        l1[excess] = 1
    objectives.append(l1)
    if t_star >= 1:
        l2 = [0] * n_vars
        for i in range(m):
            ov = oidx[i]
            assert ov is not None
            l2[ov] = 1
        objectives.append(l2)
    for v in vertices:
        short, excess = vidx[v]
        for j in (short, excess):
            o = [0] * n_vars
            o[j] = 1
            objectives.append(o)
    for i in range(m):
        ov = oidx[i]
        if ov is None:
            continue
        o = [0] * n_vars
        o[ov] = 1
        objectives.append(o)

    status, x, values = solve_lex(n_vars, rows, objectives)
    if status != "optimal":
        # 软约束 LP 必然可行（违约变量可吸收一切），不可达
        raise RuntimeError("诊断 LP 意外不可行")

    conservation_gap = int(values[0])
    stage_label = f"第 {t_star + 1} 阶段"
    base = {
        "stage": t_star,
        "stage_label": stage_label,
        "total_stages": len(model["stages"]),
    }

    if conservation_gap > 0:
        deficits, surpluses = [], []
        for v in vertices:
            short, excess = vidx[v]
            if x[short] > 0:
                deficits.append({"vertex": v, "kind": kind_of[v],
                                 "shortfall": int(x[short])})
            if x[excess] > 0:
                surpluses.append({"vertex": v, "kind": kind_of[v],
                                  "excess": int(x[excess])})
        reasons = _conservation_reasons(model, t_star, deficits, surpluses)
        return {**base,
                "kind": "conservation",
                "conservation": {
                    "gap": conservation_gap,
                    "deficits": deficits,
                    "surpluses": surpluses,
                },
                "adjustment": None,
                "reasons": reasons}

    flows = _extract_flows(model, n_prefix, x)
    adjustment_gap = int(values[1]) if t_star >= 1 else 0
    limited = []
    if t_star >= 1:
        for i, p in enumerate(pipes):
            ov = oidx[i]
            assert ov is not None
            if x[ov] > 0:
                prev_f, next_f = flows[t_star - 1][i], flows[t_star][i]
                limited.append({
                    "pipe_id": p["id"], "order": p["order"],
                    "from_flow": prev_f, "to_flow": next_f,
                    "needed_adjust": abs(next_f - prev_f),
                    "max_adjust": p["max_adjust"],
                    "gap": int(x[ov]),
                })
    reasons = _adjustment_reasons(model, t_star, limited, adjustment_gap)
    return {**base,
            "kind": "adjustment",
            "conservation": None,
            "adjustment": {
                "gap": adjustment_gap,
                "limited_pipes": limited,
            },
            "reasons": reasons}


def _conservation_reasons(model: Dict[str, Any], t_star: int,
                          deficits: List[Dict[str, Any]],
                          surpluses: List[Dict[str, Any]]) -> List[str]:
    st = model["stages"][t_star]
    total = st["source_total"]
    total_demand = sum(st["demands"][zid] for zid in model["zone_ids"])
    label = f"第 {t_star + 1} 阶段"
    reasons: List[str] = []
    if total != total_demand:
        reasons.append(
            f"{label}水源总量 {total} 与分区需求合计 {total_demand} 不相等，"
            "该阶段收支无法闭合（守恒网络中二者必须相等）")
    for d in deficits:
        v, amount = d["vertex"], d["shortfall"]
        if d["kind"] == "分区":
            demand = st["demands"][v]
            reasons.append(
                f"{label}分区 {v}（需求 {demand}）至少还差 {amount} 单位进水："
                "通向该分区的管路容量不足或根本不通")
        elif d["kind"] == "水源":
            reasons.append(
                f"{label}水源 {v} 出水管路的最小量之和已超过总量 {total}，"
                f"至少需再压低 {amount} 单位：请调小相关管路最小量或上调该阶段总量")
        else:
            reasons.append(
                f"{label}分流节点 {v} 需再净收入 {amount} 单位，"
                "但上游管路送不进来（容量不足或未连通）")
    for s in surpluses:
        v, amount = s["vertex"], s["excess"]
        if s["kind"] == "水源":
            reasons.append(
                f"{label}水源 {v} 有 {amount} 单位水送不出去："
                "下游管路总容量不足或网络不通")
        elif s["kind"] == "分区":
            demand = st["demands"][v]
            reasons.append(
                f"{label}分区 {v} 的进水量压不到需求 {demand}："
                f"至少多出 {amount} 单位，请放宽进水管路的最小量")
        else:
            reasons.append(
                f"{label}分流节点 {v} 有 {amount} 单位来水无处排出："
                "下游管路容量不足或存在死路")
    if not reasons:
        reasons.append(f"{label}网络结构不满足全部守恒/范围约束，"
                       "请检查管路连接与容量")
    return reasons


def _adjustment_reasons(model: Dict[str, Any], t_star: int,
                        limited: List[Dict[str, Any]],
                        gap: int) -> List[str]:
    label = f"第 {t_star + 1} 阶段"
    prev_label = f"第 {t_star} 阶段"
    reasons = [
        f"{label}本身配水可行，但无论前序阶段如何安排，都无法在允许的"
        f"调节量内从{prev_label}过渡过来：调节总缺口 {gap} 单位",
    ]
    for lp in limited:
        reasons.append(
            f"管路 {lp['pipe_id']} 需要由 {lp['from_flow']} 调节到 "
            f"{lp['to_flow']}（变化 {lp['needed_adjust']}），超过允许的 "
            f"{lp['max_adjust']}，缺口 {lp['gap']}：请放宽该管最大调节量、"
            "或减小相邻阶段间的需求/总量落差")
    return reasons
