# -*- coding: utf-8 -*-
"""
布局矩阵测试 —— 用模拟的屏幕尺寸和 DPI 缩放跑一遍界面，验证不越界。

真机只有一台（200% 缩放），但布局是按 S = 当前DPI/96 算的，
所以可以伪造 S 和屏幕尺寸，把常见组合都过一遍。

运行：python selftest_layout.py
退出码 0 = 全部通过。
"""
import ctypes
import os
import sys
import tempfile
import time
import traceback
import tkinter as tk

ctypes.windll.shcore.SetProcessDpiAwareness(1)
import battery_guardian as bg

FAILS = []

# (屏宽, 屏高, DPI 百分比)
CASES = [
    (1366, 768, 100),
    (1366, 768, 150),
    (1920, 1080, 100),
    (1920, 1080, 125),
    (1920, 1080, 150),
    (2560, 1440, 150),
    (2560, 1440, 200),
    (3120, 2080, 200),
    (3840, 2160, 200),
    (3840, 2160, 250),
    (1280, 720, 200),     # 小屏 + 高缩放：最容易挤爆的组合
    (1024, 768, 100),
]


def pump(root, sec=0.25):
    end = time.time() + sec
    while time.time() < end:
        root.update()
        time.sleep(0.01)


def settle(root, app, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if not app._animating:
            root.update()
            return True
        time.sleep(0.01)
    return False


def check(case, name, cond, detail=""):
    if not cond:
        FAILS.append(f"{case} / {name} {detail}")
        print(f"    FAIL  {name}   {detail}")
        return False
    return True


def make_app(root, sw, sh, dpi):
    root.tk.call("tk", "scaling", dpi / 100.0 * 96.0 / 72.0)
    cfg = bg.default_config()
    cfg["poll_seconds"] = 60
    app = bg.App(root, cfg, bg.default_stats(), demo_pct=45)
    app._grace_until = time.time() + 9999
    app._last_hover = time.time() + 9999

    app.S = dpi / 100.0
    app.hud.winfo_screenwidth = lambda: sw
    app.hud.winfo_screenheight = lambda: sh
    app._center = lambda win, w, h: win.geometry(
        f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
    app.hud_h = app.px(48)
    app.canvas.configure(height=app.hud_h)
    app.render_hud()
    pump(root)
    return app


def teardown(app):
    app._alive = False
    for w in (app.hud, app.dialog, app.failsafe_win, app.banner, app.ledger_win):
        if w is not None:
            try:
                w.destroy()
            except tk.TclError:
                pass


def run_case(root, sw, sh, dpi):
    tag = f"{sw}x{sh}@{dpi}%"
    print(f"\n[{tag}]")
    app = make_app(root, sw, sh, dpi)

    # 1. 展开态：必须完整落在屏幕内
    w, h = app.hud_w, app.hud_h
    check(tag, "浮条不宽于屏幕", w <= sw, f"w={w} sw={sw}")
    check(tag, "浮条左边界不为负", app.hud_x >= 0, f"x={app.hud_x}")
    check(tag, "浮条右边界不越界", app.hud_x + w <= sw,
          f"right={app.hud_x + w} sw={sw}")
    check(tag, "浮条顶部不为负", app.hud_y >= 0, f"y={app.hud_y}")
    check(tag, "浮条底部不越界", app.hud_y + h <= sh,
          f"bottom={app.hud_y + h} sh={sh}")
    print(f"    展开 {w}x{h} @ ({app.hud_x},{app.hud_y})")

    # 2. 收起态：必须贴边且完整可见
    app.collapse()
    if not settle(root, app):
        check(tag, "收起动画完成", False, "动画未结束")
    else:
        bw = app.hud_w
        check(tag, "彩条贴右边", app.hud_x + bw == sw,
              f"right={app.hud_x + bw} sw={sw}")
        check(tag, "彩条完整可见", app.hud_x >= 0 and bw <= sw,
              f"x={app.hud_x} w={bw}")
        print(f"    收起 {bw}x{app.hud_h} @ ({app.hud_x},{app.hud_y})")

    # 3. 展开回来
    app.expand()
    settle(root, app)
    check(tag, "展开后仍贴边", app.hud_x + app.hud_w == sw,
          f"right={app.hud_x + app.hud_w} sw={sw}")

    # 4. 确认框：不能超出屏幕
    app._open_dialog()
    pump(root, 0.4)
    if app.dialog is not None and app.dialog.winfo_exists():
        dw, dh = app.dialog.winfo_width(), app.dialog.winfo_height()
        dx, dy = app.dialog.winfo_x(), app.dialog.winfo_y()
        check(tag, "对话框不宽于屏幕", dw <= sw, f"dw={dw} sw={sw}")
        check(tag, "对话框不高于屏幕", dh <= sh, f"dh={dh} sh={sh}")
        check(tag, "对话框不越右", dx + dw <= sw, f"right={dx + dw} sw={sw}")
        check(tag, "对话框不越下", dy + dh <= sh, f"bottom={dy + dh} sh={sh}")
        print(f"    对话框 {dw}x{dh} @ ({dx},{dy})")

    # 5. 左吸附
    app._close_dialog()
    app.dock_edge = "left"
    app.collapsed = False
    app._sync_geometry()
    app.render_hud()
    pump(root, 0.15)
    check(tag, "左吸附贴左边", app.hud_x == 0, f"x={app.hud_x}")

    teardown(app)
    pump(root, 0.1)


def main():
    # 同 selftest_ui：统一走 isolated_data，别各写一套。
    with bg.isolated_data("bg-layout-") as tmp:
        return _main_body(tmp)


def _main_body(tmp):

    print("=" * 62)
    print("布局矩阵测试")
    print("=" * 62)

    root = tk.Tk()
    root.withdraw()
    root.report_callback_exception = lambda *a: traceback.print_exception(*a)

    for sw, sh, dpi in CASES:
        try:
            run_case(root, sw, sh, dpi)
        except Exception:
            traceback.print_exc()
            FAILS.append(f"{sw}x{sh}@{dpi}% / 未捕获异常")

    root.destroy()

    print()
    print("=" * 62)
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print(f"全部通过（{len(CASES)} 种屏幕 × DPI 组合）")
    sys.exit(0)


if __name__ == "__main__":
    main()
