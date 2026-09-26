# 纸本修复室 · 雾化管路配平系统

回湿脆化古画前，审核雾化管路能否把规定水量稳定送到每个分区：
**一处水源、2~4 个分区、0~4 个分流节点、4~10 条有向管路**，求整数流量方案。

- 水源流出**恰等于**水源总量；
- 每个分流节点流入 = 流出（不会凭空增减流量）；
- 每个分区流入**恰等于**精确需求（不会因某条支路有余量就让下游缺水）；
- 每条管路流量为整数且在 `[最小量, 最大量]` 内；
- 可行方案先最小化**相对优选量的绝对偏差和**，再按**管路录入顺序的流量序列字典序**稳定决胜；
- 不可行时给出逐点收支诊断（缺水点 / 积压点、缺口水量、可读原因）。

## 缓升计划（多阶段联合配平）

古画需分阶段回湿时，在既有管路草稿旁录入 **2~4 个按顺序的湿润阶段**：
每阶段填写水源总量与各分区精确需求，并为每条既有管路填写
**相邻阶段允许的整数最大调节量** `max_adjust`。提交后服务端把
**全部阶段放进同一个整数规划联合求解**（绝不逐阶段独立配平后再拼接）：

- 任一阶段仍满足水源、分流节点、分区守恒及原有上下限；
- 同一管路相邻阶段的流量差绝对值不得越过其调节量；
- 所有可行计划先取**相对各管路既有优选量的总绝对偏差**最小，
  再按**阶段顺序优先、每阶段管路录入顺序次之**展平的流量序列稳定决胜；
- 无可行计划时，按**最早无法与前序阶段同时满足调节约束的阶段**返回
  越限缺口与受限管路（附放松调节后的参考配水），
  阶段自身不守恒时给出逐点收支诊断；
- 页面展示逐阶段流量、相邻调整量和守恒明细；
  修改草稿或任一阶段输入后，已生成的旧计划立即失效清除。

## 架构

| 组件 | 技术 | 说明 |
|---|---|---|
| `api` | Python 3.11 标准库（零第三方依赖） | 整数最小费用流（单阶段）+ 精确 MILP（多阶段联合）+ HTTP API，端口 8000，`GET /healthz` 健康检查 |
| `web` | nginx + 原生静态页 | 录入草稿与缓升阶段、发起配平/联合求解、展示逐管流量与节点收支；反代 `/api/` 到 api，`GET /healthz` 健康检查 |
| `verify` | 一次性容器 | 单元测试 → 字节码构建核查 → API 业务冒烟（含缓升计划跨阶段可行/调节超限不可行/旧接口兼容）→ Web/API 联调冒烟，跑完即退出，**退出码即验收结论** |

## 快速开始

```bash
# 宿主机端口可用环境变量配置（默认 web 8080 / api 8000）
WEB_PORT=8081 API_PORT=9000 docker compose up --build

# 浏览器打开 http://localhost:8081
```

只跑一次性验收（测试 + 构建 + 冒烟），并用 verify 的退出码报告结果：

```bash
docker compose build
docker compose up \
  --abort-on-container-exit \
  --exit-code-from verify verify
echo "验收退出码：$?"   # 0 通过，非 0 失败
```

> `--exit-code-from verify` 使整条 compose 命令返回 verify 容器的退出码；
> verify 依赖 api、web 健康检查通过后才开始冒烟，结束后自行退出，
> api/web 仍可按需要常驻（`docker compose up`）或由调用方停止。

## 端口配置

`docker-compose.yml` 读取宿主机环境变量，均有默认值：

| 变量 | 默认 | 含义 |
|---|---|---|
| `WEB_PORT` | `8080` | Web 页面宿主机端口 |
| `API_PORT` | `8000` | API 宿主机端口（一般只需经 Web 反代访问） |

可复制 `.env.example` 为 `.env` 后调整（`docker compose` 自动读取）。

## HTTP API

### `POST /api/balance`

请求体：

```json
{
  "source": {"id": "S"},
  "source_total": 10,
  "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}],
  "nodes": [{"id": "N"}],
  "pipes": [
    {"id": "p1", "from": "S", "to": "N", "min": 0, "max": 10, "preferred": 5},
    {"id": "p2", "from": "N", "to": "A", "min": 0, "max": 10, "preferred": 3},
    {"id": "p3", "from": "N", "to": "B", "min": 0, "max": 10, "preferred": 7},
    {"id": "p4", "from": "S", "to": "A", "min": 0, "max": 0,  "preferred": 0}
  ]
}
```

- 数量/连接方向/范围（`min ≤ preferred ≤ max`、非负整数）等校验失败返回 `400 {"error": ...}`。
- 校验通过返回 `200`，业务可行性由 `feasible` 表达：
  - 可行：`objective`（绝对偏差和）、`tie_sequence`（决胜流量序列）、
    `flows[]`（逐管流量与偏差）、`balances`（水源/节点/分区收支，守恒差额均为 0）；
  - 不可行：`infeasibility` 含总量与需求合计、已成立/缺口流量、
    `deficits`（进水不足点）、`surpluses`（来水积压点）与中文 `reasons`。

### `GET /healthz`

返回 `200 {"status":"ok","service":"balance-api"}`。

### `POST /api/plan`（缓升计划，多阶段联合配平）

请求体在 `/api/balance` 草稿基础上增加：

- 每条管路一个非负整数 `max_adjust`：相邻阶段允许的最大流量变化量；
- `stages[]`：2~4 个阶段，按回湿顺序排列，每个阶段给出
  `source_total` 与全部草稿分区的 `zones[].demand`（分区集合必须与草稿一致）。

```json
{
  "source": {"id": "S"},
  "source_total": 10,
  "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}],
  "nodes": [{"id": "N"}],
  "pipes": [
    {"id": "p1", "from": "S", "to": "N", "min": 0, "max": 10, "preferred": 5, "max_adjust": 2},
    {"id": "p2", "from": "N", "to": "A", "min": 0, "max": 10, "preferred": 3, "max_adjust": 2},
    {"id": "p3", "from": "N", "to": "B", "min": 0, "max": 10, "preferred": 7, "max_adjust": 2},
    {"id": "p4", "from": "S", "to": "A", "min": 0, "max": 0,  "preferred": 0, "max_adjust": 2}
  ],
  "stages": [
    {"source_total": 10, "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}]},
    {"source_total": 10, "zones": [{"id": "A", "demand": 8}, {"id": "B", "demand": 2}]}
  ]
}
```

- 阶段数量、分区一致性、非负整数调节量等校验失败返回 `400 {"error": ...}`。
- 校验通过返回 `200`，业务可行性由 `feasible` 表达：
  - 可行：`objective`（全阶段绝对偏差总和）、`tie_sequence`（按阶段×管序展平的决胜序列）、
    `stages[]`（逐阶段流量与守恒明细，第 2 阶段起每行含 `change`/`change_limit`/`within_limit`）、
    `adjustments[]`（每个阶段衔接的逐管相邻调整量与是否越限）；
  - 不可行：`infeasibility` 含 `failing_stage`（**最早**无法与前序阶段同时满足调节约束的阶段，0 基）、
    `kind`（`adjustment` / `stage_infeasible`）、`gap_total`（越限缺口合计）、
    `blocked_pipes`（受限管路：前序末段锚定流量、本阶段所需流量、所需变化、允许调节、缺口）、
    `relaxed_stage_flows`（放松调节后该阶段最小越限的参考配水）、
    `deficits`/`surpluses`（阶段自身不守恒时的逐点诊断）与中文 `reasons`。
- `POST /api/balance` 的行为与响应契约保持不变：携带 `stages`/`max_adjust`
  的请求走旧接口时这些字段被忽略，仍按原单阶段草稿求解。

## 算法（app/）

- `mincost.py`：连续最短路增广（SPFA/Bellman-Ford）的整数最小费用流（单阶段 `/api/balance`）。
- `balance.py`：以优选量为初始预流，再用超源/超汇调整（同既有实现）。
- `milp.py`：零第三方依赖的小型**整数线性规划**——有界变量两阶段单纯形
  （Fraction 精确运算、稀疏表、Bland 防循环）+ 分支定界。
  跨阶段约束使每个流量变量进入 4 个约束行（本层守恒 + 相邻阶段调节），
  约束矩阵不再是网络矩阵，故不能套用最小费用流。
- `multistage.py`：把全部阶段联合建成一个 MILP：
  - 流量变量 f[k][i] ∈ [min,max]；每阶段水源/节点/分区等式守恒；
  - 相邻阶段以 d[k][i] ≥ |f[k]-f[k-1]|、0 ≤ d ≤ max_adjust 编码调节上限；
  - 偏差 epigraph t[k][i] ≥ |f[k][i]-preferred| 承载主目标；
  - 目标沿用分层大权：每偏离优选量 1 单位权重 P，
    展平序列第 k·m+i 位的字典序权重 q[k][i]，
    最小费用 ⇔ 先最小化总绝对偏差、再取展平序列字典序最小。
  - 不可行时逐阶段前缀联合试解定位最早失败点；再以联合最优末段流量为锚、
    放松调节约束最小化越限总量，给出缺口、受限管路与参考配水。
- 测试在随机小网络上对**全量整数解暴力枚举**逐例对照
  可行性、最优目标值、字典序决胜序列与最早失败阶段
  （`tests/test_balance.py` 400 例、`tests/test_multistage.py` 另含 MILP 对照）。

## 本地开发（无需 Docker）

```bash
python3 -m app.server                 # 起 API：http://localhost:8000
python3 -m unittest discover -s tests # 跑测试
BASE_URL=http://127.0.0.1:8000 python3 smoke/smoke.py
```

## 目录

```
app/                 API 与求解器（标准库）
  mincost.py         整数最小费用流（单阶段）
  balance.py         单阶段草稿校验与求解
  milp.py            精确有界单纯形 + 分支定界的小型 MILP
  multistage.py      缓升计划校验、跨阶段联合建模、最早失败诊断
web/index.html       前端单页（草稿/缓升阶段录入、配平/联合求解、结果展示）
nginx/default.conf   静态托管 + /api/ 反代 + Web 健康检查
tests/               unittest 单元/随机对照测试
smoke/               API 冒烟、Web 联调、健康等待、verify 入口
Dockerfile           多阶段镜像：api / web / verify 三个 target（均含 HEALTHCHECK）
docker-compose.yml   api / web / verify 三服务
```
