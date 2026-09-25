# 纸本修复室 · 雾化管路配平系统

回湿脆化古画前，审核雾化管路能否把规定水量稳定送到每个分区：
**一处水源、2~4 个分区、0~4 个分流节点、4~10 条有向管路**，求整数流量方案。

单阶段配平（`/api/balance`）：

- 水源流出**恰等于**水源总量；
- 每个分流节点流入 = 流出（不会凭空增减流量）；
- 每个分区流入**恰等于**精确需求（不会因某条支路有余量就让下游缺水）；
- 每条管路流量为整数且在 `[最小量, 最大量]` 内；
- 可行方案先最小化**相对优选量的绝对偏差和**，再按**管路录入顺序的流量序列字典序**稳定决胜；
- 不可行时给出逐点收支诊断（缺水点 / 积压点、缺口水量、可读原因）。

缓升计划（`/api/plan`，分阶段回湿）：

- 在现有管路草稿旁追加 **2~4 个按顺序的湿润阶段**，每阶段填写水源总量与各分区精确需求，
  并为每条既有管路填写相邻阶段允许的整数**最大调节量**；
- 服务端从完整草稿**联合**求出全部阶段的整数流量（**不逐阶段独立配平后再拼接**）：
  任一阶段仍满足水源/分流节点/分区守恒与原有上下限，
  且同一管路相邻阶段的流量差绝对值不越过调节量；
- 所有可行计划先取相对各管路既有优选量的**总绝对偏差**最小，
  再按**阶段顺序 × 每阶段管路录入顺序**展开的流量序列字典序稳定决胜；
- 页面展示逐阶段流量、相邻调整量与守恒明细；修改草稿或阶段输入后旧计划立即失效不再显示；
- 无可行计划时，按**最早**无法与前序阶段同时满足约束的阶段返回诊断：
  该阶段守恒缺口（逐点缺水/积压），或相邻过渡的调节缺口与受限管路。

## 架构

| 组件 | 技术 | 说明 |
|---|---|---|
| `api` | Python 3.11 标准库（零第三方依赖） | 整数最小费用流（单阶段）+ 精确字典序 LP（多阶段）+ HTTP API，端口 8000，`GET /healthz` 健康检查 |
| `web` | nginx + 原生静态页 | 录入草稿、发起配平、录入缓升计划、展示逐管流量/逐阶段流量/相邻调整量与节点收支；反代 `/api/` 到 api，`GET /healthz` 健康检查 |
| `verify` | 一次性容器 | 单元测试 → 字节码构建核查 → API 业务冒烟（含旧接口兼容、跨阶段可行、调节超限不可行）→ Web/API 联调冒烟，跑完即退出，**退出码即验收结论** |

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

### `POST /api/plan`

请求体（分区只列 id，需求按阶段给出；每条管路带 `max_adjust`）：

```json
{
  "source": {"id": "S"},
  "nodes": [{"id": "N"}],
  "zones": [{"id": "A"}, {"id": "B"}],
  "pipes": [
    {"id": "p1", "from": "S", "to": "N", "min": 0, "max": 10, "preferred": 5, "max_adjust": 3},
    {"id": "p2", "from": "N", "to": "A", "min": 0, "max": 10, "preferred": 3, "max_adjust": 2},
    {"id": "p3", "from": "N", "to": "B", "min": 0, "max": 10, "preferred": 7, "max_adjust": 2},
    {"id": "p4", "from": "S", "to": "A", "min": 0, "max": 0,  "preferred": 0, "max_adjust": 0}
  ],
  "stages": [
    {"source_total": 4,  "zones": [{"id": "A", "demand": 2}, {"id": "B", "demand": 2}]},
    {"source_total": 7,  "zones": [{"id": "A", "demand": 4}, {"id": "B", "demand": 3}]},
    {"source_total": 10, "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}]}
  ]
}
```

- 校验（2~4 个阶段、每阶段覆盖全部草稿分区、`max_adjust` 为非负整数、
  管路规则同 `/api/balance`）失败返回 `400 {"error": ...}`。
- 校验通过返回 `200`：
  - 可行：`objective`（全部阶段的总绝对偏差）、`tie_sequence`
    （按阶段顺序 × 每阶段管路录入顺序展开的决胜序列）、
    `stages[]`（逐阶段 `flows` 与 `balances` 守恒明细）、
    `adjustments[]`（每段相邻过渡逐管的 `previous_flow`/`next_flow`/
    `change`/`abs_change`/`max_adjust`/`within_limit`）；
  - 不可行：`infeasibility` 含 `stage`（最早无法与前序阶段同时满足约束的阶段，
    0 起）与 `kind`：
    - `"conservation"`：该阶段守恒本身不可行，`conservation` 给出
      `gap`（守恒缺口合计）、`deficits`/`surpluses`（逐点缺水/积压）；
    - `"adjustment"`：该阶段单独可行但无法从前序阶段在调节量内过渡，
      `adjustment` 给出 `gap`（调节总缺口）与 `limited_pipes`
      （每条受限管路的 `needed_adjust`/`max_adjust`/`gap`）。

### `GET /healthz`

返回 `200 {"status":"ok","service":"balance-api"}`。

## 算法（app/）

- `mincost.py`：连续最短路增广（SPFA/Bellman-Ford）的整数最小费用流。
  整数容量按整数瓶颈增广，流量必为整数。
- `balance.py`（单阶段 `/api/balance`，行为保持不变）：以**优选量为初始预流**，
  再用超源/超汇调整：
  - 增大边费用 `P+q`、减小边费用 `P-q`，全部为正，无负费用环；
  - 每偏离优选量 1 单位，费用的主目标分量恰为 `P`；
    净变化携带录入顺序权重 `q`，故最小费用
    ⇔ 先最小化绝对偏差和、再最小化录入顺序字典序；
  - 超源/超汇两侧必须同时饱和才可行（总量 ≠ 需求合计时两侧总量不等，必然不可行）。
- `plan.py` + `lpsolve.py`（多阶段 `/api/plan`）：
  - 变量：管路 i 阶段 t 的流量 `y = min + z1 + z2`，
    `z1 ∈ [0, pref-min]`、`z2 ∈ [0, max-pref]`（`|y-pref|` 的线性化，
    最优解自动先填满 z1）；
  - 约束：每阶段守恒等式、相邻阶段差分 `±(y_t - y_{t-1}) ≤ max_adjust`、
    z 变量上界——该约束矩阵为**全单模（TU）**，故 LP 顶点解必为整数；
  - `lpsolve.py`：两阶段原始单纯形 + Bland 规则，目标为**字典序多目标行**
    （① 总绝对偏差；② 展开流量序列逐位），全程精确整数运算
    （TU 矩阵主元恒为 ±1；非 ±1 时自动切换 Fraction 兜底）；
  - 不可行诊断：前缀可行性单调，先定位最早不可行阶段 t*；
    再对前缀 0..t* 求最小违约 LP（阶段 t* 守恒软约束优先、
    末段过渡调节溢出其次），区分守恒缺口与调节缺口/受限管路。
- 测试在随机小网络上对**全量整数解暴力枚举**逐例对照
  可行性、最优目标值与字典序决胜序列
  （`tests/test_balance.py` 单阶段 400 例、`tests/test_plan.py` 多阶段 150 例）。

## 本地开发（无需 Docker）

```bash
python3 -m app.server                 # 起 API：http://localhost:8000
python3 -m unittest discover -s tests # 跑测试
BASE_URL=http://127.0.0.1:8000 python3 smoke/smoke.py
```

## 目录

```
app/                 API 与求解器（标准库）
  mincost.py         整数最小费用流（单阶段配平）
  balance.py         单阶段校验 + 建模 + 诊断（/api/balance，行为不变）
  lpsolve.py         精确字典序两阶段单纯形 LP（多阶段联合求解）
  plan.py            缓升计划校验 + LP 建模 + 不可行诊断（/api/plan）
  server.py          HTTP 路由（/healthz、/api/balance、/api/plan）
web/index.html       前端单页（草稿/配平/缓升计划录入与结果展示）
nginx/default.conf   静态托管 + /api/ 反代 + Web 健康检查
tests/               unittest 单元/随机对照测试（test_balance.py、test_plan.py）
smoke/               API 冒烟、Web 联调、健康等待、verify 入口
Dockerfile           多阶段镜像：api / web / verify 三个 target（均含 HEALTHCHECK）
docker-compose.yml   api / web / verify 三服务
```
