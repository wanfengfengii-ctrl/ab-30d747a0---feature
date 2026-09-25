#!/bin/sh
# 一次性验收：代码测试 -> 构建核查 -> API 冒烟 -> Web/API 联调冒烟。
# 任一环节失败立即以非零退出码报告。
set -eu

echo "== [1/4] 单元测试（含 400 例随机网络暴力枚举对照） =="
python3 -m unittest discover -s tests -v

echo "== [2/4] 构建核查：全量字节码编译 =="
python3 -m compileall -q app smoke

echo "== [3/4] 等待 API 健康并执行业务冒烟 =="
python3 /work/smoke/wait_for.py http://api:8000/healthz
BASE_URL=http://api:8000 python3 /work/smoke/smoke.py

echo "== [4/4] 等待 Web 健康并执行联调冒烟 =="
python3 /work/smoke/wait_for.py http://web/healthz
WEB_URL=http://web python3 /work/smoke/web_check.py

echo "== verify 全部通过：测试 / 构建 / API 冒烟 / Web 联调 =="
