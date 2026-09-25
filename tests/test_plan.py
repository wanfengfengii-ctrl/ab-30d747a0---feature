"""缓升计划（多阶段联合配平）单元测试（标准库 unittest，零第三方依赖）。"""

import itertools
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.balance import ValidationError  # noqa: E402
from app.plan import solve_plan, validate_plan  # noqa: E402


def pipe(pid, u, v, lo, hi, pref, adj):
    return {"id": pid, "from": u, "to": v, "min": lo, "max": hi,
            "preferred": pref, "max_adjust": adj}


def stage(total, demands):
    return {"source_total": total,
            "zones": [{"id": z, "demand": d} for z, d in demands]}


def base_payload():
    return {
        "source": {"id": "S"},
        "nodes": [{"id": "N"}],
        "zones": [{"id": "A"}, {"id": "B"}],
        "pipes": [
            pipe("p1", "S", "N", 0, 10, 5, 3),
            pipe("p2", "N", "A", 0, 10, 3, 2),
            pipe("p3", "N", "B", 0, 10, 7, 2),
            pipe("p4", "S", "A", 0, 0, 0, 0),
        ],
        "stages": [
            stage(4, [("A", 2), ("B", 2)]),
            stage(7, [("A", 4), ("B", 3)]),
            stage(10, [("A", 6), ("B", 4)]),
        ],
    }


class TestPlanValidation(unittest.TestCase):
    def test_stage_count_bounds(self):
        p = base_payload()
        p["stages"] = p["stages"][:1]
        with self.assertRaises(ValidationError):
            validate_plan(p)
        p = base_payload()
        p["stages"] = p["stages"] * 2  # 6 个阶段 > 4
        with self.assertRaises(ValidationError):
            validate_plan(p)

    def test_stage_must_cover_all_zones(self):
        p = base_payload()
        p["stages"][0]["zones"] = [{"id": "A", "demand": 2}]  # 缺 B
        with self.assertRaises(ValidationError):
            validate_plan(p)
        p = base_payload()
        p["stages"][0]["zones"].append({"id": "C", "demand": 1})  # 未知分区
        with self.assertRaises(ValidationError):
            validate_plan(p)
        p = base_payload()
        p["stages"][0]["zones"].append({"id": "A", "demand": 1})  # 重复
        with self.assertRaises(ValidationError):
            validate_plan(p)

    def test_adjust_and_int_checks(self):
        p = base_payload()
        p["pipes"][0]["max_adjust"] = -1
        with self.assertRaises(ValidationError):
            validate_plan(p)
        p = base_payload()
        p["pipes"][0]["max_adjust"] = 1.5
        with self.assertRaises(ValidationError):
            validate_plan(p)
        p = base_payload()
        p["stages"][0]["source_total"] = True  # bool 不是整数
        with self.assertRaises(ValidationError):
            validate_plan(p)
        p = base_payload()
        p["stages"][0]["zones"][0]["demand"] = -1
        with self.assertRaises(ValidationError):
            validate_plan(p)

    def test_pipe_rules_inherited(self):
        p = base_payload()
        p["pipes"][0]["preferred"] = 99  # 超出 [min, max]
        with self.assertRaises(ValidationError):
            validate_plan(p)
        p = base_payload()
        p["pipes"][1]["from"] = "A"  # 分区不能出水
        with self.assertRaises(ValidationError):
            validate_plan(p)
        p = base_payload()
        del p["pipes"][0]["max_adjust"]
        with self.assertRaises(ValidationError):
            validate_plan(p)


class TestPlanFeasible(unittest.TestCase):
    def test_joint_flows_and_adjustments(self):
        r = solve_plan(base_payload())
        self.assertTrue(r["feasible"])
        self.assertEqual(r["tie_sequence"],
                         [4, 2, 2, 0, 7, 4, 3, 0, 10, 6, 4, 0])
        self.assertEqual(r["objective"],
                         (1 + 1 + 5 + 0) + (2 + 1 + 4 + 0) + (5 + 3 + 3 + 0))
        # 逐阶段守恒
        for st in r["stages"]:
            self.assertEqual(st["balances"]["source"]["difference"], 0)
            self.assertTrue(all(n["difference"] == 0
                                for n in st["balances"]["nodes"]))
            self.assertTrue(all(z["difference"] == 0
                                for z in st["balances"]["zones"]))
            for f in st["flows"]:
                self.assertLessEqual(f["min"], f["flow"])
                self.assertLessEqual(f["flow"], f["max"])
        # 相邻调整量
        self.assertEqual(len(r["adjustments"]), 2)
        for adj in r["adjustments"]:
            for prow in adj["pipes"]:
                self.assertTrue(prow["within_limit"])
                self.assertEqual(
                    abs(prow["next_flow"] - prow["previous_flow"]),
                    prow["abs_change"])
        p1_first = r["adjustments"][0]["pipes"][0]
        self.assertEqual((p1_first["previous_flow"], p1_first["next_flow"]),
                         (4, 7))

    def test_joint_not_per_stage_concatenation(self):
        # 逐阶段独立最优：阶段1 p1=0,p3=7（偏差 14），阶段2 p1=0,p3=3（偏差 14），
        # 拼接后 p3 相邻阶段跳变 7-3=4，超过调节量 2，**不是可行计划**；
        # 联合求解必须让 p1 在阶段1 多承担 2 单位以分摊 p3 的落差：
        # 联合最优 p1 阶段1 取 2（牺牲 2 单位偏差换取相邻差 ≤ 2）。
        payload = {
            "source": {"id": "S"},
            "nodes": [],
            "zones": [{"id": "A"}, {"id": "B"}],
            "pipes": [
                pipe("p1", "S", "A", 0, 10, 0, 2),
                pipe("p2", "S", "B", 0, 10, 0, 2),
                pipe("p3", "S", "A", 0, 10, 10, 2),
                pipe("p4", "S", "B", 0, 10, 10, 2),
            ],
            "stages": [
                stage(10, [("A", 7), ("B", 3)]),
                stage(6, [("A", 3), ("B", 3)]),
            ],
        }
        r = solve_plan(payload)
        self.assertTrue(r["feasible"])
        seq = r["tie_sequence"]
        self.assertEqual(seq, [2, 0, 5, 3, 0, 0, 3, 3])
        self.assertEqual(seq[0], 2)  # p1 阶段1 = 2，而非独立最优的 0
        self.assertEqual(r["objective"], 28)
        for adj in r["adjustments"]:
            for prow in adj["pipes"]:
                self.assertTrue(prow["within_limit"])

    def test_lexicographic_tie_break(self):
        # 两条并联管供 X，优选量相同；字典序应取靠前的管路流量更小
        payload = {
            "source": {"id": "S"},
            "nodes": [{"id": "N"}],
            "zones": [{"id": "X"}, {"id": "Y"}],
            "pipes": [
                pipe("p1", "S", "N", 4, 4, 4, 0),
                pipe("p2", "N", "X", 0, 4, 2, 4),
                pipe("p3", "N", "X", 0, 4, 2, 4),
                pipe("p4", "N", "Y", 2, 2, 2, 0),
            ],
            "stages": [stage(4, [("X", 2), ("Y", 2)]),
                       stage(4, [("X", 2), ("Y", 2)])],
        }
        r = solve_plan(payload)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["tie_sequence"], [4, 0, 2, 2, 4, 0, 2, 2])

    def test_zero_adjust_forces_constant_flow(self):
        payload = {
            "source": {"id": "S"},
            "nodes": [],
            "zones": [{"id": "A"}, {"id": "B"}],
            "pipes": [
                pipe("p1", "S", "A", 0, 10, 5, 0),
                pipe("p2", "S", "B", 0, 10, 5, 0),
                pipe("p3", "S", "A", 0, 10, 0, 10),
                pipe("p4", "S", "B", 0, 10, 0, 10),
            ],
            "stages": [stage(6, [("A", 3), ("B", 3)]),
                       stage(8, [("A", 4), ("B", 4)])],
        }
        r = solve_plan(payload)
        self.assertTrue(r["feasible"])
        seq = r["tie_sequence"]
        self.assertEqual(seq[0], seq[4])  # p1 两阶段相同（调节量 0）
        self.assertEqual(seq[1], seq[5])  # p2 两阶段相同


class TestPlanInfeasible(unittest.TestCase):
    def test_adjustment_infeasible_earliest_stage(self):
        p = base_payload()
        for pipe_ in p["pipes"]:
            pipe_["max_adjust"] = 1
        p["stages"] = [
            stage(4, [("A", 2), ("B", 2)]),
            stage(10, [("A", 6), ("B", 4)]),  # 跳变过大
            stage(10, [("A", 6), ("B", 4)]),
        ]
        r = solve_plan(p)
        self.assertFalse(r["feasible"])
        info = r["infeasibility"]
        self.assertEqual(info["stage"], 1)  # 最早不可行的是第 2 阶段
        self.assertEqual(info["kind"], "adjustment")
        self.assertGreater(info["adjustment"]["gap"], 0)
        self.assertTrue(info["adjustment"]["limited_pipes"])
        for lp in info["adjustment"]["limited_pipes"]:
            self.assertGreater(lp["needed_adjust"], lp["max_adjust"])
            self.assertEqual(lp["needed_adjust"] - lp["max_adjust"], lp["gap"])
        self.assertTrue(info["reasons"])

    def test_conservation_infeasible_stage(self):
        p = base_payload()
        # 第 3 阶段 A 需求 60：总量与需求不等且容量不足
        p["stages"][2] = stage(64, [("A", 60), ("B", 4)])
        r = solve_plan(p)
        self.assertFalse(r["feasible"])
        info = r["infeasibility"]
        self.assertEqual(info["stage"], 2)
        self.assertEqual(info["kind"], "conservation")
        self.assertGreater(info["conservation"]["gap"], 0)
        self.assertTrue(info["conservation"]["deficits"]
                        or info["conservation"]["surpluses"])
        self.assertTrue(info["reasons"])

    def test_first_stage_infeasible(self):
        p = base_payload()
        p["stages"][0] = stage(99, [("A", 2), ("B", 2)])  # 总量≠需求合计
        r = solve_plan(p)
        self.assertFalse(r["feasible"])
        info = r["infeasibility"]
        self.assertEqual(info["stage"], 0)
        self.assertEqual(info["kind"], "conservation")

    def test_infeasible_fields_cleared(self):
        p = base_payload()
        p["stages"][1] = stage(99, [("A", 6), ("B", 4)])
        r = solve_plan(p)
        self.assertFalse(r["feasible"])
        self.assertIsNone(r["stages"])
        self.assertIsNone(r["adjustments"])
        self.assertIsNone(r["tie_sequence"])
        self.assertIsNone(r["objective"])


# ----------------------------------------------------------------------
# 随机小网络暴力枚举对照
# ----------------------------------------------------------------------

def _brute_best(payload):
    """枚举全部阶段全部管路取值，返回 (obj, seq)；不可行返回 (None, None)。"""
    model = validate_plan(payload)
    pipes = model["pipes"]
    stages = model["stages"]
    T = len(stages)
    m = len(pipes)
    vertices = [model["source_id"]] + model["nodes"] + model["zone_ids"]
    options = [tuple(range(p["min"], p["max"] + 1)) for p in pipes]
    best = None
    for combo in itertools.product(*(options * T)):
        ok = True
        for t in range(T):
            ys = combo[t * m:(t + 1) * m]
            inflow = {v: 0 for v in vertices}
            outflow = {v: 0 for v in vertices}
            for p, f in zip(pipes, ys):
                outflow[p["from"]] += f
                inflow[p["to"]] += f
            st = stages[t]
            if outflow[model["source_id"]] != st["source_total"]:
                ok = False
                break
            if any(inflow[n] != outflow[n] for n in model["nodes"]):
                ok = False
                break
            if any(inflow[z] != st["demands"][z] for z in model["zone_ids"]):
                ok = False
                break
        if not ok:
            continue
        for t in range(1, T):
            if any(abs(combo[t * m + i] - combo[(t - 1) * m + i])
                   > pipes[i]["max_adjust"] for i in range(m)):
                ok = False
                break
        if not ok:
            continue
        obj = sum(abs(combo[t * m + i] - p["preferred"])
                  for t in range(T) for i, p in enumerate(pipes))
        if best is None or obj < best[0] or (obj == best[0]
                                             and combo < best[1]):
            best = (obj, tuple(combo))
    return best if best else (None, None)


def _random_payload(rng):
    n_nodes = rng.randrange(0, 3)
    n_zones = rng.randrange(2, 4)
    nodes = [f"N{i}" for i in range(n_nodes)]
    zones = [f"Z{i}" for i in range(n_zones)]
    sources = ["S"] + nodes
    targets = nodes + zones
    candidates = [(u, v) for u in sources for v in targets if u != v]
    n_pipes = rng.randrange(4, 6)
    pipes = []
    for i in range(n_pipes):
        u, v = rng.choice(candidates)
        lo = rng.randrange(0, 2)
        hi = lo + rng.randrange(0, 3)
        pipes.append(pipe(f"p{i}", u, v, lo, hi,
                          rng.randrange(lo, hi + 1), rng.randrange(0, 3)))
    T = rng.randrange(2, 4)
    stages = [stage(rng.randrange(0, 5),
                    [(z, rng.randrange(0, 4)) for z in zones])
              for _ in range(T)]
    return {"source": {"id": "S"}, "nodes": [{"id": n} for n in nodes],
            "zones": [{"id": z} for z in zones],
            "pipes": pipes, "stages": stages}


def _random_feasible_payload(rng):
    """先造跨阶段守恒流量（相邻差受控），再反推需求/总量/范围。"""
    n_nodes = rng.randrange(0, 3)
    n_zones = rng.randrange(2, 4)
    nodes = [f"N{i}" for i in range(n_nodes)]
    zones = [f"Z{i}" for i in range(n_zones)]
    edges = []
    for i, ni in enumerate(nodes):
        edges.append(("S", ni))
        targets = [f"N{j}" for j in range(i + 1, n_nodes)] + zones
        edges.append((ni, rng.choice(targets)))
    edges.append(("S", rng.choice(zones)))
    candidates = [("S", t) for t in nodes + zones]
    for i, ni in enumerate(nodes):
        for t in [f"N{j}" for j in range(i + 1, n_nodes)] + zones:
            candidates.append((ni, t))
    rng.shuffle(candidates)
    for c in candidates:
        if len(edges) >= rng.randrange(4, 6):
            break
        edges.append(c)
    while len(edges) < 4:
        edges.append(rng.choice(candidates))
    edges = edges[:6]
    m = len(edges)
    T = rng.randrange(2, 4)

    base = [rng.randrange(0, 3) for _ in range(m)]
    adjs = [rng.randrange(0, 3) for _ in range(m)]
    flows = []
    prev = base[:]
    for t in range(T):
        if t:
            prev = [max(0, prev[i] + rng.randrange(-adjs[i], adjs[i] + 1))
                    for i in range(m)]
        flows.append(prev[:])

    stages = []
    for t in range(T):
        inflow = {z: 0 for z in zones}
        outs = 0
        bal = {n: 0 for n in nodes}
        for (u, v), f in zip(edges, flows[t]):
            if u == "S":
                outs += f
            if v in inflow:
                inflow[v] += f
            if u in bal:
                bal[u] -= f
            if v in bal:
                bal[v] += f
        if any(bal[n] != 0 for n in bal):
            return None
        stages.append(stage(outs, [(z, inflow[z]) for z in zones]))

    pipes = []
    for i, (u, v) in enumerate(edges):
        col = [flows[t][i] for t in range(T)]
        lo = max(0, min(col) - rng.randrange(0, 2))
        hi = max(col) + rng.randrange(0, 2)
        pipes.append(pipe(f"p{i}", u, v, lo, hi,
                          rng.randrange(lo, hi + 1), adjs[i]))
    return {"source": {"id": "S"}, "nodes": [{"id": n} for n in nodes],
            "zones": [{"id": z} for z in zones],
            "pipes": pipes, "stages": stages}


class TestPlanRandomAgainstBruteForce(unittest.TestCase):
    """随机小实例与暴力枚举对照：可行性、目标值、字典序决胜序列、诊断。"""

    def test_random_plans(self):
        rng = random.Random(20260925)
        feasible_checked = 0
        infeasible_checked = 0
        cases = 0
        while cases < 150:
            if rng.random() < 0.6:
                payload = _random_feasible_payload(rng)
                if payload is None:
                    continue
            else:
                payload = _random_payload(rng)
            cases += 1
            bf_obj, bf_seq = _brute_best(payload)
            r = solve_plan(payload)
            if bf_obj is None:
                self.assertFalse(r["feasible"], msg=str(payload))
                info = r["infeasibility"]
                self.assertIn(info["kind"], ("conservation", "adjustment"))
                self.assertTrue(info["reasons"])
                if info["kind"] == "adjustment":
                    self.assertGreaterEqual(info["stage"], 1)
                    self.assertGreater(info["adjustment"]["gap"], 0)
                    self.assertTrue(info["adjustment"]["limited_pipes"])
                else:
                    self.assertGreater(info["conservation"]["gap"], 0)
                infeasible_checked += 1
                continue
            self.assertTrue(r["feasible"], msg=str(payload))
            self.assertEqual(r["objective"], bf_obj, msg=str(payload))
            self.assertEqual(tuple(r["tie_sequence"]), bf_seq,
                             msg=str(payload))
            for st in r["stages"]:
                self.assertEqual(st["balances"]["source"]["difference"], 0)
                self.assertTrue(all(n["difference"] == 0
                                    for n in st["balances"]["nodes"]))
                self.assertTrue(all(z["difference"] == 0
                                    for z in st["balances"]["zones"]))
            for adj in r["adjustments"]:
                self.assertTrue(all(pr["within_limit"]
                                    for pr in adj["pipes"]))
            feasible_checked += 1
        self.assertGreater(feasible_checked, 40)
        self.assertGreater(infeasible_checked, 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
