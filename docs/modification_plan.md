# 基于 Mambo 参考的修改方案

**来源**：上一轮会话中用户给我看了 Mambo 截图方案的 21 张源码照片（见 `mambo_reference.md` 完整转录）。token 用光前我提了修改方向，没落地。本文是**基于当前 session 重新读完全部 Mambo 代码后的方案**——比上一轮记录更准确。

**上一轮已经写进 `scripts/capture_screenshots.py` 但没提交的改动**（独立于本方案，是"抢救当前 RPC 路线"的补丁）：
- 从 `/status` 抓 `trace_processor` 版本号 → `https://ui.perfetto.dev/{version}/` 避开 mismatch 对话框
- `launch_persistent_context` + `--disable-web-security` 等 chromium flags 让 HTTPS UI 能 fetch 本地 HTTP RPC
- RPC 对话框点击三级 fallback

这是"把现有架构修好"的方向。下面是"借鉴 Mambo 重构"的方向，两条路互补、不冲突。

---

## Mambo 架构的核心洞察

读完全部 11 个文件后，Mambo 的方案跟我们在三个层面根本不同：

### 1. Trace 加载方式：**URL deep-link + 本地 file_server**

Mambo 不用 trace_processor RPC，也不用 file_chooser。它**自己起一个 HTTP file_server**暴露本地 trace 目录，然后拼一个 Perfetto UI 的 deep link：

```
https://perfetto.rnd.hihonor.com/#!/?url=http://127.0.0.1:{port}/{rel_path}
  &referrer=&openTraceInUIStart={begin_ns}&clockEnd={end_ns}
```

这个方式的优势：
- **避开 HTTPS→HTTP RPC 跨域问题**——Perfetto UI 主动 fetch 一个 URL，属于同域 `fetch`，不受 `disable-web-security` 的折腾
- **时间范围锁在 URL 里**——打开就 zoom 到卡顿区间，不需要后续 `setVisibleWindow` JS 调用
- **无版本耦合**——deep link 格式跨 Perfetto UI 版本稳定，不像 JS API `window.app._activeTrace` 每版都可能变
- **不需要 trace_processor binary**——省掉 `/home/wq/workspace/test_render_traces/trace_processor` 这个依赖

代价：
- 要自己起 file_server（Python 一行 `http.server` 就能搞定）
- `fix bug in file_server.py` 这个 commit 说明 Mambo 也踩过 file_server 的坑

### 2. Trace 预切片：**SYSTRACE → `.cut` 子集**

Mambo 的 `screenshot_manager.run()` 在调 PerfettoScreenshot 之前，**先把 trace 切出卡顿区间那一段**：

```python
if trace_type == PerfettoFileType.SYSTRACE:
    trace_path_out = f"{...}_{str(ns_to_ms(begin_time))}_.cut"
    self.perfetto_file.systrace_file.write_sub_line_to_file(
        begin_time, end_time, trace_path_out)
    screenshot_config.trace_path = trace_path_out
```

对应 `trace_cutter.py`（没转录但文件名摆在那里）。

**价值**：64MB 的 trace 只要截 100ms 就够了，切完可能只有几 MB，加载从 ~20s 压到 ~2s。我们当前是把整个 trace 喂给 Perfetto UI 的。

### 3. 版本化 locator：**运行时检测 → 动态派发**

```python
version = get_version_v53() or get_version_v49()  # 试 XPath
# 解析 "v54.x" → 数字
if version_big_digital >= 53: return LocatorManagerV53(driver)
elif version_big_digital >= 49: return LocatorManagerV49(driver)
elif version_big_digital >= 48: return LocatorManagerV48(driver)
else: return LocatorManager(driver)
```

每次 Perfetto UI 升版只加一个新 LocatorManager 子类，不改旧的。我们当前的命令 ID 硬编码在 `capture_screenshots.py` 里，没法这么干。

---

## 落地方案（按优先级排序）

### P0 — 立刻能改（最小侵入，独立价值）

#### P0.1 切换到 "DOM 文本" ready 判定（替代 `window.app._activeTrace`）

当前 `scripts/capture_screenshots.py:161-164`：
```python
page.wait_for_function(
    "() => window.app && window.app._activeTrace && window.app._activeTrace.timeline",
    timeout=120000,
)
```

问题：`window.app._activeTrace` 是 Perfetto 内部实现，下一版升级可能就没了。

改成 Mambo v48 的 "Process" 文本等待（跨 v48/v49/v53 都稳）：
```python
page.wait_for_selector(
    "xpath=//div[contains(text(), 'Process ')]",
    timeout=120000,
    state="visible",
)
```

或者更稳的 v53 loading 完成信号（直接等进度条消失）：
```python
page.wait_for_selector(
    "xpath=//div[@class='pf-linear-progress pf-ui-main__loading' and @state='none']",
    timeout=120000,
)
```

#### P0.2 Trace 预切片（Python 版 trace_cutter）

在 Phase 2 之前加一步：从 `app_jank.json` 拿 top 5 帧的时间范围，用 `trace_processor` 命令行的 `-q` 或者更简单——用 Perfetto 的 Python API：

```python
# Before capture_screenshots, cut trace to [min(ts) - 500ms, max(ts+dur) + 500ms]
from tp_utils import cut_trace  # to be written
cut_path = cut_trace(trace, jank_window_begin, jank_window_end)
```

Linux 上 SYSTRACE 切片比较简单（文本行过滤），二进制 `.perfetto-trace` 切片需要调 `trace_processor` 或者干脆让 file_server 用 HTTP Range 请求只喂相关区段——**这条先缓，优先级让给 P0.1 和 P1**。

#### P0.3 截图裁剪（保留上 2/3）

```python
from PIL import Image
img = Image.open(screenshot_path)
img.crop((0, 0, img.width, img.height * 2 // 3)).save(screenshot_path)
```

切掉底部 Perfetto 状态栏/搜索面板残留。当前 viewport 2400px，裁后 1600px，更聚焦卡顿 slice。

### P1 — 结构升级（分 2~3 个 commit）

#### P1.1 改 URL deep-link 加载

这是 Mambo 架构最大价值的点。步骤：

1. 新增 `scripts/file_server.py`：
   ```python
   from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
   import threading

   def start_file_server(serve_dir, port=9002):
       handler = lambda *a, **kw: SimpleHTTPRequestHandler(
           *a, directory=str(serve_dir), **kw)
       server = ThreadingHTTPServer(('127.0.0.1', port), handler)
       thread = threading.Thread(target=server.serve_forever, daemon=True)
       thread.start()
       return server
   ```
   需要处理 CORS：Perfetto UI 跨域 fetch 会要求 `Access-Control-Allow-Origin: *`，handler 要覆盖 `end_headers` 加上这个头。

2. 在 `capture_screenshots.py` 把 `_start_trace_processor` 替换成 `_start_file_server`；把 `page.goto(ui_url)` 改成：
   ```python
   rel = trace.name  # trace is in serve_dir
   begin_ns = int(top_frames[0]['ts']) - 500_000_000
   end_ns = int(top_frames[-1]['ts'] + top_frames[-1]['dur']) + 500_000_000
   url = (f"https://ui.perfetto.dev/#!/?url="
          f"http://127.0.0.1:{FILE_SERVER_PORT}/{rel}"
          f"&referrer=&openTraceInUIStart={begin_ns}&clockEnd={end_ns}")
   page.goto(url)
   ```

3. 保留当前 RPC 实现作 fallback，`--load-mode {url,rpc,file}` 选择。

#### P1.2 版本化 locator 模块

新目录 `scripts/locators/`：
- `base.py` — `class LocatorBase` 包含所有选择器常量和命令 ID
- `v53.py` — `class LocatorV53(LocatorBase)` override 变动的部分
- `v54.py` — 当前我们用的版本，继承 v53 override
- `__init__.py::get_locator(version_str)` → 解析版本号返回对应类

主脚本所有直接写 `'dev.perfetto.PinTracksByRegex'` 的地方改成 `locator.CMD_PIN_TRACKS`。主脚本身上不留硬编码。

#### P1.3 拆分 capture_screenshots.py 为 operator/manager/config

对应 Mambo 的三层结构：
- `perfetto_operator.py` — 低层 UI 操作（`pin_group` / `zoom_to` / `take_screenshot` / `hide_nonpinned`），每个函数都带 try/except 和日志
- `screenshot_manager.py` — 外层循环（top_frames × pin_groups）
- `screenshot_config.py` — pin_groups 定义、viewport 尺寸、容错策略

当前 `capture_screenshots.py` 800+ 行都塞在一个 `main()` 里，拆完每个文件 200 行以内、职责清晰，而且 operator 可以被别的 phase 复用。

### P2 — 错误保护 / 容错降级（参考"增加报错保护"系列 commit）

1. **所有 `_cmd` 调用统一装饰器**：
   ```python
   def safe_op(retries=1, on_fail='warn'):
       def dec(fn):
           def wrapper(*a, **kw):
               for attempt in range(retries + 1):
                   try: return fn(*a, **kw)
                   except Exception as e:
                       if attempt == retries:
                           log(f"[{fn.__name__}] failed after {retries+1}: {e}")
                           return None if on_fail == 'warn' else raise
           return wrapper
       return dec
   ```

2. **单帧失败不影响其他帧**：当前 `capture_screenshots.py:234-264` 的内层循环如果 `_zoom_to` 或 `_take_screenshot` 抛异常会直接挂掉整个 run。加 per-frame try/except + `results[i]["success"] = False` + 占位图。

3. **"未找到卡顿点"降级**：对应 Mambo `screenshot_config.py` 从 `tsa_delimitation_list` fallback 到 `scene.key_threads`。我们对应的逻辑是 `thread_map.json` 里 pin_patterns 抓不到时，降级到 `target['process_name']` 整进程 expand。

### P3 — 可选：Edge + Selenium 路线

如果 P1.1 的 file_server 路线走不通（比如 Perfetto UI 对 URL 加载的 cookie/CORS 卡得死），可以考虑完全按 Mambo 重写一份 Selenium + Edge 的版本。不建议——Playwright 比 Selenium 稳太多，放弃它是退步。仅作为保底。

---

## 第一刀应该切哪里？

建议：**P0.1（DOM ready 判定）→ P1.1（URL deep-link 加载）→ P1.2（版本化 locator）**

理由：
- P0.1 独立、几行、立刻验证当前抓图是否更稳
- P1.1 一旦走通，HTTPS/RPC/version-mismatch 三个老大难一起消失
- P1.2 是为下一次 Perfetto 升版买保险（v55 一出来当前脚本必挂）
- 上一轮没提交的 RPC 抢救补丁（version matching + disable-web-security）可以先 commit 存档，然后 P1.1 走通了再删

---

## 需用户确认的问题

1. **内网镜像** `https://perfetto.rnd.hihonor.com/` 从 Linux 开发机能否直接访问？如果可以，deep link 直接用它，跨域/版本问题自然消失。如果不能访问，就用 `https://ui.perfetto.dev/` + 本地 file_server（CORS 头需要处理）
2. **是否能拿到 Mambo 完整源码** — 照片转录有盲区（trace_cutter.py 完全没拍到；perfetto_operator.py 最后几行可能不全）。如果能 clone Mambo，直接移植远比重写快
3. **P1.1 / P1.2 / P1.3 的合并顺序** — 倾向一个一个 commit（三个独立改动，易回滚），还是一次性重构（少 CI 跑动）？
4. **Edge vs Playwright** — 有没有历史原因必须用 Edge？（Mambo 用 Edge 可能是因为 Windows 环境 / 公司统一要求；我们 Linux + Chromium 无此约束）
