"""精确字典序线性规划求解器（纯标准库，零第三方依赖）。

两阶段原始单纯形法 + Bland 规则（保证有限终止）：
- 约束：Σ a_j·x_j ≤ b（要求 b ≥ 0）或 Σ a_j·x_j = b（右端任意符号，内部规整）；
- 变量：x_j ≥ 0；上界请用 ≤ 约束显式给出；
- 目标：若干目标行按传入顺序**字典序最小化**（第一行绝对优先）。

精确性
------
全部运算为精确有理数运算：本求解器服务的约束矩阵是全单模（TU）的，
基矩阵行列式恒为 ±1，旋转主元恒为 ±1，tableau 始终保持整数；
若意外遇到非 ±1 主元（理论兜底），自动切换 Fraction 继续，结果仍精确。
"""

from __future__ import annotations

from fractions import Fraction
from typing import List, Sequence, Tuple

# 单纯形迭代上限（Bland 规则保证有限终止，此上限仅为防御性兜底）
MAX_ITERATIONS = 200_000


class LPError(RuntimeError):
    """求解器内部错误（如出现理论上不可达的无界/不可行中间态）。"""


def _pivot(tab: List[list], extra_rows: List[list], r: int, c: int) -> None:
    """以 tab[r][c] 为主元做旋转变换，extra_rows（目标行）同步消元。"""
    prow = tab[r]
    pivot = prow[c]
    if pivot == 0:
        raise LPError("主元为 0")
    if pivot == -1:
        prow = [-v for v in prow]
    elif pivot != 1:
        # TU 矩阵下不会走到这里；兜底切换 Fraction，保证任意输入精确
        prow = [Fraction(v, pivot) for v in prow]
    tab[r] = prow
    for i, row in enumerate(tab):
        if i == r:
            continue
        f = row[c]
        if f:
            tab[i] = [a - f * b for a, b in zip(row, prow)]
    for k, o in enumerate(extra_rows):
        f = o[c]
        if f:
            extra_rows[k] = [a - f * b for a, b in zip(o, prow)]


def _bland_simplex(tab: List[list], basis: List[int],
                   obj_rows: List[list], ncols: int) -> None:
    """在当前可行基上按 Bland 规则迭代至字典序最优（就地修改）。"""
    nc = ncols  # 右端列下标
    nrows = len(tab)
    in_basis = [False] * ncols
    for b in basis:
        in_basis[b] = True

    for _ in range(MAX_ITERATIONS):
        # 入基列：下标最小且既约成本向量字典序 < 0（首个非零分量为负）
        enter = -1
        for j in range(ncols):
            if in_basis[j]:
                continue
            improving = False
            for o in obj_rows:
                v = o[j]
                if v < 0:
                    improving = True
                    break
                if v > 0:
                    break
            if improving:
                enter = j
                break
        if enter < 0:
            return  # 字典序最优

        # 出基行：最小比值 tab[i][NC]/tab[i][enter]（分母恒正），
        # 并列时取基变量下标最小者（Bland 规则）
        leave = -1
        for i in range(nrows):
            a = tab[i][enter]
            if a <= 0:
                continue
            num = tab[i][nc]
            if num < 0:
                raise LPError("基本解不可行（右端为负）")
            if leave < 0:
                leave = i
                continue
            lnum, lden = tab[leave][nc], tab[leave][enter]
            lhs = num * lden
            rhs = lnum * a
            if lhs < rhs or (lhs == rhs and basis[i] < basis[leave]):
                leave = i
        if leave < 0:
            # 本项目全部 LP 均有界（结构性变量都有上界行；诊断 LP 的
            # 松弛变量目标系数非负），理论上不可达
            raise LPError("问题无界")

        _pivot(tab, obj_rows, leave, enter)
        in_basis[basis[leave]] = False
        in_basis[enter] = True
        basis[leave] = enter

    raise LPError("单纯形迭代次数超限")


def solve_lex(n_vars: int,
              constraints: Sequence[Tuple[Sequence[int], str, int]],
              objectives: Sequence[Sequence[int]]
              ) -> Tuple[str, list, list]:
    """求解 min (obj_1, obj_2, ...)（字典序），约束见模块 docstring。

    返回 ("optimal", x, values) 或 ("infeasible", None, None)；
    x 为前 n_vars 个结构变量的取值，values 为各目标行的最优值。
    """
    if n_vars <= 0:
        raise LPError("变量数必须为正")
    for coeffs, sense, _rhs in constraints:
        if len(coeffs) != n_vars:
            raise LPError("约束系数长度与变量数不符")
        if sense not in ("<=", "="):
            raise LPError(f"未知约束方向：{sense}")
    for coeffs in objectives:
        if len(coeffs) != n_vars:
            raise LPError("目标系数长度与变量数不符")

    n_le = sum(1 for _, s, _ in constraints if s == "<=")
    n_eq = len(constraints) - n_le
    ncols = n_vars + n_le + n_eq
    nc = ncols  # 右端列下标

    tab: List[list] = []
    basis: List[int] = []
    slack = n_vars
    art = n_vars + n_le
    artificials = set()
    for coeffs, sense, rhs in constraints:
        row = list(coeffs) + [0] * (n_le + n_eq) + [rhs]
        if sense == "<=":
            if rhs < 0:
                raise LPError("≤ 约束的右端必须非负")
            row[slack] = 1
            basis.append(slack)
            slack += 1
        else:  # "="
            if rhs < 0:
                row = [-v for v in row]
            row[art] = 1
            basis.append(art)
            artificials.add(art)
            art += 1
        tab.append(row)

    # ---- 第一阶段：人工变量和最小化，求初始可行基 ----
    if artificials:
        obj1 = [0] * (ncols + 1)
        for a in artificials:
            obj1[a] = 1
        for i, b in enumerate(basis):
            if b in artificials:
                f = obj1[b]
                if f:
                    obj1 = [o - f * v for o, v in zip(obj1, tab[i])]
        # 注意：_pivot 会替换目标行对象，必须通过包装列表回读
        phase1_rows = [obj1]
        _bland_simplex(tab, basis, phase1_rows, ncols)
        obj1 = phase1_rows[0]
        if -obj1[nc] > 0:
            return "infeasible", None, None

        # 人工变量出基；非人工列全零的行是冗余等式，直接删除
        for i in reversed(range(len(tab))):
            if basis[i] not in artificials:
                continue
            piv = next((j for j in range(ncols)
                        if j not in artificials and tab[i][j] != 0), None)
            if piv is not None:
                _pivot(tab, [], i, piv)
                basis[i] = piv
            else:
                if tab[i][nc] != 0:
                    raise LPError("人工变量残留但右端非零")
                del tab[i]
                del basis[i]
        # 删除人工列并重映射下标
        keep = [j for j in range(ncols) if j not in artificials]
        remap = {old: new for new, old in enumerate(keep)}
        tab = [[row[j] for j in keep] + [row[nc]] for row in tab]
        basis = [remap[b] for b in basis]
        ncols = len(keep)
        nc = ncols

    # ---- 第二阶段：字典序多目标 ----
    obj_rows: List[list] = []
    for coeffs in objectives:
        o = list(coeffs) + [0] * (ncols - n_vars) + [0]
        for i, b in enumerate(basis):
            f = o[b]
            if f:
                o = [x - f * v for x, v in zip(o, tab[i])]
        obj_rows.append(o)
    if obj_rows:
        _bland_simplex(tab, basis, obj_rows, ncols)

    x = [0] * n_vars
    for i, b in enumerate(basis):
        if b < n_vars:
            x[b] = tab[i][nc]
    values = [-o[nc] for o in obj_rows]
    return "optimal", x, values
