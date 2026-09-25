"""整数最小费用流求解器（纯标准库实现）。

Successive Shortest Augmenting Path（连续最短路增广）：每轮在残余
网络中求费用最短路，按路径残余容量瓶颈整体增广。用 Bellman-Ford 的
SPFA 队列优化实现，因此残余反向弧的负费用也能正确处理。

调用约定：初始网络不得含有负费用环（本项目的调整边费用全部为正，
仅有增广后产生的反向弧为负，SSP 对此天然正确）。容量、费用为整数，
瓶颈增广为整数，故所得流量为整数。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import List, Tuple


@dataclass
class _Arc:
    to: int
    cap: int
    cost: int
    rev: int  # 反向弧在邻接表中的下标


class MinCostFlow:
    def __init__(self, n: int):
        self.n = n
        self.g: List[List[_Arc]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int, cost: int) -> None:
        forward = _Arc(to=v, cap=cap, cost=cost, rev=len(self.g[v]))
        backward = _Arc(to=u, cap=0, cost=-cost, rev=len(self.g[u]))
        self.g[u].append(forward)
        self.g[v].append(backward)

    def flow(self, s: int, t: int, maxf: int) -> Tuple[int, int]:
        """从 s 向 t 推送至多 maxf 单位流，返回 (实际流量, 总费用)。"""
        total_flow = 0
        total_cost = 0
        n = self.n
        g = self.g

        while total_flow < maxf:
            dist = [None] * n
            in_queue = [False] * n
            prev_v = [-1] * n
            prev_e = [-1] * n
            dist[s] = 0
            queue = deque([s])
            in_queue[s] = True

            while queue:
                v = queue.popleft()
                in_queue[v] = False
                dv = dist[v]
                for i, e in enumerate(g[v]):
                    if e.cap > 0 and (dist[e.to] is None
                                      or dist[e.to] > dv + e.cost):
                        dist[e.to] = dv + e.cost
                        prev_v[e.to] = v
                        prev_e[e.to] = i
                        if not in_queue[e.to]:
                            queue.append(e.to)
                            in_queue[e.to] = True

            if dist[t] is None:
                break  # 残余网络中已无增广路

            add = maxf - total_flow
            v = t
            while v != s:
                add = min(add, g[prev_v[v]][prev_e[v]].cap)
                v = prev_v[v]

            v = t
            while v != s:
                e = g[prev_v[v]][prev_e[v]]
                e.cap -= add
                g[v][e.rev].cap += add
                v = prev_v[v]

            total_flow += add
            total_cost += add * dist[t]

        return total_flow, total_cost

    def used_flow(self, u: int, edge_index: int) -> int:
        """查询从 u 发出的第 edge_index 条正向边的已用流量。"""
        e = self.g[u][edge_index]
        return self.g[e.to][e.rev].cap
