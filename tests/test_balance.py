"""配平求解器单元测试（标准库 unittest，无需第三方依赖）。"""

import itertools
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.balance import ValidationError, solve, validate_and_build  # noqa: E402
from app.mincost import MinCostFlow  # noqa: E402


def pipe(pid, u, v, lo, hi, pref):
    return {"id": pid, "from": u, "to": v,
            "min": lo, "max": hi, "preferred": pref}


class TestMinCostFlow(unittest.TestCase):
    def test_basic_min_cost(self):
        # 两条路径：贵路（费用 5，容量 2）与便宜路（费用 1，容量 3）
        m = MinCostFlow(4)
        m.add_edge(0, 1, 2, 5)
        e_cheap = len(m.g[0])
        m.add_edge(0, 2, 3, 1)
        m.add_edge(1, 3, 3, 0)
        m.add_edge(2, 3, 3, 0)
        f, c = m.flow(0, 3, 4)
        self.assertEqual((f, c), (4, 8))  # 便宜路 3×1 + 贵路 1×5
        self.assertEqual(m.used_flow(0, e_cheap), 3)

    def test_negative_edge_shortest_path(self):
        # 初始前向边含负费用（但无负环）：Bellman-Ford 必须正确取最短路。
        # 直送费用 10，绕行 a→b 费用 0-5+10=5，故两单位中能绕则绕。
        m = MinCostFlow(4)
        s, a, b, t = 0, 1, 2, 3
        m.add_edge(s, a, 2, 0)
        m.add_edge(a, b, 1, -5)
        m.add_edge(b, t, 2, 10)
        e_direct = len(m.g[s])
        m.add_edge(s, b, 2, 10)
        f, c = m.flow(s, t, 2)
        self.assertEqual(f, 2)
        self.assertEqual(c, 25)  # 1 单位绕行费用 5 + 1 单位直送费用 20
        self.assertEqual(m.used_flow(s, e_direct), 1)

    def test_integer_bottleneck(self):
        m = MinCostFlow(3)
        m.add_edge(0, 1, 7, 0)
        m.add_edge(1, 2, 3, 0)
        f, _ = m.flow(0, 2, 10)
        self.assertEqual(f, 3)  # 受瓶颈限制只推 3

    def test_unreachable_sink(self):
        m = MinCostFlow(3)
        m.add_edge(0, 1, 5, 0)
        f, c = m.flow(0, 2, 5)
        self.assertEqual((f, c), (0, 0))


class TestBalanceBasic(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "source": {"id": "S"},
            "source_total": 10,
            "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}],
            "nodes": [{"id": "N"}],
            "pipes": [
                pipe("p1", "S", "N", 0, 10, 5),
                pipe("p2", "N", "A", 0, 10, 3),
                pipe("p3", "N", "B", 0, 10, 7),
            ],
        }

    def test_feasible_flows_and_balances(self):
        # 管路数下限 4，补一条不影响结果的辅助旁路（容量 0..0 不行，
        # 这里用 N->A 并联管承载 0 流量）
        self.payload["pipes"].append(pipe("p4", "N", "A", 0, 0, 0))
        r = solve(self.payload)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["tie_sequence"], [10, 6, 4, 0])
        self.assertEqual(r["objective"], 5 + 3 + 3 + 0)
        src = r["balances"]["source"]
        self.assertEqual((src["outflow"], src["total"], src["difference"]),
                         (10, 10, 0))
        node = r["balances"]["nodes"][0]
        self.assertEqual((node["inflow"], node["outflow"], node["difference"]),
                         (10, 10, 0))
        z = {row["id"]: row for row in r["balances"]["zones"]}
        self.assertEqual(z["A"]["inflow"] - z["A"]["demand"], 0)
        self.assertEqual(z["B"]["inflow"] - z["B"]["demand"], 0)
        for row in r["flows"]:
            self.assertLessEqual(row["min"], row["flow"])
            self.assertLessEqual(row["flow"], row["max"])

    def test_lexicographic_tie_break(self):
        # X 需求 2，由两条并联管 p2/p3 供水；优选量都是 2。
        # 三种分配 (0,2)/(1,1)/(2,0) 偏差和同为 2，
        # 按录入顺序字典序应取 (0, 2)。
        payload = {
            "source": {"id": "S"},
            "source_total": 4,
            "zones": [{"id": "X", "demand": 2}, {"id": "Y", "demand": 2}],
            "nodes": [{"id": "N"}],
            "pipes": [
                pipe("p1", "S", "N", 4, 4, 4),
                pipe("p2", "N", "X", 0, 4, 2),
                pipe("p3", "N", "X", 0, 4, 2),
                pipe("p4", "N", "Y", 2, 2, 2),
            ],
        }
        r = solve(payload)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["tie_sequence"], [4, 0, 2, 2])
        self.assertEqual(r["objective"], 0 + 2 + 0 + 0)

    def test_infeasible_capacity_shortfall(self):
        payload = {
            "source": {"id": "S"},
            "source_total": 10,
            "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}],
            "nodes": [],
            "pipes": [
                pipe("p1", "S", "A", 0, 5, 3),  # A 需 6，最多进 5
                pipe("p2", "S", "B", 0, 4, 4),
                pipe("p3", "S", "A", 0, 2, 1),  # A 合计最多 7，仍可够？
                pipe("p4", "S", "B", 0, 2, 1),
            ],
        }
        # 合计出水容量够，但 A 总容量只有 5 < 需求 6 → 不可行
        payload["pipes"][2]["max"] = 0
        payload["pipes"][2]["preferred"] = 0
        r = solve(payload)
        self.assertFalse(r["feasible"])
        info = r["infeasibility"]
        self.assertGreater(info["shortfall_flow"], 0)
        self.assertTrue(info["reasons"])
        self.assertTrue(any("A" == d["vertex"] for d in info["deficits"]))

    def test_total_demand_mismatch(self):
        self.payload["source_total"] = 9  # 需求合计 10
        self.payload["pipes"].append(pipe("p4", "N", "A", 0, 0, 0))
        r = solve(self.payload)
        self.assertFalse(r["feasible"])
        self.assertIn("不相等", r["infeasibility"]["reasons"][0])

    def test_lower_bounds_forced(self):
        # 下届迫使 p2 向 A 多送，配合需求形成唯一解
        payload = {
            "source": {"id": "S"},
            "source_total": 5,
            "zones": [{"id": "A", "demand": 3}, {"id": "B", "demand": 2}],
            "nodes": [],
            "pipes": [
                pipe("p1", "S", "A", 2, 5, 5),
                pipe("p2", "S", "B", 0, 5, 0),
                pipe("p3", "S", "A", 0, 5, 0),
                pipe("p4", "S", "B", 1, 5, 5),
            ],
        }
        r = solve(payload)
        self.assertTrue(r["feasible"])
        # A=3 且 p1≥2；最小 |p1-5|+|p3-0|+|p2-0|+|p4-5|
        # p4≥1，B=2 => p2+p4=2，p1+p3=3，p1≥2。
        # 枚举可得 p1=2,p3=1,p4=1,p2=1：偏差 3+1+1+4=9；
        # p1=3,p3=0,p4=1,p2=1：偏差 2+0+1+4=7 更优；
        # p4=2,p2=0：p1=3,p3=0 偏差 2+0+0+3=5 最优。
        self.assertEqual(r["tie_sequence"], [3, 0, 0, 2])
        self.assertEqual(r["objective"], 5)


class TestValidation(unittest.TestCase):
    def base(self):
        return {
            "source": {"id": "S"},
            "source_total": 5,
            "zones": [{"id": "A", "demand": 3}, {"id": "B", "demand": 2}],
            "nodes": [],
            "pipes": [
                pipe("p1", "S", "A", 0, 5, 2),
                pipe("p2", "S", "B", 0, 5, 2),
                pipe("p3", "S", "A", 0, 5, 1),
                pipe("p4", "S", "B", 0, 5, 0),
            ],
        }

    def test_counts(self):
        p = self.base()
        p["zones"].pop()
        with self.assertRaises(ValidationError):
            validate_and_build(p)
        p = self.base()
        p["pipes"].pop()
        with self.assertRaises(ValidationError):
            validate_and_build(p)
        p = self.base()
        for k in range(7):
            p["pipes"].append(pipe(f"x{k}", "S", "A", 0, 1, 0))
        with self.assertRaises(ValidationError):  # 11 条 > 10
            validate_and_build(p)

    def test_bad_ranges_and_unknown_vertex(self):
        p = self.base()
        p["pipes"][0]["max"] = 10
        p["pipes"][0]["min"] = 11
        with self.assertRaises(ValidationError):
            validate_and_build(p)

        p = self.base()
        p["pipes"][0]["preferred"] = 9
        with self.assertRaises(ValidationError):
            validate_and_build(p)

        p = self.base()
        p["pipes"][1]["to"] = "GHOST"
        with self.assertRaises(ValidationError):
            validate_and_build(p)

        p = self.base()
        p["pipes"][1]["from"] = "A"  # 分区不能出水
        with self.assertRaises(ValidationError):
            validate_and_build(p)

        p = self.base()
        p["pipes"][1]["to"] = "S"  # 不能接入水源
        with self.assertRaises(ValidationError):
            validate_and_build(p)

    def test_duplicate_and_non_int(self):
        p = self.base()
        p["zones"][0]["id"] = "S"
        with self.assertRaises(ValidationError):
            validate_and_build(p)

        p = self.base()
        p["source_total"] = 5.0
        with self.assertRaises(ValidationError):
            validate_and_build(p)

        p = self.base()
        p["source_total"] = True  # bool 不得当作整数
        with self.assertRaises(ValidationError):
            validate_and_build(p)

        p = self.base()
        p["pipes"][0]["id"] = "p2"
        with self.assertRaises(ValidationError):
            validate_and_build(p)


def _brute_force(payload):
    """枚举所有管路整数取值，返回 (feasible, [(seq, objective)...]) 全量可行解。"""
    model = validate_and_build(payload)
    pipes = model["pipes"]
    zones = {z["id"]: z["demand"] for z in model["zones"]}
    vertices = (
        [model["source_id"]] + model["nodes"] + [z["id"] for z in model["zones"]]
    )
    total = model["total"]

    options = [range(p["min"], p["max"] + 1) for p in pipes]
    feasible_solutions = []
    for vals in itertools.product(*options):
        inflow = {v: 0 for v in vertices}
        outflow = {v: 0 for v in vertices}
        for p, f in zip(pipes, vals):
            outflow[p["from"]] += f
            inflow[p["to"]] += f
        if outflow[model["source_id"]] != total:
            continue
        if any(inflow[z] != d for z, d in zones.items()):
            continue
        if any(inflow[n] != outflow[n] for n in model["nodes"]):
            continue
        obj = sum(abs(f - p["preferred"]) for p, f in zip(pipes, vals))
        feasible_solutions.append((tuple(vals), obj))
    return feasible_solutions


def _random_feasible_payload(rng):
    """按拓扑分层随机分流，先造守恒流量再反推需求/总量/范围。

    保证存在可行解；额外的零流量并联管提供替代走法，
    使最优解往往不等于初始随机解。
    """
    n_nodes = rng.randrange(0, 3)
    n_zones = rng.randrange(2, 4)
    nodes = [f"N{i}" for i in range(n_nodes)]
    zones = [f"Z{i}" for i in range(n_zones)]

    # 有向边只允许从 S/N_i 到 N_j(j>i)/分区（无环，便于分层分流）
    edges = []
    # 保证连通：S 接到每个节点；每个节点至少接出一条到下游
    for i, ni in enumerate(nodes):
        edges.append(("S", ni))
        targets = [f"N{j}" for j in range(i + 1, n_nodes)] + zones
        edges.append((ni, rng.choice(targets)))
    # S 至少直连一个分区
    edges.append(("S", rng.choice(zones)))
    # 随机增补边（允许并联：同端点可有多条不同管路）
    candidates = [("S", t) for t in nodes + zones]
    for i, ni in enumerate(nodes):
        for t in [f"N{j}" for j in range(i + 1, n_nodes)] + zones:
            candidates.append((ni, t))
    rng.shuffle(candidates)
    target_n = rng.randrange(5, 9)
    for c in candidates:
        if len(edges) >= target_n:
            break
        edges.append(c)
    while len(edges) < 4:
        edges.append(rng.choice(candidates))
    edges = edges[:10]

    # 分层分流：按 S,N0,N1,... 顺序，把当前存量随机切成若干份
    out_edges = {v: [] for v in ["S"] + nodes}
    for i, (u, v) in enumerate(edges):
        out_edges.setdefault(u, []).append(i)
    inflow_acc = {v: 0 for v in nodes + zones}
    flows = [0] * len(edges)

    w = rng.randrange(2, 7)
    supply = {"S": w}
    for v in nodes:
        supply[v] = 0
    for v in ["S"] + nodes:
        ids = out_edges.get(v, [])
        if not ids:
            # 该点不该有存量（无出口）；若有则重造
            if supply.get(v, 0) > 0:
                return None
            continue
        amount = supply[v]
        k = len(ids)
        if k == 1:
            vals = [amount]
        else:
            # 隔板法：从 amount+k-1 个位置中抽 k-1 个隔板
            cuts = sorted(rng.sample(range(amount + k - 1), k - 1))
            vals = []
            marker = 0
            for c in cuts:
                vals.append(c - marker)
                marker = c + 1
            vals.append(amount + k - 1 - marker)
        for ei, fval in zip(ids, vals):
            flows[ei] = fval
            _, to = edges[ei]
            inflow_acc[to] += fval
            if to in supply:
                supply[to] += fval

    demands = [inflow_acc[z] for z in zones]
    if sum(demands) != w:
        return None  # 守恒校验失败则弃用该样本

    pipes = []
    for i, ((u, v), f) in enumerate(zip(edges, flows)):
        lo = max(0, f - rng.randrange(0, 3))
        hi = f + rng.randrange(0, 3)
        pref = rng.randrange(lo, hi + 1)
        pipes.append(pipe(f"e{i}", u, v, lo, hi, pref))

    # 50% 概率加一对节点间反向管（制造有向环），原守恒解中其流量为 0
    if n_nodes >= 2 and rng.random() < 0.5 and len(edges) <= 8:
        i, j = sorted(rng.sample(range(n_nodes), 2))
        cap = rng.randrange(1, 3)
        k = len(edges)
        edges.append((f"N{i}", f"N{j}"))
        pipes.append(pipe(f"e{k}", f"N{i}", f"N{j}", 0, cap,
                          rng.randrange(0, cap + 1)))
        edges.append((f"N{j}", f"N{i}"))
        pipes.append(pipe(f"e{k+1}", f"N{j}", f"N{i}", 0, cap,
                          rng.randrange(0, cap + 1)))

    return {
        "source": {"id": "S"},
        "source_total": w,
        "zones": [{"id": z, "demand": d} for z, d in zip(zones, demands)],
        "nodes": [{"id": n} for n in nodes],
        "pipes": pipes,
    }


def _random_loose_payload(rng):
    """完全松散的随机小网络，大概率不可行，用于检验可行性判定。"""
    n_nodes = rng.randrange(0, 3)
    n_zones = rng.randrange(2, 4)
    nodes = [f"N{i}" for i in range(n_nodes)]
    zones = [f"Z{i}" for i in range(n_zones)]
    sources = ["S"] + nodes
    targets = nodes + zones
    candidates = [(u, v) for u in sources for v in targets if u != v]

    n_pipes = rng.randrange(4, 9)
    chosen = [rng.choice(candidates) for _ in range(n_pipes)]
    pipes = []
    for i, (u, v) in enumerate(chosen):
        lo = rng.randrange(0, 2)
        hi = lo + rng.randrange(0, 3)
        pref = rng.randrange(lo, hi + 1)
        pipes.append(pipe(f"e{i}", u, v, lo, hi, pref))

    return {
        "source": {"id": "S"},
        "source_total": rng.randrange(0, 7),
        "zones": [{"id": z, "demand": rng.randrange(0, 5)}
                  for z in zones],
        "nodes": [{"id": n} for n in nodes],
        "pipes": pipes,
    }


class TestRandomAgainstBruteForce(unittest.TestCase):
    """随机小网络上与暴力枚举逐例对照：可行性、目标值、字典序决胜序列。

    约 70% 样本由守恒流量反推构造（必然可行，考验最优性与决胜），
    其余为松散随机（大多不可行，考验可行性判定与守恒结论）。
    """

    def test_random_networks(self):
        rng = random.Random(20260925)
        cases = 0
        feasible_checked = 0
        infeasible_checked = 0
        while cases < 400:
            if rng.random() < 0.7:
                payload = _random_feasible_payload(rng)
                if payload is None:
                    continue
            else:
                payload = _random_loose_payload(rng)
            cases += 1

            brute = _brute_force(payload)
            r = solve(payload)
            if not brute:
                self.assertFalse(r["feasible"], msg=str(payload))
                infeasible_checked += 1
                continue
            self.assertTrue(r["feasible"], msg=str(payload))
            best_obj = min(o for _, o in brute)
            self.assertEqual(r["objective"], best_obj, msg=str(payload))
            best_seq = min(seq for seq, o in brute if o == best_obj)
            self.assertEqual(tuple(r["tie_sequence"]), best_seq,
                             msg=str(payload))
            # 结果自洽：逐管收支必须闭合
            bal = r["balances"]
            self.assertEqual(bal["source"]["difference"], 0)
            self.assertTrue(all(row["difference"] == 0
                                for row in bal["nodes"]))
            self.assertTrue(all(row["difference"] == 0
                                for row in bal["zones"]))
            feasible_checked += 1
        self.assertGreater(feasible_checked, 100)
        self.assertGreater(infeasible_checked, 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
