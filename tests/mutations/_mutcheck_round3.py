# -*- coding: utf-8 -*-
"""第三轮审查列出的 15 项变异，逐项验证测试是否真能抓住。

读法：每一项都是「把源码改坏一处，跑测试，看会不会红」。
捕获 = 测试有效；漏网 = 这个位置没有测试覆盖，属于待办。

片段必须唯一（上一轮的教训）：replace(old, new, 1) 如果命中了别处，
你以为在测 A 其实改了 B，结果毫无意义。所以每项先检查出现次数。

用法
----
    python _mutcheck_round3.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

def _find_repo():
    """向上找含 battery_guardian.py 的目录。

    脚本可能在 _publish/ 也可能在 tests/mutations/。写死路径会在挪位置
    或换机器时静默失效——这个项目已经因为「写死的东西不同步」栽过三次。
    """
    d = HERE
    for _ in range(4):
        if os.path.exists(os.path.join(d, "battery_guardian.py")):
            return d
        d = os.path.dirname(d)
    raise RuntimeError(f"从 {HERE} 向上找不到 battery_guardian.py")

REPO = _find_repo()
PY = sys.executable
SRC = os.path.join(REPO, "battery_guardian.py")
PY = sys.executable

# (编号, 说明, 原文, 改成)
MUTS = [
    ("M1", "离电时长去掉 60 秒上限",
     "    return min(60.0, max(0.0, now - last))",
     "    return max(0.0, now - last)"),

    ("M2", "历史天数上限 21 -> 9999",
     "MAX_HISTORY_DAYS = 21",
     "MAX_HISTORY_DAYS = 9999"),

    ("M3", "周对比 i < 7 -> i <= 7（本周多吃一天）",
     "        if i < 7:",
     "        if i <= 7:"),

    ("M4", "fmt_hours 分钟阈值 60 -> 1（1 分钟以上就显示小时）",
     "    if m < 60:\n        return f\"{m} 分钟\"",
     "    if m < 1:\n        return f\"{m} 分钟\""),

    ("M5", "日志轮转阈值 1MB -> 1TB",
     "            if os.path.getsize(LOG_PATH) > 1_000_000:",
     "            if os.path.getsize(LOG_PATH) > 10 ** 12:"),

    ("M6", "去掉容量总量合理性校验",
     "    if not (PACK_WH_MIN <= wh <= PACK_WH_MAX):",
     "    if False:"),

    ("M7", "去掉「只在默认值时读容量」条件",
     '        return abs(float(cfg.get("capacity_wh", 0)) - 60.0) < 1e-6',
     "        return True"),

    ("M8", "powercfg 超时 25 -> 0",
     "POWERCAP_TIMEOUT = 25",
     "POWERCAP_TIMEOUT = 0"),

    ("M9", "开机自启去掉 demo 守卫",
     "        want = bool(self.autostart_var.get())\n"
     "        if self.demo:",
     "        want = bool(self.autostart_var.get())\n"
     "        if False:"),

    ("M10", "exe 自启命令去掉引号",
     '    if FROZEN:\n        return f\'"{exe}"\'',
     "    if FROZEN:\n        return exe"),

    ("M11", "关闭自启时值不存在返回 False",
     "    except FileNotFoundError:\n"
     "        # 关的时候值本来就不存在，算成功\n"
     "        return True",
     "    except FileNotFoundError:\n"
     "        return False"),

    ("M12", "SetSuspendState 休眠(1) -> 待机(0)",
     "ctypes.windll.powrprof.SetSuspendState(1, 1, 0)",
     "ctypes.windll.powrprof.SetSuspendState(0, 1, 0)"),

    ("M13", "失败提示窗去重失效",
     "        if self.fail_win is not None:\n"
     "            return\n"
     "        win = tk.Toplevel(self.root)",
     "        win = tk.Toplevel(self.root)"),

    ("C1", "【对照】_can_collapse 的 > 改成 >=",
     '        return pct > int(self.cfg.get("collapse_lock_below", 30))',
     '        return pct >= int(self.cfg.get("collapse_lock_below", 30))'),

    ("C2", "【对照】去掉 tick 的 demo 落盘守卫",
     "        if not self.demo and now - self._save_timer > 20:",
     "        if now - self._save_timer > 20:"),
]

def run(args):
    p = subprocess.run(args, cwd=REPO, capture_output=True, text=True,
                       encoding="utf-8", errors="ignore")
    return p.returncode, (p.stdout or "") + (p.stderr or "")

def check_anchors():
    """启动就校验所有锚点唯一，失配直接失败，而不是运行时静默跳过。

    这个项目连续三轮栽在同一件事上：改了源码，忘了同步变异脚本的锚点，
    于是「少跑了一项」被报告成「全部通过」。与其每次事后补，
    不如让它开不了口。
    """
    orig = open(SRC, encoding="utf-8").read()
    bad = []
    for t in MUTS:
        code, desc, old = (t[0], t[1], t[2]) if len(t) == 4 else (t[0], t[0], t[1])
        n = orig.count(old)
        if n != 1:
            bad.append((code, desc, n))
    if bad:
        print("锚点校验失败 —— 变异脚本和源码不同步：")
        for c, d, n in bad:
            print(f"  [{c}] 出现 {n} 次（必须 1）  {d}")
        print()
        print("改完源码要同步这里的锚点，否则「少跑一项」会被当成「全部通过」。")
        return False
    return True

def main():
    if not check_anchors():
        return 1
    orig = open(SRC, encoding="utf-8").read()
    caught = 0
    total = 0
    survivors = []
    skips = []

    print("=" * 66)
    print("第三轮变异验收（15 项）")
    print("=" * 66)

    try:
        for code, desc, old, new in MUTS:
            n = orig.count(old)
            if n != 1:
                print(f"  [{code}] 跳过：片段出现 {n} 次（必须唯一）  {desc}")
                skips.append((code, desc))
                continue
            total += 1
            open(SRC, "w", encoding="utf-8", newline="\n").write(orig.replace(old, new, 1))

            rc1, out1 = run([PY, "battery_guardian.py", "--self-test"])
            red1 = rc1 != 0 or "AssertionError" in out1

            red2 = False
            if not red1:
                rc2, out2 = run([PY, "selftest_ui.py"])
                red2 = rc2 != 0 or "FAIL" in out2

            if red1 or red2:
                caught += 1
                who = "self-test" if red1 else "selftest_ui"
                print(f"  [捕获 ✓] {code} {desc}   （{who}）")
            else:
                survivors.append((code, desc))
                print(f"  [漏网 ✗] {code} {desc}")

            open(SRC, "w", encoding="utf-8", newline="\n").write(orig)
    finally:
        open(SRC, "w", encoding="utf-8", newline="\n").write(orig)

    print("-" * 66)
    extra = []
    if survivors:
        extra.append(f"漏网 {len(survivors)}")
    if skips:
        extra.append(f"跳过 {len(skips)} 项（按规则判失败）")
    print(f"捕获 {caught} / {total} 项"
          + ("｜" + "｜".join(extra) if extra else "｜全部捕获"))
    if survivors:
        print("\n漏网清单（这些位置没有测试能发现被改坏）：")
        for code, desc in survivors:
            print(f"  {code}  {desc}")
    return 0 if (not survivors and not skips) else 1

if __name__ == "__main__":
    sys.exit(main())
