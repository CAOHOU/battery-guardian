# -*- coding: utf-8 -*-
"""验证 verify_exe.py 不会碰用户真实数据。

为什么需要这个测试
------------------
verify_exe.py 的初版为了「从干净的 cli.log 开始」，写了

    shutil.rmtree(os.path.dirname(os.path.dirname(CLI_LOG)))

而 CLI_LOG = %LOCALAPPDATA%\\BatteryGuardian\\data\\cli.log，
两次 dirname 得到的是**整个 BatteryGuardian 目录**——每打一次包
就把用户的 config.json / stats.json（账本、连续天数、21 天历史）删掉，
而且 ignore_errors=True 让它完全静默。

现在改成把子进程的 LOCALAPPDATA 指到临时目录，与真实数据完全隔离。
这个脚本就是钉住这个回归：造一个含 config.json / stats.json 的假 app_home，
跑完 verify_exe.py 后断言它们仍在。

用法
----
    python selftest_verify.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
VERIFY = os.path.join(HERE, "verify_exe.py")
EXE = os.path.join(HERE, "dist", "BatteryGuardian.exe")

KEEP = ("config.json", "stats.json")


def main():
    print("=" * 60)
    print("verify_exe.py 数据安全测试")
    print("=" * 60)

    if not os.path.exists(EXE):
        print("跳过：dist/BatteryGuardian.exe 不存在（先跑 build_exe.bat）")
        return 0

    fake = tempfile.mkdtemp(prefix="bg-verify-test-")
    data = os.path.join(fake, "BatteryGuardian", "data")
    os.makedirs(data)
    for name in KEEP:
        with open(os.path.join(data, name), "w", encoding="utf-8") as f:
            f.write('{"probe": true}')

    env = dict(os.environ)
    env["LOCALAPPDATA"] = fake

    # 反向断言：真实 data/ 的 mtime 也不能变。
    # 只验证「假 app_home 没被删」是不够的——如果将来有人把路径改成读真实
    # LOCALAPPDATA（比如用 expanduser("~") 拼而不是读环境变量），假目录当然还在，
    # 但用户真实数据已经被动了，而上面那三项照样 PASS。
    real_data = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                             "BatteryGuardian", "data")
    real_before = None
    if os.path.isdir(real_data):
        real_before = (os.stat(real_data).st_mtime_ns,
                       sorted(os.listdir(real_data)))

    try:
        p = subprocess.run([sys.executable, VERIFY], env=env,
                           capture_output=True, text=True, timeout=120)
        print(f"verify_exe.py 退出码 {p.returncode}")
        for line in (p.stdout or "").strip().splitlines():
            print(f"  | {line}")

        ok = True

        # 先确认 verify 自己跑成功了。少了这一条，「verify 崩溃退出」也会被
        # 当成通过——文件当然还在，因为它根本没跑到删除那一步。
        if p.returncode != 0:
            print(f"  FAIL verify_exe.py 本身失败（退出码 {p.returncode}）")
            if p.stderr:
                for line in p.stderr.strip().splitlines()[-6:]:
                    print(f"  | {line}")
            ok = False

        for name in KEEP:
            path = os.path.join(data, name)
            alive = os.path.exists(path)
            print(("  PASS" if alive else "  FAIL") +
                  f" {name} {'仍在' if alive else '被删了 —— 这会清掉用户账本'}")
            ok = ok and alive

        # 目录本身也不能被删
        alive_dir = os.path.isdir(data)
        print(("  PASS" if alive_dir else "  FAIL") +
              f" data 目录 {'仍在' if alive_dir else '被删了'}")
        ok = ok and alive_dir

        # 真实目录必须原封不动
        if real_before is None:
            print("  SKIP 真实 data/ 不存在，跳过 mtime 反向断言")
        else:
            real_after = (os.stat(real_data).st_mtime_ns,
                          sorted(os.listdir(real_data)))
            same = real_after == real_before
            print(("  PASS" if same else "  FAIL") +
                  f" 真实 data/ 未被触碰（mtime + 文件列表）")
            if not same:
                print(f"  | 运行前 {real_before}")
                print(f"  | 运行后 {real_after}")
            ok = ok and same
    finally:
        shutil.rmtree(fake, ignore_errors=True)

    print("=" * 60)
    print("全部通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
