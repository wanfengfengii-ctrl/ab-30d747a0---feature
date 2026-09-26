"""缓升计划（多阶段联合配平）与 MILP 求解器单元测试（标准库 unittest）。"""

import itertools
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.milp import Model, solve_lp, solve_milp  # noqa: E402
from app.multistage import solve_plan, validate_plan  # noqa: E402


def pipe(pid, u, v, lo, hi, pref, adj):
    return {"id": pid, "from": u, "to": v,
            "min": lo, "max": hi, "preferred": pref, "max_adjust": adj}


def stage(total, demand_a, demand_b):
    return {"source_total": total,
            "zones": [{"id": "A", "demand": demand_a},
                      {"id": "B", "demand": demand_b}]}


class TestMILPSolver(unittest.TestCase):
    def test_minimize_with_bounds(self):
        m = Model()
        x = m.var(0, 10, 3, "x")
        y = m.var(0, 10, 5, "y")
        m.eq([(x, 1), (y, 1)], 10)
        st, vals, obj = solve_milp(m)
        self.assertEqual(st, "optimal")
        self.assertEqual((vals[0], vals[1], obj), (10, 0, 30))

    def test_negative_cost_integer(self):
        m = Model()
        x = m.var(0, 4, -1, "x")
        y = m.var(0, 4, 10, "y")
        m.eq([(x, 1), (y, 1)], 2)
        st, vals, _ = solve_milp(m)
        self.assertEqual((vals[0], vals[1]), (2, 0))

    def test_infeasible_bounds(self):
        m = Model()
        x = m.var(0, 3)
        y = m.var(0, 3)
        m.eq([(x, 1), (y, 1)], 10)
        self.assertEqual(solve_milp(m)[0], "infeasible")

    def test_abs_value_epigraph(self):
        # min |x-4|+|y-7|, x+y=10 → x=3,y=7 或 x=4,y=6 等并列，目标 1
        m = Model()
        x = m.var(0, 10, 0, "x")
        y = m.var(0, 10, 0, "y")
        t1 = m.var(0, 10, 1, "t1", branchable=False)
        t2 = m.var(0, 10, 1, "t2", branchable=False)
        m.ge([(t1, 1), (x, -1)], -4)
        m.ge([(t1, 1), (x, 1)], 4)
        m.ge([(t2, 1), (y, -1)], -7)
        m.ge([(t2, 1), (y, 1)], 7)
        m.eq([(x, 1), (y, 1)], 10)
        st, vals, obj = solve_milp(m)
        self.assertEqual(st, "optimal")
        self.assertEqual(obj, 1)
        self.assertEqual(vals[2] + vals[3], 1)  # t1+t2=1
        self.assertEqual(vals[0] + vals[1], 10)

    def test_lp_fractional_then_branch(self):
        # 松弛最优为分数 (2.5,2.5)；整数最优需分支定界找到
        m = Model()
        x = m.var(0, 5, 1, "x")
        y = m.var(0, 5, 1, "y")
        m.eq([(x, 2), (y, 2)], 9)  # x+y=4.5，无整数解 → 不可行
        st, vals, obj = solve_milp(m)
        self.assertEqual(st, "infeasible")

        m = Model()
        x = m.var(0, 5, 1, "x")
        y = m.var(0, 5, 1, "y")
        m.eq([(x, 2), (y, 2)], 11)  # x+y=5.5，无整数解
        self.assertEqual(solve_milp(m)[0], "infeasible")

    def test_lp_fraction_against_hand_solve(self):
        # min x+2y, x+y>=7, 0<=x,y<=10 → x=7,y=0
        m = Model()
        x = m.var(0, 10, 1, branchable=False)
        y = m.var(0, 10, 2, branchable=False)
        m.ge([(x, 1), (y, 1)], 7)
        st, xf = solve_lp(m)
        self.assertEqual(st, "optimal")
        self.assertEqual((xf[0], xf[1]), (7, 0))


class TestPlanBasic(unittest.TestCase):
    def base(self, adj=2):
        return {
            "source": {"id": "S"},
            "source_total": 10,
            "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}],
            "nodes": [{"id": "N"}],
            "pipes": [
                pipe("p1", "S", "N", 0, 10, 5, adj),
                pipe("p2", "N", "A", 0, 10, 3, adj),
                pipe("p3", "N", "B", 0, 10, 7, adj),
                pipe("p4", "S", "A", 0, 0, 0, adj),
            ],
            "stages": [
                stage(10, 6, 4),
                stage(10, 8, 2),
            ],
        }

    def test_feasible_joint_solution(self):
        r = solve_plan(self.base(adj=2))
        self.assertTrue(r["feasible"])
        self.assertEqual(r["stage_count"], 2)
        # 展平决胜序列：阶段优先、管序次之
        self.assertEqual(r["tie_sequence"],
                         [10, 6, 4, 0, 10, 8, 2, 0])
        self.assertEqual(r["objective"], 11 + 15)  # 两阶段偏差和 11+15
        # 逐阶段守恒
        for s in r["stages"]:
            self.assertEqual(s["balances"]["source"]["difference"], 0)
            self.assertEqual(
                [n["difference"] for n in s["balances"]["nodes"]], [0])
            self.assertEqual(
                [z["difference"] for z in s["balances"]["zones"]], [0, 0])
        # 相邻调节明细
        adj = r["adjustments"][0]
        self.assertTrue(adj["within_limit"])
        self.assertEqual({p["pipe_id"]: p["change"] for p in adj["pipes"]},
                         {"p1": 0, "p2": 2, "p3": 2, "p4": 0})
        for prow in adj["pipes"]:
            self.assertLessEqual(prow["change"], prow["limit"])
        # 阶段内流量行携带相邻调整量
        k1 = {f["pipe_id"]: f for f in r["stages"][1]["flows"]}
        self.assertEqual(k1["p2"]["change"], 2)
        self.assertEqual(k1["p2"]["change_limit"], 2)
        self.assertTrue(k1["p2"]["within_limit"])

    def test_adjustment_overlimit_infeasible(self):
        r = solve_plan(self.base(adj=1))
        self.assertFalse(r["feasible"])
        info = r["infeasibility"]
        self.assertEqual(info["failing_stage"], 1)  # 第 2 阶段最早失败
        self.assertEqual(info["kind"], "adjustment")
        self.assertGreater(info["gap_total"], 0)
        ids = {b["pipe_id"] for b in info["blocked_pipes"]}
        self.assertEqual(ids, {"p2", "p3"})
        for b in info["blocked_pipes"]:
            self.assertGreater(b["excess"], 0)
            self.assertEqual(
                b["required_change"], b["allowed_change"] + b["excess"])
        # 放松解全部管路守恒
        rows = info["relaxed_stage_flows"]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(not row["within_limit"]
                            for row in rows if row["pipe_id"] in ids))

    def test_first_stage_infeasible(self):
        # 第 1 阶段水源总量与需求不等
        payload = self.base()
        payload["stages"][0] = stage(9, 6, 4)
        r = solve_plan(payload)
        self.assertFalse(r["feasible"])
        info = r["infeasibility"]
        self.assertEqual(info["failing_stage"], 0)
        self.assertEqual(info["kind"], "stage_infeasible")
        self.assertTrue(info["reasons"])

    def test_later_stage_internal_infeasible(self):
        # 第 1 阶段可行；第 2 阶段总量与需求不等（放松调节也救不回来）
        payload = self.base(adj=100)
        payload["stages"][1] = stage(11, 8, 2)
        r = solve_plan(payload)
        self.assertFalse(r["feasible"])
        info = r["infeasibility"]
        self.assertEqual(info["failing_stage"], 1)
        self.assertEqual(info["kind"], "stage_infeasible")

    def test_joint_not_stitch_of_independent_optima(self):
        # 逐阶段独立最优：阶段1 (0,2) 偏好，阶段2 需要 (4,0)；
        # 调节上限 1 禁止一步到位，联合解必须让阶段 1 提前偏离。
        payload = {
            "source": {"id": "S"},
            "source_total": 4,
            "zones": [{"id": "A", "demand": 2}, {"id": "B", "demand": 2}],
            "nodes": [],
            "pipes": [
                pipe("p1", "S", "A", 0, 4, 0, 1),
                pipe("p2", "S", "A", 0, 4, 4, 1),
                pipe("p3", "S", "B", 0, 4, 0, 1),
                pipe("p4", "S", "B", 0, 4, 4, 1),
            ],
            "stages": [
                stage(4, 2, 2),
                stage(4, 4, 0),
            ],
        }
        r = solve_plan(payload)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["tie_sequence"], [0, 2, 1, 1, 1, 3, 0, 0])
        # 阶段 1 的 A 分区流量（p1+p2=2）没有直接取 (0,2)，
        # 而是 (0,2)→(1,3) 平滑过渡，验证联合而非拼接。
        self.assertEqual([f["flow"] for f in r["stages"][0]["flows"]],
                         [0, 2, 1, 1])
        self.assertEqual([f["flow"] for f in r["stages"][1]["flows"]],
                         [1, 3, 0, 0])

    def test_three_stages(self):
        payload = self.base(adj=2)
        payload["stages"] = [
            stage(10, 6, 4),
            stage(10, 8, 2),
            stage(10, 6, 4),
        ]
        r = solve_plan(payload)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["stage_count"], 3)
        self.assertEqual(len(r["stages"]), 3)
        self.assertEqual(len(r["adjustments"]), 2)
        self.assertEqual(len(r["tie_sequence"]), 12)


class TestPlanValidation(unittest.TestCase):
    def base(self):
        return {
            "source": {"id": "S"},
            "source_total": 5,
            "zones": [{"id": "A", "demand": 3}, {"id": "B", "demand": 2}],
            "nodes": [],
            "pipes": [
                pipe("p1", "S", "A", 0, 5, 2, 1),
                pipe("p2", "S", "B", 0, 5, 2, 1),
                pipe("p3", "S", "A", 0, 5, 1, 1),
                pipe("p4", "S", "B", 0, 5, 0, 1),
            ],
            "stages": [stage(5, 3, 2), stage(5, 3, 2)],
        }

    def test_stage_count(self):
        p = self.base()
        p["stages"] = [stage(5, 3, 2)]  # 只有 1 个
        with self.assertRaises(Exception):
            validate_plan(p)
        p = self.base()
        p["stages"] = [stage(5, 3, 2) for _ in range(5)]  # 5 个
        with self.assertRaises(Exception):
            validate_plan(p)

    def test_missing_zone_demand_in_stage(self):
        p = self.base()
        p["stages"][0]["zones"] = [{"id": "A", "demand": 5}]
        with self.assertRaises(Exception):
            validate_plan(p)

    def test_unknown_zone_in_stage(self):
        p = self.base()
        p["stages"][0]["zones"].append({"id": "X", "demand": 0})
        with self.assertRaises(Exception):
            validate_plan(p)

    def test_missing_max_adjust(self):
        p = self.base()
        del p["pipes"][0]["max_adjust"]
        with self.assertRaises(Exception):
            validate_plan(p)

    def test_negative_max_adjust(self):
        p = self.base()
        p["pipes"][0]["max_adjust"] = -1
        with self.assertRaises(Exception):
            validate_plan(p)

    def test_non_integer_stage_total(self):
        p = self.base()
        p["stages"][1]["source_total"] = 3.5
        with self.assertRaises(Exception):
            validate_plan(p)


# --------------------------------------------------------------------------- #
# 随机暴力枚举对照（小规模网络，确保在验收容器内快速完成）
# --------------------------------------------------------------------------- #

def _brute_joint(model, stages, limits):
    pipes = model["pipes"]
    m = len(pipes)
    src = model["source_id"]
    nodes = model["nodes"]
    zones = [z["id"] for z in model["zones"]]
    options = [range(p["min"], p["max"] + 1) for p in pipes]
    per_stage = []
    for st in stages:
        feas = []
        for vals in itertools.product(*options):
            infl = {v: 0 for v in [src] + nodes + zones}
            outfl = {v: 0 for v in [src] + nodes + zones}
            for p, f in zip(pipes, vals):
                outfl[p["from"]] += f
                infl[p["to"]] += f
            if outfl[src] != st["total"]:
                continue
            if any(infl[z] != st["demands"][z] for z in zones):
                continue
            if any(infl[n] != outfl[n] for n in nodes):
                continue
            feas.append(vals)
        per_stage.append(feas)
        if not feas:
            return False, None, None
    best_obj, best_tie = None, None
    for combo in itertools.product(*per_stage):
        if any(abs(combo[k][i] - combo[k - 1][i]) > limits[i]
               for k in range(1, len(combo)) for i in range(m)):
            continue
        obj = sum(abs(combo[k][i] - pipes[i]["preferred"])
                  for k in range(len(combo)) for i in range(m))
        tie = tuple(v for c in combo for v in c)
        if best_obj is None or obj < best_obj or (
                obj == best_obj and tie < best_tie):
            best_obj, best_tie = obj, tie
    return best_obj is not None, best_obj, best_tie


def _random_payload(rng):
    """构造式随机缓升网络：先造守恒的阶段流量序列，再反推范围与需求。

    约 1/4 概率收紧某管调节上限制造调节性不可行；
    规模控制在 4~5 管、范围 ≤3，保证暴力枚举快速完成。
    """
    n_nodes = rng.randrange(0, 2)
    zones = ["A", "B"]
    nodes = [f"N{i}" for i in range(n_nodes)]
    edges = []
    for i, ni in enumerate(nodes):
        edges.append(("S", ni))
        edges.append((ni, rng.choice(zones)))
    edges.append(("S", rng.choice(zones)))
    candidates = [("S", t) for t in nodes + zones]
    for ni in nodes:
        for t in zones:
            candidates.append((ni, t))
    rng.shuffle(candidates)
    for c in candidates:
        if len(edges) >= 5:
            break
        edges.append(c)
    while len(edges) < 4:
        edges.append(rng.choice(candidates))
    edges = edges[:5]
    m = len(edges)
    T = rng.randrange(2, 4)
    out_e = {}
    for i, (u, v) in enumerate(edges):
        out_e.setdefault(u, []).append(i)

    stage_flows = []
    stage_demands = []
    prev_w = None
    w0 = rng.randrange(2, 6)
    for _ in range(T):
        w = w0 if prev_w is None else max(0, min(w0 + 5,
                                                prev_w + rng.randrange(-2, 3)))
        supply = {v: 0 for v in nodes}
        supply["S"] = w
        vals = [0] * m
        for v in ["S"] + nodes:
            ids = out_e.get(v, [])
            if not ids:
                if supply.get(v, 0) > 0:
                    return None
                continue
            amount = supply[v]
            kk = len(ids)
            if kk == 1:
                cuts = [amount]
            elif amount == 0:
                cuts = [0] * kk
            else:
                parts = sorted(rng.sample(range(amount + kk - 1), kk - 1))
                cuts = []
                mark = 0
                for c in parts:
                    cuts.append(c - mark)
                    mark = c + 1
                cuts.append(amount + kk - 1 - mark)
            for ei, fv in zip(ids, cuts):
                vals[ei] = fv
                to = edges[ei][1]
                if to in supply:
                    supply[to] += fv
        dem = {z: 0 for z in zones}
        for i, (u, v) in enumerate(edges):
            if v in dem:
                dem[v] += vals[i]
        stage_flows.append(vals)
        stage_demands.append((w, dem))
        prev_w = w

    pipes = []
    for i, (u, v) in enumerate(edges):
        fvals = [stage_flows[k][i] for k in range(T)]
        lo = max(0, min(fvals) - rng.randrange(0, 2))
        hi = max(fvals) + rng.randrange(0, 2)
        pref = rng.randrange(lo, hi + 1)
        maxdiff = max(abs(fvals[k] - fvals[k - 1]) for k in range(1, T))
        adj = maxdiff + rng.randrange(0, 2)
        if rng.random() < 0.25:
            adj = max(0, maxdiff - rng.randrange(1, 3))
        pipes.append(pipe(f"p{i}", u, v, lo, hi, pref, adj))

    stages = [stage(w, d["A"], d["B"]) for w, d in stage_demands]
    return {
        "source": {"id": "S"},
        "source_total": stages[0]["source_total"],
        "zones": [{"id": "A", "demand": 0}, {"id": "B", "demand": 0}],
        "nodes": [{"id": n} for n in nodes],
        "pipes": pipes,
        "stages": stages,
    }


class TestPlanRandomBruteForce(unittest.TestCase):
    def test_random_networks(self):
        rng = random.Random(20260926)
        feasible_checked = infeasible_checked = 0
        attempts = 0
        while feasible_checked < 40 and attempts < 600:
            attempts += 1
            payload = _random_payload(rng)
            if payload is None:
                continue
            model, stages, limits = validate_plan(payload)
            bf, bobj, btie = _brute_joint(model, stages, limits)
            r = solve_plan(payload)
            if not bf:
                self.assertFalse(r["feasible"], msg=str(payload))
                # 最早失败阶段必须与逐前缀暴力枚举一致
                expect_fail = None
                for k in range(1, len(stages) + 1):
                    ok, _, _ = _brute_joint(model, stages[:k], limits)
                    if not ok:
                        expect_fail = k - 1
                        break
                self.assertEqual(
                    r["infeasibility"]["failing_stage"], expect_fail,
                    msg=str(payload))
                infeasible_checked += 1
                continue
            self.assertTrue(r["feasible"], msg=str(payload))
            self.assertEqual(r["objective"], bobj, msg=str(payload))
            self.assertEqual(tuple(r["tie_sequence"]), btie,
                             msg=str(payload))
            for s in r["stages"]:
                self.assertEqual(s["balances"]["source"]["difference"], 0)
                self.assertTrue(all(n["difference"] == 0
                                    for n in s["balances"]["nodes"]))
                self.assertTrue(all(z["difference"] == 0
                                    for z in s["balances"]["zones"]))
            for a in r["adjustments"]:
                for prow in a["pipes"]:
                    self.assertLessEqual(prow["change"], prow["limit"])
            feasible_checked += 1
        self.assertGreaterEqual(feasible_checked, 40)
        self.assertGreaterEqual(infeasible_checked, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
