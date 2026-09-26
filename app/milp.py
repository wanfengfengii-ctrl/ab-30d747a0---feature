"""小型整数线性规划求解器（纯标准库）。

仅服务于"缓升计划"联合求解：变量与约束规模都很小
（≤10 管 × ≤4 阶段：流量变量 ≤40，等式约束 ≤176）。

方法
----
- 线性松弛：**有界变量两阶段单纯形**（bounded-variable simplex），
  稀疏表实现（每行一个 dict，只存非零元）。
  所有变量统一平移为 y = x - lb ∈ [0, U]；非基变量贴下界（状态 L，
  表中列即 y）或贴上界（状态 U，表中列用 ȳ = U - y 代替、列取负）。
  入基检验统一为检验数 > 0；阻断量同时考虑：
    1. 入基变量自己撞到对面界（只翻 L/U 状态、不换基）；
    2. 基变量撞到下界 0 或上界 U（按基行定向判断撞哪个界）。
  从上界方向入基的变量成为基变量后，其基行是 U 定向的
  （物理值 = ub - 行 RHS），用 b_orient 逐行跟踪；
  出基时按"行定向 × 撞界方向"决定该列是否翻向。
- 全部数字用 Fraction 精确表示，无浮点误差；Bland 规则防循环。
- 整数化：在松弛解上分支定界（x ≤ ⌊v⌋ / x ≥ ⌈v⌉），
  目标为整数权重，界直接精确比较剪枝。

这不是通用高性能 MILP，只保证本项目小整数网络上的正确性与可复现性。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Dict, List, Optional, Sequence, Tuple

# 分支节点数上限：本项目的小网络正常只需个位数节点；
# 超过说明输入异常，明确报错而不是无限挂起。
DEFAULT_NODE_LIMIT = 20000

# 非基/基变量状态与基行定向
LOWER = 0   # 非基贴下界（表列 = y = x-lb）
BASIC = 1
UPPER = 2   # 非基贴上界（表列 = U-y）

_ZERO = Fraction(0)


class MILPError(RuntimeError):
    """建模/求解异常（如无界等不应出现的情况）。"""


class Model:
    """min  c·x  s.t. Ax = b, lb ≤ x ≤ ub。

    branchable=False 的变量仅用于辅助表达（如偏差/越限变量）：
    它们的值由可分支变量与约束唯一确定，不参与分支与整性检查。
    """

    def __init__(self) -> None:
        self.c: List[int] = []
        self.lb: List[int] = []
        self.ub: List[int] = []
        self.branchable: List[bool] = []
        self.names: List[str] = []
        # 每条等式：({var_idx: coeff}, rhs)
        self.rows: List[Tuple[Dict[int, int], int]] = []

    def var(self, lb: int, ub: int, coef: int = 0, name: str = "",
            branchable: bool = True) -> int:
        if lb > ub:
            raise MILPError(f"变量 {name or '(无名)'} 下界 {lb} 大于上界 {ub}")
        idx = len(self.c)
        self.c.append(coef)
        self.lb.append(lb)
        self.ub.append(ub)
        self.branchable.append(branchable)
        self.names.append(name)
        return idx

    def eq(self, terms: Sequence[Tuple[int, int]], rhs: int) -> None:
        row: Dict[int, int] = {}
        for j, a in terms:
            if a == 0:
                continue
            row[j] = row.get(j, 0) + a
        self.rows.append(({j: a for j, a in row.items() if a != 0}, rhs))

    def ge(self, terms: Sequence[Tuple[int, int]], rhs: int) -> None:
        """Σ a·x ≥ rhs：引入非负松弛 s，写成 Σ a·x - s = rhs。"""
        terms = [(j, a) for j, a in terms if a != 0]
        # s 的有限上界：当前各项物理摆幅与右端之和（必然够用）
        slack_ub = abs(rhs) + sum(
            abs(a) * (self.ub[j] - self.lb[j]) for j, a in terms)
        s = self.var(0, slack_ub, 0, branchable=False)
        self.eq(list(terms) + [(s, -1)], rhs)


def _sub_row(row: Dict[int, Fraction], factor: Fraction,
             prow: Dict[int, Fraction]) -> None:
    """row -= factor * prow（稀疏，就地，零元即删）。"""
    for k, v in prow.items():
        nv = row.get(k, _ZERO) - factor * v
        if nv == 0:
            row.pop(k, None)
        else:
            row[k] = nv


def _flip_col(rows: List[Dict[int, Fraction]], obj: Dict[int, Fraction],
              j: int, upper: int, rhs_col: int) -> None:
    """列 j 换定向：y ↔ ȳ=U-y（列取负、RHS 平移 U）。"""
    for row in rows:
        a = row.get(j)
        if a is not None:
            row[rhs_col] = row.get(rhs_col, _ZERO) - a * upper
            row[j] = -a
    a = obj.get(j)
    if a is not None:
        obj[rhs_col] = obj.get(rhs_col, _ZERO) - a * upper
        obj[j] = -a


def _simplex_drive(rows: List[Dict[int, Fraction]],
                   obj: Dict[int, Fraction], basis: List[int],
                   b_orient: List[int], status: List[int],
                   rhs_col: int, enterable: List[bool],
                   col_upper: List[Optional[int]]) -> Fraction:
    """有界单纯形主循环（稀疏表）。原地旋转至最优，返回目标行 RHS。"""
    nrows = len(rows)

    while True:
        entering = -1
        for j in sorted(obj.keys()):  # Bland：取下标最小的正检验数
            if j != rhs_col and enterable[j] and obj[j] > 0:
                entering = j
                break
        if entering < 0:
            return obj.get(rhs_col, _ZERO)

        u = col_upper[entering]
        theta: Optional[Fraction] = Fraction(u) if u is not None else None
        blocker = -1
        leave_upper = False
        for i in range(nrows):
            a = rows[i].get(entering)
            if a is None or a == 0:
                continue
            bv = basis[i]
            bu = col_upper[bv]
            rhs_i = rows[i].get(rhs_col, _ZERO)
            if b_orient[i] == LOWER:
                # L 行：基值 = rhs；a>0 撞下界，a<0 撞上界
                if a > 0:
                    t = rhs_i / a
                    hit_up = False
                else:
                    if bu is None:
                        continue
                    t = (Fraction(bu) - rhs_i) / (-a)
                    hit_up = True
            else:
                # U 行：ȳ = rhs，物理值 = ub - ȳ；
                # a>0：ȳ 减 → 物理撞上界（t = rhs/a）；
                # a<0：ȳ 增 → 物理撞下界（t = (U-rhs)/(-a)）。
                if a > 0:
                    t = rhs_i / a
                    hit_up = True
                else:
                    if bu is None:
                        continue
                    t = (Fraction(bu) - rhs_i) / (-a)
                    hit_up = False
            if (theta is None or t < theta or
                    (t == theta and blocker >= 0
                     and bv < basis[blocker])):
                theta, blocker, leave_upper = t, i, hit_up

        if theta is None:
            raise MILPError("线性松弛无界（模型变量均应有界）")

        if blocker < 0:
            # 入基变量先撞到对面界：只翻转 L/U 状态，不换基
            _flip_col(rows, obj, entering, col_upper[entering], rhs_col)
            status[entering] = (UPPER if status[entering] == LOWER
                                else LOWER)
            continue

        leaving = basis[blocker]
        row_orient = b_orient[blocker]  # 出基行的旧定向（换基前读取）
        prow = rows[blocker]
        pv = prow[entering]
        if pv != 1:  # 主行归一
            for k in list(prow.keys()):
                prow[k] /= pv
        for i in range(nrows):  # Gauss-Jordan 消元
            if i == blocker:
                continue
            factor = rows[i].get(entering)
            if factor:
                _sub_row(rows[i], factor, prow)
        factor = obj.get(entering)
        if factor:
            _sub_row(obj, factor, prow)
        basis[blocker] = entering
        b_orient[blocker] = status[entering]  # 新基行沿用入基方向
        status[entering] = BASIC

        # 出基变量贴它撞到的界。其列的当前定向 = 出基行的旧定向：
        # 非基约定要求列表示"当前取 0 的定向变量"，故是否翻列
        # 取决于行定向与撞界方向的组合：
        #   L 行撞下界(y=0)：列即 y，不翻；L 行撞上界(y=U)：翻成 ȳ。
        #   U 行撞上界(ȳ=0)：列即 ȳ，不翻；U 行撞下界(ȳ=U)：翻成 y。
        need_flip = (row_orient == LOWER) == leave_upper
        if need_flip:
            _flip_col(rows, obj, leaving, col_upper[leaving], rhs_col)
        status[leaving] = UPPER if leave_upper else LOWER


def solve_lp(model: Model,
             lb_extra: Optional[Dict[int, int]] = None,
             ub_extra: Optional[Dict[int, int]] = None
             ) -> Tuple[str, List[Fraction]]:
    """解一次线性松弛，返回 (status, x)。"""
    lb_extra = lb_extra or {}
    ub_extra = ub_extra or {}

    n = len(model.c)
    lbs = [max(model.lb[j], lb_extra.get(j, model.lb[j])) for j in range(n)]
    ubs = [min(model.ub[j], ub_extra.get(j, model.ub[j])) for j in range(n)]
    for j in range(n):
        if lbs[j] > ubs[j]:
            return "infeasible", []
    upper: List[int] = [ubs[j] - lbs[j] for j in range(n)]

    # ---- 平移到 0..U，构造初始人造基稀疏表 [A | I | b]，b ≥ 0 ----
    m = len(model.rows)
    rhs_col = n + m
    rows: List[Dict[int, Fraction]] = []
    for row, rhs in model.rows:
        b = rhs
        r: Dict[int, Fraction] = {}
        for j, a in row.items():
            r[j] = Fraction(a)
            b -= a * lbs[j]
        r[n + len(rows)] = Fraction(1)  # 人工列
        if b < 0:
            r = {k: -v for k, v in r.items()}
            b = -b
        if b != 0:
            r[rhs_col] = Fraction(b)
        rows.append(r)

    basis = [n + i for i in range(m)]
    b_orient = [LOWER] * m
    status = [LOWER] * n + [BASIC] * m
    col_upper: List[Optional[int]] = list(upper) + [None] * m
    enterable = [j < n for j in range(n + m)]  # 人工列不得重新入基

    # 第一阶段目标行：各行直接求和（基列系数非零无碍——人工列不可入基，
    # Gauss-Jordan 换基后结构列系数始终是正确的检验数）。
    obj: Dict[int, Fraction] = {}
    for r in rows:
        for k, v in r.items():
            obj[k] = obj.get(k, _ZERO) + v
    obj = {k: v for k, v in obj.items() if v != 0}

    wval = _simplex_drive(rows, obj, basis, b_orient, status, rhs_col,
                          enterable, col_upper)
    if wval != 0:
        return "infeasible", []

    # 仍在基中的人工变量：零步退化换出；全零结构行属冗余等式，删除。
    keep_rows: List[int] = []
    for i in range(m):
        if basis[i] < n:
            keep_rows.append(i)
            continue
        cand = -1
        for j in sorted(rows[i].keys()):
            if j < n:
                cand = j
                break
        if cand < 0:
            continue
        # 退化旋转（人工基取值为 0，主元归一不改变任何 RHS）；
        # 新基行定向沿用 cand 当前的 L/U 状态。
        prow = rows[i]
        pv = prow[cand]
        if pv != 1:
            for k in list(prow.keys()):
                prow[k] /= pv
        for ii in range(m):
            if ii == i:
                continue
            factor = rows[ii].get(cand)
            if factor:
                _sub_row(rows[ii], factor, prow)
        factor = obj.get(cand)
        if factor:
            _sub_row(obj, factor, prow)
        basis[i] = cand
        b_orient[i] = status[cand]
        status[cand] = BASIC
        keep_rows.append(i)

    if not keep_rows:
        return "optimal", [Fraction(lbs[j]) for j in range(n)]

    # ---- 压缩：去冗余行、去人工列、RHS 列重编号为 n ----
    tab: List[Dict[int, Fraction]] = []
    for i in keep_rows:
        r: Dict[int, Fraction] = {}
        for k, v in rows[i].items():
            if k == rhs_col:
                r[n] = v
            elif k < n:
                r[k] = v
        tab.append(r)
    basis = [basis[i] for i in keep_rows]
    b_orient = [b_orient[i] for i in keep_rows]
    status = status[:n]
    rhs_col = n

    # ---- 第二阶段目标行 ----
    # 表中 L 定向列代表 y=x-lb；U 定向列代表 ȳ=U-y（基行同理）。
    # z = Σc·lb + Σ_{U类} c·U + Σ_{L列} c·y - Σ_{U列} c·ȳ
    # 写成 z + Σr·列 = RHS：L 类列系数 -c，U 类列系数 +c。
    basic_orient = {basis[i]: b_orient[i] for i in range(len(tab))}
    obj2: Dict[int, Fraction] = {}
    const = sum(model.c[j] * lbs[j] for j in range(n))
    for j in range(n):
        u_oriented = (status[j] == UPPER or
                      (status[j] == BASIC and basic_orient[j] == UPPER))
        coef = model.c[j]
        if coef == 0:
            continue
        if u_oriented:
            obj2[j] = Fraction(coef)
            const += coef * upper[j]
        else:
            obj2[j] = Fraction(-coef)
    if const != 0:
        obj2[rhs_col] = Fraction(const)
    # 消去基列（单位列系数恒为 +1，无论基行 L/U 定向）
    for i in range(len(tab)):
        factor = obj2.get(basis[i])
        if factor:
            _sub_row(obj2, factor, tab[i])

    _simplex_drive(tab, obj2, basis, b_orient, status, rhs_col,
                   [True] * n, list(upper))

    # ---- 还原物理取值 ----
    x = [_ZERO] * n
    for i, bv in enumerate(basis):
        rhs_i = tab[i].get(rhs_col, _ZERO)
        if b_orient[i] == LOWER:
            x[bv] = rhs_i
        else:
            x[bv] = Fraction(upper[bv]) - rhs_i
    for j in range(n):
        if status[j] != BASIC:
            x[j] = Fraction(upper[j]) if status[j] == UPPER else _ZERO
    return "optimal", [x[j] + lbs[j] for j in range(n)]


def solve_milp(model: Model,
               node_limit: int = DEFAULT_NODE_LIMIT
               ) -> Tuple[str, Optional[List[int]], Optional[int]]:
    """分支定界求整数最优。

    返回 (status, x, objective)：
    - "optimal"：x 为全部变量的整数取值，objective 为最小目标值；
    - "infeasible"：x/objective 为 None。
    可分支变量之外的辅助变量由约束确定，不单独判整/分支。
    """
    n = len(model.c)
    # 目标权重大的变量先分支（主导位尽早固定）；同权重保持建模顺序。
    priority = sorted(
        (j for j in range(n) if model.branchable[j]),
        key=lambda j: (-abs(model.c[j]), j))

    stack: List[Tuple[Dict[int, int], Dict[int, int]]] = [({}, {})]
    incumbent: Optional[List[int]] = None
    inc_obj: Optional[int] = None
    nodes = 0

    while stack:
        lb_extra, ub_extra = stack.pop()
        nodes += 1
        if nodes > node_limit:
            raise MILPError(f"分支节点超过 {node_limit}，请缩小水量/范围后重试")

        status, xf = solve_lp(model, lb_extra, ub_extra)
        if status == "infeasible":
            continue
        objf = sum(Fraction(model.c[j]) * xf[j] for j in range(n))
        # 目标为整数权重：松弛值 ≥ 现任整数解，则枝上不可能更优
        if inc_obj is not None and objf >= inc_obj:
            continue

        frac_j = -1
        for j in priority:
            if xf[j].denominator != 1:
                frac_j = j
                break

        if frac_j < 0:
            xint = [int(xf[j]) for j in range(n)]
            obj = sum(model.c[j] * xint[j] for j in range(n))
            if inc_obj is None or obj < inc_obj:
                incumbent, inc_obj = xint, obj
            continue

        v = xf[frac_j]
        floor_v = v.numerator // v.denominator
        # 字典序更小的一侧（向下压）后入栈先探
        stack.append((lb_extra, {**ub_extra, frac_j: floor_v}))
        stack.append(({**lb_extra, frac_j: floor_v + 1}, ub_extra))

    if incumbent is None:
        return "infeasible", None, None
    return "optimal", incumbent, inc_obj
