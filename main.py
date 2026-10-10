"""
WorkBuddy 自动签到 v6.0 - 图形界面版

全新特性：
1. 桌面 GUI 窗口（tkinter）：签到打卡日历、历史统计、设置开关
2. 无鼠标签到（Windows UI Automation，完全不移动鼠标光标）
3. 签到记录本地持久化（checkin_records.json）
4. 定时提醒：常驻系统托盘 + Windows 计划任务
5. 保留原有命令行模式（--cli / --auto）

技术要点：
- DPI 感知适配，解决高分屏模糊问题
- 签到在后台线程运行，不阻塞界面
- UI Automation 直接 Invoke 控件，零鼠标干扰
"""

import os
import sys
import time
import json
import logging
import argparse
import ctypes
import threading
import calendar
import queue
import atexit
import faulthandler
from datetime import datetime, date, timedelta

# ==================== DPI 感知（必须在创建 Tk 之前调用） ====================
def set_dpi_aware():
    """启用 Windows DPI 感知，解决高分屏下 tkinter 模糊问题"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

set_dpi_aware()

import tkinter as tk
from tkinter import ttk, messagebox

# ==================== UI Automation（无鼠标自动化） ====================
try:
    import uiautomation as auto
    HAS_UIA = True
except Exception:
    HAS_UIA = False

# ==================== 路径处理（兼容 PyInstaller 打包） ====================
def get_resource_dir():
    """获取资源目录，兼容 PyInstaller 打包"""
    if getattr(sys, 'frozen', False):
        if hasattr(sys, '_MEIPASS'):
            return sys._MEIPASS
        exe_dir = os.path.dirname(sys.executable)
        internal_dir = os.path.join(exe_dir, "_internal")
        if os.path.isdir(internal_dir):
            return internal_dir
        return exe_dir
    return os.path.dirname(os.path.abspath(__file__))

PROJECT_DIR = get_resource_dir()
# 数据目录：打包后存 exe 所在目录（重启不丢失），开发时存源码目录
if getattr(sys, 'frozen', False):
    DATA_DIR = os.path.dirname(sys.executable)
else:
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(DATA_DIR, "checkin.log")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
CHECKIN_RECORDS_FILE = os.path.join(DATA_DIR, "checkin_records.json")

# 注册表相关
AUTO_RUN_REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTO_RUN_NAME = "WorkBuddyAutoCheckin"
AUTO_EXEC_KEY = "AutoExecEnabled"

# 签到流程参数
LOAD_WAIT = 1.5  # 每次 Invoke 后的界面加载等待时间（UIA 方案）

# ==================== 日志配置 ====================
def setup_logging():
    handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    # 控制台输出仅在可用时添加（GUI 窗口模式下无控制台）
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers
    )
    # windowed 打包模式下 sys.stdout/stderr 为 None，任何 print 或异常回溯都会
    # 因 None.write() 而崩溃且不可见。这里把两者重定向到日志文件，确保可诊断。
    try:
        if sys.stdout is None:
            sys.stdout = open(LOG_FILE, 'a', encoding='utf-8')
        if sys.stderr is None:
            sys.stderr = open(LOG_FILE, 'a', encoding='utf-8')
    except Exception:
        pass
    # 启用 faulthandler：进程若因段错误/被强制终止，会把调用栈写入日志
    try:
        _fh_log = open(LOG_FILE, 'a', encoding='utf-8')
        faulthandler.enable(_fh_log)
    except Exception:
        pass
    return logging.getLogger(__name__)

logger = setup_logging()

# ==================== 开机自启动管理 ====================
def _open_run_key(mode):
    import winreg
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTO_RUN_REGISTRY_PATH, 0, mode)

def is_auto_run_enabled():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTO_RUN_REGISTRY_PATH, winreg.KEY_READ)
        value, _ = winreg.QueryValueEx(key, AUTO_RUN_NAME)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False

def set_auto_run(enabled):
    try:
        import winreg
        key = _open_run_key(winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, AUTO_RUN_NAME, 0, winreg.REG_SZ, sys.executable)
        else:
            try:
                winreg.DeleteValue(key, AUTO_RUN_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        logger.info(f"开机自启动：{'已启用' if enabled else '已禁用'}")
        return True
    except Exception as e:
        logger.error(f"设置开机自启动失败：{e}")
        return False

def is_auto_exec_enabled():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTO_RUN_REGISTRY_PATH, winreg.KEY_READ)
        value, _ = winreg.QueryValueEx(key, AUTO_EXEC_KEY)
        winreg.CloseKey(key)
        return value == "1"
    except Exception:
        return False

def set_auto_exec(enabled):
    try:
        import winreg
        key = _open_run_key(winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, AUTO_EXEC_KEY, 0, winreg.REG_SZ, "1")
        else:
            try:
                winreg.DeleteValue(key, AUTO_EXEC_KEY)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        logger.info(f"启动后自动签到：{'已启用' if enabled else '已禁用'}")
        return True
    except Exception as e:
        logger.error(f"设置启动后自动签到失败：{e}")
        return False

# ==================== 配置管理 ====================
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载配置失败：{e}")
    return {}

def save_config(config):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"保存配置失败：{e}")
        return False

# ==================== 签到记录持久化 ====================
def load_checkin_records():
    """加载签到记录，返回 {日期字符串: 签到时间字符串}"""
    if os.path.exists(CHECKIN_RECORDS_FILE):
        try:
            with open(CHECKIN_RECORDS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载签到记录失败：{e}")
    return {}

def save_checkin_records(records):
    try:
        with open(CHECKIN_RECORDS_FILE, 'w', encoding='utf-8') as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"保存签到记录失败：{e}")
        return False

def record_today_checkin():
    """记录今天已签到"""
    records = load_checkin_records()
    today = date.today().isoformat()
    if today not in records:
        records[today] = datetime.now().strftime("%H:%M:%S")
        save_checkin_records(records)
        logger.info(f"已记录今日签到：{today}")
    return records

def is_checked_on(day):
    """判断指定日期是否已签到"""
    records = load_checkin_records()
    return day.isoformat() in records

def calc_stats():
    """计算签到统计：累计次数、连续天数、本月天数、本月签到率"""
    records = load_checkin_records()
    today = date.today()

    # 累计签到次数
    total = len(records)

    # 连续签到天数（今天未签则从昨天起算）
    streak = 0
    d = today
    if d.isoformat() not in records:
        d = d - timedelta(days=1)
    while d.isoformat() in records:
        streak += 1
        d = d - timedelta(days=1)

    # 本月统计
    month_days = calendar.monthrange(today.year, today.month)[1]
    month_checked = 0
    for day in range(1, today.day + 1):
        if date(today.year, today.month, day).isoformat() in records:
            month_checked += 1
    month_rate = (month_checked / today.day * 100) if today.day > 0 else 0

    return {
        "total": total,
        "streak": streak,
        "month_checked": month_checked,
        "month_rate": month_rate,
    }

# ==================== UI Automation 无鼠标签到 ====================
def find_workbuddy_window():
    """找到 WorkBuddy 主窗口"""
    if not HAS_UIA:
        return None
    try:
        for w in auto.GetRootControl().GetChildren():
            if w.ClassName == 'Chrome_WidgetWin_1' and w.Name == 'WorkBuddy':
                return auto.ControlFromHandle(w.NativeWindowHandle)
    except Exception as e:
        logger.error(f"查找 WorkBuddy 窗口失败：{e}")
    return None

def restore_workbuddy_window(wb):
    """如果 WorkBuddy 窗口最小化，恢复它（不移动鼠标）"""
    try:
        user32 = ctypes.windll.user32
        hwnd = wb.NativeWindowHandle
        if user32.IsIconic(hwnd):  # 处于最小化状态
            logger.info("WorkBuddy 窗口已最小化，正在恢复...")
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.8)
            return True
        return False
    except Exception as e:
        logger.warning(f"恢复窗口失败：{e}")
        return False

def walk_controls(ctrl, depth=0, max_depth=15):
    """递归遍历所有控件"""
    if depth > max_depth:
        return
    try:
        children = ctrl.GetChildren()
    except Exception:
        return
    for c in children:
        yield c
        for sub in walk_controls(c, depth + 1, max_depth):
            yield sub

def find_control_by_name(ctrl, name_fragment, ctrl_type=None):
    """按名称片段查找控件"""
    for c in walk_controls(ctrl):
        try:
            nm = c.Name
            ct = c.ControlTypeName
        except Exception:
            continue
        if nm and name_fragment in nm:
            if ctrl_type is None or ct == ctrl_type:
                return c
    return None

def uia_invoke(ctrl):
    """无鼠标触发控件（通过 Invoke 模式，不移动鼠标光标）"""
    try:
        ctrl.GetInvokePattern().Invoke()
        return True
    except Exception as e:
        logger.error(f"Invoke 失败：{e}")
        return False

def find_avatar(wb, config):
    """定位头像控件（左下角的用户菜单入口）

    优先使用配置中的昵称匹配，否则用位置启发式（左下角底部区域）。
    """
    # 1. 用配置昵称匹配
    avatar_name = config.get("avatar_name")
    if avatar_name:
        c = find_control_by_name(wb, avatar_name, 'MenuItemControl')
        if c:
            return c
        c = find_control_by_name(wb, avatar_name, 'ImageControl')
        if c:
            return c

    # 2. 位置启发式：找左下角底部的可交互控件（优先有名称的）
    try:
        wb_rect = wb.BoundingRectangle
        win_w = wb_rect.right - wb_rect.left
        win_h = wb_rect.bottom - wb_rect.top
    except Exception:
        return None

    candidates = []
    for c in walk_controls(wb):
        try:
            ct = c.ControlTypeName
            if ct not in ('MenuItemControl', 'ButtonControl', 'ImageControl'):
                continue
            rect = c.BoundingRectangle
            # 底部区域（bottom 在窗口高度 80% 以下）
            if rect.bottom < wb_rect.top + win_h * 0.8:
                continue
            # 左侧区域（left 在窗口宽度 40% 以内）
            if rect.left > wb_rect.left + win_w * 0.4:
                continue
            candidates.append(c)
        except Exception:
            continue

    if not candidates:
        return None

    # 优先有名称的控件，其次越靠底部越优先
    def _score(c):
        try:
            has_name = 1 if c.Name else 0
            bottom_gap = wb_rect.bottom - c.BoundingRectangle.bottom
        except Exception:
            return (0, 10**9)
        return (has_name, -bottom_gap)

    candidates.sort(key=_score, reverse=True)
    return candidates[0]

def perform_checkin_uia(config):
    """用 UI Automation 完成签到，完全不移动鼠标

    返回：
        'success' - 签到成功（点击了签到按钮）
        'already' - 今日已签（检测到"今日已领"）
        'failed'  - 失败（找不到窗口或控件）
    """
    if not HAS_UIA:
        return 'failed'

    logger.info("=" * 50)
    logger.info("开始无鼠标签到（UI Automation）")
    logger.info("=" * 50)

    wb = find_workbuddy_window()
    if wb is None:
        logger.error("未找到 WorkBuddy 窗口，请先启动 WorkBuddy")
        return 'failed'

    # 如果窗口最小化，先恢复
    restore_workbuddy_window(wb)

    # 判断当前是否已在加油站页面（有「关闭 Buddy 加油站」按钮）
    close_btn = find_control_by_name(wb, '关闭 Buddy 加油站', 'ButtonControl')
    already_in_station = close_btn is not None

    if not already_in_station:
        # 步骤 1：点击头像，展开用户菜单
        logger.info("步骤 1：查找并触发头像...")
        station = find_control_by_name(wb, 'Buddy加油站', 'ButtonControl')
        if station is None:
            avatar = find_avatar(wb, config)
            if avatar is None:
                logger.error("未找到头像控件")
                return 'failed'
            # 记录头像昵称，方便下次快速匹配
            try:
                if avatar.Name and avatar.Name != config.get("avatar_name"):
                    config["avatar_name"] = avatar.Name
                    save_config(config)
            except Exception:
                pass
            if not uia_invoke(avatar):
                logger.error("头像触发失败")
                return 'failed'
            time.sleep(LOAD_WAIT)
            station = find_control_by_name(wb, 'Buddy加油站', 'ButtonControl')

        # 步骤 2：点击 Buddy加油站，进入加油站页面
        if station is None:
            logger.error("未找到 Buddy加油站 入口")
            return 'failed'
        logger.info(f"步骤 2：进入 Buddy加油站：{station.Name}")
        if not uia_invoke(station):
            logger.error("Buddy加油站 触发失败")
            return 'failed'
        time.sleep(LOAD_WAIT)
    else:
        logger.info("检测到已在 Buddy 加油站页面，直接检测签到状态")

    # 步骤 3：检测签到状态
    logger.info("步骤 3：检测签到状态...")
    already = find_control_by_name(wb, '今日已领', 'ButtonControl')
    if already is not None:
        logger.info("检测到「今日已领」，今日已完成签到")
        return 'already'

    # 未签到：查找签到按钮
    sign_btn = None
    for kw in ('立即领取', '立即签到', '签到领积分'):
        sign_btn = find_control_by_name(wb, kw, 'ButtonControl')
        if sign_btn:
            break

    if sign_btn is None:
        logger.error("未找到签到按钮（既无「今日已领」也无「立即领取」）")
        return 'failed'

    logger.info(f"找到签到按钮：{sign_btn.Name}，触发签到...")
    if not uia_invoke(sign_btn):
        logger.error("签到按钮触发失败")
        return 'failed'
    time.sleep(LOAD_WAIT)

    # 再次确认签到结果
    if find_control_by_name(wb, '今日已领', 'ButtonControl') is not None:
        logger.info("签到成功！已显示「今日已领」")
    else:
        logger.info("签到按钮已点击（结果未确认，按成功处理）")
    return 'success'

def read_checkin_count_uia(config):
    """用 UIA 读取 Buddy 加油站页面的「已领 N 天」「累计领取 M 分」，完全不移动鼠标。

    返回 (已领天数, 累计积分)；失败返回 (None, None)。
    """
    if not HAS_UIA:
        return None, None

    try:
        wb = find_workbuddy_window()
        if wb is None:
            logger.error("读取签到天数失败：未找到 WorkBuddy 窗口")
            return None, None

        restore_workbuddy_window(wb)

        # 若不在加油站页面，导航进去（头像 → Buddy加油站）
        close_btn = find_control_by_name(wb, '关闭 Buddy 加油站', 'ButtonControl')
        if close_btn is None:
            station = find_control_by_name(wb, 'Buddy加油站', 'ButtonControl')
            if station is None:
                avatar = find_avatar(wb, config)
                if avatar is None:
                    logger.error("读取签到天数失败：未找到头像入口")
                    return None, None
                uia_invoke(avatar)
                time.sleep(LOAD_WAIT)
                station = find_control_by_name(wb, 'Buddy加油站', 'ButtonControl')
            if station is None:
                logger.error("读取签到天数失败：未找到 Buddy加油站 入口")
                return None, None
            uia_invoke(station)
            time.sleep(LOAD_WAIT)

        # 收集所有文本控件（遍历顺序通常自左向右、自上而下）
        texts = []
        for c in walk_controls(wb):
            try:
                if c.ControlTypeName == 'TextControl':
                    texts.append(c)
            except Exception:
                continue

        def _num_after(label):
            """找文本为 label 的控件后紧跟的数字"""
            for i, c in enumerate(texts):
                try:
                    if c.Name != label:
                        continue
                except Exception:
                    continue
                for j in range(i + 1, min(i + 4, len(texts))):
                    try:
                        nxt = texts[j].Name
                    except Exception:
                        continue
                    if nxt and nxt.isdigit():
                        return int(nxt)
                return None
            return None

        days = _num_after('已领')
        credits = _num_after('累计领取')
        if days is None:
            logger.warning("加油站页面未读到「已领 N 天」数字")
        return days, credits
    except Exception as e:
        logger.error(f"读取签到天数异常：{e}")
        return None, None

# ==================== 定时提醒（计划任务） ====================
TASK_NAME = "WorkBuddyAutoCheckinReminder"

def _exe_path():
    if getattr(sys, 'frozen', False):
        return sys.executable
    return sys.executable

def is_scheduled_task_exists():
    """检查计划任务是否存在"""
    try:
        import subprocess
        r = subprocess.run(
            ['schtasks', '/Query', '/TN', TASK_NAME],
            capture_output=True, text=True
        )
        return r.returncode == 0
    except Exception:
        return False

def setup_scheduled_task(remind_time):
    """创建每日定时自动签到计划任务

    remind_time: "HH:MM" 格式
    到点后静默执行签到，成功不打扰，失败才弹提醒。
    """
    try:
        import subprocess
        exe = _exe_path()
        cmd = [
            'schtasks', '/Create', '/TN', TASK_NAME,
            '/TR', f'"{exe}" --auto',
            '/SC', 'DAILY', '/ST', remind_time, '/F'
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            logger.info(f"已创建每日定时自动签到任务：{remind_time}")
            return True
        logger.error(f"创建计划任务失败：{r.stderr}")
        return False
    except Exception as e:
        logger.error(f"创建计划任务异常：{e}")
        return False

def remove_scheduled_task():
    """删除每日提醒计划任务"""
    try:
        import subprocess
        subprocess.run(['schtasks', '/Delete', '/TN', TASK_NAME, '/F'],
                       capture_output=True, text=True)
        logger.info("已删除每日提醒计划任务")
        return True
    except Exception as e:
        logger.error(f"删除计划任务异常：{e}")
        return False

def migrate_scheduled_task():
    """若存在旧版任务（--remind 提醒），自动迁移为 --auto 自动签到"""
    if not is_scheduled_task_exists():
        return
    try:
        import subprocess
        r = subprocess.run(['schtasks', '/Query', '/TN', TASK_NAME, '/XML'],
                           capture_output=True, text=True)
        if '--auto' in (r.stdout or ""):
            return  # 已是自动签到任务
        cfg = load_config()
        remind_time = cfg.get("remind_time", "21:00")
        if setup_scheduled_task(remind_time):
            logger.info("已将旧版提醒任务迁移为每日自动签到任务")
    except Exception as e:
        logger.warning(f"迁移计划任务失败：{e}")

# ==================== 设计令牌 ====================
C = {
    "bg":         "#FFFFFF",   # 窗口背景
    "card":       "#F5F6F8",   # 统计卡片底
    "border":     "#E5E7EB",   # 边框
    "text":       "#1F2937",   # 正文
    "muted":      "#6B7280",   # 次要文字
    "success":    "#22C55E",   # 签到按钮/成功
    "hover":      "#16A34A",   # 按钮悬停
    "warn":       "#F59E0B",   # 倒计时/进行中
    "warn_hover": "#D97706",
    "danger":     "#EF4444",   # 失败
    "today_bg":   "#DBEAFE",   # 今日格子底
    "today_t":    "#1E40AF",   # 今日格子字
    "checked":    "#DCFCE7",   # 已签到格子底
    "checked_t":  "#166534",   # 已签到格子字
    "future_bg":  "#F3F4F6",   # 未来格子底
    "future_t":   "#9CA3AF",   # 未来格子字
    "cell_bg":    "#FFFFFF",   # 未签到格子底
    "cell_t":     "#4B5563",   # 未签到格子字
}
FONT = "微软雅黑"

# ==================== GUI 主界面 ====================
class CheckinApp:
    def __init__(self, root):
        self.root = root
        self.root.title("WorkBuddy 无感签到助手")
        self.root.configure(bg=C["bg"])
        # 新布局（设置默认折叠）；高度 720 完整容纳全部内容
        self.root.geometry("480x720")
        self.root.minsize(460, 660)
        self.root.resizable(True, True)

        self.config = load_config()
        self.records = load_checkin_records()
        self.current_year = date.today().year
        self.current_month = date.today().month
        self.checking = False
        self.syncing = False
        self._cancel_requested = False
        self.settings_expanded = False
        self.result_queue = queue.Queue()

        # 捕获 Tk 回调中的异常并写入日志（windowed 模式下默认会静默丢失）
        self.root.report_callback_exception = self._report_cb_exc

        self._build_scroll_container()
        self._build_ui()
        self._refresh()

        # 启动主线程轮询队列（处理后台签到结果，避免跨线程操作 Tk）
        self._poll_queue()

        # 启动后自动签到（带 5 秒倒计时，可取消）；今日已签则跳过
        if is_auto_exec_enabled() and date.today().isoformat() not in self.records:
            self._cancel_requested = False
            self.root.after(800, self._start_auto_checkin_countdown)

        # 调试：启动后自动展开设置区（自动化测试截图用）
        if '--debug-expand' in sys.argv:
            self.root.after(800, self._toggle_settings)

        # 调试：启动后自动同步真实签到天数（自动化测试用）
        if '--debug-sync' in sys.argv:
            self.root.after(1200, self._on_sync)

    def _report_cb_exc(self, exc_type, exc_value, exc_tb):
        logger.error("Tk 回调异常", exc_info=(exc_type, exc_value, exc_tb))

    # ---------- 队列轮询（线程安全结果回传） ----------
    def _poll_queue(self):
        try:
            while True:
                result = self.result_queue.get_nowait()
                if isinstance(result, tuple):
                    self._on_sync_done(result)
                else:
                    self._on_checkin_done(result)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    # ---------- 可滚动容器 ----------
    def _build_scroll_container(self):
        """把全部内容放进一个可滚动画布，设置展开等内容超出时可滚动查看"""
        self.canvas = tk.Canvas(self.root, bg=C["bg"], highlightthickness=0, bd=0)
        self.scrollbar = tk.Scrollbar(self.root, orient='vertical', command=self.canvas.yview)
        self.content = tk.Frame(self.canvas, bg=C["bg"])

        self._content_window = self.canvas.create_window((0, 0), window=self.content, anchor='nw')
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        # 内容宽度跟随画布宽度；滚动区域随内容高度自动更新
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.content.bind("<Configure>", self._on_content_configure)

        self.canvas.pack(side='left', fill='both', expand=True)
        self.scrollbar.pack(side='right', fill='y')

        # 鼠标滚轮滚动：进入内容区才绑定，避免与 Spinbox 的滚轮改值冲突
        self.content.bind("<Enter>", self._bind_wheel)
        self.content.bind("<Leave>", self._unbind_wheel)

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._content_window, width=event.width)

    def _on_content_configure(self, event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _bind_wheel(self, event=None):
        self.content.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_wheel(self, event=None):
        self.content.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        # 内容高度未超出画布时 yview_scroll 自然无效，无副作用
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    # ---------- UI 构建 ----------
    def _build_ui(self):
        # 标题
        title = tk.Label(self.content, text="WorkBuddy 无感签到助手", font=(FONT, 16, "bold"),
                         fg=C["text"], bg=C["bg"])
        title.pack(pady=(16, 2))

        # 状态横幅（一眼可见：今日已签到 / 未签到）
        self.banner = tk.Label(self.content, text="今日未签到", font=(FONT, 13, "bold"),
                               fg=C["today_t"], bg=C["today_bg"], padx=16, pady=10)
        self.banner.pack(fill='x', padx=20, pady=(8, 6))

        # 主按钮（立即签到）
        self.checkin_btn = tk.Button(
            self.content, text="立即签到", font=(FONT, 14, "bold"),
            bg=C["success"], fg="white", activebackground=C["hover"],
            relief='flat', pady=12, cursor="hand2",
            command=self._on_checkin
        )
        self.checkin_btn.pack(fill='x', padx=20, pady=(0, 2))

        # 同步真实签到天数（从 Buddy 加油站读取「已领 N 天」）
        self.sync_btn = tk.Button(
            self.content, text="同步真实签到天数", font=(FONT, 10),
            bg=C["card"], fg=C["text"], activebackground=C["border"],
            relief='flat', pady=6, cursor="hand2",
            command=self._on_sync
        )
        self.sync_btn.pack(fill='x', padx=20, pady=(0, 6))

        # 取消自动签到按钮（倒计时时才显示，与主按钮分离）
        self.cancel_btn = tk.Button(
            self.content, text="取消自动签到", font=(FONT, 10),
            bg=C["warn"], fg="white", activebackground=C["warn_hover"],
            relief='flat', pady=4, cursor="hand2",
            command=self._cancel_auto_checkin
        )

        # 日历区（水平居中显示）
        self.cal_title = tk.Label(self.content, text="签到打卡日历", font=(FONT, 12, "bold"),
                                  fg=C["text"], bg=C["bg"])
        self.cal_title.pack(pady=(10, 2))
        self.calendar_frame = tk.Frame(self.content, bg=C["bg"])
        self.calendar_frame.pack(anchor='center', padx=20, pady=2)

        # 统计区（签到成果汇总）
        self.stats_frame = tk.Frame(self.content, bg=C["bg"])
        self.stats_frame.pack(fill='x', padx=20, pady=(10, 2))
        self.stat_total = self._make_stat(self.stats_frame, "累计签到", "0 次", 0)
        self.stat_streak = self._make_stat(self.stats_frame, "连续签到", "0 天", 1)
        self.stat_month = self._make_stat(self.stats_frame, "本月签到", "0 天", 2)

        # 状态提示
        self.status_label = tk.Label(self.content, text="就绪", fg=C["muted"],
                                     font=(FONT, 10), bg=C["bg"])
        self.status_label.pack(pady=(4, 0))

        # 设置区（默认折叠）
        self.settings_header = tk.Button(
            self.content, text="▸ 设置", anchor='w', relief='flat',
            bg=C["bg"], fg=C["muted"], font=(FONT, 10, "bold"),
            activebackground=C["bg"], activeforeground=C["text"],
            command=self._toggle_settings, cursor="hand2", padx=4
        )
        self.settings_header.pack(fill='x', padx=20, pady=(8, 0))

        self.settings_frame = tk.Frame(self.content, bg=C["card"],
                                       highlightbackground=C["border"], highlightthickness=1)

        # 开机自启动开关
        self.auto_run_var = tk.BooleanVar(value=is_auto_run_enabled())
        tk.Checkbutton(
            self.settings_frame, text="开机自启动", variable=self.auto_run_var,
            command=lambda: set_auto_run(self.auto_run_var.get()),
            font=(FONT, 10), bg=C["card"], activebackground=C["card"]
        ).pack(anchor='w', padx=12, pady=(10, 0))

        # 启动后自动签到开关
        self.auto_exec_var = tk.BooleanVar(value=is_auto_exec_enabled())
        tk.Checkbutton(
            self.settings_frame, text="启动后自动签到（5 秒倒计时）", variable=self.auto_exec_var,
            command=lambda: set_auto_exec(self.auto_exec_var.get()),
            font=(FONT, 10), bg=C["card"], activebackground=C["card"]
        ).pack(anchor='w', padx=12, pady=(4, 0))

        # 每日定时自动签到
        remind_frame = tk.Frame(self.settings_frame, bg=C["card"])
        remind_frame.pack(anchor='w', padx=12, pady=(8, 10), fill='x')
        self.remind_var = tk.BooleanVar(value=is_scheduled_task_exists())
        tk.Checkbutton(
            remind_frame, text="每日定时自动签到", variable=self.remind_var,
            command=self._on_toggle_remind,
            font=(FONT, 10), bg=C["card"], activebackground=C["card"]
        ).pack(side='left')

        remind_time = self.config.get("remind_time", "21:00")
        self.remind_hour = tk.StringVar(value=remind_time.split(":")[0])
        self.remind_minute = tk.StringVar(value=remind_time.split(":")[1])
        hour_spin = tk.Spinbox(remind_frame, from_=0, to=23, width=3, textvariable=self.remind_hour,
                               format="%02.0f", font=(FONT, 10))
        hour_spin.pack(side='left', padx=(8, 2))
        tk.Label(remind_frame, text=":", font=(FONT, 10), bg=C["card"]).pack(side='left')
        min_spin = tk.Spinbox(remind_frame, from_=0, to=59, width=3, textvariable=self.remind_minute,
                              format="%02.0f", font=(FONT, 10))
        min_spin.pack(side='left')
        # 时间改动后失焦即生效（开关开启时）
        hour_spin.bind("<FocusOut>", lambda e: self._apply_remind_time())
        min_spin.bind("<FocusOut>", lambda e: self._apply_remind_time())

        # 底部按钮
        self.bottom_frame = tk.Frame(self.content, bg=C["bg"])
        self.bottom_frame.pack(fill='x', padx=20, pady=(6, 12))
        tk.Button(self.bottom_frame, text="刷新", command=self._refresh,
                  font=(FONT, 10), cursor="hand2", relief='flat',
                  bg=C["bg"], fg=C["muted"], activebackground=C["card"]).pack(side='left')
        tk.Button(self.bottom_frame, text="退出", command=self._on_exit,
                  font=(FONT, 10), cursor="hand2", relief='flat',
                  bg=C["bg"], fg=C["muted"], activebackground=C["card"]).pack(side='right')

    def _toggle_settings(self):
        """展开/收起设置区"""
        if self.settings_expanded:
            self.settings_frame.pack_forget()
            self.settings_header.config(text="▸ 设置")
            self.settings_expanded = False
        else:
            self.settings_frame.pack(fill='x', padx=20, pady=(2, 0), before=self.bottom_frame)
            self.settings_header.config(text="▾ 设置")
            self.settings_expanded = True
            self._auto_fit_height()

    def _auto_fit_height(self):
        """内容超出窗口时自动加高窗口（不超过屏幕工作区；剩余部分由滚动条兜底）"""
        self.root.update_idletasks()
        need = self.content.winfo_reqheight()
        have = self.canvas.winfo_height()
        if need <= have:
            return
        # 屏幕工作区（扣除任务栏），保证窗口不会被推到屏幕外
        class RECT(ctypes.Structure):
            _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                        ('right', ctypes.c_long), ('bottom', ctypes.c_long)]
        wa = RECT()
        ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(wa), 0)  # SPI_GETWORKAREA
        screen_bottom = wa.bottom
        cur_top = self.root.winfo_rooty()
        cur_h = self.root.winfo_height()
        new_h = min(cur_h + (need - have), screen_bottom - cur_top - 8)
        if new_h > cur_h:
            self.root.geometry(f"{self.root.winfo_width()}x{int(new_h)}")

    def _apply_remind_time(self):
        """时间输入失焦后，若开关已开启则立即应用新时间"""
        if not self.remind_var.get():
            return
        h = self.remind_hour.get().zfill(2)
        m = self.remind_minute.get().zfill(2)
        remind_time = f"{h}:{m}"
        if setup_scheduled_task(remind_time):
            self.config["remind_time"] = remind_time
            save_config(self.config)
            self._set_status(f"已更新每日 {remind_time} 自动签到", C["checked_t"])

    def _make_stat(self, parent, label, value, col):
        frame = tk.Frame(parent, bg=C["card"], highlightbackground=C["border"], highlightthickness=1)
        frame.grid(row=0, column=col, padx=4, sticky='ew')
        parent.grid_columnconfigure(col, weight=1)
        tk.Label(frame, text=label, font=(FONT, 10), fg=C["muted"], bg=C["card"]).pack(pady=(8, 0))
        lbl = tk.Label(frame, text=value, font=(FONT, 20, "bold"), fg=C["text"], bg=C["card"])
        lbl.pack(pady=(0, 8))
        setattr(self, f"stat_val_{col}", lbl)
        return lbl

    # ---------- 日历渲染 ----------
    def _render_calendar(self):
        for w in self.calendar_frame.winfo_children():
            w.destroy()

        year, month = self.current_year, self.current_month
        today = date.today()

        # 年月导航栏（◀◀ 上一年 / ◀ 上月 / 点击年月回到今天 / ▶ 下月 / ▶▶ 下一年）
        nav = tk.Frame(self.calendar_frame, bg=C["bg"])
        nav.grid(row=0, column=0, columnspan=7, pady=(0, 4))
        nav_btn_cfg = dict(font=(FONT, 10, "bold"), fg=C["text"], bg=C["bg"],
                           activebackground=C["card"], activeforeground=C["text"],
                           relief='flat', bd=0, padx=6, pady=2, cursor="hand2")
        tk.Button(nav, text="◀◀", command=lambda: self._change_year(-1), **nav_btn_cfg).pack(side='left', padx=(0, 2))
        tk.Button(nav, text="◀", command=lambda: self._change_month(-1), **nav_btn_cfg).pack(side='left', padx=2)
        self.month_label = tk.Label(nav, text=f"{year}年 {month}月", font=(FONT, 11, "bold"),
                                    fg=C["text"], bg=C["bg"], width=11, cursor="hand2")
        self.month_label.pack(side='left', padx=8)
        self.month_label.bind("<Button-1>", lambda e: self._goto_today())
        tk.Button(nav, text="▶", command=lambda: self._change_month(1), **nav_btn_cfg).pack(side='left', padx=2)
        tk.Button(nav, text="▶▶", command=lambda: self._change_year(1), **nav_btn_cfg).pack(side='left', padx=(2, 0))

        # 星期标题
        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        for i, wd in enumerate(weekdays):
            tk.Label(self.calendar_frame, text=wd, font=(FONT, 9, "bold"),
                     fg=C["muted"], bg=C["bg"], width=4).grid(row=1, column=i, pady=2)

        # 计算当月日历（monthrange 返回值 0=周一，与第一列"一"直接对应）
        first_weekday, num_days = calendar.monthrange(year, month)

        row = 2
        col = first_weekday
        for day in range(1, num_days + 1):
            d = date(year, month, day)
            d_str = d.isoformat()
            checked = d_str in self.records
            is_today = (d == today)
            is_future = d > today

            # 颜色方案（浅色主题，令牌化）
            if checked:
                bg = C["checked"]       # 已签到：浅绿
                fg = C["checked_t"]
            elif is_today:
                bg = C["today_bg"]      # 今日：浅蓝
                fg = C["today_t"]
            elif is_future:
                bg = C["future_bg"]     # 未来：浅灰
                fg = C["future_t"]
            else:
                bg = C["cell_bg"]       # 未签到：白
                fg = C["cell_t"]

            cell = tk.Label(self.calendar_frame, text=str(day), font=(FONT, 9),
                            bg=bg, fg=fg, width=4, height=1, relief='flat',
                            highlightbackground=C["border"], highlightthickness=1)
            if is_today:
                cell.config(font=(FONT, 9, "bold"))
            cell.grid(row=row, column=col, padx=1, pady=1)

            # 手动补签：点击非未来日期切换签到状态
            if not is_future:
                cell.bind("<Button-1>", lambda e, ds=d_str: self._toggle_manual_checkin(ds))
                cell.config(cursor="hand2")

            col += 1
            if col > 6:
                col = 0
                row += 1

    def _change_month(self, delta):
        """切换月份（delta=±1），跨年自动处理"""
        m = self.current_month + delta
        y = self.current_year
        if m < 1:
            m = 12
            y -= 1
        elif m > 12:
            m = 1
            y += 1
        self.current_year, self.current_month = y, m
        self._render_calendar()

    def _change_year(self, delta):
        """切换年份（delta=±1）"""
        self.current_year += delta
        self._render_calendar()

    def _goto_today(self):
        """点击年月文字，回到今天所在月"""
        self.current_year = date.today().year
        self.current_month = date.today().month
        self._render_calendar()
        self._set_status("已回到今天", C["muted"])

    def _toggle_manual_checkin(self, day_str):
        """手动补签/取消：点击日历格子切换指定日期的签到状态"""
        if day_str in self.records:
            del self.records[day_str]
            self._set_status(f"已取消 {day_str} 的签到标记", C["muted"])
        else:
            self.records[day_str] = datetime.now().strftime("%H:%M:%S")
            self._set_status(f"已补签 {day_str}", C["checked_t"])
        save_checkin_records(self.records)
        self._refresh()

    # ---------- 刷新 ----------
    def _refresh(self):
        self.records = load_checkin_records()
        stats = calc_stats()
        today_str = date.today().isoformat()
        if today_str in self.records:
            self.banner.config(text="✓ 今日已签到", bg=C["checked"], fg=C["checked_t"])
        else:
            self.banner.config(text="今日未签到", bg=C["today_bg"], fg=C["today_t"])
        # 累计签到：优先显示从加油站同步到的真实已领天数，否则用本地记录数
        synced = self.config.get("synced_days")
        total_display = max(stats["total"], int(synced)) if synced else stats["total"]
        self.stat_total.config(text=f"{total_display} 次")
        self.stat_streak.config(text=f"{stats['streak']} 天")
        self.stat_month.config(text=f"{stats['month_checked']} 天")
        self._render_calendar()

    # ---------- 签到 ----------
    def _start_auto_checkin_countdown(self, remaining=5):
        """启动后自动签到：5 秒倒计时，可取消"""
        if self._cancel_requested:
            return
        if remaining <= 0:
            self._hide_cancel_btn()
            self._set_status("自动签到执行中...")
            self.checking = True
            self.checkin_btn.config(state='disabled', text="签到中...")

            def worker():
                result = launch_checkin_subprocess()
                self.result_queue.put(result)

            threading.Thread(target=worker, daemon=True).start()
            return
        self._set_status(f"自动签到将在 {remaining} 秒后开始", C["warn"])
        # 显示独立取消按钮；主按钮禁用，避免「执行/取消」混淆
        self.cancel_btn.config(text=f"取消自动签到 ({remaining}s)")
        if not self.cancel_btn.winfo_ismapped():
            self.cancel_btn.pack(fill='x', padx=20, pady=(2, 4), before=self.cal_title)
        self.checkin_btn.config(state='disabled')
        self.root.after(1000, lambda: self._start_auto_checkin_countdown(remaining - 1))

    def _hide_cancel_btn(self):
        if self.cancel_btn.winfo_ismapped():
            self.cancel_btn.pack_forget()

    def _cancel_auto_checkin(self):
        self._cancel_requested = True
        self._hide_cancel_btn()
        self.checkin_btn.config(state='normal')
        self._reset_checkin_btn()
        self._set_status("已取消自动签到")

    def _reset_checkin_btn(self):
        self.checkin_btn.config(text="立即签到", bg=C["success"],
                                activebackground=C["hover"],
                                command=self._on_checkin)

    def _on_checkin(self):
        if self.checking:
            return
        self.checking = True
        self.checkin_btn.config(state='disabled', text="签到中...")
        self._set_status("正在无鼠标签到，请稍候...")

        def worker():
            result = launch_checkin_subprocess()
            self.result_queue.put(result)

        threading.Thread(target=worker, daemon=True).start()

    def _on_checkin_done(self, result):
        self.checking = False
        self._reset_checkin_btn()

        if result == 'success':
            record_today_checkin()
            self._set_status("签到成功！已记录今日签到", C["checked_t"])
        elif result == 'already':
            record_today_checkin()
            self._set_status("今日已签到，无需重复", C["checked_t"])
        else:
            self._set_status("签到失败，请确认 WorkBuddy 已启动并登录", C["danger"])
        self._refresh()

    # ---------- 同步真实签到天数 ----------
    def _on_sync(self):
        """点击「同步真实签到天数」：后台 UIA 读取加油站「已领 N 天」"""
        if self.syncing:
            return
        self.syncing = True
        self.sync_btn.config(state='disabled', text="同步中...")
        self._set_status("正在读取 Buddy 加油站的签到天数...")

        def worker():
            days, credits = read_checkin_count_uia(self.config)
            self.result_queue.put(('sync', days, credits))

        threading.Thread(target=worker, daemon=True).start()

    def _on_sync_done(self, result):
        _, days, credits = result
        self.syncing = False
        self.sync_btn.config(state='normal', text="同步真实签到天数")

        if days is None:
            self._set_status("同步失败：请先启动并登录 WorkBuddy", C["danger"])
            return

        self.config["synced_days"] = days
        if credits is not None:
            self.config["synced_credits"] = credits
        save_config(self.config)
        if credits is not None:
            self._set_status(f"已同步：本期已领 {days} 天，累计 {credits} 分", C["checked_t"])
        else:
            self._set_status(f"已同步：本期已领 {days} 天", C["checked_t"])
        self._refresh()

    def _set_status(self, text, color=None):
        self.status_label.config(text=text, fg=color or C["muted"])

    # ---------- 定时提醒 ----------
    def _on_toggle_remind(self):
        if self.remind_var.get():
            h = self.remind_hour.get().zfill(2)
            m = self.remind_minute.get().zfill(2)
            remind_time = f"{h}:{m}"
            if setup_scheduled_task(remind_time):
                self.config["remind_time"] = remind_time
                save_config(self.config)
                self._set_status(f"已启用每日 {remind_time} 自动签到", C["checked_t"])
            else:
                self.remind_var.set(False)
                self._set_status("创建计划任务失败", C["danger"])
        else:
            remove_scheduled_task()
            self._set_status("已关闭每日自动签到")

    # ---------- 退出 ----------
    def _on_exit(self):
        self.root.destroy()

# ==================== 提醒模式 ====================
def launch_checkin_subprocess():
    """启动独立子进程执行签到（隔离 COM 环境，避免与 tkinter 冲突）

    返回 'success' / 'already' / 'failed'
    """
    import subprocess
    if getattr(sys, 'frozen', False):
        cmd = [sys.executable, '--checkin']
    else:
        cmd = [sys.executable, os.path.abspath(__file__), '--checkin']
    try:
        creationflags = 0
        if hasattr(subprocess, 'CREATE_NO_WINDOW'):
            creationflags = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags
        )
        proc.wait(timeout=90)
        if proc.returncode == 0:
            return 'success'
        elif proc.returncode == 2:
            return 'already'
        return 'failed'
    except Exception as e:
        logger.error(f"子进程签到异常：{e}")
        return 'failed'

def run_checkin_and_notify():
    """执行签到并返回结果消息（主线程调用）"""
    result = perform_checkin_uia(load_config())
    if result in ('success', 'already'):
        record_today_checkin()
        return "签到成功！" if result == 'success' else "今日已领，无需重复签到！"
    return "签到失败，请确认 WorkBuddy 已启动并登录"

def show_reminder(failed=False):
    """弹出签到提醒窗口（由计划任务触发）

    failed=True：表示定时自动签到已尝试但失败，提示手动处理
    failed=False：到点提醒用户签到
    """
    root = tk.Tk()
    root.title("签到提醒")
    root.geometry("340x180")
    root.attributes('-topmost', True)
    if failed:
        title_text = "自动签到未成功"
        sub_text = "定时自动签到失败，请检查 WorkBuddy 是否已启动"
    else:
        title_text = "该签到啦！"
        sub_text = "今天还没在 WorkBuddy 签到哦"
    tk.Label(root, text=title_text, font=("微软雅黑", 14, "bold")).pack(pady=(16, 4))
    tk.Label(root, text=sub_text, font=("微软雅黑", 10)).pack()
    status = tk.Label(root, text="", font=("微软雅黑", 9), fg=C["checked_t"])
    status.pack(pady=2)

    def do_checkin():
        btn.config(state='disabled', text="签到中...")
        root.update()
        msg = run_checkin_and_notify()  # 主线程同步执行
        status.config(text=msg)

    def close():
        root.destroy()

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=10)
    btn = tk.Button(btn_frame, text="立即签到", bg=C["success"], fg="white",
                    command=do_checkin, width=10)
    btn.pack(side='left', padx=6)
    tk.Button(btn_frame, text="稍后再说", command=close, width=10).pack(side='left', padx=6)
    root.mainloop()

# ==================== 托盘常驻 ====================
def run_tray():
    """常驻系统托盘，定时检查提醒时间"""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:
        return

    # 生成托盘图标
    img = Image.new('RGB', (64, 64), (76, 175, 80))
    d = ImageDraw.Draw(img)
    d.ellipse([8, 8, 56, 56], fill=(255, 255, 255))
    d.text((20, 18), "签", fill=(76, 175, 80))

    def on_checkin(icon, item):
        threading.Thread(target=run_checkin_and_notify, daemon=True).start()

    def on_open(icon, item):
        root = tk.Tk()
        CheckinApp(root)
        root.mainloop()

    def on_exit(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem('立即签到', on_checkin),
        pystray.MenuItem('打开主界面', on_open),
        pystray.MenuItem('退出', on_exit),
    )
    icon = pystray.Icon('WorkBuddyCheckin', img, 'WorkBuddy 无感签到助手', menu)
    icon.run()

# ==================== 命令行模式 ====================
def run_cli_mode():
    """原始命令行交互模式"""
    def menu():
        print("\n" + "=" * 50)
        print("  WorkBuddy 自动签到 v6.0 (命令行)")
        print("=" * 50)
        print("  1. 执行签到（无鼠标）")
        print("  2. 启用/禁用开机自启动")
        print("  3. 启用/禁用启动后自动签到")
        print("  4. 查看签到统计")
        print("  5. 退出")
        print("=" * 50)

    while True:
        menu()
        try:
            choice = input("请选择 (1-5): ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if choice == '1':
            result = perform_checkin_uia(load_config())
            if result == 'success':
                record_today_checkin()
                print("\n[成功] 签到成功！")
            elif result == 'already':
                record_today_checkin()
                print("\n[提示] 今日已签到！")
            else:
                print("\n[失败] 签到失败")
        elif choice == '2':
            cur = is_auto_run_enabled()
            set_auto_run(not cur)
            print(f"\n开机自启动：{'已启用' if not cur else '已禁用'}")
        elif choice == '3':
            cur = is_auto_exec_enabled()
            set_auto_exec(not cur)
            print(f"\n启动后自动签到：{'已启用' if not cur else '已禁用'}")
        elif choice == '4':
            stats = calc_stats()
            print(f"\n累计签到：{stats['total']} 次")
            print(f"连续签到：{stats['streak']} 天")
            print(f"本月签到：{stats['month_checked']} 天")
            print(f"本月签到率：{stats['month_rate']:.1f}%")
        elif choice == '5':
            break
        else:
            print("\n无效选择")

# ==================== 主入口 ====================
def main():
    parser = argparse.ArgumentParser(description='WorkBuddy 自动签到')
    parser.add_argument('--cli', action='store_true', help='使用命令行模式')
    parser.add_argument('--auto', action='store_true', help='定时自动签到（到点静默签到，失败才提醒）')
    parser.add_argument('--remind', action='store_true', help='弹出签到提醒')
    parser.add_argument('--tray', action='store_true', help='常驻系统托盘')
    parser.add_argument('--checkin', action='store_true', help='独立进程执行签到（内部使用，退出码 0=成功 2=已签 1=失败）')
    parser.add_argument('--debug-expand', action='store_true', help='启动后自动展开设置区（调试用）')
    parser.add_argument('--debug-sync', action='store_true', help='启动后自动同步真实签到天数（调试用）')
    args = parser.parse_args()

    logger.info(f"[启动] sys.argv = {sys.argv}, auto={args.auto}, cli={args.cli}, remind={args.remind}, tray={args.tray}")

    # 独立子进程签到模式（避免与 tkinter 主循环的 COM 冲突）
    if args.checkin:
        result = perform_checkin_uia(load_config())
        if result == 'success':
            record_today_checkin()
            sys.exit(0)
        elif result == 'already':
            record_today_checkin()
            sys.exit(2)
        else:
            sys.exit(1)

    # 提醒模式
    if args.remind:
        show_reminder()
        return

    # 托盘模式
    if args.tray:
        run_tray()
        return

    # 静默自动签到模式（定时任务 / 开机自启动调用）
    if args.auto:
        result = perform_checkin_uia(load_config())
        if result in ('success', 'already'):
            record_today_checkin()
            logger.info("自动签到成功，静默退出")
        else:
            logger.warning("自动签到失败，弹出提醒")
            show_reminder(failed=True)
        return

    # 命令行模式
    if args.cli:
        run_cli_mode()
        return

    # 默认：图形界面
    # 进程退出原因记录（覆盖正常退出、异常退出、被强杀等场景）
    def _on_exit_log():
        try:
            logger.info("[GUI] 进程即将退出（atexit 触发）")
        except Exception:
            pass
    atexit.register(_on_exit_log)

    # 后台线程未捕获异常记录（windowed 下默认打印到 stderr=None 会静默）
    def _thread_exc_hook(args):
        logger.error("后台线程异常", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = _thread_exc_hook

    logger.info("[GUI] 准备创建 Tk 窗口")
    # 迁移旧版提醒任务为自动签到任务（若存在）
    migrate_scheduled_task()
    root = tk.Tk()
    logger.info("[GUI] Tk 窗口创建成功")
    CheckinApp(root)
    logger.info("[GUI] CheckinApp 初始化完成，进入 mainloop")
    try:
        root.mainloop()
        logger.info("[GUI] mainloop 已结束（正常返回）")
    except Exception as e:
        logger.exception(f"[GUI] mainloop 抛出异常：{e}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # GUI 模式下异常时弹窗提示
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("程序错误", f"程序出错：{e}")
        except Exception:
            pass
        print(f"[错误] 程序出错：{e}")
        logger.exception("程序异常退出")
        sys.exit(1)
