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

import contextlib
import ctypes
import io
import json
import os
import re
import sys
import time
import subprocess
import tempfile
import threading
import tkinter as tk
import types
import tkinter.font as tkfont
from collections import deque
from datetime import date, datetime, timedelta

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
    # 提示音：只在"需要你真的做点什么"的两档响，别的地方保持安静
    "sound_enabled": True,
    "sound_on_dialog": True,       # L3 确认框弹出时
    "sound_on_failsafe": True,     # L6 休眠倒计时弹出时
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


def _clean_secs(secs):
    """清洗 BatteryLifeTime：0 / 负数 / 超过 48 小时都当未知。

    超过 48 小时基本是驱动在瞎报哨兵值，显示出来就是"还能撑 30 天"，
    比不显示更糟。抽成函数是为了能直接测——原来这段夹在 read_power 里，
    随机变异测试发现把 `secs is not None` 改成 `is None` 没有任何测试会红。
    """
    if secs is None:
        return None
    if secs <= 0 or secs > 48 * 3600:
        return None
    return secs


_CAP_TAG_RE = {
    # 允许标签带属性（`<FullChargeCapacity unit="mWh">`）。当前 powercfg 不加属性，
    # 但正则会因为那个 `>` 前面多了空格而整段匹配不上，静默退回 None —— 属未来兼容风险。
    "FullChargeCapacity": re.compile(
        r"<FullChargeCapacity(?:\s[^>]*)?>\s*([0-9]+(?:\.[0-9]+)?)\s*</FullChargeCapacity>"),
    "DesignCapacity": re.compile(
        r"<DesignCapacity(?:\s[^>]*)?>\s*([0-9]+(?:\.[0-9]+)?)\s*</DesignCapacity>"),
}


def _capacity_from_report_xml(xml_text):
    """从 powercfg batteryreport 的 XML 里取电池容量（Wh）。

    优先 FullChargeCapacity（当前实际满充），退回 DesignCapacity（出厂设计）。
    账本算的是「这次放电实际放出多少电」，所以当前值更贴切。

    **多电池按段累加**：XML 里每块电池一个 `<Battery>` 段。只取第一个的话，
    两块 40 Wh 的机器会读成 40 Wh（少算一半），而 40 落在 5-300 Wh 的合理性
    区间里，校验根本拦不住——这种错会静默地把账本数字砍半。

    这里刻意**不用 xml.etree**：构建脚本为了减体积排除了整个 xml 包，
    在 exe 里 import 它会失败（而且实测会卡住自检）。字段就两个数字，
    正则足够，还省掉一个依赖。
    """
    if not xml_text:
        return None

    # 没有 <Battery> 段就退化成全文搜索（某些版本或精简报告可能没有分段）
    # 分段正则也要容忍属性：`<Battery id="BAT1">`。否则两块 40 Wh 会退化成 40 Wh
    # （只匹配到第一段之后的裸标签全都不中，最后走全文回退只取第一个）。
    # 当前 powercfg 输出的是裸 `<Battery>`，但同一个文件里容量标签已经支持属性了，
    # 两处写法不一致本身就是隐患。
    blocks = re.findall(r"<Battery(?:\s[^>]*)?>(.*?)</Battery>",
                        xml_text, re.S) or [xml_text]

    def pick(tag, text):
        m = _CAP_TAG_RE[tag].search(text)
        if not m:
            return None
        try:
            v = float(m.group(1))
        except ValueError:
            return None
        return v if v > 0 else None

    total_mwh = 0.0
    for blk in blocks:
        mwh = pick("FullChargeCapacity", blk) or pick("DesignCapacity", blk)
        # 单块先各自校验：一个离谱的分段不该把整台机器的容量算飞，
        # 也不该因为它让另外几块被一起丢掉。
        if mwh and CELL_MWH_MIN <= mwh <= CELL_MWH_MAX:
            total_mwh += mwh
    if total_mwh <= 0:
        return None

    wh = total_mwh / 1000.0
    # 总量再兜一次，比单块宽：三块 100 Wh 的移动工作站也要能读出来
    if not (PACK_WH_MIN <= wh <= PACK_WH_MAX):
        return None
    return round(wh, 1)


# powercfg 的超时。单独提成常量是为了能被断言——它是「读不到容量就静默退回
# 默认 60Wh」这条退化路径的唯一闸门，改成 0 会让账本永远算错，而原先没有任何测试会红。
POWERCAP_TIMEOUT = 25


def read_battery_capacity_wh(timeout=POWERCAP_TIMEOUT):
    """跑 powercfg 读本机电池容量，读不到返回 None。

    用 -xml 而不是默认的 HTML：HTML 是给人看的，本地化之后字段名会变。
    注意这台机器的 powercfg 只认连字符写法（-output / -xml），斜杠写法报「参数无效」。
    """
    fd, path = tempfile.mkstemp(suffix=".xml", prefix="bg-batt-")
    os.close(fd)
    try:
        os.remove(path)          # powercfg 自己会创建，先删掉免得它拒绝覆盖
    except OSError:
        pass
    try:
        subprocess.run(["powercfg", "-batteryreport", "-output", path, "-xml"],
                       capture_output=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return _capacity_from_report_xml(f.read())
    except Exception:
        return None
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _ac_from_status(v):
    """ACLineStatus：1 = 已接电，0 = 未接电，255 = 未知。

    `== 1` 改成 `!= 1` 会让插电状态整个反过来——但没有任何测试会红，
    因为测试从不经过 read_power。抽出来单独喂值。
    """
    return v == 1


def should_autofill_capacity(cfg, demo=False):
    """是否需要在后台读一次真实电池容量。

    只在用户没填过（还是默认 60 Wh）时才读——否则会把他手工填的值覆盖掉。
    这个判断原先内联在 __init__ 里，没有任何测试覆盖：去掉条件后整套测试全绿，
    但用户精心填的 capacity_wh 每次启动都会被 60 附近的实测值顶掉。
    """
    if demo:
        return False
    try:
        return abs(float(cfg.get("capacity_wh", 0)) - 60.0) < 1e-6
    except (TypeError, ValueError):
        return False


def clamp_tick_dt(now, last):
    """两次 tick 的间隔，上限 60 秒。

    防的是「休眠 / 挂起几小时后醒来，把整段时间一口气算进今天」——
    那会让账本的离电时长直接翻几倍，而账本正是这个工具唯一的反馈来源。
    """
    return min(60.0, max(0.0, now - last))


def _charging_from_flag(flag, has_battery):
    """充电中 = 标志位 8 且真的有电池。

    没有电池的台式机也常置位 8，不乘 has_battery 就会显示"充电中"。
    """
    return bool(flag & 8) and has_battery


def _clean_pct(pct):
    """电量百分比的越界防御：只接受 0-100 的整数，其余一律当未知。

    抽出来是因为随机变异把这里的 `not` 去掉后，**所有正常电量都会变成 None**，
    而没有任何测试会红——测试全都直接塞假的 power 字典，从不经过 read_power。
    """
    if pct is None:
        return None
    if not (0 <= pct <= 100):
        return None
    return int(pct)


def _secs_from_raw(raw):
    """把 GetSystemPowerStatus 的原始 BatteryLifeTime 转成秒数或 None。

    单独抽出来是因为随机变异把这里的 `== NO_TIME` 改成 `!=` 时，
    没有任何测试会红——而它会让所有正常的剩余时间变成 None。
    """
    return _clean_secs(None if raw == NO_TIME else int(raw))


def read_power():
    """返回 {ac, pct, secs, charging, has_battery}，读不到返回 None。"""
    st = _SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
        return None

    has_battery = not bool(st.BatteryFlag & 128)      # 128 = No system battery
    pct = None if st.BatteryLifePercent == UNKNOWN else int(st.BatteryLifePercent)
    if not has_battery:
        pct = None                                     # 台式机：没有电量可言
    pct = _clean_pct(pct)                              # 防御：越界一律当未知

    secs = _secs_from_raw(st.BatteryLifeTime)

    return {
        "ac": _ac_from_status(st.ACLineStatus),
        "pct": pct,
        "secs": secs,
        "charging": _charging_from_flag(st.BatteryFlag, has_battery),
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
        r = subprocess.run(["shutdown", "/h"], capture_output=True, timeout=10,
                           stdin=subprocess.DEVNULL)
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
        "days": {},                    # 每天离电秒数，用于周对比
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


def minutes_until_morning(now=None, hour=7):
    """到「明早」几点，返回分钟数。

    原来右键菜单写死 mute(480)，那是 8 小时，不是「明早」——
    凌晨 1 点用它是 9 点，晚上 11 点用它是第二天 7 点，纯属巧合。
    """
    now = datetime.now() if now is None else now
    today_target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    target = today_target if today_target > now else today_target + timedelta(days=1)
    return max(1, int(round((target - now).total_seconds() / 60.0)))


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
        # pinned 是唯一漏净化的下游字段：手写 "false" 会被 bool() 当成 True，
        # 浮条从此永远不收起，而且没有任何提示。
        cfg["pinned"] = _as_bool(cfg.get("pinned"), False)
        cfg["sound_enabled"] = _as_bool(cfg.get("sound_enabled"), True)
        cfg["sound_on_dialog"] = _as_bool(cfg.get("sound_on_dialog"), True)
        cfg["sound_on_failsafe"] = _as_bool(cfg.get("sound_on_failsafe"), True)

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
        # 阈值必须满足 hint >= nudge >= dialog >= cost >= fail_safe。
        # 只把每个值单独 clamp 到 0-100 是不够的：level_for() 是从高优先级往下判，
        # 一旦 fail_safe 被手改成 50，45% 就会落进保底层 → 高电量直接休眠。
        # README 明确鼓励用户手改 config.json，所以这是条真实可达的路径。
        ordered = {}
        prev = 100
        for k in ("hint", "nudge", "dialog", "cost", "fail_safe"):
            raw = th.get(k)
            v = int(_as_num(raw, DEFAULTS["thresholds"][k], 0, 100))
            clamped = min(v, prev)
            # 静默改写用户配置是最气人的那种 bug：他改了，以为生效了，其实没有
            if clamped != v and os.path.isdir(DATA_DIR):
                log(f"阈值 {k} 从 {v} 压到 {clamped}"
                    f"（必须满足 hint>=nudge>=dialog>=cost>=fail_safe）")
            ordered[k] = clamped
            prev = clamped
        cfg["thresholds"] = ordered
        return cfg
    except Exception:
        return default_config()


def load_stats():
    st = _load_json(STATS_PATH, default_stats())
    if (st.get("today") or {}).get("date") != str(date.today()):
        st["today"] = {"date": str(date.today()), "kwh": 0.0, "wear": 0.0}
    return st


MAX_HISTORY_DAYS = 21

# 连续多少次 tick 失败之后，界面上要说实话。
# 6 次 × 默认 5 秒 = 30 秒：短到用户还在这台机器前，长到不会被一次抖动触发。
TICK_FAIL_WARN = 6

# 连续多少次 TclError 之后彻底放弃重排。
# 这个只针对 TclError：它通常是「浮条窗口被外部销毁」这类不可恢复的状态，
# 再重排一万次也画不出来。60 次 × 5 秒 ≈ 5 分钟，够人反应过来。
# 读电量失败不走这条——那可能是临时的，不该 5 分钟就放弃。
TICK_TCL_GIVE_UP = 60

# 电池容量的合理区间（毫瓦时）。单块和总量分开校验：
# 单块拦「解析到别的数字」，总量拦「累加出一台电动车」。
CELL_MWH_MIN = 1_000        # 1 Wh
CELL_MWH_MAX = 200_000      # 200 Wh（市面最大笔记本电池约 100 Wh 的两倍余量）
PACK_WH_MIN = 5.0
PACK_WH_MAX = 400.0         # 多电池移动工作站

# `py -0p` 的超时。这个调用发生在 UI 线程上（点菜单 → 写注册表 → 拼命令），
# 给太大只会让界面在异常情况下白冻。
PY_LAUNCHER_TIMEOUT = 3


def record_on_battery(stats, seconds, today=None):
    """把「离电秒数」记到当天，并只保留最近 21 天。

    账本原来只会算钱，从不告诉用户「有没有变好」。记时长是为了做周对比。
    """
    if seconds is None or seconds <= 0:
        return stats
    today = today or date.today()
    days = stats.setdefault("days", {})
    key = str(today)
    days[key] = round(float(days.get(key, 0.0)) + float(seconds), 1)
    if len(days) > MAX_HISTORY_DAYS:
        for old in sorted(days)[:-MAX_HISTORY_DAYS]:
            days.pop(old, None)
    return stats


def weekly_on_battery(stats, today=None):
    """返回 (本周秒数, 上周秒数)。本周 = 最近 7 天（含今天），上周 = 再往前 7 天。"""
    today = today or date.today()
    days = stats.get("days") or {}
    this_w = last_w = 0.0
    for i in range(14):
        s = float(days.get(str(today - timedelta(days=i)), 0.0))
        if i < 7:
            this_w += s
        else:
            last_w += s
    return this_w, last_w


def fmt_hours(seconds):
    """秒数 → 「3.2 小时」/「40 分钟」/「—」。"""
    if not seconds or seconds <= 0:
        return "—"
    if seconds < 3600:
        return f"{int(round(seconds / 60))} 分钟"
    return f"{seconds / 3600.0:.1f} 小时"


def advance_streak(stats, today=None):
    """推进「连续没让自己断电」的天数。

    抽成模块级纯函数是为了能直接测：原来埋在 App 方法里，
    把 delta == 1 改成 delta <= 2（断一天也算连续）没有任何测试会红。
    """
    today = today or date.today()
    today_s = str(today)
    if stats.get("dirty_today") == today_s:
        return stats
    if stats.get("last_clean_date") == today_s:
        return stats

    last = stats.get("last_clean_date")
    if last is None:
        stats["streak_days"] = 1
    else:
        try:
            d0 = datetime.strptime(last, "%Y-%m-%d").date()
            delta = (today - d0).days
            stats["streak_days"] = (stats.get("streak_days", 0) + 1
                                    if delta == 1 else 1)
        except Exception:
            stats["streak_days"] = 1
    stats["last_clean_date"] = today_s
    return stats


# 只在两个"必须做点什么"的档位响，其余保持安静：
# 到处响等于没响，跟一开始那个被忽略的系统弹窗是一个道理。
SOUND_ALIASES = {
    "dialog": "SystemExclamation",   # 该插电了
    "failsafe": "SystemHand",        # 即将休眠，最急
}


def play_alert(kind):
    """播一声系统提示音，异步、失败静默。

    不能阻塞 UI（PlaySound 用 SND_ASYNC），也不能因为没声卡/非 Windows
    把主流程带崩——提示音是增强，不是依赖。
    """
    try:
        import winsound
        alias = SOUND_ALIASES.get(kind, "SystemAsterisk")
        winsound.PlaySound(alias, winsound.SND_ALIAS | winsound.SND_ASYNC)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 开机自启

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "BatteryGuardian"


def _parse_py_launcher_output(text):
    """从 `py -0p` 的输出里挑出默认解释器路径。

    输出形如：

        -V:3.14 *        C:\\...\\Python314\\python.exe
        -V:3.13          C:\\...\\Python313\\python.exe

    带 `*` 的是默认版本。**没有 `*` 就返回 None，不猜**——猜错的代价是
    把开机自启指向一个非默认版本，用户下次开机用的解释器跟他预期的不一样。
    抽成纯函数是为了能直接喂文本测，不用真跑 py.exe。
    """
    for line in text.splitlines():
        if "*" not in line:
            continue
        m = re.search(r"([A-Za-z]:\\[^\r\n]*?python\.exe)", line, re.I)
        if m:
            return m.group(1).strip()
    return None


def _resolve_py_launcher(exe=None):
    """`py.exe` 是启动器（控制台程序），问它真实解释器在哪。

    用 `py -0p` 列出已安装版本，带 `*` 的那行是默认版本。
    解析失败一律返回 None —— 调用方保持原样（退回 py.exe，最坏只是闪一下黑框），
    绝不能因为解析出错把自启指向一个不存在的路径，那样开机根本起不来。

    timeout 只给 3 秒：这个调用发生在 UI 线程上（点菜单 → 写注册表 → 拼命令），
    `py -0p` 本身是瞬时的，给 10 秒只会让界面在异常情况下白冻 10 秒。
    """
    exe = exe or sys.executable
    try:
        r = subprocess.run([exe, "-0p"], capture_output=True,
                           timeout=PY_LAUNCHER_TIMEOUT,
                           stdin=subprocess.DEVNULL)
        text = (r.stdout or b"").decode("utf-8", "ignore")
    except Exception:
        return None

    cand = _parse_py_launcher_output(text)
    # 存在性检查留在这一层：纯函数只负责"输出里写了什么"，
    # "那个文件还在不在"是环境事实，两者混在一起就没法单独测解析了。
    return cand if cand and os.path.exists(cand) else None


def _autostart_command(exe=None):
    """开机自启要执行的命令。

    exe 版直接指向 exe；源码版指向 pythonw + 脚本（用 pythonw 才不会闪黑框）。
    路径必须加引号——这台机器的目录里就有空格。

    `exe` 参数是为了测试注入：自检不该去改 `sys.executable`——
    在 PyInstaller onefile 下那个值是运行时自己用的，改它风险远大于收益。
    """
    exe = exe or sys.executable
    if FROZEN:
        return f'"{exe}"'
    # `py -3 xxx.py` 启动时 sys.executable 是 py.exe（控制台程序），
    # 它旁边没有 pythonw.exe，直接拼出来的命令会在登录时闪一个黑框。
    if os.path.basename(exe).lower() == "py.exe":
        real = _resolve_py_launcher(exe)
        if real:
            exe = real
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = exe
    script = os.path.join(APP_HOME, "battery_guardian.py")
    return f'"{pyw}" "{script}"'


def autostart_enabled():
    """读注册表判断是否已开启。读不到一律当没开。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
            try:
                val, _ = winreg.QueryValueEx(k, AUTOSTART_NAME)
                return bool(val)
            except FileNotFoundError:
                return False
    except Exception:
        return False


def set_autostart(on):
    """写 / 删 HKCU 的 Run 项。返回是否成功。

    只用 HKCU，不碰 HKLM：不需要管理员权限，卸载时删掉值就干净了。
    """
    try:
        import winreg
        if on:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ,
                                  _autostart_command())
            log("已开启开机自启")
        else:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY,
                                0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, AUTOSTART_NAME)
            log("已关闭开机自启")
        return True
    except FileNotFoundError:
        # 关的时候值本来就不存在，算成功
        return True
    except Exception as e:
        log(f"设置开机自启失败：{e!r}")
        return False


def log(msg):
    try:
        _ensure_dirs()
        # 轮转：开机自启之后这个文件是长期 append 的，不切会一直涨
        try:
            if os.path.getsize(LOG_PATH) > 1_000_000:
                bak = LOG_PATH + ".1"
                if os.path.exists(bak):
                    os.remove(bak)
                os.replace(LOG_PATH, bak)
        except OSError:
            pass
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

# 鼠标离开浮条多久之后自动收起。抽成常量是为了测试能直接引用它，
# 而不是在测试里也写一个 1.5——那样改坏代码测试会跟着一起错。
HOVER_COLLAPSE_DELAY_S = 1.5
HOVER_POLL_MS = 120
HOVER_GRACE_S = 5.0      # 启动后先别急着藏，让人看清楚


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
        self.fail_win = None
        self.pulse_on = False
        self._esc_timer = None
        self._save_timer = 0.0
        self._incident_flag = False
        # 休眠失败后不再重开倒计时。没有这个标记的话，失败 → 窗口关闭 →
        # 下一个 tick 又满足 failsafe_win is None → 重开 → 再响一声，
        # 变成「每 65 秒响一次、关不掉」的无限循环。
        self._hibernate_failed = False
        self._last_tick = time.time()
        # 连续 tick 失败次数：到 TICK_FAIL_WARN 就让浮条承认读不到
        self._tick_fails = 0
        # 其中连续 TclError 的单独计数：界面画不出来和电量读不到，
        # 对用户的含义不一样，放弃的门槛也不一样
        self._tick_tcl_fails = 0

        # 账本默认 60Wh 只是占位符，和真机容量能差 40%。用户没填过时后台读一次
        # 本机真实容量——放线程里，别让 2 秒的 powercfg 卡住启动。
        if should_autofill_capacity(self.cfg, self.demo):
            threading.Thread(target=self._autofill_capacity, daemon=True).start()

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
        self._grace_until = time.time() + HOVER_GRACE_S
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
        # 不再写死 480 分钟（8 小时），改成真的算到次日 07:00
        self.menu.add_command(label="安静到明早", command=self._mute_until_morning)
        self._menu_morning_idx = self.menu.index("end")
        self.menu.add_separator()
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        self.menu.add_checkbutton(label="开机自启", variable=self.autostart_var,
                                  command=self._toggle_autostart)
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
        # demo 也不能写真实 config：拖一下浮条就把用户真实的窗口位置改掉了。
        # 这是第三条写盘路径——tick 和 quit 守住了，这条原来漏了。
        if self.demo:
            return
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

    def _hover_decision(self, mx, my, now):
        """鼠标位置 + 时间 → 该做什么：'expand' / 'collapse' / None。

        抽成独立判定是为了能测。selftest 里没法真的移动鼠标，
        而「鼠标移上去滑出、移开自动收起」是这个工具的核心交互——
        原来整个 hover 路径零覆盖，把感应带改成 0 像素测试照样全绿。
        """
        over = self._pointer_over(mx, my)
        if self.collapsed:
            return "expand" if over else None
        if over:
            self._last_hover = now
            return None
        if (self.dock_edge is not None and not self.pinned
                and now > self._grace_until
                and now - self._last_hover > HOVER_COLLAPSE_DELAY_S
                and self._can_collapse()):
            return "collapse"
        return None

    def _hover_loop(self):
        if not self._alive:
            return
        try:
            mx, my = self.hud.winfo_pointerxy()
            action = self._hover_decision(mx, my, time.time())
            if action == "expand":
                self.expand()
            elif action == "collapse":
                self.collapse()
        except tk.TclError:
            return
        except Exception as e:
            # 只捕 TclError 是不够的：_can_collapse / collapse 抛任何别的异常
            # 都会跳过下面的 after 重排，循环从此死掉——浮条静默地再也不收起，
            # 而且没有日志，用户只会觉得"它今天不收起了"。
            log(f"hover 循环异常：{e!r}")
        self.root.after(HOVER_POLL_MS, self._hover_loop)

    def _mute_until_morning(self):
        self.mute(minutes_until_morning())

    def _toggle_autostart(self):
        """右键菜单里的开机自启开关。

        演示模式不碰注册表——截图、调界面不该在系统里留下东西。
        失败时把勾选状态回滚成真实状态，别让菜单骗人。
        """
        want = bool(self.autostart_var.get())
        if self.demo:
            self.autostart_var.set(autostart_enabled())
            log("演示模式：不修改开机自启")
            return
        if not set_autostart(want):
            self.autostart_var.set(autostart_enabled())

    def _autofill_capacity(self):
        """后台读本机电池容量，写进 cfg。

        只改内存里的 cfg，不在这里落盘——落盘交给 quit() / 拖动时的 _save_cfg，
        避免和 tick 的 stats 写入抢同一个文件。

        注意：线程里只允许改**已存在**的键。给 cfg 新增键的话，
        主线程 json.dump 迭代时可能抛 RuntimeError，被外层 except 吞掉，
        变成「配置静默不落盘」——用户以为改了，重启就没了。
        """
        try:
            wh = read_battery_capacity_wh()
        except Exception:
            wh = None
        if wh:
            self.cfg["capacity_wh"] = wh
            log(f"已自动读取本机电池容量 {wh} Wh（默认值是 60 Wh）")

    def _morning_menu_state(self):
        """「安静到明早」在低电量时应置灰。

        那个组合基本等于亲手安排一次硬关机，值得挡一下。
        抽成纯判定是为了能测——原来埋在 _popup_menu 里，弹出菜单要真的
        tk_popup，自动化里没法稳定触发，等于零覆盖。
        """
        pct = self.power.get("pct")
        low = (pct is not None and not self.power.get("ac")
               and pct <= int(self.cfg["thresholds"]["nudge"]))
        if low:
            return "disabled", "安静到明早（电量已低，不建议）"
        return "normal", "安静到明早"

    def _popup_menu(self, e):
        # 每次弹出都重读真实状态。autostart_var 只在建菜单时初始化一次，
        # 如果用户在别处改过注册表（或另一个实例改的），菜单会一直说着旧话。
        try:
            self.autostart_var.set(autostart_enabled())
        except Exception:
            pass
        # 低电量时把「安静到明早」置灰
        try:
            state, label = self._morning_menu_state()
            self.menu.entryconfigure(
                self._menu_morning_idx,
                state=tk.DISABLED if state == "disabled" else tk.NORMAL,
                label=label)
        except Exception:
            pass
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
        try:
            if level_style(self.level)[3]:
                self.pulse_on = not self.pulse_on
                self.render_hud()
        except tk.TclError:
            return
        except Exception as e:
            log(f"脉冲循环异常：{e!r}")
        self.root.after(760, self._pulse_loop)

    def sub_text(self):
        # 连续失败够久就别再装作在读数——浮条还"活着"但数据已经陈旧，
        # 这是比直接死掉更糟的状态：用户以为它在工作。
        # 两种失败分开说：界面画不出来 ≠ 电量读不到，后者才该怪电池/驱动。
        # getattr 是为了兼容自检里那些只带 power/level 的 stub。
        if getattr(self, "_tick_tcl_fails", 0) >= TICK_FAIL_WARN:
            return "界面刷新失败"
        if getattr(self, "_tick_fails", 0) >= TICK_FAIL_WARN:
            return "电量读取失败"
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
        # 分钟档取中值，小时档原来取上界，两档口径不一致（小时档偏乐观）
        return f"约 {(lo + hi) / 2 / 60.0:.1f} 小时"

    def minutes_left(self):
        lo, hi, _ = self.estimate()
        if lo is None:
            return None
        return max(0, int(round((lo + hi) / 2)))

    # ------------------------------------------------------------ 主循环

    def tick(self):
        """主循环：读电量 → 判等级 → 升级处理 → 重绘 → 排下一次。

        这里必须兜住异常，原因不是「防御式编程」，而是一个具体的失效模式：
        最后那句 after 是循环唯一的续命动作，任何异常跳过它，循环就永久停止——
        浮条不再刷新、L3 确认框不再弹、**L6 保底层不再触发**，而且没有任何提示，
        工具退化成一根装饰条。开机自启让进程变成 7×24 长期运行之后，
        「跑几天撞上一次」就从理论风险变成了迟早发生：
        render_hud 的 TclError（RDP / 注销 / 显示器变化）、read_power 的 ctypes 异常、
        stats 结构被外部改坏，都在这条路径上。
        """
        if not self._alive:
            return
        try:
            self._tick_once()
        except tk.TclError as e:
            # TclError 分两种，不能一刀切 return：
            #  - 瞬时（RDP 重连、显示器切换）→ 应该重排，让它自己恢复；
            #  - 真销毁（退出/注销）→ mainloop 已经返回，没人再调 tick；
            #    quit() 也会先置 _alive=False。**不要指望 after 来报错**——
            #    实测 root.destroy() 之后 after() 是静默成功的，不会抛。
            # 上一版对 TclError 直接 return，恰好漏掉了 docstring 里列为
            # 「加这个保护的主要动机」的那个场景。
            self._tick_tcl_fails += 1
            self._note_tick_failure(e)
        except Exception as e:
            self._tick_tcl_fails = 0
            self._note_tick_failure(e)
        else:
            self._tick_tcl_fails = 0
            self._note_tick_ok()
        if self._alive:
            # 浮条被外部销毁后，after 会一直成功、tick 一直失败——
            # 一次/5 秒不烧 CPU，但永远不会停。到 GIVE_UP 就承认放弃。
            if self._tick_tcl_fails >= TICK_TCL_GIVE_UP:
                log(f"tick 连续 TclError {self._tick_tcl_fails} 次，"
                    f"停止重试：浮条窗口可能已被销毁，需要重启程序")
                return
            try:
                self.root.after(int(self.cfg["poll_seconds"] * 1000), self.tick)
            except tk.TclError:
                pass

    def _note_tick_failure(self, err):
        """记一次 tick 失败。

        只在状态变化时写日志：每 tick 都记的话，read_power 持续失败会一天
        刷 1.7 万行（虽然 1MB 轮转兜得住，但真出事时日志反而没法看）。
        """
        self._tick_fails += 1
        if self._tick_fails == 1:
            log(f"tick 首次失败：{err!r}")
        elif self._tick_fails == TICK_FAIL_WARN:
            log(f"tick 已连续失败 {self._tick_fails} 次，浮条改显示「电量读取失败」")

    def _note_tick_ok(self):
        if self._tick_fails:
            log(f"tick 恢复正常（此前连续失败 {self._tick_fails} 次）")
            self._tick_fails = 0

    def _tick_once(self):
        if self.demo_pct is not None:
            p = {"ac": False, "pct": self.demo_pct, "secs": None,
                 "charging": False, "has_battery": True}
        else:
            p = read_power() or self.power
        self.power = p

        now = time.time()
        # 离电时长按两次 tick 的间隔累加，上限 60 秒（见 clamp_tick_dt）。
        dt = clamp_tick_dt(now, self._last_tick)
        self._last_tick = now
        self.rate.add(now, p["pct"], p["ac"])
        self.ledger.tick(p["pct"], p["ac"])
        # demo 守卫：假电量算出来的离电时长会污染真实统计。
        # 现在它「只是不落盘」，所以去掉守卫也只脏内存、不脏磁盘——但这是个地雷：
        # 一旦哪天 demo 开始写盘，就会重演第二轮那个「demo 覆盖真实 stats」的 bug。
        # 有测试专门钉这一条（selftest_ui 的 run_demo_no_persist）。
        if not self.demo and not p["ac"] and p["pct"] is not None:
            record_on_battery(self.stats, dt)

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

        # 演示模式不落盘：假电量算出来的账本会污染真实统计。
        # 之前 demo 版和正式版能双开（互斥名不同），两个进程都往同一个
        # stats.json 写，假数据还会覆盖真数据。
        if not self.demo and now - self._save_timer > 20:
            self._save_timer = now
            _save_json(STATS_PATH, self.stats)

    def _handle_escalation(self, now):
        pct = self.power["pct"]
        if pct is None:
            return
        th = self.cfg["thresholds"]

        # L6 保底层 —— 必须排在静音判断之前。
        # 静音是「别烦我」，不是「让机器死机」。原来这一层被静音一起短路，
        # 5% 时点一次「安静到明早」就等于亲手保证硬关机，
        # L6 存在的全部理由（让最坏结果不是硬关机）正好被逃生口关掉。
        if self.level == "fail_safe":
            if not self._incident_flag:
                self._incident_flag = True
                if not self.demo:
                    self.stats["incidents"] = self.stats.get("incidents", 0) + 1
                    self.stats["dirty_today"] = str(date.today())
                    self.stats["streak_days"] = 0
                    _save_json(STATS_PATH, self.stats)
                    log(f"记录一次断电事件，累计 {self.stats['incidents']} 次")
            # 休眠失败过就别再重开倒计时：重开只会变成无限响铃
            if (self.cfg.get("enable_fail_safe") and not self.demo
                    and not self._hibernate_failed):
                if self.failsafe_win is None:
                    self._open_failsafe()
            return

        # 静音只挡 L3 / L4 这类「打扰」，不挡保底层
        if now < self.muted_until:
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

    def _alert(self, kind):
        """按配置决定要不要响。

        静音（mute）不挡保底层的提示音，跟 L6 本身不受静音影响保持一致：
        静音是"别烦我"，不是"别提醒我要关机了"。
        """
        if not self.cfg.get("sound_enabled", True):
            return False
        if kind == "dialog" and not self.cfg.get("sound_on_dialog", True):
            return False
        if kind == "failsafe" and not self.cfg.get("sound_on_failsafe", True):
            return False
        return play_alert(kind)

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
        self._alert("dialog")

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
        self._alert("failsafe")
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
        ok = hibernate()
        if ok:
            return
        # 休眠失败（系统禁用了休眠 / 权限不足）时，原来什么都不做，
        # 于是下一个 tick 又满足 failsafe_win is None → 重开倒计时 → 再响一声，
        # 变成每约 65 秒响一次、且三条出口全堵死的无限循环。
        self._hibernate_failed = True
        log("休眠失败，停止重试（系统可能禁用了休眠）")
        self._show_hibernate_failed()

    def _show_hibernate_failed(self):
        """休眠失败后的提示窗：不响、不重试、允许关闭。

        这一屏是给用户自己动手的——工具已经无能为力，至少别让它变成噪音源。

        刻意用**中性底色 + 红字**，而不是保底层那种整片红底：
        那个红窗的意思是「马上要断电了，快插电」，这一屏的意思是「我失败了，
        你自己看着办」。两者用同一种视觉语言，会让人误以为保底层可以被关掉。
        """
        if self.fail_win is not None:
            return
        win = tk.Toplevel(self.root)
        self.fail_win = win
        win.title("自动休眠失败")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.configure(bg=C["surface_soft"])
        self._set_icon(win)
        win.protocol("WM_DELETE_WINDOW", self._close_fail_win)

        wrap = tk.Frame(win, bg=C["surface_soft"])
        pad_x, pad_y = self.px(24), self.px(20)
        wrap.pack(fill="both", expand=True, padx=pad_x, pady=pad_y)

        tk.Label(wrap, text="自动休眠失败", bg=C["surface_soft"], fg=C["red"],
                 font=self.f_title, anchor="w").pack(fill="x")
        tk.Label(wrap,
                 text="系统没有接受休眠请求（可能禁用了休眠）。\n"
                      "不会再重试，也不会再响。请自己保存工作并插电。",
                 bg=C["surface_soft"], fg=C["muted"], font=self.f_txt,
                 anchor="w", justify="left",
                 wraplength=self.px(340)).pack(fill="x", pady=(self.px(8), self.px(14)))

        btn = tk.Label(wrap, text="知道了", bg=C["surface_soft"], fg=C["text"],
                       font=self.f_txt, bd=0, padx=self.px(12), pady=self.px(9),
                       cursor="hand2")
        btn.pack(fill="x")
        btn.bind("<Button-1>", lambda e: self._close_fail_win())

        self._fit(win, wrap, pad_x, pad_y, self.px(340))

    def _close_fail_win(self):
        if self.fail_win is not None:
            try:
                self.fail_win.destroy()
            except tk.TclError:
                pass
        self.fail_win = None

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

        # 只算钱是不够的——还得告诉用户"有没有变好"
        wk, lw = weekly_on_battery(self.stats)
        tk.Label(rows, text="离电时长", bg=C["surface"], fg=C["faint"],
                 font=self.f_sm, anchor="w").pack(fill="x", pady=(self.px(12), 0))
        row("本周", fmt_hours(wk), C["text"])
        row("上周", fmt_hours(lw), C["muted"])
        if lw > 0 and wk > 0:
            pct = (wk - lw) / lw * 100.0
            row("变化", f"{'↓' if pct < 0 else '↑'}{abs(pct):.0f}%",
                C["green"] if pct < 0 else C["red"])

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
        """静音一段时间。

        只关「打扰」类窗口（确认框 / 横幅），**不关保底层**。
        原来这里会 _close_failsafe()，等于静音顺手把最后一道防线也关了。
        """
        self.muted_until = time.time() + minutes * 60
        self._close_dialog()
        self._hide_banner()
        log(f"静音 {minutes} 分钟（保底层仍然生效）")

    def _mark_clean_day(self):
        advance_streak(self.stats)

    def quit(self):
        self._alive = False
        # 演示模式不写盘：否则截个图就把真实统计和窗口位置改掉了
        if not self.demo:
            _save_json(STATS_PATH, self.stats)
            _save_json(CFG_PATH, self.cfg)
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------- 自检

def self_test():
    # 自检不能往真实 data/ 写东西（mute() 现在会写日志），统一重定向到临时目录。
    # 这条是硬要求：审查验收里「data/ 跑完仍为空」。
    global DATA_DIR, LOG_PATH
    _tmp = tempfile.mkdtemp(prefix="bg-selftest-")
    DATA_DIR = _tmp
    LOG_PATH = os.path.join(_tmp, "app.log")

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
        failsafe_win = None

        def __init__(self):
            self.closed = []

        def _close_dialog(self):
            self.closed.append("dialog")

        def _close_failsafe(self):
            self.closed.append("failsafe")

        def _hide_banner(self):
            self.closed.append("banner")

    ms = _MuteStub()
    App.mute(ms, 30)
    assert ms.muted_until > time.time() + 29 * 60, \
        f"静音 30 分钟应把截止时间推到未来，实际 {ms.muted_until - time.time():.0f}s"

    # P0-1：静音不能连保底层一起关掉。
    # 原来 mute() 里会 _close_failsafe()，等于「安静到明早」= 亲手保证硬关机。
    assert "failsafe" not in ms.closed, \
        f"静音不应关掉保底层，实际关闭了 {ms.closed}"
    assert "dialog" in ms.closed, "静音应该关掉确认框"

    # P0-1 的行为面：静音期间到 5%，保底层仍然要起来
    class _EscStub:
        """只保留 _handle_escalation 需要的字段，用来验证静音与保底层的优先级。

        属性要给全：顺序一旦被改坏，缺属性会以 AttributeError 崩掉，
        而不是干净地断言失败——那等于测试本身也坏了。
        """
        cfg = default_config()
        demo = False
        failsafe_win = None
        dialog = None
        _incident_flag = True          # 跳过计数落盘
        _hibernate_failed = False
        level = "fail_safe"
        power = {"ac": False, "pct": 5, "secs": None,
                 "charging": False, "has_battery": True}

        def __init__(self):
            self.muted_until = time.time() + 3600
            self.opened = 0
            self.dialogs = 0

        def _open_failsafe(self):
            self.opened += 1

        def _open_dialog(self):
            self.dialogs += 1

    es = _EscStub()
    App._handle_escalation(es, time.time())
    assert es.opened == 1, "静音期间到 5% 必须仍然弹出保底层，实际没有弹"

    # 休眠失败过之后不能再重开倒计时，否则就是无限响铃
    es_failed = _EscStub()
    es_failed._hibernate_failed = True
    App._handle_escalation(es_failed, time.time())
    assert es_failed.opened == 0, "休眠失败后不应重开保底层"

    # 反过来：静音期间 L3 确认框不该弹（静音本来就是为了别打扰）
    es2 = _EscStub()
    es2.level = "dialog"
    App._handle_escalation(es2, time.time())
    assert es2.dialogs == 0, "静音期间不该弹确认框"

    # 不静音时必须弹，证明上面那条不是因为别的原因没弹（否则是空断言）
    es3 = _EscStub()
    es3.level = "dialog"
    es3.muted_until = 0
    App._handle_escalation(es3, time.time())
    assert es3.dialogs == 1, "未静音时应当弹确认框"

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

    # P0-2：阈值必须保持 hint >= nudge >= dialog >= cost >= fail_safe。
    # 只把每个值单独 clamp 到 0-100 不够：fail_safe 手改成 50 时，
    # level_for() 从高优先级往下判，45% 会直接落进保底层 → 高电量自动休眠。
    bad_th = _sanitize_config({"thresholds": {"hint": 60, "nudge": 30,
                                              "dialog": 20, "cost": 10,
                                              "fail_safe": 50}})
    t = bad_th["thresholds"]
    assert t["hint"] >= t["nudge"] >= t["dialog"] >= t["cost"] >= t["fail_safe"], t
    assert level_for(45, False, bad_th) != "fail_safe", \
        f"阈值乱序不得让 45% 走保底层，实际 {level_for(45, False, bad_th)}"

    # P2-1：pinned 是唯一漏净化的下游字段，手写 "false" 会被 bool() 当成 True
    for raw in ("false", "off", "no", "0", 0):
        p = _sanitize_config({"pinned": raw})["pinned"]
        assert p is False, f"pinned={raw!r} 应净化成 False，实际 {p!r}"
    assert _sanitize_config({"pinned": "true"})["pinned"] is True

    # 连续天数：断一天必须从头计。delta == 1 改成 <= 2 会让断档也算连续。
    d0 = date(2026, 10, 30)
    st = {"streak_days": 5, "last_clean_date": str(d0 - timedelta(days=1))}
    advance_streak(st, d0)
    assert st["streak_days"] == 6, f"隔一天应接上，实际 {st['streak_days']}"

    st = {"streak_days": 5, "last_clean_date": str(d0 - timedelta(days=2))}
    advance_streak(st, d0)
    assert st["streak_days"] == 1, f"断一天必须重置为 1，实际 {st['streak_days']}"

    st = {"streak_days": 5, "last_clean_date": str(d0)}
    advance_streak(st, d0)
    assert st["streak_days"] == 5, "同一天重复调用不应累加"

    st = {"streak_days": 5, "last_clean_date": str(d0 - timedelta(days=1)),
          "dirty_today": str(d0)}
    advance_streak(st, d0)
    assert st["streak_days"] == 5, "当天断过电不应记连续"

    # 「安静到明早」原来写死 480 分钟（8 小时），不是明早
    m = minutes_until_morning(datetime(2026, 10, 30, 23, 0))
    assert abs(m - 480) <= 1, f"23:00 → 次日 7:00 应约 480 分钟，实际 {m}"
    m = minutes_until_morning(datetime(2026, 10, 30, 1, 0))
    assert abs(m - 360) <= 1, f"01:00 → 7:00 应约 360 分钟，实际 {m}"
    m = minutes_until_morning(datetime(2026, 10, 30, 6, 30))
    assert abs(m - 30) <= 1, f"06:30 → 7:00 应约 30 分钟，实际 {m}"

    # 时长文案：小时档要取中值，不能取上界（分钟档本来就是中值，两档口径要一致）
    assert App.fmt_duration(None, 100, 106) == "约 1.7 小时", \
        App.fmt_duration(None, 100, 106)

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

    # 随机变异抽样发现的盲区：把 `secs is not None` 改成 `is None`，
    # 哨兵值（0 / 负数 / 超过 48 小时）就会原样显示成"还能撑 30 天"。
    assert _clean_secs(None) is None
    assert _clean_secs(0) is None, "0 秒应视为未知"
    assert _clean_secs(-1) is None, "负数应视为未知"
    assert _clean_secs(48 * 3600 + 1) is None, "超过 48 小时应视为未知"
    assert _clean_secs(3600) == 3600, "正常值应原样保留"
    assert _clean_secs(48 * 3600) == 48 * 3600, "边界值应保留"

    # 原始值 → 秒数：哨兵必须变 None，正常值必须留着。
    # 这里 `== NO_TIME` 被改成 `!=` 时，所有正常剩余时间都会消失。
    assert _secs_from_raw(NO_TIME) is None, "0xFFFFFFFF 应视为未知"
    assert _secs_from_raw(3600) == 3600, "3600 秒应原样保留"
    assert _secs_from_raw(0) is None, "0 秒应视为未知"

    # 电量越界防御：`not` 被去掉时所有正常电量都会变成 None，
    # 而测试全都直接塞假 power 字典，从不经过 read_power，所以必须单独测这里
    assert _clean_pct(50) == 50, "正常电量应原样保留"
    assert _clean_pct(0) == 0, "0% 是合法值"
    assert _clean_pct(100) == 100, "100% 是合法值"
    assert _clean_pct(101) is None, "越界应视为未知"
    assert _clean_pct(-1) is None, "负数应视为未知"
    assert _clean_pct(None) is None

    # 充电标志必须乘 has_battery：台式机常置位 8，否则会显示"充电中"
    assert _charging_from_flag(8, True) is True
    assert _charging_from_flag(8, False) is False, "无电池时不能显示充电中"
    assert _charging_from_flag(0, True) is False

    # 保底层边界：8% 必须正好触发（<= 改成 < 会让 8% 这一档漏掉）
    assert level_for(8, False, cfg) == "fail_safe", "8% 必须触发保底层"
    assert level_for(9, False, cfg) != "fail_safe", "9% 不该触发保底层"
    assert level_for(20, False, cfg) == "dialog", "20% 必须触发确认框"

    # 插电时必须直接返回 ac，不能被阈值逻辑吞掉（or 改成 and 会让插电也走档位判断）
    assert level_for(50, True, cfg) == "ac", "插电时不论电量都应为 ac"
    assert level_for(None, True, cfg) == "ac"
    assert level_for(None, False, cfg) == "calm"

    # AC 状态解析：== 改成 != 会让插电整个反过来
    assert _ac_from_status(1) is True
    assert _ac_from_status(0) is False
    assert _ac_from_status(255) is False, "未知不能当成已接电"

    # _can_collapse 的两个前置分支（return False 改成 True 会让危险档位也能藏）
    def _cc(dock="right", pinned=False, ac=True):
        return type("C", (), {
            "dock_edge": dock, "pinned": pinned,
            "dialog": None, "failsafe_win": None, "banner": None,
            "power": {"ac": ac, "pct": 90},
            "cfg": cfg,
        })()
    assert App._can_collapse(_cc(dock=None)) is False, "未停靠必须禁止收起"
    assert App._can_collapse(_cc(pinned=True)) is False, "固定后必须禁止收起"
    assert App._can_collapse(_cc()) is True, "正常情况应允许收起"

    # 提示音：三个开关必须真的生效，且关掉确认框提示音不能连带关掉保底层
    _calls = []
    _orig_play = globals()["play_alert"]
    globals()["play_alert"] = lambda k: _calls.append(k) or True
    try:
        def _snd(**kw):
            return type("S", (), {"cfg": kw})()

        App._alert(_snd(sound_enabled=True, sound_on_dialog=True,
                        sound_on_failsafe=True), "dialog")
        assert _calls == ["dialog"], _calls

        App._alert(_snd(sound_enabled=False), "dialog")
        assert _calls == ["dialog"], "总开关关掉后不应再响"

        App._alert(_snd(sound_enabled=True, sound_on_dialog=False,
                        sound_on_failsafe=True), "dialog")
        assert _calls == ["dialog"], "单独关掉确认框提示音后不应响"

        App._alert(_snd(sound_enabled=True, sound_on_dialog=False,
                        sound_on_failsafe=True), "failsafe")
        assert _calls == ["dialog", "failsafe"], "关掉确认框提示音不应连带关掉保底层"
    finally:
        globals()["play_alert"] = _orig_play

    # 提示音的真实行为：音色别名 + 异步标志。改成同步会卡住 UI 循环，
    # 但没有任何测试会红——用假的 winsound 模块把调用参数钉死。
    _fake = types.ModuleType("winsound")
    _fake.SND_ALIAS = 0x10000
    _fake.SND_ASYNC = 0x1
    _got = []
    _fake.PlaySound = lambda name, flags: _got.append((name, flags))
    _saved_ws = sys.modules.get("winsound")
    sys.modules["winsound"] = _fake
    try:
        assert play_alert("dialog") is True
        assert play_alert("failsafe") is True
        assert _got[0] == ("SystemExclamation", 0x10001), _got[0]
        assert _got[1] == ("SystemHand", 0x10001), _got[1]
        assert _got[1][1] & _fake.SND_ASYNC, "必须异步播放，同步会卡住 UI"
    finally:
        if _saved_ws is None:
            sys.modules.pop("winsound", None)
        else:
            sys.modules["winsound"] = _saved_ws

    # --panel failsafe 必须强制演示模式，否则倒计时到 0 会真的休眠
    p1 = type("P", (), {"demo": False})()
    assert force_demo_for_panel(p1, "failsafe") is True
    assert p1.demo is True, "failsafe 面板必须强制演示模式"
    p2 = type("P", (), {"demo": False})()
    assert force_demo_for_panel(p2, "ledger") is False
    assert p2.demo is False, "其他面板不该改 demo"
    p3 = type("P", (), {"demo": True})()
    assert force_demo_for_panel(p3, "failsafe") is False, "已经是 demo 就不必再改"

    # main 里的面板分发：--panel failsafe 必须真的走到强制 demo，
    # 只测 force_demo_for_panel 是不够的——删掉调用点测试照样绿。
    class _FakeRoot:
        def __init__(self):
            self.afters = []

        def after(self, ms, fn):
            self.afters.append(fn)

    class _PanelApp:
        demo = False
        opened = 0

        def _open_failsafe(self):
            self.opened += 1

        def open_ledger(self):
            pass

        def open_about(self):
            pass

    pa, fr = _PanelApp(), _FakeRoot()
    with contextlib.redirect_stdout(io.StringIO()):
        dispatch_panel(pa, "failsafe", fr)
    assert pa.demo is True, "dispatch_panel 必须强制演示模式"
    assert fr.afters, "必须排入打开保底层的调用"
    fr.afters[0]()
    assert pa.opened == 1, "排入的应该是 _open_failsafe"

    # 其他面板不该改 demo
    pa2, fr2 = _PanelApp(), _FakeRoot()
    dispatch_panel(pa2, "ledger", fr2)
    assert pa2.demo is False, "非 failsafe 面板不该改 demo"

    # 右键「安静到明早」的置灰判定（弹出菜单本身没法在自动化里触发）
    def _mm(pct, ac=False):
        return type("M", (), {"power": {"ac": ac, "pct": pct}, "cfg": cfg})()

    assert App._morning_menu_state(_mm(30))[0] == "disabled", "正好在 nudge 线上要置灰"
    assert App._morning_menu_state(_mm(29))[0] == "disabled"
    assert App._morning_menu_state(_mm(31))[0] == "normal"
    assert App._morning_menu_state(_mm(5, ac=True))[0] == "normal", "插电时不该置灰"
    assert App._morning_menu_state(_mm(None))[0] == "normal", "电量未知时不该置灰"

    # 开机自启：读写删都要对，而且**绝不碰真实注册表**——用假 winreg 模块
    cmd = _autostart_command()
    assert cmd.startswith('"'), f"命令必须带引号（目录里有空格）：{cmd}"
    assert cmd.count('"') >= 2, cmd

    # exe 分支的引号：源码模式走不到这条路径，但去掉引号后含空格的安装目录
    # 开机根本起不来，而绝大多数用户用的是 exe。
    _saved_frozen = globals()["FROZEN"]
    globals()["FROZEN"] = True
    try:
        cmd_exe = _autostart_command()
        assert cmd_exe == f'"{sys.executable}"', \
            f"exe 分支必须给 exe 路径加引号：{cmd_exe}"
        # 显式喂一个带空格的路径，钉住「引号真的包住了整条路径」这件事
        assert _autostart_command(r"C:\Program Files\BG\battery.exe") == \
            r'"C:\Program Files\BG\battery.exe"', "带空格的 exe 路径必须整体加引号"
    finally:
        globals()["FROZEN"] = _saved_frozen

    # py.exe 解析失败必须返回 None（调用方退回 py.exe，最坏只是闪黑框）。
    # 绝不能返回不存在的路径——那会让开机自启直接失效，而且用户看不出来。
    #
    # 用假 subprocess 而不是真跑：自检不该依赖「本机有没有 py.exe」，
    # 更不该在 PyInstaller 窗口模式下启动子进程（frozen 环境里没有控制台）。
    class _NoSub:
        DEVNULL = -3

        @staticmethod
        def run(*a, **k):
            raise FileNotFoundError("injected: 模拟 py.exe 不存在")

    _saved_sub = globals()["subprocess"]
    globals()["subprocess"] = _NoSub()
    try:
        assert _resolve_py_launcher(r"C:\definitely\not\exists\py.exe") is None
    finally:
        globals()["subprocess"] = _saved_sub

    # 解析出来的路径必须真的存在。只测 _parse_py_launcher_output 不够——
    # 去掉 `os.path.exists` 检查后，自启会指向一个不存在的解释器，
    # **开机直接起不来**，是这条链上后果最重的一环。
    class _SubWithPath:
        DEVNULL = -3

        @staticmethod
        def run(*a, **k):
            class _R:
                stdout = (b" -V:3.14 *        "
                          b"C:\\definitely\\not\\exists\\python.exe\n")
            return _R()

    globals()["subprocess"] = _SubWithPath()
    try:
        assert _resolve_py_launcher("C:\\Windows\\py.exe") is None, \
            "解析出的路径不存在时必须返回 None"
    finally:
        globals()["subprocess"] = _saved_sub

    # 超时是 UI 线程上的等待上限，太大等于界面白冻
    assert 1 <= PY_LAUNCHER_TIMEOUT <= 5, \
        f"PY_LAUNCHER_TIMEOUT={PY_LAUNCHER_TIMEOUT} 不在合理区间"

    # `py -0p` 的解析：只认带 * 的默认版本，没 * 就不猜
    _pyout = (" -V:3.14 *        C:\\Python314\\python.exe\n"
              " -V:3.13          C:\\Python313\\python.exe\n")
    assert _parse_py_launcher_output(_pyout) == "C:\\Python314\\python.exe", \
        _parse_py_launcher_output(_pyout)
    assert _parse_py_launcher_output(
        " -V:3.14          C:\\Python314\\python.exe\n") is None, \
        "没有默认标记时不该猜"
    assert _parse_py_launcher_output("") is None
    assert _parse_py_launcher_output("nothing here") is None

    # py.exe 分支必须真的被接上。只测 _resolve_py_launcher 函数本身不够：
    # 把 `if os.path.basename(exe).lower() == "py.exe":` 改成 `if False:`
    # 测试照样全绿，而实际效果是自启命令退化成 py.exe（登录闪黑框）。
    #
    # 两处必须显式处理，否则这段在 exe 里会直接失败：
    #  - FROZEN 在 exe 里是 True，`_autostart_command` 会提前 return，走不到 py 分支；
    #  - 不去动 sys.executable —— 那个值在 onefile 模式下是 PyInstaller 运行时
    #    自己用的，改它会让 exe 的自检卡死（实测过，进程不退、无输出）。
    # 所以：FROZEN 临时置假 + exe 参数注入。
    _saved_frozen2 = globals()["FROZEN"]
    _saved_resolve = globals()["_resolve_py_launcher"]
    globals()["FROZEN"] = False
    globals()["_resolve_py_launcher"] = lambda exe=None: "C:\\fake\\real\\python.exe"
    try:
        _cmd_py = _autostart_command("C:\\Windows\\py.exe")
        assert "real" in _cmd_py, \
            f"exe 是 py.exe 时必须用解析出的真实解释器：{_cmd_py}"
    finally:
        globals()["_resolve_py_launcher"] = _saved_resolve
        globals()["FROZEN"] = _saved_frozen2

    # py.exe 解析失败必须返回 None（调用方退回 py.exe，最坏只是闪黑框）。
    # 绝不能返回不存在的路径——那会让开机自启直接失效，而且用户看不出来。
    # （上面已经用假 subprocess 验过两条路径，这里不再真跑外部进程。）

    # 连续失败的两个阈值：太短会误报，太长则用户盯着陈旧数字太久
    assert 3 <= TICK_FAIL_WARN <= 20, \
        f"TICK_FAIL_WARN={TICK_FAIL_WARN} 不在合理区间"
    assert 10 <= TICK_TCL_GIVE_UP <= 600, \
        f"TICK_TCL_GIVE_UP={TICK_TCL_GIVE_UP} 不在合理区间"

    # hibernate() 必须用「休眠」而不是「待机」：SetSuspendState 第一个参数
    # 1=Hibernate、0=Suspend。改成 0 会退化成待机——继续耗电，最终仍然硬关机，
    # 正好是 L6 要防的那件事。整套测试因为整体打桩 hibernate()，从来没验过这里。
    class _FakePowrprof:
        def __init__(self):
            self.calls = []

        def SetSuspendState(self, hibernate_flag, force, disable_wake):
            self.calls.append((hibernate_flag, force, disable_wake))
            return 1

    _fake_pp = _FakePowrprof()
    _saved_windll = ctypes.windll

    class _FakeWindll:
        powrprof = _fake_pp

    ctypes.windll = _FakeWindll()
    try:
        assert hibernate() is True
        assert _fake_pp.calls, "hibernate() 没有调用 SetSuspendState"
        assert _fake_pp.calls[0][0] == 1, \
            f"SetSuspendState 第一个参数必须是 1（休眠），实际 {_fake_pp.calls}"
    finally:
        ctypes.windll = _saved_windll

    class _Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeWinreg:
        HKEY_CURRENT_USER = 1
        REG_SZ = 1
        KEY_SET_VALUE = 2

        def __init__(self):
            self.data = {}

        def OpenKey(self, root, path, *a):
            if path not in self.data:
                raise FileNotFoundError(path)
            return _Key(path)

        def CreateKey(self, root, path):
            self.data.setdefault(path, {})
            return _Key(path)

        def QueryValueEx(self, key, name):
            d = self.data.get(key.path, {})
            if name not in d:
                raise FileNotFoundError(name)
            return d[name], 1

        def SetValueEx(self, key, name, reserved, typ, value):
            self.data.setdefault(key.path, {})[name] = value

        def DeleteValue(self, key, name):
            d = self.data.get(key.path, {})
            if name not in d:
                raise FileNotFoundError(name)
            del d[name]

    _saved_reg = sys.modules.get("winreg")
    _fr = _FakeWinreg()
    sys.modules["winreg"] = _fr
    try:
        assert autostart_enabled() is False, "初始应为未开启"
        assert set_autostart(True) is True
        assert autostart_enabled() is True
        assert _fr.data[AUTOSTART_KEY][AUTOSTART_NAME] == _autostart_command()
        assert set_autostart(False) is True
        assert autostart_enabled() is False
        assert set_autostart(False) is True, "重复关闭不该报错"

        # 演示模式绝不能写注册表
        class _Var:
            def __init__(self, v):
                self.v = v

            def get(self):
                return self.v

            def set(self, v):
                self.v = v

        calls = []
        _orig_set = globals()["set_autostart"]
        globals()["set_autostart"] = lambda on: calls.append(on) or True
        try:
            stub = type("A", (), {"demo": True, "autostart_var": _Var(True)})()
            App._toggle_autostart(stub)
            assert calls == [], "演示模式不能写注册表"
        finally:
            globals()["set_autostart"] = _orig_set
    finally:
        if _saved_reg is None:
            sys.modules.pop("winreg", None)
        else:
            sys.modules["winreg"] = _saved_reg

    # 电池容量解析：本机 92.0/84.1 Wh，默认的 60 Wh 会让账本系统性偏低 40%
    _xml = ("<BatteryReport><Battery><DesignCapacity>92041</DesignCapacity>"
            "<FullChargeCapacity>84065</FullChargeCapacity></Battery></BatteryReport>")
    assert abs(_capacity_from_report_xml(_xml) - 84.1) < 0.15, \
        _capacity_from_report_xml(_xml)
    assert abs(_capacity_from_report_xml(
        "<a><DesignCapacity>60000</DesignCapacity></a>") - 60.0) < 0.15, \
        "没有 FullChargeCapacity 时应退回设计容量"
    assert abs(_capacity_from_report_xml(
        "<a><FullChargeCapacity> 50000 </FullChargeCapacity></a>") - 50.0) < 0.15, \
        "标签里的空白要能容忍"
    assert _capacity_from_report_xml("not xml") is None
    assert _capacity_from_report_xml(
        "<a><FullChargeCapacity>0</FullChargeCapacity></a>") is None, "0 应视为无效"
    assert _capacity_from_report_xml(
        "<a><FullChargeCapacity>999999</FullChargeCapacity></a>") is None, "离谱值应丢弃"

    # 多电池必须累加。只取第一个的话，两块 40 Wh 会读成 40 Wh（少算一半），
    # 而 40 落在 5-300 Wh 的合理性区间里，校验拦不住 —— 静默把账本数字砍半。
    _two = ("<BatteryReport>"
            "<Battery><FullChargeCapacity>40000</FullChargeCapacity></Battery>"
            "<Battery><FullChargeCapacity>40000</FullChargeCapacity></Battery>"
            "</BatteryReport>")
    assert abs(_capacity_from_report_xml(_two) - 80.0) < 0.15, \
        f"两块 40Wh 应累加为 80Wh，实际 {_capacity_from_report_xml(_two)}"

    # 标签带属性时也要能解析（当前 powercfg 不加，属未来兼容）
    assert abs(_capacity_from_report_xml(
        '<a><FullChargeCapacity unit="mWh">50000</FullChargeCapacity></a>') - 50.0) < 0.15, \
        "带属性的标签必须能解析"

    # <Battery> 段本身带属性时也要能分段。两处正则写法不一致是隐患：
    # 容量标签支持属性、分段标签不支持，遇到 `<Battery id="BAT1">` 会退化成
    # 只取第一块（两块 40Wh 读成 40Wh，回到修之前的 bug）。
    _two_attr = ("<BatteryReport>"
                 '<Battery id="BAT1"><FullChargeCapacity>40000</FullChargeCapacity></Battery>'
                 '<Battery id="BAT2"><FullChargeCapacity>40000</FullChargeCapacity></Battery>'
                 "</BatteryReport>")
    assert abs(_capacity_from_report_xml(_two_attr) - 80.0) < 0.15, \
        f"带属性的 Battery 段也要累加，实际 {_capacity_from_report_xml(_two_attr)}"

    # 三块 100 Wh = 300 Wh：旧的 300 上限卡在边界上，正好会被丢掉
    _three = ("<BatteryReport>"
              + "<Battery><FullChargeCapacity>100000</FullChargeCapacity></Battery>" * 3
              + "</BatteryReport>")
    assert abs(_capacity_from_report_xml(_three) - 300.0) < 0.15, \
        f"三块 100Wh 应读出 300Wh，实际 {_capacity_from_report_xml(_three)}"

    # 单块离谱值只丢那一块，不该把整台机器的容量算飞、也不该连累其它块
    _mixed = ("<BatteryReport>"
              "<Battery><FullChargeCapacity>40000</FullChargeCapacity></Battery>"
              "<Battery><FullChargeCapacity>999999999</FullChargeCapacity></Battery>"
              "</BatteryReport>")
    assert abs(_capacity_from_report_xml(_mixed) - 40.0) < 0.15, \
        f"离谱分段应被丢弃，实际 {_capacity_from_report_xml(_mixed)}"

    # 单块下限：0.5 Wh 是「解析到别的数字」的典型值。
    # 注意不能只测「单独一块 0.5Wh → None」——那会被总量下限兜住（0.5 < 5.0），
    # 去掉单块下限照样通过。必须让总量够大，只有「丢弃那一块」才能得到正确结果。
    assert _capacity_from_report_xml(
        "<a><FullChargeCapacity>500</FullChargeCapacity></a>") is None, \
        "单块低于 1Wh 应丢弃"
    _tiny = ("<BatteryReport>"
             "<Battery><FullChargeCapacity>40000</FullChargeCapacity></Battery>"
             "<Battery><FullChargeCapacity>500</FullChargeCapacity></Battery>"
             "</BatteryReport>")
    assert abs(_capacity_from_report_xml(_tiny) - 40.0) < 0.15, \
        f"低于 1Wh 的分段应被丢弃，实际 {_capacity_from_report_xml(_tiny)}"

    # 总量上限：5 × 100Wh = 500Wh 超过 PACK_WH_MAX，整包丢弃
    _five = ("<BatteryReport>"
             + "<Battery><FullChargeCapacity>100000</FullChargeCapacity></Battery>" * 5
             + "</BatteryReport>")
    assert _capacity_from_report_xml(_five) is None, \
        f"5×100Wh=500Wh 应超上限被丢弃，实际 {_capacity_from_report_xml(_five)}"

    # 4 × 100Wh = 400Wh 正好在上界，必须能读出来（否则上限就设得太紧）
    _four = ("<BatteryReport>"
             + "<Battery><FullChargeCapacity>100000</FullChargeCapacity></Battery>" * 4
             + "</BatteryReport>")
    assert abs(_capacity_from_report_xml(_four) - 400.0) < 0.15, \
        f"4×100Wh=400Wh 应正好通过，实际 {_capacity_from_report_xml(_four)}"

    # 离电时长与周对比
    _d = date(2026, 10, 31)
    st = {}
    record_on_battery(st, 3600, _d)
    record_on_battery(st, 1800, _d)
    assert abs(st["days"][str(_d)] - 5400) < 0.01, st
    record_on_battery(st, 0, _d)
    record_on_battery(st, -5, _d)
    assert abs(st["days"][str(_d)] - 5400) < 0.01, "0 或负数不该改变结果"

    st2 = {"days": {str(_d): 3600, str(_d - timedelta(days=8)): 7200}}
    wk, lw = weekly_on_battery(st2, _d)
    assert abs(wk - 3600) < 0.01 and abs(lw - 7200) < 0.01, (wk, lw)

    st3 = {}
    for i in range(30):
        record_on_battery(st3, 60, _d - timedelta(days=i))
    assert len(st3["days"]) == MAX_HISTORY_DAYS, f"应只保留 {MAX_HISTORY_DAYS} 天"

    assert fmt_hours(0) == "—"
    assert fmt_hours(1800) == "30 分钟", fmt_hours(1800)
    assert fmt_hours(7200) == "2.0 小时", fmt_hours(7200)

    # 本周 / 上周各 7 天，不能重叠。`i < 7` 改成 `i <= 7` 会让本周多吃一天、
    # 上周少吃一天，周对比的「变化」跟着错。
    _wk_days = {str(_d - timedelta(days=i)): 3600.0 for i in range(14)}
    wk, lw = weekly_on_battery({"days": _wk_days}, _d)
    assert abs(wk - 7 * 3600) < 1e-6, f"本周应恰好 7 天，实际 {wk / 3600:.1f} 天"
    assert abs(lw - 7 * 3600) < 1e-6, f"上周应恰好 7 天，实际 {lw / 3600:.1f} 天"

    # 离电时长的单次上限：休眠/挂起几小时醒来后，不能把整段时间算进今天
    assert clamp_tick_dt(1000.0, 0.0) == 60.0, "间隔再长也只能记 60 秒"
    assert clamp_tick_dt(100.0, 40.0) == 60.0
    assert clamp_tick_dt(50.0, 40.0) == 10.0, "正常间隔应原样返回"
    assert clamp_tick_dt(40.0, 50.0) == 0.0, "时间倒退不该产生负数"

    # 只在用户没填过容量时才自动读，否则会覆盖他手工填的值
    assert should_autofill_capacity({"capacity_wh": 60.0}) is True
    assert should_autofill_capacity({"capacity_wh": 84.1}) is False, \
        "用户手填的容量不能被自动读取覆盖"
    # 容差必须是「等于 60」，不是「接近 60」。真有 60.5 Wh 的电池时，
    # 放宽到 1.0 会把用户填的值当成默认值覆盖掉。
    assert should_autofill_capacity({"capacity_wh": 60.5}) is False, \
        "60.5 是用户填的值，不是默认值"
    assert should_autofill_capacity({"capacity_wh": 59.5}) is False
    assert should_autofill_capacity({"capacity_wh": 60.0}, demo=True) is False, \
        "演示模式不该起线程读硬件"
    assert should_autofill_capacity({}) is False, "缺键时不该当成默认值去读"
    assert should_autofill_capacity({"capacity_wh": "abc"}) is False, "脏值要能兜住"

    # powercfg 超时是「读不到就静默退回 60Wh」的唯一闸门
    assert POWERCAP_TIMEOUT >= 10, \
        f"超时太短会永远读不到容量（当前 {POWERCAP_TIMEOUT} 秒）"

    # 日志轮转：开机自启之后这个文件是长期 append 的
    with open(LOG_PATH, "w", encoding="utf-8") as _f:
        _f.write("x" * 1_000_100)
    log("轮转测试")
    assert os.path.exists(LOG_PATH + ".1"), "超过 1MB 应轮转出 .1"
    assert os.path.getsize(LOG_PATH) < 10_000, "轮转后新日志应很小"

    # 账本里当天数据必须计入：`self.stats.get("today") or {}` 改成 and 会归零
    lg_today = Ledger(default_config(), {
        "kwh": 0.0, "wear": 0.0,
        "today": {"date": str(date.today()), "kwh": 10.0, "wear": 10.0},
    })
    s_today = lg_today.summary()
    assert s_today["today_saved"] > 0, "当天数据必须计入 today_saved"
    assert s_today["today_cost"] > 0, "当天数据必须计入 today_cost"

    # has_battery 缺省时要按"有电池"处理，否则笔记本被当成台式机、永远显示"未接电"
    nb = type("S", (), {
        "power": {"ac": False, "pct": 80, "secs": None},
        "stats": {"streak_days": 0},
        "rate": RateEstimator(),
        "level": "calm",
        "estimate": App.estimate,
    })()
    assert App.sub_text(nb) != "未接电", \
        f"缺 has_battery 时不应判为无电池，实际 {App.sub_text(nb)}"

    # chip_text 也要看 has_battery：缺省成 False 会把连续天数整段吞掉
    nb_chip = type("S", (), {
        "power": {"ac": False, "pct": 80, "secs": None},
        "stats": {"streak_days": 3},
    })()
    assert App.chip_text(nb_chip) != "", \
        "缺 has_battery 时不应丢掉连续天数"

    print("自检：等级判定、速率估算、区间文案、台式机无电池、配置净化、"
          "阈值顺序、静音不关保底层、连续天数、明早时长、账本、"
          "电池容量解析、离电时长周对比、日志轮转全部通过")


# ---------------------------------------------------------------- 入口

def force_demo_for_panel(app, panel):
    """`--panel failsafe` 绕过电量判断直接开保底层，倒计时到 0 会真的调休眠。

    没带 --demo 就等于「看一眼界面，机器睡过去」，所以强制演示模式。
    抽成函数是为了能被测——原来这段埋在 main() 里，删掉它没有任何测试会红。
    返回是否强制过。
    """
    if panel == "failsafe" and not app.demo:
        app.demo = True
        return True
    return False


def dispatch_panel(app, panel, root):
    """按 --panel 打开对应窗口。

    单独抽出来是为了能测：`--panel failsafe` 的强制演示模式原来写在 main() 里，
    删掉它没有任何测试会红——而那正好是「看一眼界面，机器睡过去」的入口。
    """
    if panel == "ledger":
        root.after(300, app.open_ledger)
    elif panel == "failsafe":
        if force_demo_for_panel(app, panel):
            log("--panel failsafe 未带 --demo，已强制演示模式（不会真的休眠）")
            print("提示：--panel failsafe 未带 --demo，已强制演示模式，不会真的休眠。")
        root.after(300, app._open_failsafe)
    elif panel == "about":
        root.after(300, app.open_about)


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
    dispatch_panel(app, panel, root)
    root.mainloop()


if __name__ == "__main__":
    main()
