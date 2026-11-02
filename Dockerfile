# 后端的计算侧车（积分制轮换赛程搜索，见 go/）要编译成静态二进制。
# 单开一个构建阶段，最终镜像里只有一个二进制，运行时不带 Go 工具链。
FROM golang:1.23-alpine AS ntecalc

WORKDIR /src
COPY go/ ./
# CGO 关掉才能得到完全静态的二进制（slim 镜像里没有 glibc 的编译期依赖）
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X main.buildTag=docker" \
    -o /out/ntecalc ./cmd/ntecalc

# 主镜像仍是纯 Python：前端是原生 ESM + CSS，没有打包器也就没有构建产物。
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
# 依赖按 uv.lock 装；**再装一份中文字体**：比赛卡片图与机器人帮助图是服务器上画出来的，
# 而 slim 镜像里一个 CJK 字体都没有——不装的话图里的中文全是方框（见 app/fonts.py）。
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY static ./static
# 计算侧车装进 PATH：app/gocalc.py 找不到 go/bin/ntecalc 时会退到 which(ntecalc)。
# 少这一步就只能跑纯 Python 实现——功能一样，只是赛程生成慢十几倍。
COPY --from=ntecalc /out/ntecalc /usr/local/bin/ntecalc

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
