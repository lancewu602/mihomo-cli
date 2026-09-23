# 构建 macOS / Linux 二进制。目标机器不需要装 Python。
#
#   make deps           建 .venv 并装 PyInstaller + ruff（一次性；也可以直接用 PATH 上的）
#   make lint           ruff check（只检查）
#   make fmt            ruff format + ruff check --fix（会重排代码，见 docs/README.md 的约定）
#   make test           跑单元测试（stdlib unittest，零依赖、不装 pytest）
#   make build          目录版：dist/dir/mihomo-cli/mihomo-cli  ← 默认，启动快
#   make build-onefile  单文件：dist/mihomo-cli（就一个文件，但每次启动都要解包）
#   make check          跑一遍产物（--help；本机装了 mihomo 时顺带 status）
#                       make check BIN=dist/mihomo-cli 可以单独验单文件那份
#   make install        装到 $(PREFIX)/bin（默认 /usr/local；目录版会整目录装 + 放个 exec 包装）
#   make uninstall      从 $(PREFIX) 移除
#   make clean          删掉 build/ 与 dist/
#
# 为什么默认是目录版：单文件每次启动都要把 ~8 MB 解包成一个新的临时可执行文件，
# 在会逐个校验可执行文件的环境里（本机 macOS 26 就是）实测 --help 要 6 秒；
# 目录版把这份代价只付一次，之后与源码版一样快。实测数字见 docs/packaging.md。
# 两个平台命令完全一样；平台差异（架构、glibc、签名）也在那篇里。

PREFIX ?= /usr/local
LIBEXEC = $(PREFIX)/libexec/mihomo-cli
VENV   ?= .venv
DIST   ?= dist
BUILD  ?= build

ONEFILE = $(DIST)/mihomo-cli
ONEDIR  = $(DIST)/dir/mihomo-cli/mihomo-cli
SOURCES = $(wildcard src/mihomo_cli/*.py)

BIN ?= $(ONEDIR)

# .venv 里装了就用它，否则用 PATH 上的
PYINSTALLER ?= $(if $(wildcard $(VENV)/bin/pyinstaller),$(VENV)/bin/pyinstaller,pyinstaller)
# 同理：ruff 优先 .venv，其次 PATH，都没有就用 uvx 临时跑（不装进项目）
RUFF ?= $(shell if [ -x $(VENV)/bin/ruff ]; then echo $(VENV)/bin/ruff; \
                 elif command -v ruff >/dev/null 2>&1; then echo ruff; \
                 else echo "uvx ruff"; fi)

.PHONY: build build-onefile deps lint fmt test check install uninstall clean

build: $(ONEDIR)
build-onefile: $(ONEFILE)

$(ONEFILE): mihomo-cli.spec packaging/entry.py $(SOURCES)
	$(PYINSTALLER) --clean --noconfirm --distpath $(DIST) --workpath $(BUILD) mihomo-cli.spec

$(ONEDIR): mihomo-cli.spec packaging/entry.py $(SOURCES)
	MIHOMO_CLI_ONEDIR=1 $(PYINSTALLER) --clean --noconfirm \
		--distpath $(DIST)/dir --workpath $(BUILD)/dir mihomo-cli.spec

deps:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip 'pyinstaller>=6.0' 'ruff>=0.16'

lint:
	$(RUFF) check .

# 测试是 stdlib unittest：运行时零依赖这个底线连开发期也不想破，所以不引 pytest。
# src 布局下包不在仓库根，得靠 PYTHONPATH 指路（也因此测的就是源码，不是装好的那份）。
test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

fmt:
	$(RUFF) format .
	$(RUFF) check --fix .

check:
	@test -x $(BIN) || { echo "没有 $(BIN)，先 make build（或 make build-onefile）"; exit 1; }
	$(BIN) --help > /dev/null
	@file $(BIN) | sed 's/^/  /'
	@ls -lh $(BIN) | awk '{print "  体积: " $$5}'
	@$(BIN) status > /dev/null 2>&1 && echo "  status 冒烟：通过" \
		|| echo "  status 冒烟：跳过（本机没装 mihomo，或内核没在跑）"

# 装哪个由 BIN 决定（默认目录版）。目录版**必须整目录装**：那个 2 MB 的可执行文件
# 要和同目录的 _internal/ 一起才跑得起来，单独拷出去会报找不到 Python 运行时。
# 所以目录版装到 $(PREFIX)/libexec/mihomo-cli/，bin 里放一个两行的 exec 包装脚本。
install:
	@test -x $(BIN) || { echo "没有 $(BIN)，先 make build（或 make build-onefile）"; exit 1; }
	@mkdir -p $(PREFIX)/bin
	@if [ "$(BIN)" = "$(ONEDIR)" ]; then \
		rm -rf $(LIBEXEC) && mkdir -p $(LIBEXEC) && \
		cp -R $(dir $(ONEDIR)). $(LIBEXEC)/ && \
		printf '#!/bin/sh\nexec %s/mihomo-cli "$$@"\n' "$(LIBEXEC)" > $(PREFIX)/bin/mihomo-cli && \
		chmod 0755 $(PREFIX)/bin/mihomo-cli && \
		echo "已装到 $(PREFIX)/bin/mihomo-cli（实体在 $(LIBEXEC)/）"; \
	else \
		install -m 0755 $(BIN) $(PREFIX)/bin/mihomo-cli && \
		echo "已装到 $(PREFIX)/bin/mihomo-cli"; \
	fi

uninstall:
	rm -f $(PREFIX)/bin/mihomo-cli
	rm -rf $(LIBEXEC)
	@echo "已从 $(PREFIX) 移除"

clean:
	rm -rf $(BUILD) $(DIST)
	@echo "已清掉 $(BUILD)/ $(DIST)/"
