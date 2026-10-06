# 单阶段就够了：这个项目没有构建步骤——后端是纯 Python，前端是原生 ESM + CSS，
# 没有打包器也就没有构建产物要拷来拷去。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖严格按 uv.lock 装（--frozen 不更新锁文件、--no-dev 不装测试依赖、
# --no-install-project 只装依赖不装本项目——我们直接跑源码）。
# 这样 pyproject 里的依赖清单不会在这里被抄第二份。
#
# 图片推送（「比赛信息」发卡片图）与机器人帮助图都靠 Pillow 渲染，它是**正式依赖**，
# 所以这里不需要任何 build arg（早期版本的 NTE_CARDS 开关已经删掉了，
# 见 README「图片推送：比赛卡片」）。
COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY static ./static

# 数据（config / data / backups）统一放 /data：挂一个卷就够，容器重建不丢赛事数据。
# 见 app/store.py 的 DATA_ROOT（默认在仓库里，这里被挪到卷上）。
ENV NTE_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"

# 用 `python -m app` 而不是 `uvicorn app.main:app`：前者会把当前工作目录（/app）
# 放进 sys.path，于是 STATIC_DIR 能正确指向 /app/static。监听地址与端口走
# NTE_HOST / NTE_PORT（默认 0.0.0.0:8000，见 app/cli.py）。
CMD ["uv", "run", "--no-sync", "python", "-m", "app"]
