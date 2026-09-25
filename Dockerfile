# 多阶段镜像：api / web / verify 三个 target，由 docker-compose.yml 分别选用。

# ---------- 业务 API：纯 Python 标准库，无 pip 依赖 ----------
FROM python:3.11-slim AS api

WORKDIR /srv
COPY app/ ./app/
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /srv
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"
CMD ["python3", "-m", "app.server"]

# ---------- Web：nginx 静态页 + /api/ 反代 ----------
FROM nginx:1.27-alpine AS web

RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY web/ /usr/share/nginx/html/
EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD wget -qO- http://127.0.0.1/healthz >/dev/null || exit 1
CMD ["nginx", "-g", "daemon off;"]

# ---------- verify：一次性验收（测试 + 构建核查 + 冒烟） ----------
FROM python:3.11-slim AS verify

WORKDIR /work
COPY app/ ./app/
COPY tests/ ./tests/
COPY smoke/ ./smoke/
RUN chmod +x smoke/verify-entrypoint.sh \
    && python3 -m compileall -q app smoke
# 不暴露端口、不常驻；入口脚本退出码即验收结论
CMD ["sh", "smoke/verify-entrypoint.sh"]
