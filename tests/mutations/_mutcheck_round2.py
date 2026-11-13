# -*- coding: utf-8 -*-
"""第二轮审查列的 N1-N8 + P1 守卫，逐条验证现在能不能被抓到。

改文件用文本替换、finally 恢复，不用 git checkout。
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

MUTS = [
    ("N1 删掉保底层提示音",
     '        self._alert("failsafe")\n', ""),
    ("N2 右键置灰阈值改成 0",
     'pct <= int(self.cfg["thresholds"]["nudge"])', "pct <= 0"),
    # 这个片段必须带上前面的 except，否则会命中 __init__ 里同名的两行赋值
    ("N3 _close_dialog 不重置 dialog_grow",
     "            except tk.TclError:\n"
     "                pass\n"
     "        self.dialog = None\n"
     "        self.dialog_grow = 0\n",
     "            except tk.TclError:\n"
     "                pass\n"
     "        self.dialog = None\n"),
    ("N4 最急那档音色降级",
     '"failsafe": "SystemHand",', '"failsafe": "SystemAsterisk",'),
    ("N5 提示音改同步播放",
     "winsound.SND_ALIAS | winsound.SND_ASYNC", "winsound.SND_ALIAS"),
    ("N6 _sync_geometry 丢掉左边界钳制",
     "self.hud_x = max(0, min(self.hud_x, sw - self.hud_w))",
     "self.hud_x = min(self.hud_x, sw - self.hud_w)"),
    ("N7 --panel failsafe 不强制 demo",
     "if force_demo_for_panel(app, panel):", "if False:"),
    ("N8 _fit 的 min_w 保底失效",
     "        if min_w:\n            w = max(w, min_w)",
     "        if False:\n            w = max(w, min_w)"),
    ("P1a 休眠失败后仍重开保底层",
     '            if (self.cfg.get("enable_fail_safe") and not self.demo\n'
     "                    and not self._hibernate_failed):",
     '            if (self.cfg.get("enable_fail_safe") and not self.demo):'),
    ("P1b 休眠失败不置标记",
     '        self._hibernate_failed = True\n        log("休眠失败，停止重试',
     '        self._hibernate_failed = False\n        log("休眠失败，停止重试'),
]

def run(cmd):
    p = subprocess.run(cmd, cwd=REPO, capture_output=True, timeout=240)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "ignore")

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
    with open(SRC, encoding="utf-8", newline="") as f:
        orig = f.read()

    caught = 0
    total = 0
    skips = []
    try:
        for name, old, new in MUTS:
            if old not in orig:
                print(f"  [跳过] {name}：源码里找不到该片段")
                skips.append(name)
                continue
            total += 1
            with open(SRC, "w", encoding="utf-8", newline="") as f:
                f.write(orig.replace(old, new, 1))

            rc1, out1 = run([PY, "battery_guardian.py", "--self-test"])
            red1 = rc1 != 0 or "AssertionError" in out1
            if red1:
                caught += 1
                print(f"  [捕获 ✓] {name}   （self-test 变红）")
                continue

            rc2, out2 = run([PY, "selftest_ui.py"])
            if rc2 != 0 or "FAIL" in out2:
                caught += 1
                print(f"  [捕获 ✓] {name}   （selftest_ui 变红）")
            else:
                print(f"  [漏网 ✗] {name}")

            with open(SRC, "w", encoding="utf-8", newline="") as f:
                f.write(orig)
    finally:
        with open(SRC, "w", encoding="utf-8", newline="") as f:
            f.write(orig)

    print(f"\n结果：{caught} / {total} 捕获"
          + (f"｜跳过 {len(skips)} 项（按规则判失败）" if skips else ""))
    if skips:
        print("跳过清单：")
        for n in skips:
            print(f"  {n}")
    return 0 if (caught == total and not skips) else 1

if __name__ == "__main__":
    sys.exit(main())
