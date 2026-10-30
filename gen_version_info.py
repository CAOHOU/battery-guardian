# -*- coding: utf-8 -*-
"""
从 battery_guardian.py 读取元信息，生成 PyInstaller 用的 version_info.txt。

用 ast 解析而不是 import —— 为了拿几个字符串把 tkinter 拉起来不划算。

build_exe.bat 会在打包前自动调用它，所以改署名只需要改 battery_guardian.py
顶部那几个常量，exe 属性里的「公司 / 版权 / 版本」会跟着变。

单独运行：python gen_version_info.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "battery_guardian.py")
OUT = os.path.join(HERE, "version_info.txt")

WANT = ("__version__", "__author__", "__license__", "__year__",
        "REPO_NAME", "APP_NAME")


def read_meta(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    meta = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in WANT:
                try:
                    meta[target.id] = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    pass
    return meta


def version_tuple(ver):
    parts = (str(ver).split(".") + ["0", "0", "0", "0"])[:4]
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)


def main():
    if not os.path.exists(SRC):
        print(f"找不到 {SRC}")
        return 1

    meta = read_meta(SRC)
    for key in ("__version__", "__author__"):
        if not meta.get(key):
            print(f"缺少元信息：{key}")
            return 1

    ver = str(meta["__version__"])
    author = str(meta["__author__"])
    year = str(meta.get("__year__", ""))
    name = str(meta.get("APP_NAME", "BatteryGuardian"))
    vt = version_tuple(ver)

    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={vt},
    prodvers={vt},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404b0',
        [StringStruct('CompanyName', '{author}'),
         StringStruct('FileDescription', '{name}'),
         StringStruct('FileVersion', '{ver}'),
         StringStruct('InternalName', 'BatteryGuardian'),
         StringStruct('LegalCopyright', 'Copyright (c) {year} {author}'),
         StringStruct('OriginalFilename', 'BatteryGuardian.exe'),
         StringStruct('ProductName', '{name}'),
         StringStruct('ProductVersion', '{ver}')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"version_info.txt -> {name} v{ver} / {author} / {meta.get('__license__')}")

    # 一致性检查：LICENSE 里的署名没跟着改的话提醒一下
    lic_path = os.path.join(HERE, "LICENSE")
    if os.path.exists(lic_path):
        with open(lic_path, encoding="utf-8") as f:
            if author not in f.read():
                print(f"[警告] LICENSE 里没有出现署名「{author}」，记得一起改。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
