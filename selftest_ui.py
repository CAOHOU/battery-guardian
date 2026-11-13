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
SOUNDS = []          # 收集 play_alert 的调用，测试期间不真的响


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
              app.ledger_win, app.about_win, app.fail_win):
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
    # 随机变异抽样发现的盲区：初始值被改成 True 会跳过整个断电计数
    check("初始未标记断电", app._incident_flag is False)
    check("ESC 长按初始未就绪", app.key_ok is True)
    check("浮条置顶", bool(app.hud.attributes("-topmost")))

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

    stage("拖动")
    class _E:
        x_root = 600
        y_root = 300
    app._drag_start(_E())
    check("拖动开始时清空标记", app._dragged is False)
    app._drag_move(_E())
    # 随机变异抽样发现的盲区：把 `if not self._dragged` 的 not 去掉，
    # 整段（标记 + 解除停靠）都被跳过，拖了等于没拖
    check("拖动后标记为已拖动", app._dragged is True)
    check("拖动后解除停靠", app.dock_edge is None, f"dock={app.dock_edge}")
    app._drag_end(_E())
    pump(root, 0.2)

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

    stage("右键菜单")
    # 菜单项用索引定位，插一项就可能错位。这条钉住索引与标签的对应关系。
    labels = []
    for i in range(app.menu.index("end") + 1):
        try:
            labels.append(app.menu.entrycget(i, "label"))
        except tk.TclError:
            labels.append("(sep)")
    check("菜单里有「开机自启」", "开机自启" in labels, str(labels))
    check("菜单索引指向「安静到明早」",
          labels[app._menu_morning_idx].startswith("安静到明早"),
          f"idx={app._menu_morning_idx} label={labels[app._menu_morning_idx]}")

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

    # has_battery 缺省时必须按"有电池"处理。缺省成 False 的话，
    # 笔记本会被判成台式机：百分比消失、显示"无电池"。
    app.power = {"ac": False, "pct": 80, "secs": None, "charging": False}
    app.level = "calm"
    app.render_hud()
    pump(root, 0.2)
    texts2 = [app.canvas.itemcget(i, "text")
              for i in app.canvas.find_all() if app.canvas.type(i) == "text"]
    check("缺 has_battery 时仍显示百分比",
          any("80%" in t for t in texts2), str(texts2))
    check("缺 has_battery 时不显示无电池",
          not any("无电池" in t for t in texts2), str(texts2))

    teardown(app)
    pump(root, 0.2)


def run_hover(root):
    """hover 判定：感应带 / 容差 / 收起等待。

    原来整条 hover 路径零覆盖——测试全程直接调 expand()/collapse()，
    所以把感应带改成 0 像素、容差改成 0、等待改成 60 秒，测试照样全绿。
    """
    app = make_app(root, 80, "right")
    pump(root, 0.4)
    stage("hover 判定")

    sw = app.hud.winfo_screenwidth()
    tol = app.px(6)
    cx = app.hud_x + app.hud_w // 2
    cy = app.hud_y + app.hud_h // 2

    check("命中浮条内部", app._pointer_over(cx, cy))
    check("容差内仍算悬停", app._pointer_over(app.hud_x - tol + 1, cy), f"tol={tol}")
    # 四条边都要测：判定是一行里四个 <=，只测左边会漏掉另外三个
    check("左边缘算命中", app._pointer_over(app.hud_x - tol, cy), f"tol={tol}")
    check("右边缘算命中",
          app._pointer_over(app.hud_x + app.hud_w + tol, cy), f"tol={tol}")
    check("上边缘算命中", app._pointer_over(cx, app.hud_y - tol), f"tol={tol}")
    check("下边缘算命中",
          app._pointer_over(cx, app.hud_y + app.hud_h + tol), f"tol={tol}")
    check("超出容差不算悬停",
          not app._pointer_over(app.hud_x - tol - app.px(5), cy))

    # 收起态：6px 太细，贴边用 px(30) 的感应带
    app.collapsed = True
    app.hud_w = app._bar_w()
    band = app.px(30)
    check("收起态感应带命中", app._pointer_over(sw - band + 1, cy),
          f"band={band} x={sw - band + 1}")
    check("收起态远离边缘不命中",
          not app._pointer_over(sw - band - app.px(20), cy))

    # 收起等待阈值。这里必须用绝对值（1 秒 / 2 秒），
    # 不能引用 bg.HOVER_COLLAPSE_DELAY_S —— 那样把常量改成 60 测试也跟着变，永远绿。
    app.collapsed = False
    app._grace_until = time.time() - 1
    now = time.time()
    app._last_hover = now - 1.0
    check("离开 1 秒还不收起", app._hover_decision(-100, -100, now) is None)
    app._last_hover = now - 2.0
    check("离开 2 秒要收起",
          app._hover_decision(-100, -100, now) == "collapse")

    # 收起态鼠标进感应带 → 展开
    app.collapsed = True
    app.hud_w = app._bar_w()
    check("收起态进感应带 → 展开",
          app._hover_decision(sw - 1, cy, now) == "expand")

    teardown(app)
    pump(root, 0.2)


def run_fit(root):
    """_fit 的尺寸必须含边框 / 标题栏占位，否则内容被裁。"""
    app = make_app(root, 80, "right")
    pump(root, 0.3)
    stage("_fit 尺寸")

    win = tk.Toplevel(root)
    win.geometry("200x120+60+60")
    win.update()
    dw = max(0, win.winfo_rootx() - win.winfo_x())
    dh = max(0, win.winfo_rooty() - win.winfo_y())

    wrap = tk.Frame(win, width=300, height=80)
    wrap.pack()
    win.update_idletasks()
    req_w, req_h = wrap.winfo_reqwidth(), wrap.winfo_reqheight()

    got = {}
    orig_center = app._center
    app._center = lambda w, ww, hh: got.update(w=ww, h=hh)
    pad_x, pad_y = app.px(24), app.px(20)
    app._fit(win, wrap, pad_x, pad_y)
    app._center = orig_center

    exp_w = req_w + 2 * pad_x + dw
    exp_h = req_h + 2 * pad_y + dh
    # 若 dw=0，宽度断言就退化成「不含 dw 也成立」，等于白测。先确认可测。
    check("窗口边框占位可测（dw>0）", dw > 0, f"dw={dw}")
    check("宽度含边框占位", got.get("w") == exp_w,
          f"got={got.get('w')} 期望={exp_w} dw={dw}")
    check("高度含标题栏占位", got.get("h") == exp_h,
          f"got={got.get('h')} 期望={exp_h} dh={dh}")

    # 真实 _center（selftest_layout 把它整个替换掉，等于零覆盖）
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    app._center(win, 400, 300)
    win.update()
    check("水平居中", abs(win.winfo_x() - (sw - 400) // 2) <= 3,
          f"x={win.winfo_x()} 期望={(sw - 400) // 2}")
    check("垂直在 1/3 处", abs(win.winfo_y() - max(0, (sh - 300) // 3)) <= 3,
          f"y={win.winfo_y()} 期望={max(0, (sh - 300) // 3)}")

    win.destroy()
    teardown(app)
    pump(root, 0.2)


def run_slide_frames(root):
    """收起动画要有中间帧，不能一帧跳到底。"""
    app = make_app(root, 80, "right")
    pump(root, 0.4)
    stage("收起动画帧数")

    sw = app.hud.winfo_screenwidth()
    full = app.hud_w
    lo, hi = sw - full, sw - app._bar_w()

    xs = []
    orig = app.hud.geometry

    def rec(spec=None):
        if spec:
            try:
                xs.append(int(spec.split("+")[1]))
            except (IndexError, ValueError):
                pass
        return orig(spec)

    app.hud.geometry = rec
    app.collapse()
    settle(root, app)
    app.hud.geometry = orig

    mids = {x for x in xs if lo < x < hi}
    check("收起过程有足够中间帧", len(mids) >= 5,
          f"中间帧 {len(mids)} 个")

    teardown(app)
    pump(root, 0.2)


def run_force_expand(root):
    """危险档位必须强制展开：收起的浮条在 25% 时要自己滑出来。"""
    app = make_app(root, 25, "right")
    pump(root, 0.4)
    stage("危险档位强制展开")

    app.collapsed = True
    app.hud_w = app._bar_w()
    app._sync_geometry()
    check("前置：浮条已收起", app.collapsed and app.hud_w == app._bar_w())

    app.tick()
    ok = settle(root, app)
    check("tick 后自动展开", ok and not app.collapsed,
          f"animating={app._animating} collapsed={app.collapsed}")

    teardown(app)
    pump(root, 0.2)


def run_tick_resilience(root):
    """tick() 里抛异常后，主循环必须还能继续跑。

    失效模式很具体：tick 最后那句 after 是循环唯一的续命动作，
    任何异常跳过它，循环就永久停止——浮条不刷新、确认框不弹、保底层不触发，
    而且没有任何提示。开机自启让进程 7×24 长期运行之后，这从理论风险变成迟早发生。
    """
    app = make_app(root, 80, "right")
    pump(root, 0.4)
    stage("tick 异常后循环仍继续")

    calls = {"n": 0}
    real = app._tick_once

    def boom():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("注入的异常")
        return real()

    saved_poll = app.cfg["poll_seconds"]
    app.cfg["poll_seconds"] = 0.2      # 让重排来得快一点
    app._tick_once = boom
    try:
        app.tick()                      # 第一次：抛异常，由 tick 自己兜住
        check("异常没有冒到调用方", True)
        pump(root, 0.8)                 # 等 after 重排触发第二次
        check("异常后 tick 仍被再次调用", calls["n"] >= 2, f"calls={calls['n']}")
        check("循环状态仍然存活", app._alive)
    finally:
        app._tick_once = real
        app.cfg["poll_seconds"] = saved_poll

    teardown(app)
    pump(root, 0.3)

    # TclError 必须单独用一个 app：上面那个 app 还排着 after，
    # 复用会让「上一轮排的 after」冒充「TclError 分支重排了」，
    # 结果就是「把 TclError 改回 return」测试照样绿。
    app2 = make_app(root, 80, "right")
    pump(root, 0.4)
    stage("TclError 后循环仍继续")

    tcl = {"n": 0}
    real2 = app2._tick_once

    def boom_tcl():
        tcl["n"] += 1
        if tcl["n"] == 1:
            raise tk.TclError("injected transient")
        return real2()

    saved_poll2 = app2.cfg["poll_seconds"]
    app2.cfg["poll_seconds"] = 0.2
    app2._tick_once = boom_tcl
    try:
        app2.tick()
        pump(root, 0.8)
        check("瞬时 TclError 后 tick 仍被再次调用", tcl["n"] >= 2, f"calls={tcl['n']}")
    finally:
        app2._tick_once = real2
        app2.cfg["poll_seconds"] = saved_poll2

    # 连续失败够久，浮条必须承认「读不到」，而不是继续显示陈旧电量。
    # 一个还在动但已经瞎了的工具，比直接死掉更难被发现。
    app2._tick_fails = 0
    for _ in range(bg.TICK_FAIL_WARN - 1):
        app2._note_tick_failure(RuntimeError("x"))
    check("未到阈值仍显示正常文案", app2.sub_text() != "电量读取失败", app2.sub_text())
    app2._note_tick_failure(RuntimeError("x"))
    check("连续失败到阈值后改口", app2.sub_text() == "电量读取失败", app2.sub_text())
    app2._note_tick_ok()
    check("恢复后清空计数并回到正常文案",
          app2._tick_fails == 0 and app2.sub_text() != "电量读取失败", app2.sub_text())

    teardown(app2)
    pump(root, 0.2)


def run_demo_no_persist():
    """演示模式不能往真实 stats/config 写东西。

    之前 demo 版和正式版能双开（互斥名不同），两个进程都写同一个 stats.json，
    假电量算出来的账本会覆盖真数据。截图看一眼就把统计改掉，很难察觉。
    """
    for p in (bg.STATS_PATH, bg.CFG_PATH):
        try:
            os.remove(p)
        except OSError:
            pass

    r = tk.Tk()
    r.withdraw()
    r.report_callback_exception = lambda *a: None
    app = make_app(r, 80, "right")
    pump(r, 0.3)

    stage("演示模式不落盘")
    # 离电时长也不能被假电量累加。现在它「只是不落盘」，所以去掉守卫只脏内存；
    # 但一旦哪天 demo 开始写盘，就会重演「假数据覆盖真统计」那个 bug。
    before_days = dict(app.stats.get("days") or {})
    app.tick()
    pump(r, 0.2)
    check("tick 后没写 stats.json", not os.path.exists(bg.STATS_PATH))
    check("demo 不累加离电时长",
          (app.stats.get("days") or {}) == before_days,
          f"{(app.stats.get('days') or {})}")

    # 第三条写盘路径：拖动/单击浮条会走 _save_cfg()，它原来没有 demo 守卫
    app._save_cfg()
    check("_save_cfg 不写 config.json", not os.path.exists(bg.CFG_PATH))

    app.quit()          # 会销毁自己的 root，之后不能再 pump
    check("退出后也没写 stats.json", not os.path.exists(bg.STATS_PATH))
    check("退出后也没写 config.json", not os.path.exists(bg.CFG_PATH))


def run_sound(root):
    """提示音：确认框弹出时响一次；静音之后不再响。

    保底层那一声没有在 UI 层测——那需要把 demo 关掉再开真保底层，
    而倒计时到 0 会真的休眠这台机器。它的开关逻辑在 --self-test 里覆盖。
    """
    stage("提示音")

    del SOUNDS[:]
    app = make_app(root, 15, "right")
    pump(root, 0.5)
    check("确认框弹出时响一次", SOUNDS.count("dialog") == 1, str(SOUNDS))

    # 静音：不弹框，也就不该响
    del SOUNDS[:]
    app.mute(30)
    app.tick()
    pump(root, 0.3)
    check("静音后不再响确认音", SOUNDS == [], str(SOUNDS))

    # 关掉总开关：即使弹框也不响
    del SOUNDS[:]
    app.muted_until = 0
    app.cfg["sound_enabled"] = False
    app._close_dialog()
    app.tick()
    pump(root, 0.3)
    check("总开关关掉后不响", SOUNDS == [], str(SOUNDS))
    app.cfg["sound_enabled"] = True

    teardown(app)
    pump(root, 0.2)


def run_hibernate_failed(root):
    """休眠失败不能变成无限响铃。

    这是第二轮审查的 P1：hibernate() 返回 False 时原来什么都不做，
    下一个 tick 又满足 failsafe_win is None → 重开倒计时 → 再响一声。
    打桩 hibernate 返回 False，真机不会被休眠。
    """
    app = make_app(root, 5, "right")
    pump(root, 0.4)
    stage("休眠失败")

    app.demo = False                     # 只有非 demo 才会真的去休眠
    orig_hib = bg.hibernate
    bg.hibernate = lambda: False
    try:
        del SOUNDS[:]
        app._do_hibernate()
        pump(root, 0.2)

        check("标记为休眠失败", app._hibernate_failed is True)
        check("弹出失败提示窗", app.fail_win is not None)
        check("失败时不响铃", SOUNDS == [], str(SOUNDS))

        # 去重：重复调用不能堆出第二个窗口。去掉那个 `if self.fail_win is not None: return`
        # 之后，每来一次 tick 就多一个窗口，用户得一个个关。
        first = app.fail_win
        before = len([w for w in app.root.winfo_children()
                      if isinstance(w, tk.Toplevel)])
        app._show_hibernate_failed()
        app._show_hibernate_failed()
        pump(root, 0.2)
        after = len([w for w in app.root.winfo_children()
                     if isinstance(w, tk.Toplevel)])
        check("重复调用不新建窗口", app.fail_win is first and after == before,
              f"窗口数 {before} -> {after}")

        # 再走一遍升级逻辑：不能重开保底层，否则就是每分钟响一次
        del SOUNDS[:]
        app.level = "fail_safe"
        app.failsafe_win = None
        app._handle_escalation(time.time())
        pump(root, 0.2)
        check("失败后不再重开保底层", app.failsafe_win is None)
        check("失败后不再响保底层提示音", SOUNDS == [], str(SOUNDS))
    finally:
        bg.hibernate = orig_hib
        app._close_fail_win()
        app.demo = True

    teardown(app)
    pump(root, 0.2)


def run_failsafe_sound(root):
    """保底层提示音：demo 模式下开窗口，不会真休眠（休眠在倒计时结束时）。"""
    app = make_app(root, 5, "right")
    pump(root, 0.4)
    stage("保底层提示音")

    del SOUNDS[:]
    app.cfg["fail_safe_countdown"] = 999      # 别让倒计时走到 0
    app._open_failsafe()
    pump(root, 0.2)
    check("保底层弹出时响一次", SOUNDS.count("failsafe") == 1, str(SOUNDS))

    app._close_failsafe()
    teardown(app)
    pump(root, 0.2)


def run_dialog_reset(root):
    """确认框关闭后 dialog_grow 必须归零，否则下次一弹就是放大的。"""
    app = make_app(root, 15, "right")
    pump(root, 0.4)
    stage("确认框状态重置")

    check("初始已弹框", app.dialog is not None)
    app.dialog_grow = 2
    app._close_dialog()
    pump(root, 0.2)
    check("关闭后 dialog_grow 归零", app.dialog_grow == 0, f"grow={app.dialog_grow}")

    teardown(app)
    pump(root, 0.2)


def run_geometry_clamp(root):
    """位置钳制与 min_w 保底：算出来的坐标不能是负数。"""
    app = make_app(root, 80, "right")
    pump(root, 0.4)
    stage("位置钳制")

    sw = app.hud.winfo_screenwidth()
    app.dock_edge = "right"
    app.collapsed = False
    app.hud_w = sw + app.px(500)          # 比屏幕还宽 → 右贴边会算出负坐标
    app._sync_geometry()
    check("坐标不为负", app.hud_x >= 0, f"x={app.hud_x}")

    app.hud_w = app.px(300)
    app._sync_geometry()
    check("正常宽度仍贴右边", app.hud_x + app.hud_w == sw,
          f"x={app.hud_x} w={app.hud_w} sw={sw}")

    # _fit 的 min_w 保底
    win = tk.Toplevel(root)
    wrap = tk.Frame(win, width=10, height=10)
    wrap.pack()
    win.update_idletasks()
    got = {}
    orig_center = app._center
    app._center = lambda w, ww, hh: got.update(w=ww, h=hh)
    app._fit(win, wrap, app.px(4), app.px(4), min_w=app.px(400))
    app._center = orig_center
    check("_fit 的 min_w 生效", got.get("w", 0) >= app.px(400),
          f"got={got.get('w')} min={app.px(400)}")
    win.destroy()

    teardown(app)
    pump(root, 0.2)


def _labels(widget):
    """递归收集所有 Label 的文本，用来断言窗口里到底显示了什么。"""
    out = []
    for ch in widget.winfo_children():
        try:
            if ch.winfo_class() == "Label":
                out.append(ch.cget("text"))
        except tk.TclError:
            pass
        out.extend(_labels(ch))
    return out


def run_ledger(root):
    """账本窗口要能显示「离电时长」的周对比——只算钱是不够的。"""
    app = make_app(root, 80, "right")
    pump(root, 0.3)
    stage("账本窗口")

    app.stats["days"] = {str(bg.date.today()): 7200}
    app.open_ledger()
    pump(root, 0.3)
    texts = _labels(app.ledger_win)
    check("账本显示本周离电", any("本周" in t for t in texts), str(texts[:8]))
    check("账本显示上周离电", any("上周" in t for t in texts), str(texts[:8]))
    check("账本显示 2.0 小时", any("2.0 小时" in t for t in texts), str(texts))

    if app.ledger_win is not None:
        app.ledger_win.destroy()
        app.ledger_win = None
    teardown(app)
    pump(root, 0.2)


def main():
    # 测试写到临时目录，绝不能碰用户真实的 config / stats
    tmp = tempfile.mkdtemp(prefix="bg-test-")
    bg.DATA_DIR = tmp
    bg.CFG_PATH = os.path.join(tmp, "config.json")
    bg.STATS_PATH = os.path.join(tmp, "stats.json")
    bg.LOG_PATH = os.path.join(tmp, "app.log")

    # 测试期间不要真的响（会打断人，也会拖慢）
    bg.play_alert = lambda k: SOUNDS.append(k) or True

    print("=" * 60)
    print("界面冒烟测试")
    print(f"临时数据目录：{tmp}")
    print("=" * 60)

    root = tk.Tk()
    root.withdraw()
    root.report_callback_exception = lambda *a: traceback.print_exception(*a)

    run(root, 80, "right")
    run(root, 80, "left")
    # 25% 在 collapse_lock_below(30) 以下、又高于弹框阈值(20)：
    # 这样才真正测到"低于锁定阈值不许收起"，而不是被对话框挡住
    run(root, 25, "right", expect_collapse=False)
    # 边界：正好等于阈值时必须拒绝收起。原来只测 80/25，
    # 把 `>` 改成 `>=` 两边都仍然通过。
    run(root, 31, "right", expect_collapse=True)
    run(root, 30, "right", expect_collapse=False)
    run_desktop(root)
    run_hover(root)
    run_fit(root)
    run_slide_frames(root)
    run_force_expand(root)
    run_sound(root)
    run_failsafe_sound(root)
    run_dialog_reset(root)
    run_geometry_clamp(root)
    run_ledger(root)
    run_hibernate_failed(root)
    run_tick_resilience(root)
    run_demo_no_persist()

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
