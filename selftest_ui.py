# -*- coding: utf-8 -*-
"""
界面冒烟测试 —— 分阶段验收吸附 / 收起 / 锁定 / 固定 逻辑。

运行：python selftest_ui.py
退出码 0 = 全部通过，1 = 有失败。

会真的开窗口，跑完自动关闭。不依赖磁盘上的 config/stats，
每次都从默认值开始，避免上一轮状态污染下一轮。
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


def pump(root, sec=0.7):
    end = time.time() + sec
    while time.time() < end:
        root.update()
        time.sleep(0.015)


def settle(root, app, timeout=3.0):
    """等到滑动动画真的结束，而不是靠固定睡眠。"""
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if not app._animating:
            root.update()
            return True
        time.sleep(0.01)
    return False


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}   {detail}")
        FAILS.append(name)


def edge_gap(app):
    """到所停靠那条边的距离；未停靠时返回 None。"""
    sw = app.hud.winfo_screenwidth()
    if app.dock_edge == "left":
        return app.hud_x
    if app.dock_edge == "right":
        return sw - (app.hud_x + app.hud_w)
    return None


def make_app(root, pct, dock):
    cfg = bg.default_config()
    cfg["dock_edge"] = dock
    cfg["poll_seconds"] = 60          # 别让 tick 打断测试
    app = bg.App(root, cfg, bg.default_stats(), demo_pct=pct)
    app._grace_until = time.time() + 9999   # 关掉自动收起
    app._last_hover = time.time() + 9999
    return app


def teardown(app):
    app._alive = False
    for w in (app.hud, app.dialog, app.failsafe_win, app.banner,
              app.ledger_win, app.about_win):
        if w is not None:
            try:
                w.destroy()
            except tk.TclError:
                pass


def stage(title):
    print(f"\n[{title}]")


def run(root, pct, dock, expect_collapse=True):
    app = make_app(root, pct, dock)
    pump(root, 0.5)

    stage(f"电量 {pct}% / 停靠 {dock}")
    check("贴边无缝隙", edge_gap(app) == 0, f"gap={edge_gap(app)}")
    check("初始为展开", not app.collapsed)
    check("宽度为完整宽度", app.hud_w > app.px(150), f"w={app.hud_w}")

    stage("收起")
    can = app._can_collapse()
    # 这条必须断言，不能靠分支——否则把 collapse_lock_below 改坏也不会报错
    check(f"收起许可={'允许' if expect_collapse else '拒绝'}"
          f"（collapse_lock_below 生效）",
          can is expect_collapse, f"实际 can={can}")
    app.collapse()
    ok = settle(root, app)
    check("收起动画在 3 秒内结束", ok, f"animating={app._animating}")
    if can:
        check("收起后宽度=彩条", app.hud_w == app.px(6), f"w={app.hud_w}")
        check("收起后仍贴边", edge_gap(app) == 0, f"gap={edge_gap(app)}")
        check("状态标记为已收起", app.collapsed)
    else:
        check("低电量时拒绝收起",
              not app.collapsed and app.hud_w > app.px(200),
              f"w={app.hud_w} collapsed={app.collapsed}")

    stage("展开")
    app.expand()
    ok = settle(root, app)
    check("展开动画在 3 秒内结束", ok, f"animating={app._animating}")
    check("展开后恢复完整宽度", app.hud_w > app.px(200), f"w={app.hud_w}")
    check("展开后仍贴边", edge_gap(app) == 0, f"gap={edge_gap(app)}")
    check("状态标记为已展开", not app.collapsed)

    stage("点击固定")
    before = app.pinned
    app._on_click()
    pump(root, 0.3)
    check("点击切换 pinned", app.pinned != before)
    if app.pinned:
        check("固定后禁止收起", not app._can_collapse())
    app._on_click()
    pump(root, 0.3)

    stage("拖离边缘 → 解除停靠")
    app._dragged = True
    app.hud_x = app.hud.winfo_screenwidth() // 2
    app.hud_y = app.px(200)
    app.dock_edge = None
    app.free_x = app.hud_x
    app.collapsed = False
    app._sync_geometry()
    app.render_hud()
    pump(root, 0.3)
    check("解除停靠后不再贴边", edge_gap(app) is None)
    check("未停靠时禁止收起", not app._can_collapse())

    teardown(app)
    pump(root, 0.2)


def run_desktop(root):
    """台式机：没有电池时不能出现 -1%，也不该显示「连续 N 天」。"""
    app = make_app(root, 50, "right")
    pump(root, 0.3)

    stage("台式机（无电池）")
    app.power = {"ac": True, "pct": None, "secs": None,
                 "charging": False, "has_battery": False}
    app.level = "ac"
    app.render_hud()
    pump(root, 0.2)

    texts = [app.canvas.itemcget(i, "text")
             for i in app.canvas.find_all() if app.canvas.type(i) == "text"]
    check("不出现 -1%", not any("-1" in t for t in texts), str(texts))
    check("显示「无电池」", any("无电池" in t for t in texts), str(texts))
    check("不显示连续天数", not any("连续" in t for t in texts), str(texts))

    teardown(app)
    pump(root, 0.2)


def main():
    # 测试写到临时目录，绝不能碰用户真实的 config / stats
    tmp = tempfile.mkdtemp(prefix="bg-test-")
    bg.DATA_DIR = tmp
    bg.CFG_PATH = os.path.join(tmp, "config.json")
    bg.STATS_PATH = os.path.join(tmp, "stats.json")
    bg.LOG_PATH = os.path.join(tmp, "app.log")

    print("=" * 60)
    print("界面冒烟测试")
    print(f"临时数据目录：{tmp}")
    print("=" * 60)

    root = tk.Tk()
    root.withdraw()
    root.report_callback_exception = lambda *a: traceback.print_exception(*a)

    run(root, 80, "right")
    run(root, 80, "left")
    run(root, 20, "right", expect_collapse=False)   # 低于 collapse_lock_below，必须拒绝收起
    run_desktop(root)

    root.destroy()

    print()
    print("=" * 60)
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{', '.join(FAILS)}")
        sys.exit(1)
    print("全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
