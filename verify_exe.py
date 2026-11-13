# -*- coding: utf-8 -*-
"""打包后验证 exe —— 由 build_exe.bat 自动调用。

为什么需要这一步
----------------
打包时为了减体积排除了不少模块（ssl / xml / sqlite3 …）。
如果代码里 import 了被排除的模块，**exe 不会干净报错，而是直接卡死**——
`--where` 正常、`--self-test` 永远不返回，cli.log 是空的。
这种问题源码测试全绿，只有真的跑一次 exe 才会暴露。

所以：每次打包后自动跑一次 `--self-test`，超时就判失败。
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "BatteryGuardian.exe")
CLI_LOG = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                       "BatteryGuardian", "data", "cli.log")
TIMEOUT = 40


def main():
    if not os.path.exists(EXE):
        print("[verify] 找不到 dist/BatteryGuardian.exe")
        return 1

    # 每次从干净的 cli.log 开始，否则会读到上一次的输出
    shutil.rmtree(os.path.dirname(os.path.dirname(CLI_LOG)), ignore_errors=True)

    try:
        p = subprocess.run([EXE, "--self-test"], timeout=TIMEOUT,
                           capture_output=True)
    except subprocess.TimeoutExpired:
        print(f"[verify] 失败：--self-test 超过 {TIMEOUT} 秒没有返回。")
        print("         最可能的原因：代码里 import 了被 --exclude-module 排除的模块，")
        print("         PyInstaller 环境下会卡死而不是报错。")
        return 1

    if p.returncode != 0:
        print(f"[verify] 失败：--self-test 退出码 {p.returncode}")
        return 1

    try:
        with open(CLI_LOG, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        print(f"[verify] 失败：读不到 {CLI_LOG}")
        return 1

    if "全部通过" not in text:
        print("[verify] 失败：cli.log 里没有「全部通过」")
        print(text[:500])
        return 1

    size = os.path.getsize(EXE)
    print(f"[verify] OK  exe {size:,} 字节，自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
