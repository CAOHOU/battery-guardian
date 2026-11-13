# -*- coding: utf-8 -*-
"""自检绝不触碰真实数据目录。

**为什么要有这个文件**：`--self-test` 曾经把用户 11 天的 stats.json 覆盖成
107B 损坏载荷，退出码还是 0、照样打印「全部通过」。八条验收命令和 43 项变异
全部报绿——因为它们只验证「断言通过」，从不验证「跑完之后用户数据还在」。

用法：
    python selftest_isolation.py                 # 源码 + exe 两条入口
    python selftest_isolation.py --source-only   # 未打包时只验源码（显式豁免）

退出码 0=通过 / 1=失败。按项目「跳过 = 失败」规程：默认缺 exe 即判失败。

四个设计要点，缺一不可：
1. exe 模式的哨兵放在**隔离 LOCALAPPDATA** 里，不是去检查真实目录
   （FROZEN 时 APP_HOME 由 LOCALAPPDATA 推导，只盯真实目录永远看不到污染）
2. 断言**内容 / hash 不变**，不是只看目录 mtime（mtime 对 open(w) 原地改写无感）
3. 要求 rc==0 **且**输出含成功信号，防止「自检根本没跑起来」被当成通过
4. 源码模式与 exe 模式**分别验**
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "BatteryGuardian.exe")
PY = sys.executable
SELFTEST_SRC = os.path.join(HERE, "battery_guardian.py")
SUCCESS_MARK = "日志轮转全部通过"   # _self_test_body() 末行固定打印


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def snapshot(data_dir):
    return {n: sha(os.path.join(data_dir, n))
            for n in sorted(os.listdir(data_dir))
            if os.path.isfile(os.path.join(data_dir, n))}


def diff(before, after):
    return sorted(k for k in set(before) | set(after)
                  if before.get(k) != after.get(k))


def check_source_mode():
    """源码模式 FROZEN=False，APP_HOME=脚本目录 ⇒ 哨兵就是 repo/data/。"""
    data = os.path.join(HERE, "data")
    os.makedirs(data, exist_ok=True)
    sentinel = os.path.join(data, "stats.json")
    if not os.path.exists(sentinel):
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write('{"streak_days": 7, "incidents": 7, "days": {}}\n')
    before = snapshot(data)
    try:
        p = subprocess.run([PY, SELFTEST_SRC, "--self-test"], cwd=HERE,
                           capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("  FAIL 源码模式: 超时")
        return False
    out = (p.stdout + p.stderr).decode("utf-8", "ignore")
    changed = diff(before, snapshot(data))
    problems = []
    if p.returncode != 0:
        problems.append(f"rc={p.returncode}")
    if SUCCESS_MARK not in out:
        problems.append("输出缺少成功信号（自检可能没跑完）")
    if changed:
        problems.append(f"data/ 被改动: {changed}")
    ok = not problems
    print(f"  {'PASS' if ok else 'FAIL'} 源码模式: "
          + ("data/ 文件 hash 全不变、自检成功" if ok else "；".join(problems)))
    return ok


def check_exe_mode():
    """exe 模式：造隔离 LOCALAPPDATA + 哨兵，跑 exe --self-test。"""
    root = tempfile.mkdtemp(prefix="bg-isolation-exe-")
    try:
        data = os.path.join(root, "BatteryGuardian", "data")
        os.makedirs(data, exist_ok=True)
        for name, body in (
            ("config.json", '{"kwh_price": 0.6}\n'),
            ("stats.json", '{"streak_days": 7, "incidents": 7, "days": {}}\n'),
            ("app.log", "[2026-11-01 13:34:39] canary\n"),
            ("cli.log", ""),
        ):
            with open(os.path.join(data, name), "w", encoding="utf-8") as f:
                f.write(body)
        env = dict(os.environ)
        env["LOCALAPPDATA"] = root
        env["TEMP"] = env["TMP"] = os.path.join(root, "tmp")
        os.makedirs(env["TEMP"], exist_ok=True)

        before = snapshot(data)
        try:
            p = subprocess.run([EXE, "--self-test"], cwd=HERE, env=env,
                               capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            print("  FAIL exe 模式: 超时")
            return False
        cli = os.path.join(data, "cli.log")
        out = ""
        if os.path.exists(cli):
            with open(cli, encoding="utf-8", errors="ignore") as f:
                out = f.read()
        out += (p.stdout + p.stderr).decode("utf-8", "ignore")
        after = snapshot(data)
        # cli.log 会被 exe 正常覆写，属允许；只校验另三个数据文件
        b = {k: v for k, v in before.items() if k != "cli.log"}
        a = {k: v for k, v in after.items() if k != "cli.log"}
        changed = diff(b, a)
        problems = []
        if p.returncode != 0:
            problems.append(f"rc={p.returncode}")
        if SUCCESS_MARK not in out:
            problems.append("cli.log 缺少成功信号（自检可能没跑完）")
        if changed:
            problems.append(f"假 data/ 被改动: {changed}（含 stats.json 覆写）")
        ok = not problems
        print(f"  {'PASS' if ok else 'FAIL'} exe 模式: "
              + ("哨兵文件 hash 全不变、自检成功" if ok else "；".join(problems)))
        return ok
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    print("=" * 66)
    print("自检数据隔离回归测试")
    print("=" * 66)
    results = [check_source_mode()]
    if os.path.exists(EXE):
        results.append(check_exe_mode())
    elif "--source-only" in sys.argv:
        print("  SKIP exe 模式: 未打包（--source-only 显式豁免）")
        print("       注意：此项未覆盖，不得据此宣称『两条入口都验过』")
    else:
        print("  FAIL exe 模式: 缺 dist/BatteryGuardian.exe，按『跳过=失败』判失败")
        results.append(False)
    print("=" * 66)
    ok = all(results)
    print("全部通过" if ok else "失败（存在数据污染或未覆盖入口）")
    print("=" * 66)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
