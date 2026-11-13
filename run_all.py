# -*- coding: utf-8 -*-
"""统一验收入口：串行跑完全部门禁，只有一套退出码语义。

**为什么要有这个**：原来 8 条命令靠人手动按顺序跑、肉眼判断末行。
于是「打印了跳过但 rc=0」这种假绿能活两轮——因为没人把退出码当回事。

现在：任一条 rc≠0、或输出里出现禁止词（跳过/SKIP/Traceback/语法错误），
整条门禁就红。跑完还要验两件事：
  1. 源码被变异脚本完整还原（sha + 行数）
  2. 真实 data/ 一个字节都没变（逐文件 hash）

用法:
    python run_all.py                    # 全部（含 3 个变异脚本，约 7 分钟）
    python run_all.py --skip-mutations   # 只跑前 5 条（约 1 分钟）
"""
import hashlib
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
SRC = os.path.join(HERE, "battery_guardian.py")
EXE = os.path.join(HERE, "dist", "BatteryGuardian.exe")
BAD_WORDS = ("跳过", "SKIP", "Traceback", "SyntaxError", "ModuleNotFoundError")


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def real_data_dir():
    """真实数据目录，必须用产品自己的选址逻辑。

    原来这里是 `os.environ.get("LOCALAPPDATA", "")` 自己拼路径。
    独立全项目审查 P1-3 指出后果：环境里没有 LOCALAPPDATA 时（CI、精简
    shell、服务账户），拼出来是相对路径 `BatteryGuardian/data`，目录不存在
    → data_snapshot() 返回 None → `safe = (before is None or ...)` 为真
    → 门禁打印「PASS 真实 data/ 未变 无该目录」并 rc=0。
    也就是说这条判据在那种环境里**根本没执行**，却计入通过。
    而产品自己的 `_app_data_root()` 会回退到 USERPROFILE\\AppData\\Local，
    能找到真实目录——同一个坑 selftest_verify.py 早就踩过并改掉了。
    """
    sys.path.insert(0, HERE)
    from battery_guardian import _app_data_root
    return os.path.join(_app_data_root(), "BatteryGuardian", "data")


def data_snapshot():
    p = real_data_dir()
    if not os.path.isdir(p):
        return None
    return {n: sha(os.path.join(p, n))
            for n in sorted(os.listdir(p))
            if os.path.isfile(os.path.join(p, n))}


def commands(skip_mut):
    cs = [
        ("源码自检", [PY, SRC, "--self-test"], 120),
        ("数据隔离回归", [PY, os.path.join(HERE, "selftest_isolation.py")], 300),
        ("界面冒烟", [PY, os.path.join(HERE, "selftest_ui.py")], 300),
        ("布局矩阵", [PY, os.path.join(HERE, "selftest_layout.py")], 300),
        ("打包数据安全", [PY, os.path.join(HERE, "selftest_verify.py")], 300),
    ]
    if not skip_mut:
        for r in (2, 3, 4):
            cs.append((f"变异 round{r}",
                       [PY, os.path.join(HERE, "tests", "mutations",
                                         f"_mutcheck_round{r}.py")], 600))
    return cs


def main():
    skip_mut = "--skip-mutations" in sys.argv
    allow_no_data = "--allow-no-data-dir" in sys.argv
    src_before = sha(SRC)
    lines_before = len(open(SRC, encoding="utf-8").readlines())
    data_before = data_snapshot()
    results = []

    print("=" * 70)
    print("battery-guardian 验收入口")
    print("=" * 70)
    for name, cmd, timeout in commands(skip_mut):
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=HERE, capture_output=True, timeout=timeout)
            out = (p.stdout + p.stderr).decode("utf-8", "ignore")
            rc = p.returncode
            note = out.strip().splitlines()[-1][:60] if out.strip() else ""
        except subprocess.TimeoutExpired:
            rc, out, note = 1, "", f"超时 {timeout}s"
        except OSError as e:
            rc, out, note = 1, "", f"启动失败: {e}"
        bad = [w for w in BAD_WORDS if w in out]
        ok = (rc == 0 and not bad)
        why = []
        if rc != 0:
            why.append(f"rc={rc}")
        if bad:
            why.append(f"输出含禁止词 {bad}" + ("（疑似假绿）" if rc == 0 else ""))
        detail = f"{note}" + (f"  ← {'; '.join(why)}" if why else "")
        results.append((name, ok, rc, time.time() - t0, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name:16s} rc={rc:3d} "
              f"{time.time() - t0:5.1f}s  {detail[:120]}")

    # 源码必须被变异脚本完整还原
    src_after = sha(SRC)
    lines_after = len(open(SRC, encoding="utf-8").readlines())
    restored = (src_after == src_before and lines_after == lines_before)
    print(f"  {'PASS' if restored else 'FAIL'}  源码已还原        "
          f"sha={src_before[:8]}→{src_after[:8]} 行数={lines_before}→{lines_after}")

    # 真实数据目录不能被任何一条命令改写。
    # 三种情况分开判，别用一个 `or` 把「没查」混成「通过」：
    #   1. 前后都不存在        → 全新机器，判据无法执行（默认判红，可显式豁免）
    #   2. 跑之前不存在、跑之后出现 → 有命令在写真实目录，直接红
    #   3. 都存在              → 逐文件 hash 比对
    data_after = data_snapshot()
    if data_before is None and data_after is None:
        if allow_no_data:
            safe = True
            data_note = "目录不存在（已用 --allow-no-data-dir 豁免）"
        else:
            safe = False
            data_note = "目录不存在，判据未执行（全新机器可加 --allow-no-data-dir）"
    elif data_before is None:
        safe = False
        data_note = (f"跑之前不存在，跑之后出现 {len(data_after)} 个文件"
                     f" —— 有命令在写真实目录")
    else:
        safe = (data_before == data_after)
        data_note = "逐文件 hash 一致" if safe else "内容发生变化"
    print(f"  {'PASS' if safe else 'FAIL'}  真实 data/ 未变    {data_note}")

    print("=" * 70)
    all_ok = all(r[1] for r in results) and restored and safe
    print("门禁通过" if all_ok else "门禁失败（任一 FAIL 即不放行）")
    print("=" * 70)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
