---
name: setup-environment
description: 一键环境安装 - 建 venv + 装 Python 依赖 + 下载 Chromium + smoke test（含 CN 镜像 fallback），新用户只跑一条命令
type: setup
version: 1.0
---

# 环境安装 Skill (`scripts/setup.sh`)

给新用户用的一键环境安装,替代手动粘贴 4 行 shell 指令。

## 前置条件

- Linux / macOS(Windows 未测)
- **Python 3.10+** 已装,`python3` 在 `$PATH` 上
- `git`
- ~800 MB 空闲磁盘(venv + Chromium)

## 一键执行

从仓库根目录跑:

```bash
./scripts/setup.sh
```

就这一句。脚本会依次做 5 件事,失败会明确报错退出(`set -e`):

| 步骤 | 操作 | 幂等 |
|------|------|------|
| 1. Python 版本检查 | `python3` 存在 + `>= 3.10` | ✅ |
| 2. 创建 venv | `python3 -m venv .venv`,已存在就复用 | ✅ |
| 3. 安装 Python 依赖 | `.venv/bin/pip install -r requirements.txt`(pip 会 skip 已装) | ✅ |
| 4. 下载 Chromium | `.venv/bin/playwright install chromium`,已装就秒回 | ✅ |
| 5. Smoke test | `sync_playwright()` + `chromium.launch()` 验证端到端 | ✅ |

脚本完全幂等 —— 重复跑只会快速跳过已完成的步骤,不会重装。

## CN 镜像 fallback

Step 4 的 Chromium 下载先试官方 host(`playwright.download.prss.microsoft.com`)。国内网络经常走不通,脚本检测到非零退出后**自动 fallback** 到阿里 npmmirror:

```bash
PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright \
    .venv/bin/playwright install chromium --force
```

`--force` 保证第一次失败留下的 partial file 被清掉,镜像重试从零开始。

## 输出

成功时脚本打印:

```
==> setup complete!
   Next step:
     .venv/bin/python3 scripts/run_workflow.py \
       --trace /path/to/your.perfetto-trace \
       --output-dir /path/to/output
```

到这一步环境就绪,接着跑 `run_workflow.py` 即可。

## 失败排查

| 错误 | 原因 / 处理 |
|------|------|
| `ERROR: python3 not found on PATH` | 装 Python 3.10+(`apt install python3 python3-venv` / `brew install python`) |
| `ERROR: Python 3.9 found but >= 3.10 is required` | 升级 Python 或用 pyenv/conda 切换 |
| `python3 -m venv .venv` 报错 `ensurepip is not available` | Ubuntu 需额外装 `python3-venv`: `sudo apt install python3-venv` |
| pip install 慢 / 卡住 | 用 pip 镜像: `.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt` 后重跑 `./scripts/setup.sh` |
| Chromium 下载 fallback 也失败(完全离线) | 见 `docs/quickstart.md` Troubleshooting 里的 air-gap escape hatch(从有网机器 rsync `~/.cache/ms-playwright/`) |
| Smoke test 崩 `Executable doesn't exist` | Chromium 装到了别处。检查是否设了 `PLAYWRIGHT_BROWSERS_PATH` 指向错误路径 |

## 为什么是 skill 而不是隐式步骤

把"安装"显式提升为一个 skill + 一个脚本的好处:
1. **单入口**: 新用户粘贴一条命令而不是 4 条,错误率显著下降
2. **幂等重跑**: 环境坏了或升级时重跑就行,不用担心"哪一步之前做过"
3. **CN 网络自动适配**: fallback 逻辑固化到脚本,文档不用教用户怎么手动 export 环境变量
4. **Smoke test 作为合格判据**: 脚本退出 0 = 环境真的能用,而不是"四条命令都没报错但 `chromium.launch()` 一跑就炸"

## 和其他 skill 的关系

```
skills/setup.md  ←  (一次性) 环境准备
  ↓
skills/workflow.md  ←  (每次跑) 三 phase 工作流
  ├─ skills/analyze-jank.md
  ├─ skills/capture-screenshots.md
  └─ skills/generate-report.md
```

`setup-environment` skill 是一次性操作,跑完之后所有后续 workflow 都复用同一个 `.venv` + Chromium 缓存。
