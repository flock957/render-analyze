# Mambo Screenshot 参考实现（完整转录）

来源：Performance_Team / Mambo / Repository
路径：`mambo/base/screenshot/`
分支：`master`

参考图位置：`/home/wq/Pictures/20260408-14283*.jpg` ~ `20260408-14313*.jpg`（21 张，2026-04-08 拍摄）

以下代码是**从照片肉眼转录**，个别缩进/拼写可能有误，结构和语义可信。

---

## 仓库文件清单（`20260408-142839.jpg`）

| 文件 | Last commit message |
|------|---------------------|
| `__init__.py` | adjust new_start_activity_launch_scene.py |
| `locator_manager.py` | screenshot for v49 |
| `locator_manager_v48.py` | Add perfetto screenshot. |
| `locator_manager_v49.py` | perfetto screenshot adapt v53 |
| `locator_manager_v53.py` | 增加报错保护 |
| `perfetto_operator.py` | 增加报错保护 |
| `perfetto_screenshot.py` | fix bug in file_server.py |
| `screenshot_config.py` | 改善"未找到卡顿点" |
| `screenshot_manager.py` | 增加报错保护 |
| `selenium_demo.py` | Add perfetto screenshot. |
| `trace_cutter.py` | Add perfetto screenshot. |

---

## 1. `locator_manager.py` — 基类

```python
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait


class LocatorManager:
    def __init__(self, driver):
        self.driver = driver

    def wait_for_file_loaded(self):
        return WebDriverWait(self.driver, 100).until(
            expected_conditions.presence_of_element_located(
                (By.XPATH, "//h1[contains(text(), 'Process ')]")))

    def find_process_element(self, process_id):
        return self.driver.find_element(
            By.XPATH, "//h1[@title='Process %s']" % process_id)

    def find_thread_elements(self, thread_pid):
        return self.driver.find_element(
            By.XPATH, "//span[contains(text(), ' %s')]" % thread_pid)

    def find_item_under_process_element(self, text):
        return self.driver.find_element(
            By.XPATH, "//span[contains(text(), '%s')]" % text)

    def find_pin_button_element(self, element):
        return element.find_element(
            By.XPATH, "..//..//button[@title='Pin to top']")

    def find_file_input_element(self):
        return self.driver.find_element(By.XPATH, "//input[@type='file']")

    def find_hide_button_element(self):
        return self.driver.find_element(By.XPATH, "//li[@title='Hide menu']")

    def find_scrolling_panel(self):
        return self.driver.find_element(
            By.CSS_SELECTOR, ".viewer-page .scrolling-panel-container")
```

基类是**老版 Perfetto UI**的定位器（用 `<h1>` / `<span>` DOM 结构）。

---

## 2. `locator_manager_v48.py` — Perfetto UI v48 适配

```python
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait

import mambo_logging
from base.screenshot.locator_manager import LocatorManager


class LocatorManagerV48(LocatorManager):
    def __init__(self, driver):
        super().__init__(driver)
        mambo_logging.info("LocatorManagerV48.__init__")

    def wait_for_file_loaded(self):
        return WebDriverWait(self.driver, 100).until(
            expected_conditions.presence_of_element_located(
                (By.XPATH, "//div[contains(text(), 'Process ')]")))

    def find_process_element(self, process_id):
        return self.driver.find_element(
            By.XPATH,
            "//div[contains(text(), 'Process %s') and contains(@class, 'pf-track-title-popup')]"
            % process_id)

    def find_thread_elements(self, thread_pid):
        return self.driver.find_element(
            By.XPATH,
            "//div[contains(text(), '%s') and contains(@class, 'pf-track-title-popup')]"
            % thread_pid)

    def find_item_under_process_element(self, text):
        return self.driver.find_element(
            By.XPATH,
            "//div[contains(text(), '%s') and contains(@class, 'pf-track-title-popup')]"
            % text)

    def find_pin_button_element(self, element):
        return element.find_element(
            By.XPATH, "..//..//button[@title='Pin to top']")
```

v48 把旧的 `<h1>` / `<span>` 换成带 `pf-track-title-popup` 类的 `<div>`。

---

## 3. `locator_manager_v49.py` — UI v49（加进度条等待）

```python
from time import sleep
from selenium.common.exceptions import NoSuchElementException
from selenium.common import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait

import mambo_logging
from base.screenshot.locator_manager import LocatorManager


class LocatorManagerV49(LocatorManager):
    def __init__(self, driver):
        super().__init__(driver)
        mambo_logging.info("LocatorManagerV49.__init__")

    def wait_for_file_loaded(self):
        try:
            WebDriverWait(self.driver, 50).until(
                expected_conditions.presence_of_element_located(
                    (By.XPATH, "//div[@class='progress']")))
        except TimeoutException:
            self.driver.save_screenshot("./tmp_timeout_1.png")
            mambo_logging.warning("timeout_1")

        sleep(0.5)
        try:
            WebDriverWait(self.driver, 10).until(
                expected_conditions.presence_of_element_located(
                    (By.XPATH, "//div[@class='progress']")))
        except TimeoutException:
            mambo_logging.warning("timeout_2")

        sleep(0.5)
        try:
            WebDriverWait(self.driver, 10).until(
                expected_conditions.presence_of_element_located(
                    (By.XPATH, "loading_finish_xpath")))
        except TimeoutException:
            mambo_logging.warning("timeout_3")

    # ... (find_process_element etc. override with pf-track-title-popup XPaths)
```

**要点**：v49 的等待逻辑是**多级等待**——先等进度条出现（`//div[@class='progress']`），再等进度条消失（`loading_finish_xpath`），三次 `TimeoutException` 各自写日志。出错会存 `tmp_timeout_1.png` 调试。

---

## 4. `locator_manager_v53.py` — UI v53（进度条选择器升级）

```python
from time import sleep
from selenium.common.exceptions import NoSuchElementException
from selenium.common import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait

import mambo_logging
from base.screenshot.locator_manager import LocatorManager


class LocatorManagerV53(LocatorManager):
    def __init__(self, driver):
        super().__init__(driver)
        mambo_logging.info("LocatorManagerV53.__init__")

    def wait_for_file_loaded(self):
        loading_finish_xpath = (
            "//div[@class='pf-linear-progress pf-ui-main__loading' and @state='none']"
        )
        try:
            WebDriverWait(self.driver, 50).until(
                expected_conditions.presence_of_element_located(
                    (By.XPATH, loading_finish_xpath)))
        except TimeoutException:
            self.driver.save_screenshot("./tmp_timeout_1.png")
            mambo_logging.warning("timeout_1")

        sleep(0.5)
        try:
            WebDriverWait(self.driver, 10).until(
                expected_conditions.presence_of_element_located(
                    (By.XPATH, loading_finish_xpath)))
        except TimeoutException:
            mambo_logging.warning("timeout_2")

        sleep(0.5)
        try:
            WebDriverWait(self.driver, 10).until(
                expected_conditions.presence_of_element_located(
                    (By.XPATH, loading_finish_xpath)))
        except TimeoutException:
            mambo_logging.warning("timeout_3")

    def find_process_element(self, process_id):
        try:
            return self.driver.find_element(
                By.XPATH,
                "//div[contains(text(), '%s') and contains(@class, 'pf-track-title-popup')]"
                % process_id)
        except NoSuchElementException:
            # v53 仅在选中时才显示弹窗
            mambo_logging.warning(f"Element with process_id {process_id} not found.")
            return None

    def find_thread_elements(self, thread_pid):
        try:
            return self.driver.find_element(
                By.XPATH,
                "//div[contains(text(), '%s') and contains(@class, 'pf-track-title-popup')][parent::*]"
                % thread_pid)
        except NoSuchElementException:
            mambo_logging.warning("Element not found.")
            return []

    def find_item_under_process_element(self, text):
        try:
            return self.driver.find_element(
                By.XPATH,
                "//div[contains(text(), '%s') and contains(@class, 'pf-track-title-popup')]"
                % text)
        except NoSuchElementException:
            mambo_logging.warning("Element not found.")
            return None

    def find_pin_button_element(self, element):
        try:
            return element.find_element(
                By.XPATH, "..//..//button[@title='Pin to top']")
        except NoSuchElementException:
            mambo_logging.warning("Element not found.")
            return None

    def find_scrolling_panel(self):
        try:
            return self.driver.find_element(
                By.CSS_SELECTOR, ".pf-timeline-page__scrolling-track-tree")
        except NoSuchElementException:
            mambo_logging.warning("Element not found.")
            return None

    def find_hide_button_element(self):
        try:
            return self.driver.find_element(
                By.XPATH,
                "//li[@class='pf-icon pf-icon__left-icon' and text()='menu']")
        except NoSuchElementException:
            mambo_logging.warning("Element not found.")
            return None
```

**要点**：
- v53 的 loading 完成标志是 `pf-linear-progress` + `pf-ui-main__loading` + `state='none'`
- 所有 find 方法都有 `NoSuchElementException` 保护（"增加报错保护" commit 的主要内容）
- scrolling_panel 的 CSS 选择器从 `.viewer-page .scrolling-panel-container` 变成 `.pf-timeline-page__scrolling-track-tree`
- hide_button 的结构也变了

---

## 5. `perfetto_operator.py` — 核心交互层

```python
from __future__ import annotations
import time
import traceback

from selenium.common import (
    ElementClickInterceptedException, NoSuchElementException, TimeoutException)
from selenium.webdriver import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait

import mambo_logging
from base.screenshot.screenshot_config import ThreadTrack, ProcessTrack


class PerfettoOperator:
    def __init__(self, driver, locator_manager):
        self.driver = driver
        self.locator_manager = locator_manager

    def pin_to_top(self, process_list: list[ProcessTrack]):
        for process in process_list:
            self._pin_to_top_one_process(process)

    def back_to_top(self):
        mambo_logging.info("back_to_top")
        scrolling_panel = self.locator_manager.find_scrolling_panel()
        if scrolling_panel:
            scrolling_panel.click()
        self.driver.find_element(By.TAG_NAME, "body").send_keys(
            Keys.CONTROL + Keys.HOME)
        self.locator_manager.wait_for_file_loaded()

    def _pin_to_top_one_process(self, process: ProcessTrack):
        pid = process.pid
        timeline_list = process.timeline_list

        process_element = self.locator_manager.find_process_element(pid)
        if not process_element:
            return
        self.__scroll_to_and_click(process_element)

        for timeline in timeline_list:
            try:
                if isinstance(timeline, ThreadTrack):
                    self.__pin_to_top_one_thread(timeline.tid)
                else:
                    self.__pin_to_top_other(timeline.name)
            except ElementClickInterceptedException:
                mambo_logging.warning(
                    "ElementClickInterceptedException: arg: " + str(timeline))
            except NoSuchElementException:
                mambo_logging.warning(
                    "NoSuchElementException: arg: " + str(timeline))
            except:
                mambo_logging.info(traceback.format_exc())
            finally:
                continue

        self.__scroll_to_and_click(process_element)

    def __pin_to_top_one_thread(self, thread_pid: int):
        elements = self.locator_manager.find_thread_elements(thread_pid)
        for e in elements:
            if e.text.startswith("Cpu "):
                continue
            pin_button = self.__find_pin_button(e)
            self.__scroll_to_and_click(pin_button)
            # pin_button.click()

    def __pin_to_top_other(self, text):
        element = self.locator_manager.find_item_under_process_element(text)
        if element:
            pin_button = self.__find_pin_button(element)
            # pin_button.click()
            self.__scroll_to_and_click(pin_button)

    def __find_pin_button(self, element):
        return self.locator_manager.find_pin_button_element(element)

    def find_element(self, by, text):
        try:
            element = self.driver.find_element(by, text)
            return element
        except NoSuchElementException:
            mambo_logging.warning("NoSuchElementException: " + text)
        except:
            mambo_logging.error(traceback.format_exc())

    def find_element_and_click(self, by, text):
        element = self.find_element(by, text)
        if element:
            element.click()
            return True
        else:
            return False

    def __scroll_to_and_click(self, element):
        time.sleep(1)
        self.driver.execute_script("arguments[0].scrollIntoView();", element)
        time.sleep(1)
        self.driver.execute_script("arguments[0].click();", element)
        time.sleep(1)
```

**要点**：
- 所有点击都走 `__scroll_to_and_click` 三步：sleep 1s → `scrollIntoView` → sleep 1s → `.click()` → sleep 1s
- 直接 `.click()` 被注释掉，改用 `execute_script("arguments[0].click()")` 绕开元素遮挡
- `_pin_to_top_one_process` 在 for 循环前后**各点一次 process_element**（展开/折叠）
- 每个 timeline 的 pin 操作都包了 `ElementClickInterceptedException` / `NoSuchElementException` / 通用 except
- 线程 pin 循环过滤掉 `Cpu X` 开头的（避免 pin CPU 行）

---

## 6. `perfetto_screenshot.py` — 顶层入口

```python
from __future__ import annotations
import os.path
import time
import traceback
import platform

from selenium.webdriver.edge.options import Options
from selenium import webdriver
from selenium.webdriver.common.by import By

import mambo_logging

from PIL import Image
from io import BytesIO

from base.screenshot.locator_manager import LocatorManager
from base.screenshot.locator_manager_v48 import LocatorManagerV48
from base.screenshot.locator_manager_v49 import LocatorManagerV49
from base.screenshot.locator_manager_v53 import LocatorManagerV53
from base.screenshot.perfetto_operator import PerfettoOperator
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from base.screenshot.screenshot_config import ScreenshotConfig


class PerfettoScreenshot:
    def __init__(self, cfg, screenshot_config: ScreenshotConfig, cfg_):
        self.cfg = cfg
        self.screenshot_config = screenshot_config
        self.trace_path = screenshot_config.trace_path
        self.process_list = screenshot_config.process_list
        self.image_path = screenshot_config.image_path
        self.begin_time = screenshot_config.begin_time
        self.end_time = screenshot_config.end_time

        self.perfetto_url = cfg.perfetto_url

        options = Options()
        if cfg.enable_hide_browser:
            options.add_argument("--no-sandbox")
            options.add_argument("--headless")

        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--disable-cache")
        options.add_argument("--disable-application-cache")
        options.add_argument("--incognito")

        if cfg.linux_browser_binary_location and platform.system() == "Linux":
            options.binary_location = cfg.linux_browser_binary_location
        if cfg.user_agent:
            options.add_argument("user-agent=%s" % cfg.user_agent)

        if platform.system() == "Linux":
            webdriver_binary_location = cfg.linux_webdriver_binary_location
        else:
            webdriver_binary_location = cfg.windows_webdriver_binary_location

        if webdriver_binary_location and os.path.exists(webdriver_binary_location):
            self.driver = webdriver.Edge(
                options=options, executable_path=webdriver_binary_location)
        else:
            self.driver = webdriver.Edge(options=options)

        # TODO: 解决截图全黑问题
        self.width = 1080
        self.height = 2520
        self.driver.set_window_size(self.width, self.height)

        self.driver.get(self.get_url_with_file())

        self.version = self.get_version()
        self.locator_manager = self.__get_locate_manager(self.version)
        self.perfetto_operator = PerfettoOperator(self.driver, self.locator_manager)

        try:
            self.user_data_dir = self.driver.capabilities["msedge"]["userDataDir"]
        except:
            self.user_data_dir = ""

        mambo_logging.info(
            f"perfetto_url={self.perfetto_url}, "
            f"version={self.version}, "
            f"user_data_dir={self.user_data_dir}")
        mambo_logging.info(f"screenshot_config={str(screenshot_config.to_dict())}")

    def get_url_with_file(self) -> str:
        data_dir_path = os.path.dirname(os.path.abspath(self.cfg.trace_dir_path))
        trace_path = os.path.abspath(self.trace_path)
        file_in_server_path = os.path.relpath(
            trace_path, data_dir_path).replace('\\', '/')
        file_server_port = self.cfg.file_server_port

        url = (
            f"{self.perfetto_url}/#!/?url=http://127.0.0.1:{file_server_port}/"
            f"{file_in_server_path}&referrer="
            f"&openTraceInUIStart={self.begin_time}&clockEnd={self.end_time}"
        )

        mambo_logging.info(url)
        return url

    def get_version(self):
        version = self.get_version_v53()
        if not version:
            version = self.get_version_v49()
        return version

    def get_version_v49(self) -> str:
        try:
            return self.driver.find_element(
                By.XPATH, "//div[@class='version']/a").text
        except:
            return ""

    def get_version_v53(self) -> str:
        try:
            return self.driver.find_element(
                By.XPATH, "//div[@class='pf-sidebar__version']/a").text
        except:
            return ""

    def run(self):
        self.load_file().hide_bar().pin_to_top(
            self.process_list).back_to_top().screen_shot(self.image_path)
        return self

    def load_file(self):
        try:
            self.locator_manager.wait_for_file_loaded()
        except:
            mambo_logging.error(traceback.format_exc())
        return self

    def hide_bar(self):
        hide_bar_button = self.locator_manager.find_hide_button_element()
        if hide_bar_button:
            hide_bar_button.click()
        return self

    def pin_to_top(self, process_list):
        self.perfetto_operator.pin_to_top(process_list)
        return self

    def back_to_top(self):
        self.perfetto_operator.back_to_top()
        return self

    def screen_shot(self, path):
        image = Image.open(BytesIO(self.driver.get_screenshot_as_png()))
        image.crop((0, 0, image.width, image.height / 3 * 2)).save(path)
        return self

    def release(self):
        self.driver.close()
        self.driver.quit()
        from base.utils.delete import delete_path
        delete_path(self.user_data_dir)

    def __get_locate_manager(self, version) -> LocatorManager:
        version_big = version.split(".")[0]
        if version_big.startswith("v"):
            version_big_digital = int(version_big[1:])
            if version_big_digital >= 53:
                return LocatorManagerV53(self.driver)
            elif version_big_digital >= 49:
                return LocatorManagerV49(self.driver)
            elif version_big_digital >= 48:
                return LocatorManagerV48(self.driver)
            else:
                return LocatorManager(self.driver)
        else:
            return LocatorManager(self.driver)


def main():
    class MyConfig:
        def __init__(self):
            self.perfetto_url = "https://perfetto.rnd.hihonor.com/"
            self.linux_webdriver_binary_location = ""
            self.linux_browser_binary_location = ""
            self.windows_webdriver_binary_location = ""
            self.user_agent = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/115.0.0.0 Safari/537.36 Edg/115.0.1901.203")

    file_path = r"D:\SYSTRACE_ANALYSE_PROJECT\systraceanalyse\..."
    arg_dict = {"jank": ..., "baseline": ..., "ksim": ..., "ksimt": ..., ...}
    img_path = r"D:\SYSTRACE_ANALYSE_PROJECT\systraceanalyse\..._screenshot.png"
    PerfettoScreenshot(MyConfig(), screenshot_config, tag).run()
    print(result)


if __name__ == "__main__":
    main()
```

**要点（非常关键）**：

1. **URL 方式加载 trace**（不是 file_chooser，不是 RPC！）
   ```
   https://perfetto.rnd.hihonor.com/#!/?url=http://127.0.0.1:{port}/{rel_path}
     &referrer=&openTraceInUIStart={begin_ns}&clockEnd={end_ns}
   ```
   - Mambo 自己起一个 HTTP file_server（固定端口），把本地 trace 目录暴露出来
   - Perfetto UI 的 `#!/?url=...` deep link 会去 fetch 这个 URL 加载 trace
   - 同时传 `openTraceInUIStart` 和 `clockEnd` 做**时间范围锁定**——直接 zoom 到卡顿区间
   - `fix bug in file_server.py` 这个 commit 的含义就是修 file_server 那边的 bug

2. **窗口尺寸 1080x2520**（竖屏）— 解决"截图全黑"的 TODO

3. **版本自动检测**：先试 v53 的 XPath `//div[@class='pf-sidebar__version']/a`，失败再试 v49 的 `//div[@class='version']/a`，然后用版本号 int 比较选对应 LocatorManager

4. **浏览器选项全集**：
   ```
   --ignore-certificate-errors
   --disable-cache
   --disable-application-cache
   --incognito
   ```
   仅在 `enable_hide_browser` 为 True 时才加 `--no-sandbox --headless`

5. **run() 链式调用**：`load_file().hide_bar().pin_to_top(list).back_to_top().screen_shot(path)`

6. **截图裁剪**：`image.crop((0, 0, w, h * 2/3))` — **只保留上 2/3** —避开底部分析面板/cookie/状态栏残留

7. **清理 user_data_dir**：release 时 delete_path 掉 msedge 的 userDataDir

---

## 7. `screenshot_config.py` — 配置数据类 + 生成器

```python
from __future__ import annotations
import copy
import os.path

from base.report.delimitation_table import DelimitationTable
from base.time.time_utils import import ms_to_ns
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from base.jank.jank import Jank
    from base.perfetto.perfetto_file import PerfettoFile


class ScreenshotConfig:
    def __init__(self, trace_path: str, trace_type: str,
                 begin_time: int, end_time: int):
        self.trace_path = trace_path
        self.trace_type = trace_type

        if self.trace_path and os.path.exists(self.trace_path):
            self.trace_path = os.path.abspath(self.trace_path)

        self.begin_time = begin_time
        self.end_time = end_time
        self.process_list = []

    @property
    def image_path(self):
        return f"{self.trace_path}.png"

    def to_dict(self) -> {}:
        result_dict = copy.deepcopy(self.__dict__)
        result_dict["process_list"] = [
            process.to_dict() for process in result_dict["process_list"]]
        return result_dict

    def add_thread_track(self, thread_track: ThreadTrack):
        current_process = None
        if self.process_list:
            current_process = self.process_list[-1]

        if not current_process or current_process.pid != thread_track.pid:
            current_process = ProcessTrack(thread_track.pid)
            self.process_list.append(current_process)

        current_process.timeline_list.append(thread_track)


class ThreadTrack:
    def __init__(self, tid: int, pid: int):
        self.tid = int(tid)
        self.pid = int(pid)


class Timeline:
    def __init__(self, name: str):
        self.name = name


class ProcessTrack:
    def __init__(self, pid: int):
        self.pid = int(pid)
        self.timeline_list = []

    def to_dict(self) -> {}:
        result_dict = self.__dict__
        result_dict["timeline_list"] = [
            timeline.__dict__ for timeline in result_dict["timeline_list"]]
        return result_dict


def generate_screenshot_config(
        perfetto_file: PerfettoFile, jank: Jank) -> ScreenshotConfig | None:
    if not perfetto_file or not jank:
        return None

    file_path = perfetto_file.file_path
    file_type = perfetto_file.file_type

    begin_time = int(jank.scene.begin_time - ms_to_ns(100))
    end_time = int(jank.scene.end_time + ms_to_ns(50))

    if begin_time < perfetto_file.trace_begin_time:
        begin_time = perfetto_file.trace_begin_time

    if end_time > perfetto_file.trace_end_time:
        end_time = perfetto_file.trace_end_time

    if begin_time >= end_time:
        return None

    tsa_delimitation_list = jank.tsa_delimitation_list

    if not file_path or not file_type or not begin_time or not end_time \
            or not tsa_delimitation_list:
        return None

    config = ScreenshotConfig(file_path, file_type, begin_time, end_time)

    for tsa_table in tsa_delimitation_list:
        if not isinstance(tsa_table, DelimitationTable):
            continue

        pid = tsa_table.jank_process_id
        tid = tsa_table.jank_thread_id

        if not pid or not tid or pid < 0 or tid < 0:
            continue

        config.add_thread_track(ThreadTrack(tid, pid))

    if not config.process_list and jank.scene and jank.scene.key_threads:
        for thread_info in jank.scene.key_threads.to_list():
            config.add_thread_track(ThreadTrack(thread_info.tid, thread_info.pid))

    return config
```

**要点**：
- 卡顿时间范围前填充 100ms、后填充 50ms
- 夹紧到 trace 时间范围内
- 输入源 1：`jank.tsa_delimitation_list`（DelimitationTable）—核心
- 输入源 2：`jank.scene.key_threads`（仅在 tsa 为空时 fallback）—这就是"改善'未找到卡顿点'"的逻辑

---

## 8. `screenshot_manager.py` — 编排层

```python
from __future__ import annotations
import os

import mambo_logging
from base.perfetto import PerfettoFileType
from base.screenshot.perfetto_screenshot import PerfettoScreenshot
from base.screenshot.screenshot_config import generate_screenshot_config
from base.time.time_utils import import ns_to_ms

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from base.perfetto.perfetto_file import PerfettoFile
    from base.jank.jank import Jank


class ScreenshotManager:
    def __init__(self, perfetto_file: PerfettoFile, jank: Jank, config):
        self.perfetto_file = perfetto_file
        self.jank = jank
        self.config = config

    def run(self) -> str:
        screenshot_config = generate_screenshot_config(
            self.perfetto_file, self.jank)

        if not self.perfetto_file or not self.jank or not screenshot_config \
                or not screenshot_config.process_list:
            return ""

        trace_path = screenshot_config.trace_path
        trace_type = screenshot_config.trace_type
        begin_time = screenshot_config.begin_time
        end_time = screenshot_config.end_time

        if not trace_path or not trace_type or not begin_time or not end_time:
            return ""

        if trace_type == PerfettoFileType.SYSTRACE:
            trace_path_out = f"{os.path.splitext(trace_path)[0]}_{str(ns_to_ms(begin_time))}_.cut"
            self.perfetto_file.systrace_file.write_sub_line_to_file(
                begin_time, end_time, trace_path_out)

            if os.path.exists(trace_path_out):
                screenshot_config.trace_path = trace_path_out

        self._run_screenshot_core(screenshot_config, self.config)

        image_path = screenshot_config.image_path
        if not image_path or not os.path.exists(image_path):
            return ""
        return image_path

    def _run_screenshot_core(self, screenshot_config, config) -> str:
        if not screenshot_config:
            return ""
        screen_shot = None
        try:
            screen_shot = PerfettoScreenshot(
                screenshot_config, config).run()
        except Exception as e:
            mambo_logging.error(e)
        finally:
            if screen_shot:
                screen_shot.release()
            else:
                mambo_logging.warning("run_screenshot_core may not release")
```

**要点**：
- **trace 预切片**：SYSTRACE 类型先切出时间范围那一段写到 `.cut` 文件，降低 Perfetto UI 加载压力（这是和我们目前做法最大的结构差异）
- try/finally 兜底 release，避免 Edge 进程和 userDataDir 泄漏

---

## 9. `selenium_demo.py` — 最小可跑示例

```python
def init_browser():
    options = Options()
    #options.add_argument("--no-sandbox")
    #options.add_argument("--headless")
    #options.add_argument("--Log-Level=0")
    options.add_argument("--ignore-certificate-errors")
    # options.binary_location = "/usr/bin/microsoft-edge-stable"
    options.add_argument(
        "user-agent=%s" % "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115...")
    # driver = webdriver.Edge(options=options, executable_path="./msedgedriver")
    driver = webdriver.Edge(options=options)
    driver.set_window_size(1920, 1080)
    driver.get("https://perfetto.rnd.hihonor.com/")

    # 加载trace文件
    file_input = driver.find_element(By.XPATH, "//input[@type='file']")
    input_path = r"D:\SYSTRACE_ANALYSE_PROJECT\systraceanalyse\perfetto_screenshot\..."
    file_input.send_keys(input_path)

    # 等待文件加载完成
    try:
        process_element = WebDriverWait(driver, 100).until(
            expected_conditions.presence_of_element_located(
                (By.XPATH, "//div[contains(text(), 'Process ')]")))
        time.sleep(1)
    except:
        print(traceback.format_exc())

    time.sleep(3)

    print("back_to_top")
    driver.find_element(
        By.CSS_SELECTOR, ".viewer-page .scrolling-panel-container").click()
    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.CONTROL + Keys.HOME)
    time.sleep(1)
    driver.get_screenshot_as_file(str(time.time()) + "_debug.png")
    driver.close()
    driver.quit()


if __name__ == '__main__':
    init_browser()
```

这是 Mambo 的**玩具示例**（手动用 file_input 方式加载）。正式生产代码用的是 `perfetto_screenshot.py` 的 URL 方式，**不是这个**。

---

## 核心对照表：Mambo vs 我们当前（`trace_screenshot_skill` v4-pipeline）

| 点位 | Mambo | 当前 |
|------|-------|------|
| **浏览器** | Edge + Selenium | Chromium + Playwright |
| **Headless** | 默认否，`enable_hide_browser` 开 | 默认 `headless=True` |
| **Perfetto URL** | 内网 `perfetto.rnd.hihonor.com` | 公网 `ui.perfetto.dev` |
| **Trace 加载** | **URL deep-link + 本地 file_server + 时间范围锁定** | trace_processor HTTP RPC + file_chooser fallback |
| **时间范围** | 在 URL 里传 `openTraceInUIStart` / `clockEnd` | Playwright `setVisibleWindow` 调用 JS |
| **Trace 预切** | SYSTRACE 先切 `.cut` 子集 | 不切 |
| **版本适配** | 运行时检测 v53/v49/v48 → 选对应 LocatorManager | 命令 ID 硬编码主脚本 |
| **等待就绪** | 多级：进度条出现 → 进度条消失（v53: `pf-linear-progress state=none`） | `window.app._activeTrace.timeline` JS 探测 |
| **点击策略** | `scrollIntoView` + `execute_script("click")` + 3 次 sleep | `locator.click()` 直接 |
| **Pin 方式** | 遍历线程定位 DOM → 点 `button[@title='Pin to top']` | `dev.perfetto.PinTracksByRegex` 命令 API |
| **窗口尺寸** | 1080x2520（竖屏） | 1920x2400 |
| **图像裁剪** | PIL crop 保留上 2/3 | 无裁剪 |
| **清理** | release: close + quit + delete userDataDir | ctx.close() |
| **错误保护** | 多层 try/except + `mambo_logging.warning` + 失败存 tmp_timeout_N.png | 部分 try/except，失败抛出 |
| **容错降级** | `screenshot_config.py` 从 `tsa_delimitation_list` fallback 到 `scene.key_threads` | 无降级 |
