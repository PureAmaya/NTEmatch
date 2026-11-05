# 两个 Go 侧车都要编译成静态二进制：
#   ntecalc   —— 积分制轮换赛程搜索（见 app/gocalc.py）
#   nterender —— 出图：比赛卡片 / 结果卡片 / 帮助图 / 分享图（见 app/render.py）
# 单开一个构建阶段，最终镜像里只有二进制，运行时不带 Go 工具链。
FROM golang:1.23-alpine AS gobuild

WORKDIR /src
COPY go/ ./
# CGO 关掉才能得到完全静态的二进制（slim 镜像里没有 glibc 的编译期依赖）
# go mod download 单独一层：依赖变了才重拉，改代码不会每次都走网络
RUN go mod download
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X main.buildTag=docker" \
        -o /out/ntecalc ./cmd/ntecalc \
    && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X main.buildTag=docker" \
        -o /out/nterender ./cmd/nterender

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
# 出图与计算都在 Go 侧车里，Python 依赖里不再有图像库。
COPY pyproject.toml uv.lock README.md ./
# 依赖按 uv.lock 装；**再装一份中文字体**：图是服务器上画出来的，而 slim 镜像里
# 一个 CJK 字体都没有——不装的话图里的中文全是方框（见 go/internal/render/fonts.go）。
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY static ./static
# 单一入口：开发跑的就是它，镜像里也用同一条命令
COPY main.py ./main.py
# 侧车装进 PATH：Python 侧找不到 go/bin/<name> 时会退到 which(<name>)。
# * 少了 ntecalc：只能跑纯 Python 实现，功能一样，只是赛程生成慢十几倍；
# * 少了 nterender：卡片推送退回纯文本（信息不缺，只是没有图）。
COPY --from=gobuild /out/ntecalc /usr/local/bin/ntecalc
COPY --from=gobuild /out/nterender /usr/local/bin/nterender

# 数据（config / data / backups）统一放 /data：挂一个卷就够，容器重建不丢赛事数据。
# 见 app/store.py 的 DATA_ROOT（默认在仓库里，这里被挪到卷上）。
ENV NTE_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 10961

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:10961/api/health', timeout=2)"

# 用 `uv run main.py` 而不是 `uvicorn app.main:app`：main.py 会把仓库根（/app）
# 放进 sys.path，于是 STATIC_DIR 能正确指向 /app/static。监听地址与端口走
# NTE_HOST / NTE_PORT（默认 0.0.0.0:10961，见 app/__init__.py 的 DEFAULT_PORT）。
CMD ["uv", "run", "--no-sync", "main.py"]
