"""雾化管路配平：校验输入并求整数最优配水方案。

业务约束
--------
- 水源流出恰等于水源总量 W；
- 每个分流节点（中间节点）流入 = 流出；
- 每个分区流入恰等于其需求 d；
- 每条管路流量为整数且落在 [min, max]。

目标
----
1. 主目标：各管路流量相对"优选量"的绝对偏差和最小；
2. 决胜：在所有主目标最优解中，取按管路录入顺序的流量序列字典序最小者
   （序列稳定、可复现，不依赖求解器内部遍历顺序）。

建模
----
以优选量 p 为初始预流，再用超源 SS / 超汇 TT 调整（全部调整边费用为
正，不存在负费用环，连续最短路增广即可得到全局最小费用整数流）：
- 每条管路 u→v 先按优选量 p 预流，折入节点收支
  b(v)=Σ优选流入-Σ优选流出；再加两条调整边：
      u→v 容量 r-p，费用 P+q（在优选量之上增大 1 单位，偏差 +1）
      v→u 容量 p-l，费用 P-q（在优选量之下减小 1 单位，偏差 +1）
  最终流量 f = p + (增大边流量) - (减小边流量)，[l, r] 范围天然满足。
  每偏离优选量 1 单位，主目标费用分量恰为 P；同时净变化携带决胜
  权重 q（增大 +q、减小 -q），即费用 = P·Σ|f-p| + Σq·(f-p) + 常数。
- 业务目标净流入 r(v)：水源 -W、分区 +d、分流节点 0。
  增强网在 v 处守恒给出 b + (排向 TT) - (SS 注入) = r，即：
      b-r > 0 → SS→该点（容量 b-r，注入后经调整边送走）
      r-b > 0 → 该点→TT（容量 r-b，等待调整边补入）
  超源、超汇两侧全部饱和才可行（水源总量≠需求合计时两侧总量不等，
  不可能同时饱和，自然判为不可行）。

权重（整数大权，Python 原生大整数）：
  q_m = 1，q_i = 1 + Σ_{j>i} q_j·(r_j-l_j)（早录入管路字典序主导）；
  P = 1 + Σ_i q_i·(r_i-l_i)（1 单位主偏差压倒一切决胜差异）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .mincost import MinCostFlow

# 录入数量约束
MIN_ZONES, MAX_ZONES = 2, 4
MAX_NODES = 4
MIN_PIPES, MAX_PIPES = 4, 10
MAX_REQUEST_BYTES = 1 << 20


class ValidationError(ValueError):
    """输入草稿未通过业务校验。"""


def _is_int(x: Any) -> bool:
    # bool 是 int 的子类，业务上不接受
    return isinstance(x, int) and not isinstance(x, bool)


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


def validate_and_build(payload: Any) -> Dict[str, Any]:
    """校验草稿并整理为内部结构。"""
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")

    src = payload.get("source")
    if not isinstance(src, dict) or not str(src.get("id", "")).strip():
        raise ValidationError("必须指定一处水源（source.id）")
    source_id = str(src["id"]).strip()
    total = _require_int(payload, "source_total", "水源总量")

    raw_zones = payload.get("zones")
    raw_nodes = payload.get("nodes", [])
    raw_pipes = payload.get("pipes")
    if not isinstance(raw_zones, list):
        raise ValidationError("zones 必须是数组")
    if not isinstance(raw_nodes, list):
        raise ValidationError("nodes 必须是数组")
    if not isinstance(raw_pipes, list):
        raise ValidationError("pipes 必须是数组")

    if not (MIN_ZONES <= len(raw_zones) <= MAX_ZONES):
        raise ValidationError(f"分区数量必须在 {MIN_ZONES}~{MAX_ZONES} 个之间")
    if len(raw_nodes) > MAX_NODES:
        raise ValidationError(f"分流节点数量不能超过 {MAX_NODES} 个")
    if not (MIN_PIPES <= len(raw_pipes) <= MAX_PIPES):
        raise ValidationError(f"管路数量必须在 {MIN_PIPES}~{MAX_PIPES} 条之间")

    zones: List[Dict[str, int | str]] = []
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
        zid = check_id(z.get("id"), "分区")
        demand = _require_int(z, "demand", f"分区 {zid} 的需求")
        zones.append({"id": zid, "demand": demand})

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
        if lo > hi:
            raise ValidationError(f"{label}（{pid}）最小量不能大于最大量")
        if not (lo <= pref <= hi):
            raise ValidationError(f"{label}（{pid}）优选量必须位于最小量与最大量之间")

        pipes.append({"id": pid, "from": u, "to": v,
                      "min": lo, "max": hi, "preferred": pref, "order": i})

    return {
        "source_id": source_id,
        "total": total,
        "zones": zones,
        "nodes": nodes,
        "pipes": pipes,
    }


def solve(payload: Any) -> Dict[str, Any]:
    """求配平结果。校验失败由调用方转 400。"""
    model = validate_and_build(payload)
    source_id = model["source_id"]
    total: int = model["total"]
    zones: List[Dict[str, Any]] = model["zones"]
    nodes: List[str] = model["nodes"]
    pipes: List[Dict[str, Any]] = model["pipes"]

    zone_ids = [z["id"] for z in zones]
    demand_of = {z["id"]: z["demand"] for z in zones}
    kind_of: Dict[str, str] = {source_id: "水源"}
    kind_of.update({n: "分流节点" for n in nodes})
    kind_of.update({z: "分区" for z in zone_ids})

    # ---- 顶点：水源、分流节点、分区，再加 SS / TT ----
    business = [source_id] + nodes + zone_ids
    idx = {vid: i for i, vid in enumerate(business)}
    n_biz = len(business)
    ss, tt = n_biz, n_biz + 1
    mcf = MinCostFlow(tt + 1)

    # 分层权重：字典序决胜权重 q，主偏差权重 P
    m = len(pipes)
    q = [0] * m
    weight = 0
    for i in range(m - 1, -1, -1):
        q[i] = weight + 1
        weight += q[i] * (pipes[i]["max"] - pipes[i]["min"])
    primary_p = weight + 1

    # ---- 优选量预流，b(v)=优选流入-优选流出 ----
    balance = {vid: 0 for vid in business}
    # (起点, 增大边序号, 反向起点 v, 减小边序号)
    adj_refs: List[Tuple[int, int, int, int]] = []
    for i, p in enumerate(pipes):
        u = idx[p["from"]]
        v = idx[p["to"]]
        pref = p["preferred"]
        # 增大边 u→v
        e_up = len(mcf.g[u])
        mcf.add_edge(u, v, p["max"] - pref, primary_p + q[i])
        # 减小边 v→u
        e_down = len(mcf.g[v])
        mcf.add_edge(v, u, pref - p["min"], primary_p - q[i])
        adj_refs.append((u, e_up, v, e_down))
        balance[p["from"]] -= pref
        balance[p["to"]] += pref

    # ---- 目标净流入 r(v)：水源 -W、分区 +d、分流节点 0 ----
    required_in: Dict[str, int] = {source_id: -total}
    for n in nodes:
        required_in[n] = 0
    for zid, d in demand_of.items():
        required_in[zid] = d

    # delta = b-r：>0 预流盈余 → SS→v 注入送走；<0 预流缺口 → v→TT 补入
    ss_edges: List[Tuple[str, int]] = []
    tt_edges: List[Tuple[str, int]] = []
    ss_total = tt_total = 0
    for vid in business:
        delta = balance[vid] - required_in[vid]
        if delta > 0:
            ei = len(mcf.g[ss])
            mcf.add_edge(ss, idx[vid], delta, 0)
            ss_edges.append((vid, ei))
            ss_total += delta
        elif delta < 0:
            ei = len(mcf.g[idx[vid]])
            mcf.add_edge(idx[vid], tt, -delta, 0)
            tt_edges.append((vid, ei))
            tt_total += -delta

    pushed, _cost = mcf.flow(ss, tt, max(ss_total, tt_total))

    # 两侧必须同时饱和
    saturated = all(mcf.g[ss][ei].cap == 0 for _, ei in ss_edges) and all(
        mcf.g[idx[vid]][ei].cap == 0 for vid, ei in tt_edges
    )

    base = {
        "source_id": source_id,
        "source_total": total,
        "zones": zones,
        "nodes": nodes,
        "pipes": pipes,
    }

    if not saturated or pushed != min(ss_total, tt_total):
        return _infeasible(base, mcf, ss, ss_edges, tt_edges,
                           ss_total, tt_total, pushed, kind_of, idx, demand_of)

    flows: List[int] = []
    for p, (u, e_up, v, e_down) in zip(pipes, adj_refs):
        up_used = mcf.used_flow(u, e_up)
        down_used = mcf.used_flow(v, e_down)
        flows.append(p["preferred"] + up_used - down_used)

    return _feasible(base, flows, idx)


def _feasible(base: Dict[str, Any], flows: List[int],
              idx: Dict[str, int]) -> Dict[str, Any]:
    pipes = base["pipes"]
    inflow: Dict[str, int] = {v: 0 for v in idx}
    outflow: Dict[str, int] = {v: 0 for v in idx}

    flow_rows = []
    deviation = 0
    for p, f in zip(pipes, flows):
        dev = abs(f - p["preferred"])
        deviation += dev
        outflow[p["from"]] += f
        inflow[p["to"]] += f
        flow_rows.append({
            "pipe_id": p["id"], "order": p["order"],
            "from": p["from"], "to": p["to"],
            "min": p["min"], "max": p["max"],
            "preferred": p["preferred"], "flow": f, "deviation": dev,
        })

    sid = base["source_id"]
    node_rows = [{
        "id": n,
        "inflow": inflow[n], "outflow": outflow[n],
        "difference": inflow[n] - outflow[n],
    } for n in base["nodes"]]
    zone_rows = [{
        "id": z["id"], "demand": z["demand"],
        "inflow": inflow[z["id"]],
        "difference": inflow[z["id"]] - z["demand"],
    } for z in base["zones"]]

    return {
        "feasible": True,
        "objective": deviation,
        "tie_sequence": flows,
        "flows": flow_rows,
        "balances": {
            "source": {
                "id": sid, "outflow": outflow[sid],
                "total": base["source_total"],
                "difference": outflow[sid] - base["source_total"],
            },
            "nodes": node_rows,
            "zones": zone_rows,
        },
        "infeasibility": None,
    }


def _infeasible(base: Dict[str, Any], mcf: MinCostFlow, ss: int,
                ss_edges: List[Tuple[str, int]],
                tt_edges: List[Tuple[str, int]],
                ss_total: int, tt_total: int, pushed: int,
                kind_of: Dict[str, str], idx: Dict[str, int],
                demand_of: Dict[str, Any]) -> Dict[str, Any]:
    total_demand = sum(z["demand"] for z in base["zones"])

    # SS→v 残余：预流盈余（优选量）排不出去
    surpluses = []
    for vid, ei in ss_edges:
        remaining = mcf.g[ss][ei].cap
        if remaining > 0:
            surpluses.append({
                "vertex": vid, "kind": kind_of[vid],
                "excess": remaining,
            })

    # v→TT 残余：目标净流入补不齐
    deficits = []
    for vid, ei in tt_edges:
        remaining = mcf.g[idx[vid]][ei].cap
        if remaining > 0:
            deficits.append({
                "vertex": vid, "kind": kind_of[vid],
                "shortfall": remaining,
            })

    reasons: List[str] = []
    total = base["source_total"]
    if total != total_demand:
        reasons.append(
            f"水源总量 {total} 与分区需求合计 {total_demand} 不相等，"
            "全系统收支无法闭合（守恒网络中二者必须相等）")

    for d in deficits:
        kind, vid, amount = d["kind"], d["vertex"], d["shortfall"]
        if kind == "分区":
            reasons.append(
                f"分区 {vid}（需求 {demand_of[vid]}）至少还差 {amount} 单位进水："
                "通向该分区的管路容量不足或根本不通")
        elif kind == "水源":
            reasons.append(
                f"水源 {vid} 出水管路的优选（最小）水量之和已超过总量 "
                f"{total}，至少需再压低 {amount} 单位：请调小相关管路最小量"
                "或上调水源总量")
        else:
            reasons.append(
                f"分流节点 {vid} 需再净收入 {amount} 单位，"
                "但上游管路送不进来（容量不足或未连通）")

    for s in surpluses:
        kind, vid, amount = s["kind"], s["vertex"], s["excess"]
        if kind == "水源":
            reasons.append(
                f"水源 {vid} 有 {amount} 单位水送不出去："
                "下游管路总容量不足或网络不通")
        elif kind == "分区":
            reasons.append(
                f"分区 {vid} 的进水量压不到需求 {demand_of[vid]}："
                f"至少多出 {amount} 单位，请放宽进水管路的最小量")
        else:
            reasons.append(
                f"分流节点 {vid} 有 {amount} 单位来水无处排出："
                "下游管路容量不足或存在死路")

    if not reasons:
        reasons.append("网络结构不满足全部守恒/范围约束，请检查管路连接与容量")

    return {
        "feasible": False,
        "objective": None,
        "tie_sequence": None,
        "flows": None,
        "balances": None,
        "infeasibility": {
            "source_total": total,
            "total_demand": total_demand,
            "ss_required": ss_total,
            "tt_required": tt_total,
            "achieved_flow": pushed,
            "shortfall_flow": max(ss_total, tt_total) - pushed,
            "deficits": deficits,
            "surpluses": surpluses,
            "reasons": reasons,
        },
    }
