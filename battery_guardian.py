# -*- coding: utf-8 -*-
"""
电量守护 / Battery Guardian
------------------------------------------------
把"忘记插电"从一次可以被点掉的提醒，变成一次必须做的动作。

六层机制（由电量驱动同一条升级链，不是六个并列开关）：
  L1 换触发点   在你还顺手的时候提醒，而不是在 20% 时打断你
  L2 常驻浮条   电量翻译成"还能撑多久"，一直可见，不弹窗不消失
  L3 说谎检测   唯一按钮是"已插电"，且它真的去读充电状态
  L4 即时代价   默认关闭，只有前面几层被无视后才启用
  L5 电费账本   把"用电池省钱"这个错误信念用数字纠正过来
  L6 保底层     8% 自动休眠，让最坏结果从"硬关机"变成"安全休眠"

运行：pythonw battery_guardian.py
调试：python battery_guardian.py --once       只打印一次状态
      python battery_guardian.py --demo 23    用假电量预览界面
      python battery_guardian.py --self-test  无界面自检
"""

import ctypes
import json
import os
import sys
import time
import subprocess
import tkinter as tk
import tkinter.font as tkfont
from collections import deque
from datetime import date, datetime

# ---------------------------------------------------------------- 基础路径

APP_NAME = "电量守护"

# ---- 元信息：单一来源 ----------------------------------------------------
# 改这里就够了。build_exe.bat 会读这几个常量自动生成 version_info.txt，
# 让 exe 属性里的「公司 / 版权 / 版本」跟源码保持一致。
__version__ = "1.0.0"
__author__ = "CAOHOU"
__license__ = "MIT"
__year__ = "2026"
REPO_NAME = "battery-guardian"

# 打包成 exe 后 __file__ 指向临时解包目录，配置写在那里会在退出时被删掉。
# 这是打包后最容易被忽略的坑：源码跑得好好的，换成 exe 就"记不住"设置和账本。
FROZEN = bool(getattr(sys, "frozen", False))


def _app_data_root():
    """找一个放配置的地方。优先 LOCALAPPDATA，别把目录建在用户主目录根下。"""
    v = os.environ.get("LOCALAPPDATA")
    if v:
        return v
    up = os.environ.get("USERPROFILE")
    if up:
        return os.path.join(up, "AppData", "Local")
    v = os.environ.get("APPDATA")
    if v:
        return v
    return os.path.expanduser("~")


if FROZEN:
    APP_HOME = os.path.join(_app_data_root(), "BatteryGuardian")
    RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
else:
    APP_HOME = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = APP_HOME

DATA_DIR = os.path.join(APP_HOME, "data")
CFG_PATH = os.path.join(DATA_DIR, "config.json")
STATS_PATH = os.path.join(DATA_DIR, "stats.json")
LOG_PATH = os.path.join(DATA_DIR, "app.log")
ICON_PATH = os.path.join(RES_DIR, "assets", "icon.ico")

FAMILY = "Microsoft YaHei UI"

C = {
    "surface": "#FFFFFF",
    "surface_soft": "#F4F3EE",
    "border": "#DEDCD3",
    "text": "#1A1A1A",
    "muted": "#6B6B66",
    "faint": "#9C9C96",
    "amber": "#B0730F",
    "amber_bg": "#FBF3E2",
    "amber_bg2": "#F7E3BE",
    "red": "#A32D2D",
    "red_bg": "#FCEEEE",
    "red_bg2": "#F6C7C7",
    "green": "#0F6E56",
    "green_bg": "#EAF5F0",
    "key": "#FF00FE",
}

DEFAULTS = {
    "capacity_wh": 60.0,
    "kwh_price": 0.60,
    "battery_price": 400.0,
    "battery_cycles": 600.0,
    "thresholds": {"hint": 50, "nudge": 30, "dialog": 20, "cost": 10, "fail_safe": 8},
    "enable_cost_layer": False,
    "enable_fail_safe": True,
    "fail_safe_countdown": 60,
    "dialog_regrow_seconds": 60,
    "poll_seconds": 5,
    "hud_pos": None,
    "dock_edge": "right",
    "pinned": False,
    "collapse_lock_below": 30,
}


# ---------------------------------------------------------------- 电源读取

class _SYSTEM_POWER_STATUS(ctypes.Structure):
    # 这四个字段是 BYTE（无符号）。用 c_byte 会把 255 读成 -1，
    # 于是"未知电量"被当成 -1%，台式机上就会显示「-1%」。
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_uint),
        ("BatteryFullLifeTime", ctypes.c_uint),
    ]


UNKNOWN = 255
NO_TIME = 0xFFFFFFFF


def read_power():
    """返回 {ac, pct, secs, charging, has_battery}，读不到返回 None。"""
    st = _SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
        return None

    has_battery = not bool(st.BatteryFlag & 128)      # 128 = No system battery
    pct = None if st.BatteryLifePercent == UNKNOWN else int(st.BatteryLifePercent)
    if not has_battery:
        pct = None                                     # 台式机：没有电量可言
    if pct is not None and not (0 <= pct <= 100):
        pct = None                                     # 防御：越界一律当未知

    secs = None if st.BatteryLifeTime == NO_TIME else int(st.BatteryLifeTime)
    if secs is not None and (secs <= 0 or secs > 48 * 3600):
        secs = None

    return {
        "ac": st.ACLineStatus == 1,
        "pct": pct,
        "secs": secs,
        "charging": bool(st.BatteryFlag & 8) and has_battery,
        "has_battery": has_battery,
    }


def hibernate():
    """优先用 SetSuspendState 休眠，失败退回 shutdown /h。返回是否成功。"""
    try:
        if ctypes.windll.powrprof.SetSuspendState(1, 1, 0):
            return True
    except Exception:
        pass
    try:
        r = subprocess.run(["shutdown", "/h"], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def acquire_single_instance(name="BatteryGuardian.SingleInstance"):
    """GUI 只允许跑一个实例。

    两个实例会各自弹确认框、各自倒计时休眠，而且各自算各自的剩余时间——
    屏幕上就会出现两个互相打架的数字。返回的句柄需要一直持有。
    """
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.CreateMutexW(None, False, "Local\\" + name)
        err = ctypes.get_last_error()
        if not handle:
            return None
        if err == 183:          # ERROR_ALREADY_EXISTS
            k32.CloseHandle(handle)
            return None
        return handle
    except Exception:
        return None


def show_already_running():
    """已经有实例在跑时的提示：3.5 秒自动关，不需要用户点。"""
    try:
        r = tk.Tk()
        r.withdraw()
        win = tk.Toplevel(r)
        win.title(APP_NAME)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.configure(bg=C["surface"])
        if os.path.exists(ICON_PATH):
            try:
                win.iconbitmap(ICON_PATH)
            except tk.TclError:
                pass

        tk.Label(win, text=f"{APP_NAME} 已经在运行了",
                 bg=C["surface"], fg=C["text"],
                 font=(FAMILY, 12, "bold")).pack(padx=30, pady=(22, 6))
        tk.Label(win, text="看屏幕右上角的浮条，右键它可以退出。",
                 bg=C["surface"], fg=C["muted"],
                 font=(FAMILY, 10)).pack(padx=30, pady=(0, 22))

        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")

        r.after(3500, r.destroy)
        r.mainloop()
    except Exception:
        pass


# ---------------------------------------------------------------- 配置与统计

def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(fallback, dict):
            merged = dict(fallback)
            merged.update(data)
            if "thresholds" in fallback:
                th = dict(fallback["thresholds"])
                th.update(data.get("thresholds") or {})
                merged["thresholds"] = th
            return merged
        return data
    except Exception:
        return fallback


def _save_json(path, data):
    try:
        _ensure_dirs()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log(f"保存失败 {path}: {e}")


def load_config():
    # 必须给副本：配置文件不存在时若直接返回模块级 DEFAULTS，
    # App 里对 cfg 的写入会污染全局默认值，后面 default_config() 都会跟着变。
    return _sanitize_config(_load_json(CFG_PATH, default_config()))


def default_stats():
    return {
        "streak_days": 0,
        "last_clean_date": None,
        "dirty_today": None,
        "incidents": 0,
        "kwh": 0.0,
        "wear": 0.0,
        "today": {"date": str(date.today()), "kwh": 0.0, "wear": 0.0},
        "first_seen": str(date.today()),
    }


def default_config():
    return json.loads(json.dumps(DEFAULTS))


def _as_num(v, default, lo=None, hi=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:                      # NaN
        return default
    if lo is not None:
        f = max(lo, f)
    if hi is not None:
        f = min(hi, f)
    return f


def _as_bool(v, default):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(v, (int, float)):
        return bool(v)
    return default


def _sanitize_config(cfg):
    """把手改坏的 config 拉回可用范围。

    config.json 是文档里让用户直接编辑的，写错一个类型就会在启动时崩，
    或者在 poll_seconds=0 时变成死循环。这里统一兜住。
    """
    try:
        cfg["capacity_wh"] = _as_num(cfg.get("capacity_wh"), 60.0, 1.0, 1000.0)
        cfg["kwh_price"] = _as_num(cfg.get("kwh_price"), 0.60, 0.0, 100.0)
        cfg["battery_price"] = _as_num(cfg.get("battery_price"), 400.0, 0.0, 100000.0)
        cfg["battery_cycles"] = _as_num(cfg.get("battery_cycles"), 600.0, 1.0, 100000.0)
        # 下限 1 秒：0 会让 after(0) 变成忙等，CPU 直接跑满
        cfg["poll_seconds"] = _as_num(cfg.get("poll_seconds"), 5.0, 1.0, 600.0)
        cfg["fail_safe_countdown"] = int(_as_num(cfg.get("fail_safe_countdown"), 60, 5, 600))
        cfg["dialog_regrow_seconds"] = _as_num(cfg.get("dialog_regrow_seconds"), 60.0, 5.0, 3600.0)
        cfg["collapse_lock_below"] = int(_as_num(cfg.get("collapse_lock_below"), 30, 0, 100))
        cfg["enable_cost_layer"] = _as_bool(cfg.get("enable_cost_layer"), False)
        cfg["enable_fail_safe"] = _as_bool(cfg.get("enable_fail_safe"), True)

        if cfg.get("dock_edge") not in (None, "left", "right"):
            cfg["dock_edge"] = "right"

        pos = cfg.get("hud_pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            try:
                cfg["hud_pos"] = [int(pos[0]), int(pos[1])]
            except (TypeError, ValueError):
                cfg["hud_pos"] = None
        else:
            cfg["hud_pos"] = None

        th = cfg.get("thresholds")
        if not isinstance(th, dict):
            th = {}
        cfg["thresholds"] = {
            k: int(_as_num(th.get(k), dv, 0, 100))
            for k, dv in DEFAULTS["thresholds"].items()
        }
        return cfg
    except Exception:
        return default_config()


def load_stats():
    st = _load_json(STATS_PATH, default_stats())
    if (st.get("today") or {}).get("date") != str(date.today()):
        st["today"] = {"date": str(date.today()), "kwh": 0.0, "wear": 0.0}
    return st


def log(msg):
    try:
        _ensure_dirs()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 放电速率估算

# 两个观察窗口：短的反映"你现在在干什么"，长的反映"你平时怎么用"。
# 笔记本负载差 3-5 倍是常态，单点估值必然飘，所以给区间而不是假精度。
RATE_SHORT_S = 600     # 最近 10 分钟
RATE_LONG_S = 3600     # 最近 60 分钟
RATE_MIN_SPAN_S = 300  # 至少观察 5 分钟

# 电量只有整数百分比，1% 的量化误差足以把速率算飞：
# 60 分钟只掉 1%，算出来就是"还能撑 43 小时"。所以要求窗口内至少掉这么多才算数。
RATE_SHORT_MIN_DROP = 2.0
RATE_LONG_MIN_DROP = 3.0


class RateEstimator:
    """用实际电量下降速度估算剩余时间，比系统的 BatteryLifeTime 更贴近真实。"""

    def __init__(self, window_s=RATE_LONG_S, min_span_s=RATE_MIN_SPAN_S):
        self.samples = deque()
        self.window_s = window_s
        self.min_span_s = min_span_s

    def add(self, t, pct, ac):
        if ac or pct is None:
            self.samples.clear()
            return
        self.samples.append((t, pct))
        while self.samples and t - self.samples[0][0] > self.window_s:
            self.samples.popleft()

    def per_min(self, window_s=None, min_drop=0.0):
        """返回指定窗口内的平均放电速率（%/分钟）。

        样本不足、或者窗口内降幅小于 min_drop（量化噪声）时返回 None。
        """
        if len(self.samples) < 2:
            return None
        t1, p1 = self.samples[-1]
        span_limit = window_s if window_s else self.window_s
        cutoff = t1 - span_limit

        t0 = p0 = None
        for t, p in self.samples:
            if t >= cutoff:
                t0, p0 = t, p
                break
        if t0 is None or t0 == t1:
            return None

        span = t1 - t0
        if span < self.min_span_s:
            return None
        drop = p0 - p1
        if drop < min_drop:
            return None
        return drop / (span / 60.0)


# ---------------------------------------------------------------- 电量账本

class Ledger:
    def __init__(self, cfg, stats):
        self.cfg = cfg
        self.stats = stats
        self.prev_pct = None

    def tick(self, pct, ac):
        if pct is None:
            return
        prev = self.prev_pct
        self.prev_pct = pct
        if ac or prev is None or pct >= prev:
            return
        d = prev - pct
        if d <= 0:
            return
        kwh = self.cfg["capacity_wh"] / 1000.0 * d / 100.0
        deep = 1.5 if pct < 10 else 1.0
        wear = d / 100.0 * deep

        self.stats["kwh"] += kwh
        self.stats["wear"] += wear
        t = self.stats.get("today") or {}
        if t.get("date") != str(date.today()):
            t = {"date": str(date.today()), "kwh": 0.0, "wear": 0.0}
            self.stats["today"] = t
        t["kwh"] += kwh
        t["wear"] += wear

    def money_saved(self, kwh):
        return kwh * self.cfg["kwh_price"]

    def battery_cost(self, wear):
        return wear * (self.cfg["battery_price"] / max(1.0, self.cfg["battery_cycles"]))

    def summary(self):
        kwh = self.stats["kwh"]
        wear = self.stats["wear"]
        saved = self.money_saved(kwh)
        cost = self.battery_cost(wear)
        ratio = (cost / saved) if saved > 1e-9 else None
        t = self.stats.get("today") or {}
        return {
            "kwh": kwh,
            "saved": saved,
            "cost": cost,
            "ratio": ratio,
            "today_saved": self.money_saved(t.get("kwh", 0.0)),
            "today_cost": self.battery_cost(t.get("wear", 0.0)),
        }


# ---------------------------------------------------------------- 等级判定

def level_for(pct, ac, cfg):
    if ac:
        return "ac"
    if pct is None:
        return "calm"
    th = cfg["thresholds"]
    if pct <= th["fail_safe"]:
        return "fail_safe"
    if pct <= th["dialog"]:
        return "dialog"
    if pct <= th["nudge"]:
        return "nudge"
    if pct <= th["hint"]:
        return "hint"
    return "calm"


def level_style(level):
    """返回 (背景色, 主文字色, 强调色, 是否脉冲)"""
    return {
        "ac": (C["green_bg"], C["green"], C["green"], False),
        "calm": (C["surface"], C["text"], C["green"], False),
        "hint": (C["amber_bg"], C["amber"], C["amber"], False),
        "nudge": (C["amber_bg2"], C["amber"], C["amber"], False),
        "dialog": (C["red_bg"], C["red"], C["red"], True),
        "fail_safe": (C["red_bg2"], C["red"], C["red"], True),
    }.get(level, (C["surface"], C["text"], C["green"], False))


# ---------------------------------------------------------------- 主应用

class App:
    def __init__(self, root, cfg, stats, demo_pct=None):
        self.root = root
        self.cfg = cfg
        self.stats = stats
        self.demo_pct = demo_pct
        self.demo = demo_pct is not None
        self.rate = RateEstimator()
        self.ledger = Ledger(cfg, stats)

        # 高 DPI：字体按点数自动放大，但像素尺寸必须自己缩放
        try:
            self.S = max(1.0, root.winfo_fpixels("1i") / 96.0)
        except Exception:
            self.S = 1.0

        if demo_pct is not None:
            self.power = {"ac": False, "pct": demo_pct, "secs": None,
                          "charging": False, "has_battery": True}
        else:
            self.power = read_power() or {
                "ac": True, "pct": None, "secs": None,
                "charging": False, "has_battery": True}

        self.level = "calm"
        self.muted_until = 0.0
        self.dialog = None
        self.dialog_grow = 0
        self.dialog_next_push = 0.0
        self.dialog_ignored = 0
        self.failsafe_win = None
        self.failsafe_deadline = 0.0
        self.ledger_win = None
        self.about_win = None
        self.banner = None
        self.pulse_on = False
        self._esc_timer = None
        self._save_timer = 0.0
        self._incident_flag = False

        # 吸附 / 收起状态
        self.dock_edge = cfg.get("dock_edge", "right")
        self.pinned = bool(cfg.get("pinned", False))
        self.collapsed = False
        self.free_x = None
        self._animating = False
        self._anim_gen = 0
        self._anim_target = None
        self._suppress_geom = False
        self._last_hover = 0.0
        # 启动后先让人看清楚几秒，别一上来就藏起来
        self._grace_until = time.time() + 5.0
        self._alive = True

        self.f_pct = tkfont.Font(family=FAMILY, size=12, weight="bold")
        self.f_txt = tkfont.Font(family=FAMILY, size=10)
        self.f_sm = tkfont.Font(family=FAMILY, size=9)
        self.f_title = tkfont.Font(family=FAMILY, size=13, weight="bold")
        self.f_big = tkfont.Font(family=FAMILY, size=26, weight="bold")

        self._build_hud()
        self._pulse_loop()
        self._hover_loop()
        self.tick()

    def px(self, v):
        return int(round(v * self.S))

    # ------------------------------------------------------------ 浮条

    def _build_hud(self):
        self.hud = tk.Toplevel(self.root)
        self.hud.overrideredirect(True)
        self.hud.attributes("-topmost", True)

        self.key_ok = False
        try:
            self.hud.wm_attributes("-transparentcolor", C["key"])
            self.key_ok = True
        except tk.TclError:
            self.key_ok = False

        self.hud_h = self.px(48)
        self.canvas = tk.Canvas(
            self.hud, height=self.hud_h,
            bg=C["key"] if self.key_ok else C["surface"],
            highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        pos = self.cfg.get("hud_pos")
        if pos and isinstance(pos, (list, tuple)) and len(pos) == 2:
            self.free_x, self.hud_y = int(pos[0]), int(pos[1])
        else:
            self.free_x = self.px(28)
            self.hud_y = self.px(28)

        self.hud_w = self.px(300)
        self.canvas.configure(width=self.hud_w)
        self._sync_geometry()

        self.canvas.bind("<Button-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Double-Button-1>", lambda e: self.open_ledger())
        self.canvas.bind("<Button-3>", self._popup_menu)

        self.menu = tk.Menu(self.hud, tearoff=0)
        self.menu.add_command(label="电费账本", command=self.open_ledger)
        self.menu.add_separator()
        self.menu.add_command(label="安静 30 分钟", command=lambda: self.mute(30))
        self.menu.add_command(label="安静 1 小时", command=lambda: self.mute(60))
        self.menu.add_command(label="安静到明早", command=lambda: self.mute(480))
        self.menu.add_separator()
        self.menu.add_command(label=f"关于 {APP_NAME}", command=self.open_about)
        self.menu.add_command(label="退出", command=self.quit)

        self._drag_off = (0, 0)
        self._dragged = False

    def _bar_w(self):
        return self.px(6)

    def _sync_geometry(self):
        """按「停靠边 / 收起状态」算出实际位置。动画与预绘制期间不动窗口。"""
        if self._animating or self._suppress_geom:
            return
        sw = self.hud.winfo_screenwidth()
        if self.collapsed:
            self.hud_w = self._bar_w()
            self.hud_x = 0 if self.dock_edge == "left" else sw - self.hud_w
        elif self.dock_edge == "right":
            self.hud_x = sw - self.hud_w
        elif self.dock_edge == "left":
            self.hud_x = 0
        else:
            self.hud_x = self.free_x
        self.hud_x = max(0, min(self.hud_x, sw - self.hud_w))
        self.hud.geometry(f"{self.hud_w}x{self.hud_h}+{self.hud_x}+{self.hud_y}")

    def _drag_start(self, e):
        self._drag_off = (e.x_root - self.hud_x, e.y_root - self.hud_y)
        self._dragged = False

    def _drag_move(self, e):
        if not self._dragged:
            self._dragged = True
            if self.collapsed:
                self.collapsed = False
                self.render_hud()
            self.dock_edge = None
        self.hud_x = e.x_root - self._drag_off[0]
        self.hud_y = max(0, e.y_root - self._drag_off[1])
        self.hud.geometry(f"{self.hud_w}x{self.hud_h}+{self.hud_x}+{self.hud_y}")

    def _drag_end(self, e):
        if not self._dragged:
            self._on_click()
            return
        sw = self.hud.winfo_screenwidth()
        snap = self.px(28)
        if self.hud_x <= snap:
            self.dock_edge = "left"
        elif self.hud_x + self.hud_w >= sw - snap:
            self.dock_edge = "right"
        else:
            self.dock_edge = None
            self.free_x = self.hud_x
            self.pinned = False
        self.collapsed = False
        self._sync_geometry()
        self.render_hud()
        self._save_cfg()

    def _on_click(self):
        """点击切换「固定展开」；收起状态下点击直接展开并钉住。"""
        if self.dock_edge is None:
            return
        self.pinned = not self.pinned
        if self.pinned:
            self.expand()
        else:
            self._last_hover = 0.0
        self._save_cfg()

    def _save_cfg(self):
        self.cfg["dock_edge"] = self.dock_edge
        self.cfg["pinned"] = self.pinned
        self.cfg["hud_pos"] = [self.free_x if self.free_x is not None else self.hud_x,
                               self.hud_y]
        _save_json(CFG_PATH, self.cfg)

    def _can_collapse(self):
        """收起与「常驻可见」是冲突的：只有不缺电的时候才允许藏起来。"""
        if self.dock_edge is None or self.pinned:
            return False
        if self.dialog is not None or self.failsafe_win is not None or self.banner is not None:
            return False
        if self.power.get("ac"):
            return True
        pct = self.power.get("pct")
        if pct is None:
            return True
        return pct > int(self.cfg.get("collapse_lock_below", 30))

    def collapse(self):
        if self.collapsed or self._anim_target == "collapsed":
            return
        if not self._can_collapse():
            return
        self._slide(expanding=False)

    def expand(self):
        if self.dock_edge is None:
            return
        if not self.collapsed and self._anim_target != "collapsed":
            return
        if self._anim_target == "expanded":
            return
        self._slide(expanding=True)

    def _slide(self, expanding):
        """滑出 / 滑回。

        用「代数」而不是「是否正在动画」来判断：新的滑动直接接管旧的，
        旧动画的后续帧发现代数变了就自己退出。

        同时按「目标状态」去重：hover 循环每 120ms 会调一次 collapse()，
        而动画要 135ms。如果每次都重启动画，就永远走不完最后一帧，
        _animating 会卡在 True 再也不收起。已经在滑向同一目标就直接忽略。
        """
        if not self._alive:
            self._animating = False
            return

        target = "expanded" if expanding else "collapsed"
        if self._animating and self._anim_target == target:
            return
        self._anim_target = target

        sw = self.hud.winfo_screenwidth()
        bar = self._bar_w()

        if expanding:
            self.collapsed = False
            self._suppress_geom = True
            self.render_hud()
            self._suppress_geom = False

        full = self.hud_w
        self._anim_gen += 1
        gen = self._anim_gen
        self._animating = True

        if self.dock_edge == "right":
            x_from = sw - bar if expanding else sw - full
            x_to = sw - full if expanding else sw - bar
        else:
            x_from = -(full - bar) if expanding else 0
            x_to = 0 if expanding else -(full - bar)

        steps = 9

        def step(i):
            if gen != self._anim_gen or not self._alive:
                return
            try:
                if i > steps:
                    self._animating = False
                    self._anim_target = None
                    self.collapsed = not expanding
                    self.render_hud()
                    return
                t = i / float(steps)
                e = 1 - (1 - t) ** 3
                self.hud_x = int(round(x_from + (x_to - x_from) * e))
                self.hud.geometry(f"{full}x{self.hud_h}+{self.hud_x}+{self.hud_y}")
                self.hud.after(15, lambda: step(i + 1))
            except tk.TclError:
                self._animating = False
                self._anim_target = None

        try:
            self.hud.geometry(f"{full}x{self.hud_h}+{x_from}+{self.hud_y}")
        except tk.TclError:
            self._animating = False
            self._anim_target = None
            return
        step(1)

    def _pointer_over(self, mx, my):
        x, y, w, h = self.hud_x, self.hud_y, self.hud_w, self.hud_h
        m = self.px(6)
        if self.collapsed:
            # 6px 太细，贴边时用一条更宽的感应带
            sw = self.hud.winfo_screenwidth()
            band = self.px(30)
            if not (y - m <= my <= y + h + m):
                return False
            if self.dock_edge == "right":
                return mx >= sw - band
            if self.dock_edge == "left":
                return mx <= band
            return False
        return (x - m <= mx <= x + w + m) and (y - m <= my <= y + h + m)

    def _hover_loop(self):
        if not self._alive:
            return
        try:
            mx, my = self.hud.winfo_pointerxy()
            over = self._pointer_over(mx, my)
            now = time.time()
            if self.collapsed:
                if over:
                    self.expand()
            elif over:
                self._last_hover = now
            elif (self.dock_edge is not None and not self.pinned
                  and now > self._grace_until
                  and now - self._last_hover > 1.5
                  and self._can_collapse()):
                self.collapse()
        except tk.TclError:
            return
        self.root.after(120, self._hover_loop)

    def _popup_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _round_rect(self, x1, y1, x2, y2, rtl, rtr, rbr, rbl, **kw):
        pts = [
            x1 + rtl, y1, x2 - rtr, y1, x2, y1, x2, y1 + rtr,
            x2, y2 - rbr, x2, y2, x2 - rbr, y2, x1 + rbl, y2,
            x1, y2, x1, y2 - rbl, x1, y1 + rtl, x1, y1,
        ]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _battery_glyph(self, x, cy, pct, color, ac):
        w, h = self.px(24), self.px(12)
        y1, y2 = cy - h // 2, cy + h // 2
        self.canvas.create_rectangle(x, y1, x + w, y2, outline=color, width=self.px(1.2))
        self.canvas.create_rectangle(x + w + self.px(1.5), cy - self.px(3),
                                     x + w + self.px(3.5), cy + self.px(3),
                                     outline=color, fill=color, width=0)
        if pct is not None and pct > 0:
            pad = self.px(2.5)
            inner = int((w - 2 * pad) * min(100, pct) / 100.0)
            if inner > 0:
                self.canvas.create_rectangle(
                    x + pad, y1 + pad, x + pad + inner, y2 - pad,
                    outline=color, fill=color, width=0)
        if ac:
            self.canvas.create_line(x + w * 0.35, y2 + self.px(1),
                                    x + w * 0.62, y1 - self.px(1),
                                    fill=color, width=self.px(1.6))

    def _render_collapsed(self):
        cv = self.canvas
        cv.delete("all")
        w, h = self._bar_w(), self.hud_h
        self.hud_w = w
        cv.configure(width=w, height=h)
        _, _, accent, _ = level_style(self.level)
        r = self.px(3)
        radii = (r, 0, 0, r) if self.dock_edge == "right" else (0, r, r, 0)
        self._round_rect(0, 0, w, h, *radii, fill=accent, outline=accent)
        self._sync_geometry()

    def render_hud(self):
        if self._animating:
            return
        if self.collapsed:
            self._render_collapsed()
            return

        bg, fg, accent, pulse = level_style(self.level)
        if pulse and self.pulse_on:
            bg = C["red_bg2"] if self.level == "dialog" else "#F2B0B0"

        cv = self.canvas
        cv.delete("all")

        pct = self.power["pct"]
        if not self.power.get("has_battery", True):
            pct_txt = "无电池"
        elif pct is not None:
            pct_txt = f"{pct}%"
        else:
            pct_txt = "—"
        sub = self.sub_text()
        chip = self.chip_text()

        w_pct = self.f_pct.measure(pct_txt)
        w_sub = self.f_txt.measure(sub)
        w_chip = self.f_sm.measure(chip)

        pad_x = self.px(15)
        gap = self.px(8)
        gw = self.px(26)

        x = pad_x
        glyph_x = x
        x += gw + gap
        pct_x = x
        x += w_pct + gap
        sub_x = x
        x += w_sub
        if chip:
            x += self.px(11)
            dot_x = x
            x += self.px(9)
            chip_x = x
            x += w_chip
        else:
            dot_x = chip_x = None
        total = x + pad_x

        if abs(total - self.hud_w) > 1:
            self.hud_w = total
            cv.configure(width=self.hud_w)

        # 无条件重新贴边：宽度没变但屏幕/停靠边变了（换分辨率、插拔外接屏）时，
        # 只在宽度变化里调用会让位置停在旧值，浮条可能留在屏幕外。
        self._sync_geometry()

        cy = self.hud_h // 2
        r = self.px(13)
        if self.dock_edge == "right":
            radii = (r, 0, 0, r)
        elif self.dock_edge == "left":
            radii = (0, r, r, 0)
        else:
            radii = (r, r, r, r)
        self._round_rect(self.px(1), self.px(1),
                         self.hud_w - self.px(1), self.hud_h - self.px(1),
                         *radii, fill=bg, outline=C["border"], width=1)

        # 没有电池的机器上别画闪电，否则像"在给空电池充电"
        show_bolt = self.power["ac"] and self.power.get("has_battery", True)
        self._battery_glyph(glyph_x, cy, pct, accent, show_bolt)
        cv.create_text(pct_x, cy, text=pct_txt, anchor="w", fill=accent, font=self.f_pct)
        cv.create_text(sub_x, cy, text=sub, anchor="w", fill=fg, font=self.f_txt)
        if chip:
            cv.create_text(dot_x, cy, text="·", anchor="w", fill=C["faint"], font=self.f_sm)
            cv.create_text(chip_x, cy, text=chip, anchor="w", fill=C["muted"], font=self.f_sm)

    def _pulse_loop(self):
        if not self._alive:
            return
        if level_style(self.level)[3]:
            self.pulse_on = not self.pulse_on
            try:
                self.render_hud()
            except tk.TclError:
                return
        self.root.after(760, self._pulse_loop)

    def sub_text(self):
        pct = self.power["pct"]
        if not self.power.get("has_battery", True):
            return "已接电" if self.power["ac"] else "未接电"
        if self.power["ac"]:
            return "充电中" if self.power["charging"] else "已接电"
        if pct is None:
            return "电量未知"
        if self.level == "fail_safe":
            return "马上休眠"
        if self.level == "dialog":
            return "现在插"
        if self.level == "nudge":
            return "建议现在接电"
        lo, hi, src = self.estimate()
        if lo is None:
            return "估算中"
        txt = self.fmt_duration(lo, hi)
        # 系统估值和自己测的不是一回事，得标出来，否则两个实例看起来在互相打架
        return ("系统" + txt) if src == "system" else txt

    def chip_text(self):
        # 没有电池的机器上，"连续 N 天没断电"没有意义
        if not self.power.get("has_battery", True):
            return ""
        d = self.stats.get("streak_days", 0)
        return f"连续 {d} 天" if d > 0 else "从今天开始"

    def estimate(self):
        """剩余时间区间。

        返回 (lo_min, hi_min, source)：
          measured —— 用最近 10 分钟和 1 小时的实测放电速率分别外推
          system   —— 样本还不够，退回 Windows 自己的 BatteryLifeTime
          None     —— 什么都没有
        """
        pct = self.power["pct"]
        if pct is None:
            return None, None, None

        rates = [r for r in (self.rate.per_min(RATE_SHORT_S, RATE_SHORT_MIN_DROP),
                             self.rate.per_min(RATE_LONG_S, RATE_LONG_MIN_DROP))
                 if r and r > 0]
        if rates:
            ts = sorted(pct / r for r in rates)
            return ts[0], ts[-1], "measured"

        secs = self.power["secs"]
        if secs:
            m = secs / 60.0
            return m, m, "system"
        return None, None, None

    def fmt_duration(self, lo, hi):
        """把分钟区间写成「约 25–40 分钟」或「约 1.8–2.9 小时」。"""
        if hi < 60:
            if hi - lo >= 5:
                return f"约 {int(round(lo))}–{int(round(hi))} 分钟"
            return f"约 {int(round((lo + hi) / 2))} 分钟"
        lo_h, hi_h = lo / 60.0, hi / 60.0
        if hi_h - lo_h >= 0.4:
            return f"约 {lo_h:.1f}–{hi_h:.1f} 小时"
        return f"约 {hi_h:.1f} 小时"

    def minutes_left(self):
        lo, hi, _ = self.estimate()
        if lo is None:
            return None
        return max(0, int(round((lo + hi) / 2)))

    # ------------------------------------------------------------ 主循环

    def tick(self):
        if not self._alive:
            return
        if self.demo_pct is not None:
            p = {"ac": False, "pct": self.demo_pct, "secs": None,
                 "charging": False, "has_battery": True}
        else:
            p = read_power() or self.power
        self.power = p

        now = time.time()
        self.rate.add(now, p["pct"], p["ac"])
        self.ledger.tick(p["pct"], p["ac"])

        self.level = level_for(p["pct"], p["ac"], self.cfg)

        if p["ac"]:
            self._close_dialog()
            self._close_failsafe()
            self._hide_banner()
            self.dialog_ignored = 0
            self._incident_flag = False
            self._mark_clean_day()
        else:
            self._handle_escalation(now)

        # 进入「需要被看见」的档位，强制滑出并锁住
        if self.collapsed and not self._can_collapse():
            self.expand()

        self.render_hud()

        if now - self._save_timer > 20:
            self._save_timer = now
            _save_json(STATS_PATH, self.stats)

        self.root.after(int(self.cfg["poll_seconds"] * 1000), self.tick)

    def _handle_escalation(self, now):
        pct = self.power["pct"]
        if pct is None or now < self.muted_until:
            return
        th = self.cfg["thresholds"]

        # L6 保底层
        if self.level == "fail_safe":
            if not self._incident_flag:
                self._incident_flag = True
                if not self.demo:
                    self.stats["incidents"] = self.stats.get("incidents", 0) + 1
                    self.stats["dirty_today"] = str(date.today())
                    self.stats["streak_days"] = 0
                    _save_json(STATS_PATH, self.stats)
                    log(f"记录一次断电事件，累计 {self.stats['incidents']} 次")
            if self.cfg.get("enable_fail_safe") and not self.demo:
                if self.failsafe_win is None:
                    self._open_failsafe()
            return

        # L3 说谎检测对话框
        if self.level == "dialog":
            if self.dialog is None:
                self._open_dialog()
            elif now >= self.dialog_next_push:
                self._push_dialog()

        # L4 即时代价：用一条横幅真的占掉屏幕顶部，默认关闭
        if self.cfg.get("enable_cost_layer") and pct <= th["cost"]:
            if self.banner is None:
                self._show_banner()

    # ------------------------------------------------------------ 说谎检测对话框

    def _open_dialog(self):
        self.dialog = tk.Toplevel(self.root)
        self.dialog.title("该插电了")
        self.dialog.attributes("-topmost", True)
        self.dialog.resizable(False, False)
        self.dialog.configure(bg=C["surface"])
        self._set_icon(self.dialog)
        self.dialog.protocol("WM_DELETE_WINDOW", self._on_claim_plugged)
        self.dialog.bind("<KeyPress-Escape>", self._esc_down)
        self.dialog.bind("<KeyRelease-Escape>", self._esc_up)

        self.dialog_grow = 0
        self._render_dialog()
        self.dialog_next_push = time.time() + self.cfg["dialog_regrow_seconds"]
        self.dialog.focus_force()
        self.dialog_ignored += 1
        log(f"打开确认框 pct={self.power['pct']} 第 {self.dialog_ignored} 次")

    def _render_dialog(self):
        for child in self.dialog.winfo_children():
            child.destroy()

        extra = self.px(30) * self.dialog_grow
        inner_w = self.px(360) + extra
        pad_x, pad_y = self.px(24), self.px(20) + self.px(8) * self.dialog_grow

        wrap = tk.Frame(self.dialog, bg=C["surface"])
        wrap.pack(fill="both", expand=True, padx=pad_x, pady=pad_y)

        tk.Label(wrap, text="该插电了", bg=C["surface"], fg=C["text"],
                 font=self.f_title, anchor="w").pack(fill="x")

        pct = self.power["pct"]
        m = self.minutes_left()
        if m is not None:
            body = f"还剩 {m} 分钟。按你现在的用法，撑不到下一次插电。"
        else:
            body = f"电量 {pct}%。现在插电是顺手，再等一会儿就是打断。"
        tk.Label(wrap, text=body, bg=C["surface"], fg=C["muted"],
                 font=self.f_txt, anchor="w", justify="left",
                 wraplength=inner_w).pack(fill="x", pady=(self.px(8), self.px(16)))

        self.btn = tk.Label(
            wrap, text="已插电 —— 点击会真的读取充电状态",
            bg=C["surface_soft"], fg=C["text"], font=self.f_txt,
            bd=0, padx=self.px(12), pady=self.px(11), cursor="hand2",
        )
        self.btn.pack(fill="x")
        self.btn.bind("<Button-1>", lambda e: self._on_claim_plugged())
        self.btn.bind("<Enter>", lambda e: self.btn.configure(bg="#E9E7DF"))
        self.btn.bind("<Leave>", lambda e: self.btn.configure(bg=C["surface_soft"]))

        self.tip = tk.Label(
            wrap, text="没有「稍后再说」。没插电的话，这个窗口会再大一圈。",
            bg=C["surface"], fg=C["faint"], font=self.f_sm,
            anchor="w", justify="left", wraplength=inner_w)
        self.tip.pack(fill="x", pady=(self.px(10), 0))

        self._fit(self.dialog, wrap, pad_x, pad_y, inner_w + 2 * pad_x)

    def _fit(self, win, wrap, pad_x, pad_y, min_w=None):
        """按内容实际尺寸撑开窗口，并处理标题栏/边框占位。"""
        win.update_idletasks()
        dw = max(0, win.winfo_rootx() - win.winfo_x())
        dh = max(0, win.winfo_rooty() - win.winfo_y())
        w = wrap.winfo_reqwidth() + 2 * pad_x + dw
        h = wrap.winfo_reqheight() + 2 * pad_y + dh
        if min_w:
            w = max(w, min_w)
        self._center(win, w, h)

    def _set_icon(self, win):
        if os.path.exists(ICON_PATH):
            try:
                win.iconbitmap(ICON_PATH)
            except tk.TclError:
                pass

    def _on_claim_plugged(self):
        if self.demo_pct is not None:
            p = {"ac": False}
        else:
            p = read_power()
        if p and p.get("ac"):
            self._close_dialog()
            self._mark_clean_day()
            self.render_hud()
            return
        self._shake()
        try:
            self.tip.configure(text="检测到还没插电。插上电源后这个窗口会自己消失。",
                               fg=C["red"])
            self.btn.configure(text="还没插上 —— 再去试一次",
                               bg=C["red_bg"], fg=C["red"])
        except tk.TclError:
            pass
        self.dialog.after(2600, self._restore_btn)

    def _restore_btn(self):
        if self.dialog and self.dialog.winfo_exists():
            try:
                self.btn.configure(text="已插电 —— 点击会真的读取充电状态",
                                   bg=C["surface_soft"], fg=C["text"])
            except tk.TclError:
                pass

    def _push_dialog(self):
        if not self.dialog or not self.dialog.winfo_exists():
            return
        self.dialog_next_push = time.time() + self.cfg["dialog_regrow_seconds"]
        if self.dialog_grow < 3:
            self.dialog_grow += 1
            self._render_dialog()
        else:
            self._shake()
        self.dialog.deiconify()
        self.dialog.lift()
        self.dialog.attributes("-topmost", True)

    def _shake(self):
        if not self.dialog or not self.dialog.winfo_exists():
            return
        x, y = self.dialog.winfo_x(), self.dialog.winfo_y()
        amp = self.px(10)

        def step(i):
            if not self.dialog or not self.dialog.winfo_exists():
                return
            if i >= 6:
                self.dialog.geometry(f"+{x}+{y}")
                return
            dx = amp if i % 2 == 0 else -amp
            self.dialog.geometry(f"+{x + dx}+{y}")
            self.dialog.after(40, lambda: step(i + 1))

        step(0)

    def _close_dialog(self):
        if self.dialog is not None:
            try:
                self.dialog.destroy()
            except tk.TclError:
                pass
        self.dialog = None
        self.dialog_grow = 0

    def _esc_down(self, e):
        self._esc_timer = self.dialog.after(1500, self._esc_fire)

    def _esc_up(self, e):
        if self._esc_timer:
            try:
                self.dialog.after_cancel(self._esc_timer)
            except Exception:
                pass
            self._esc_timer = None

    def _esc_fire(self):
        # 逃生口：真的没法插电的时候，允许安静 30 分钟
        self.mute(30)
        log("用户长按 Esc，安静 30 分钟")

    # ------------------------------------------------------------ 保底层

    def _open_failsafe(self):
        cd = int(self.cfg.get("fail_safe_countdown", 60))
        self.failsafe_deadline = time.time() + cd
        self.failsafe_win = tk.Toplevel(self.root)
        self.failsafe_win.title("即将休眠")
        self.failsafe_win.attributes("-topmost", True)
        self.failsafe_win.resizable(False, False)
        self.failsafe_win.configure(bg=C["red_bg"])
        self._set_icon(self.failsafe_win)
        self.failsafe_win.protocol("WM_DELETE_WINDOW", lambda: None)

        wrap = tk.Frame(self.failsafe_win, bg=C["red_bg"])
        pad_x, pad_y = self.px(24), self.px(20)
        wrap.pack(fill="both", expand=True, padx=pad_x, pady=pad_y)

        tk.Label(wrap, text="电量低于安全线", bg=C["red_bg"], fg=C["red"],
                 font=self.f_title, anchor="w").pack(fill="x")
        self.fs_num = tk.Label(wrap, text=str(cd), bg=C["red_bg"], fg=C["red"],
                               font=self.f_big, anchor="w")
        self.fs_num.pack(fill="x", pady=(self.px(4), 0))
        tk.Label(wrap, text="秒后自动休眠。插上电源就会取消。",
                 bg=C["red_bg"], fg=C["red"], font=self.f_txt,
                 anchor="w").pack(fill="x")
        tk.Label(wrap, text="休眠不是关机 —— 内存写进硬盘，醒来还是原样。",
                 bg=C["red_bg"], fg="#7A4A4A", font=self.f_sm,
                 anchor="w", justify="left",
                 wraplength=self.px(320)).pack(fill="x", pady=(self.px(10), 0))

        self._fit(self.failsafe_win, wrap, pad_x, pad_y, self.px(340))
        self._fs_tick()

    def _fs_tick(self):
        if self.failsafe_win is None:
            return
        left = int(self.failsafe_deadline - time.time())
        if left <= 0:
            self._do_hibernate()
            return
        try:
            self.fs_num.configure(text=str(left))
        except tk.TclError:
            return
        self.failsafe_win.after(1000, self._fs_tick)

    def _do_hibernate(self):
        if self.demo:
            log("演示模式：跳过真实休眠")
            self._close_failsafe()
            return
        log("触发保底层：自动休眠")
        self._close_failsafe()
        hibernate()

    def _close_failsafe(self):
        if self.failsafe_win is not None:
            try:
                self.failsafe_win.destroy()
            except tk.TclError:
                pass
        self.failsafe_win = None

    # ------------------------------------------------------------ 即时代价横幅

    def _show_banner(self):
        if self.banner is not None:
            return
        win = tk.Toplevel(self.root)
        self.banner = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=C["red_bg2"])
        sw = win.winfo_screenwidth()
        h = self.px(46)
        win.geometry(f"{sw}x{h}+0+0")
        m = self.minutes_left()
        extra = f"，约 {m} 分钟后进入休眠" if m is not None else ""
        tk.Label(
            win, text=f"电量 {self.power['pct']}%{extra} —— 插上电源，这条横幅立刻消失",
            bg=C["red_bg2"], fg=C["red"], font=self.f_txt,
        ).pack(expand=True)

    def _hide_banner(self):
        if self.banner is not None:
            try:
                self.banner.destroy()
            except tk.TclError:
                pass
        self.banner = None

    # ------------------------------------------------------------ 关于

    def open_about(self):
        if self.about_win is not None and self.about_win.winfo_exists():
            self.about_win.lift()
            return
        win = tk.Toplevel(self.root)
        self.about_win = win
        win.title(f"关于 {APP_NAME}")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.configure(bg=C["surface"])
        self._set_icon(win)

        inner_w = self.px(340)
        pad_x, pad_y = self.px(24), self.px(20)
        wrap = tk.Frame(win, bg=C["surface"])
        wrap.pack(fill="both", expand=True, padx=pad_x, pady=pad_y)

        tk.Label(wrap, text=APP_NAME, bg=C["surface"], fg=C["text"],
                 font=self.f_title, anchor="w").pack(fill="x")
        tk.Label(wrap, text=f"版本 {__version__}", bg=C["surface"], fg=C["faint"],
                 font=self.f_sm, anchor="w").pack(fill="x", pady=(self.px(4), 0))
        tk.Label(wrap, text="把「忘记插电」从一次可以被点掉的提醒，\n变成一次必须做的动作。",
                 bg=C["surface"], fg=C["muted"], font=self.f_txt, anchor="w",
                 justify="left").pack(fill="x", pady=(self.px(16), 0))
        tk.Label(wrap, text=f"{__license__} License · © {__year__} {__author__}",
                 bg=C["surface"], fg=C["text"], font=self.f_sm,
                 anchor="w").pack(fill="x", pady=(self.px(16), 0))
        tk.Label(wrap, text=f"github.com/{__author__}/{REPO_NAME}",
                 bg=C["surface"], fg=C["faint"], font=self.f_sm,
                 anchor="w").pack(fill="x", pady=(self.px(4), 0))

        self._fit(win, wrap, pad_x, pad_y, inner_w + 2 * pad_x)

    # ------------------------------------------------------------ 电费账本

    def open_ledger(self):
        if self.ledger_win is not None and self.ledger_win.winfo_exists():
            self.ledger_win.lift()
            return
        s = self.ledger.summary()
        win = tk.Toplevel(self.root)
        self.ledger_win = win
        win.title("电费账本")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.configure(bg=C["surface"])
        self._set_icon(win)

        inner_w = self.px(360)
        pad_x, pad_y = self.px(24), self.px(20)
        wrap = tk.Frame(win, bg=C["surface"])
        wrap.pack(fill="both", expand=True, padx=pad_x, pady=pad_y)

        tk.Label(wrap, text=f"连续 {self.stats.get('streak_days', 0)} 天没让自己断电",
                 bg=C["surface"], fg=C["text"], font=self.f_title,
                 anchor="w").pack(fill="x")

        rows = tk.Frame(wrap, bg=C["surface"])
        rows.pack(fill="x", pady=(self.px(14), 0))

        def row(label, value, color):
            f = tk.Frame(rows, bg=C["surface"])
            f.pack(fill="x", pady=self.px(3))
            tk.Label(f, text=label, bg=C["surface"], fg=C["muted"],
                     font=self.f_txt, anchor="w").pack(side="left")
            tk.Label(f, text=value, bg=C["surface"], fg=color,
                     font=self.f_txt, anchor="e").pack(side="right")

        tk.Label(rows, text="今天", bg=C["surface"], fg=C["faint"],
                 font=self.f_sm, anchor="w").pack(fill="x")
        row("用电池省下的电费", f"¥{s['today_saved']:.2f}", C["green"])
        row("同时消耗的电池寿命", f"¥{s['today_cost']:.2f}", C["red"])

        tk.Label(rows, text="累计", bg=C["surface"], fg=C["faint"],
                 font=self.f_sm, anchor="w").pack(fill="x", pady=(self.px(12), 0))
        row("用电池省下的电费", f"¥{s['saved']:.2f}", C["green"])
        row("同时消耗的电池寿命", f"¥{s['cost']:.2f}", C["red"])

        if s["ratio"] and s["ratio"] > 0:
            line = f"你省下的每 1 元电费，对应约 {s['ratio']:.0f} 元的电池折损。"
        else:
            line = "还没记录到放电。"
        tk.Label(wrap, text=line, bg=C["surface"], fg=C["text"],
                 font=self.f_txt, anchor="w", justify="left",
                 wraplength=inner_w).pack(fill="x", pady=(self.px(16), 0))
        tk.Label(wrap, text="按电池 ¥%.0f / %d 次循环、电费 ¥%.2f 每度估算。"
                            % (self.cfg["battery_price"], self.cfg["battery_cycles"],
                               self.cfg["kwh_price"]),
                 bg=C["surface"], fg=C["faint"], font=self.f_sm,
                 anchor="w", justify="left",
                 wraplength=inner_w).pack(fill="x", pady=(self.px(6), 0))

        tk.Label(wrap, text="剩余时间 = 电量 ÷ 耗电速度。用最近 10 分钟和 1 小时各算一次，"
                            "两者差距大时显示区间——它反映的是「按你刚才的用法还能撑多久」，"
                            "不是承诺。刚启动时标「系统」的是 Windows 自己的估值。",
                 bg=C["surface"], fg=C["faint"], font=self.f_sm,
                 anchor="w", justify="left",
                 wraplength=inner_w).pack(fill="x", pady=(self.px(12), 0))

        self._fit(win, wrap, pad_x, pad_y, inner_w + 2 * pad_x)

    # ------------------------------------------------------------ 杂项

    def _center(self, win, w, h):
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")

    def mute(self, minutes):
        self.muted_until = time.time() + minutes * 60
        self._close_dialog()
        self._close_failsafe()
        self._hide_banner()

    def _mark_clean_day(self):
        today = str(date.today())
        if self.stats.get("dirty_today") == today:
            return
        if self.stats.get("last_clean_date") == today:
            return
        last = self.stats.get("last_clean_date")
        if last is None:
            self.stats["streak_days"] = 1
        else:
            try:
                d0 = datetime.strptime(last, "%Y-%m-%d").date()
                delta = (date.today() - d0).days
                self.stats["streak_days"] = (self.stats.get("streak_days", 0) + 1
                                             if delta == 1 else 1)
            except Exception:
                self.stats["streak_days"] = 1
        self.stats["last_clean_date"] = today

    def quit(self):
        self._alive = False
        _save_json(STATS_PATH, self.stats)
        _save_json(CFG_PATH, self.cfg)
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------- 自检

def self_test():
    # 用默认值而不是用户真实配置：改过阈值的话断言会假失败
    cfg = default_config()
    stats = default_stats()
    lg = Ledger(cfg, stats)

    lg.tick(80, False)
    lg.tick(79, False)
    s = lg.summary()
    assert s["kwh"] > 0, "放电量应大于 0"
    print(f"自检：放电 1% → 省 ¥{s['saved']:.4f} / 折损 ¥{s['cost']:.4f} "
          f"/ 比值 {s['ratio']:.1f}x")
    assert s["ratio"] > 5, "电池折损应远高于省下的电费"

    # 深度放电惩罚：同样掉 3%，落在 10% 以下的折损要是 1.5 倍
    deep_lg = Ledger(default_config(), default_stats())
    deep_lg.tick(12, False)
    deep_lg.tick(9, False)
    norm_lg = Ledger(default_config(), default_stats())
    norm_lg.tick(32, False)
    norm_lg.tick(29, False)
    ratio_wear = deep_lg.stats["wear"] / norm_lg.stats["wear"]
    assert abs(ratio_wear - 1.5) < 1e-9, f"深放电折损应为 1.5 倍，实际 {ratio_wear:.3f}"

    # 逃生口：mute() 必须真的把截止时间推到将来（否则用户没法让它安静）
    class _MuteStub:
        muted_until = 0.0

        def _close_dialog(self):
            pass

        def _close_failsafe(self):
            pass

        def _hide_banner(self):
            pass

    ms = _MuteStub()
    App.mute(ms, 30)
    assert ms.muted_until > time.time() + 29 * 60, \
        f"静音 30 分钟应把截止时间推到未来，实际 {ms.muted_until - time.time():.0f}s"

    for pct, expect in [(90, "calm"), (45, "hint"), (25, "nudge"),
                        (15, "dialog"), (5, "fail_safe")]:
        got = level_for(pct, False, cfg)
        assert got == expect, f"{pct}% 应为 {expect}，实际 {got}"
    assert level_for(5, True, cfg) == "ac", "插电时应为 ac"

    r = RateEstimator(min_span_s=60)
    r.add(0, 80, False)
    r.add(120, 78, False)
    assert abs(r.per_min() - 1.0) < 1e-6, "速率估算应为 1%/分钟"

    # 两个窗口要能分别反映「最近」和「平均」
    r2 = RateEstimator(min_span_s=60)
    r2.add(0, 100, False)          # t=0
    r2.add(2400, 90, False)        # 40 分钟掉了 10% -> 0.25%/min
    r2.add(3000, 85, False)        # 再 10 分钟掉 5%  -> 0.5%/min
    short = r2.per_min(RATE_SHORT_S)
    long_ = r2.per_min(RATE_LONG_S)
    assert abs(short - 0.5) < 1e-6, f"短窗口应为 0.5%/min，实际 {short}"
    assert abs(long_ - 0.3) < 1e-6, f"长窗口应为 0.3%/min，实际 {long_}"
    assert short > long_, "短窗口速率应快于长窗口"

    # 区间文案
    assert App.fmt_duration(None, 110, 172) == "约 1.8–2.9 小时", \
        App.fmt_duration(None, 110, 172)
    assert App.fmt_duration(None, 25, 40) == "约 25–40 分钟", \
        App.fmt_duration(None, 25, 40)
    assert App.fmt_duration(None, 90, 96) == "约 1.6 小时", \
        App.fmt_duration(None, 90, 96)

    # 台式机回归：从原始字节解析，255（未知）必须读成 255，不能是 -1。
    # 注意要用 from_buffer_copy 而不是给字段赋值——赋值测的是写入，测不出符号
    # （ctypes 赋值会回绕，改成 c_byte 后断言照样通过，是个假测试）。
    raw = bytes([1, 128, 255, 0]) + b"\xff" * 4 + b"\xff" * 4   # AC=1, Flag=128, Pct=255
    st = _SYSTEM_POWER_STATUS.from_buffer_copy(raw)
    assert st.BatteryLifePercent == 255, \
        f"应为 255，实际 {st.BatteryLifePercent}（字段符号错了）"
    assert st.BatteryFlag == 128, f"BatteryFlag 应为 128，实际 {st.BatteryFlag}"
    assert st.BatteryLifeTime == NO_TIME, f"LifeTime 应为 0xFFFFFFFF，实际 {st.BatteryLifeTime}"

    # 无电池机器上的文案
    stub = type("S", (), {
        "power": {"ac": True, "pct": None, "secs": None,
                  "charging": False, "has_battery": False},
        "stats": {"streak_days": 5},
    })()
    assert App.sub_text(stub) == "已接电", App.sub_text(stub)
    assert App.chip_text(stub) == "", App.chip_text(stub)

    # 配置净化：手改坏的 config 不能让程序崩，也不能变成忙等
    bad = {
        "poll_seconds": 0, "capacity_wh": "abc", "dock_edge": "top",
        "hud_pos": ["x", "y"], "thresholds": {"hint": "80", "nudge": None},
        "enable_fail_safe": "false", "fail_safe_countdown": -5,
    }
    fixed = _sanitize_config(dict(bad))
    assert fixed["poll_seconds"] >= 1, "poll_seconds 必须 >=1，否则 after(0) 忙等"
    assert fixed["capacity_wh"] == 60.0, fixed["capacity_wh"]
    assert fixed["dock_edge"] == "right", fixed["dock_edge"]
    assert fixed["hud_pos"] is None, fixed["hud_pos"]
    assert fixed["thresholds"]["hint"] == 80, fixed["thresholds"]
    assert fixed["thresholds"]["nudge"] == 30, fixed["thresholds"]
    assert fixed["enable_fail_safe"] is False, '字符串 "false" 不能被当成 True'
    assert fixed["fail_safe_countdown"] == 5, fixed["fail_safe_countdown"]

    # estimate()：噪声门槛必须真的生效，否则会算出"43% 还能撑 43 小时"
    def _est(power, samples):
        o = type("E", (), {})()
        o.power = power
        o.rate = RateEstimator()
        base = 100000.0
        for dt, p in samples:
            o.rate.add(base - dt, p, False)
        return o

    noisy = _est({"ac": False, "pct": 43, "secs": None},
                 [(3600, 44), (600, 43), (0, 43)])       # 60 分钟只掉 1%
    lo, hi, src = App.estimate(noisy)
    assert src is None and lo is None, f"1% 降幅应判为噪声，实际 src={src} lo={lo}"

    real = _est({"ac": False, "pct": 43, "secs": None},
                [(3600, 60), (600, 50), (0, 43)])        # 降幅够大
    lo, hi, src = App.estimate(real)
    assert src == "measured", f"应走实测，实际 {src}"
    assert lo < hi, f"区间应有宽度，实际 ({lo}, {hi})"

    sysonly = _est({"ac": False, "pct": 43, "secs": 600}, [])
    lo, hi, src = App.estimate(sysonly)
    assert src == "system" and abs(lo - 10.0) < 1e-6, f"应退回系统估值，实际 {src} {lo}"

    print("自检：等级判定、速率估算、区间文案、台式机无电池、配置净化、账本全部通过")


# ---------------------------------------------------------------- 入口

def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    # 打包成 --windowed 的 exe 后没有 stdout，命令行模式的输出会丢。
    # 这里把它重定向到 data/cli.log，方便在没有控制台的机器上排查。
    cli_mode = any(a in sys.argv for a in ("--once", "--self-test", "--where"))
    if FROZEN and cli_mode:
        try:
            _ensure_dirs()
            f = open(os.path.join(DATA_DIR, "cli.log"), "w", encoding="utf-8")
            sys.stdout = f
            sys.stderr = f
        except Exception:
            pass

    if "--self-test" in sys.argv:
        self_test()
        return

    if "--once" in sys.argv:
        p = read_power()
        cfg = load_config()
        print(json.dumps({
            "power": p,
            "level": level_for(p["pct"], p["ac"], cfg) if p else None,
        }, ensure_ascii=False, indent=2))
        return

    if "--where" in sys.argv:
        print(json.dumps({
            "frozen": FROZEN,
            "app_home": APP_HOME,
            "res_dir": RES_DIR,
            "data_dir": DATA_DIR,
            "icon": ICON_PATH,
            "icon_exists": os.path.exists(ICON_PATH),
        }, ensure_ascii=False, indent=2))
        return

    demo_pct = None
    panel = None
    for i, a in enumerate(sys.argv):
        if a == "--demo" and i + 1 < len(sys.argv):
            try:
                demo_pct = int(sys.argv[i + 1])
            except ValueError:
                print(f"--demo 需要 0-100 的整数，收到 {sys.argv[i + 1]!r}")
                return
        if a == "--panel" and i + 1 < len(sys.argv):
            panel = sys.argv[i + 1]

    _ensure_dirs()

    # 单实例：--demo 用独立互斥名，方便正式版还在跑的时候截图
    mutex_name = "BatteryGuardian.SingleInstance"
    if demo_pct is not None:
        mutex_name += ".demo"
    mutex = acquire_single_instance(mutex_name)
    if mutex is None:
        show_already_running()
        return

    root = tk.Tk()
    root.withdraw()
    app = App(root, load_config(), load_stats(), demo_pct=demo_pct)
    if panel == "ledger":
        root.after(300, app.open_ledger)
    elif panel == "failsafe":
        root.after(300, app._open_failsafe)
    elif panel == "about":
        root.after(300, app.open_about)
    root.mainloop()


if __name__ == "__main__":
    main()
