# -*- coding: utf-8 -*-
"""第四轮审查列出的 10 项变异，逐项验证测试是否真能抓住。

这轮的重点是「上一轮漏网的那几个位置现在有没有补上」：
Q2（容量容差）、Q6（py 输出解析）、Q7（py.exe 调用点）、Q9（demo 守卫）。

用法
----
    python _mutcheck_round4.py
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
    ("Q1", "clamp_tick_dt 允许负间隔",
     "    return min(60.0, max(0.0, now - last))",
     "    return min(60.0, now - last)"),

    ("Q2", "should_autofill 容差 1e-6 -> 1.0",
     '        return abs(float(cfg.get("capacity_wh", 0)) - 60.0) < 1e-6',
     '        return abs(float(cfg.get("capacity_wh", 0)) - 60.0) < 1.0'),

    ("Q3", "去掉无 <Battery> 段时的全文回退",
     '    blocks = re.findall(r"<Battery(?:\\s[^>]*)?>(.*?)</Battery>",\n'
     "                        xml_text, re.S) or [xml_text]",
     '    blocks = re.findall(r"<Battery(?:\\s[^>]*)?>(.*?)</Battery>",\n'
     "                        xml_text, re.S)"),

    ("Q4", "容量标签去掉属性支持",
     'r"<FullChargeCapacity(?:\\s[^>]*)?>\\s*([0-9]+(?:\\.[0-9]+)?)\\s*</FullChargeCapacity>"',
     'r"<FullChargeCapacity>\\s*([0-9]+(?:\\.[0-9]+)?)\\s*</FullChargeCapacity>"'),

    ("Q5", "多电池累加改成只留最后一块",
     "        if mwh and CELL_MWH_MIN <= mwh <= CELL_MWH_MAX:\n            total_mwh += mwh",
     "        if mwh and CELL_MWH_MIN <= mwh <= CELL_MWH_MAX:\n            total_mwh = mwh"),

    ("Q6", "py 输出解析不再要求带 * 的默认行",
     '    for line in text.splitlines():\n        if "*" not in line:\n            continue',
     "    for line in text.splitlines():"),

    ("Q7", "_autostart_command 不再识别 py.exe",
     '    if os.path.basename(exe).lower() == "py.exe":',
     "    if False:"),

    ("Q8", "tick 的 TclError 改回直接 return",
     "            self._note_tick_failure(e)\n        except Exception as e:",
     "            return\n        except Exception as e:"),

    ("Q9", "_tick_once 去掉 demo 守卫",
     '        if not self.demo and not p["ac"] and p["pct"] is not None:\n'
     "            record_on_battery(self.stats, dt)",
     '        if not p["ac"] and p["pct"] is not None:\n'
     "            record_on_battery(self.stats, dt)"),

    ("C1", "【对照】_can_collapse 的 > 改成 >=",
     '        return pct > int(self.cfg.get("collapse_lock_below", 30))',
     '        return pct >= int(self.cfg.get("collapse_lock_below", 30))'),

    # ---- 第五轮审查点名的漏网项（R 系列）----
    ("R2", "单块容量下限 1Wh 去掉",
     "        if mwh and CELL_MWH_MIN <= mwh <= CELL_MWH_MAX:",
     "        if mwh and mwh <= CELL_MWH_MAX:"),

    ("R3", "容量总量上限 400Wh 去掉",
     "    if not (PACK_WH_MIN <= wh <= PACK_WH_MAX):",
     "    if not (PACK_WH_MIN <= wh):"),

    ("R4", "TICK_FAIL_WARN 6 -> 1（误报）",
     "TICK_FAIL_WARN = 6",
     "TICK_FAIL_WARN = 1"),

    ("R4b", "TICK_FAIL_WARN 6 -> 100（永不报）",
     "TICK_FAIL_WARN = 6",
     "TICK_FAIL_WARN = 100"),

    ("R7", "tick 成功时不再调 _note_tick_ok",
     "        else:\n            self._tick_tcl_fails = 0\n            self._note_tick_ok()",
     "        else:\n            self._tick_tcl_fails = 0"),

    ("R9", "_resolve_py_launcher 去掉存在性检查",
     "    return cand if cand and os.path.exists(cand) else None",
     "    return cand"),

    ("R11", "py -0p 超时 3 -> 10",
     "PY_LAUNCHER_TIMEOUT = 3",
     "PY_LAUNCHER_TIMEOUT = 10"),

    ("R12", "TclError 不再单独计数",
     "            self._tick_tcl_fails += 1\n            self._note_tick_failure(e)",
     "            self._note_tick_failure(e)"),
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
    print("第四轮变异验收")
    print("=" * 66)

    try:
        for code, desc, old, new in MUTS:
            n = orig.count(old)
            if n != 1:
                print(f"  [{code}] 跳过：片段出现 {n} 次（必须唯一）  {desc}")
                skips.append((code, desc))
                continue
            total += 1
            open(SRC, "w", encoding="utf-8", newline="\n").write(
                orig.replace(old, new, 1))

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
        print("\n漏网清单：")
        for code, desc in survivors:
            print(f"  {code}  {desc}")
    return 0 if (not survivors and not skips) else 1

if __name__ == "__main__":
    sys.exit(main())
