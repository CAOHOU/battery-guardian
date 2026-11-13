# -*- coding: utf-8 -*-
"""打包后验证 exe —— 由 build_exe.bat 自动调用。

为什么需要这一步
----------------
打包时为了减体积排除了不少模块（ssl / xml / sqlite3 …）。
如果代码里 import 了被排除的模块，**exe 不会干净报错，而是直接卡死**——
`--where` 正常、`--self-test` 永远不返回，cli.log 是空的。
这种问题源码测试全绿，只有真的跑一次 exe 才会暴露。

所以：每次打包后自动跑一次 `--self-test`，超时就判失败。

为什么用临时 LOCALAPPDATA 而不是删真实 cli.log
-----------------------------------------------
初版为了「从干净的 cli.log 开始」，写了
    shutil.rmtree(os.path.dirname(os.path.dirname(CLI_LOG)))
而 CLI_LOG = %LOCALAPPDATA%\\BatteryGuardian\\data\\cli.log，
两次 dirname 得到的是**整个 BatteryGuardian 目录**，于是每打一次包
就把用户的 config.json / stats.json（账本、连续天数、21 天历史）一起删掉，
而且 ignore_errors=True 让它完全静默。

现在改成：把子进程的 LOCALAPPDATA 指到一个临时目录。exe 的 app_home
由这个变量推导，所以整个验证过程与用户真实数据完全隔离——
不需要"删掉再重建"，也不存在删错的可能。
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "BatteryGuardian.exe")
TIMEOUT = 40


def main():
    if not os.path.exists(EXE):
        print("[verify] 找不到 dist/BatteryGuardian.exe")
        return 1

    with tempfile.TemporaryDirectory(prefix="bg-verify-") as tmp:
        # 子进程看到的 LOCALAPPDATA 是临时目录 → exe 的 app_home 落在 tmp 里，
        # 用户的 config.json / stats.json 全程不被读写。
        env = dict(os.environ)
        env["LOCALAPPDATA"] = tmp
        cli_log = os.path.join(tmp, "BatteryGuardian", "data", "cli.log")

        try:
            p = subprocess.run([EXE, "--self-test"], timeout=TIMEOUT,
                               capture_output=True, env=env)
        except subprocess.TimeoutExpired:
            print(f"[verify] 失败：--self-test 超过 {TIMEOUT} 秒没有返回。")
            print("         最可能的原因：代码里 import 了被 --exclude-module 排除的模块，")
            print("         PyInstaller 环境下会卡死而不是报错。")
            return 1

        if p.returncode != 0:
            print(f"[verify] 失败：--self-test 退出码 {p.returncode}")
            return 1

        try:
            with open(cli_log, encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            print(f"[verify] 失败：读不到 {cli_log}")
            return 1

    if "全部通过" not in text:
        print("[verify] 失败：cli.log 里没有「全部通过」")
        print(text[:500])
        return 1

    size = os.path.getsize(EXE)
    print(f"[verify] OK  exe {size:,} 字节，自检通过（临时目录已清理，未碰真实数据）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
