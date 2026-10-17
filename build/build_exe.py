"""Assemble the XX Down runtime: official CPython embeddable + PySide6 + app.

Repo layout
-----------
    XX-Down/
    ├── ytdlp_gui.py          the app
    ├── launcher.c            tiny C launcher (compiled at build time)
    ├── 使用说明.txt
    ├── build/
    │   ├── build_exe.py      this script
    │   ├── installer.iss     Inno Setup 6 script
    │   ├── ChineseSimplified.isl
    │   └── xlogo4.ico        app icon
    └── vendor/               (gitignored; download dependencies here)
        ├── python-3.13.11-embed-amd64.zip
        ├── pyside/           PySide6 6.11.2 Essentials + shiboken6
        ├── site-packages/    pywin32 311, win10toast 0.9, setuptools 80.10.2
        └── bin/              yt-dlp.exe, ffmpeg.exe, ffprobe.exe, deno.exe

Build outputs (gitignored): XX Down.exe, python/, .install/, installer exe.

Why not PyInstaller
-------------------
360 AV heuristic (Trojan/Python.ShellLoader.am) quarantines the PyInstaller
build of this app when the Inno installer writes it (the setup process runs
from %TEMP%, which raises the suspicion score). Verified 2026-10-08:
  - plain PyInstaller launchers (hello / PySide6 / pywin32) pass in %TEMP%
  - this app's PyInstaller build (onefile AND onedir) is quarantined
  - official python.exe/pythonw.exe (signed, known hash) is not flagged
So we ship the official CPython embeddable runtime + plain source + a tiny C
launcher instead of a PyInstaller bundle.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "build"
VENDOR = ROOT / "vendor"
EMBED_ZIP = VENDOR / "python-3.13.11-embed-amd64.zip"
PYSIDE_SRC = VENDOR / "pyside"
BASE_SP = VENDOR / "site-packages"
BIN_DIR = VENDOR / "bin"
OUT = ROOT

# PySide6: only what the app imports (QtCore/QtGui/QtSvg/QtWidgets) plus the
# Qt DLLs proven sufficient by PyInstaller's dependency analysis.
PYSIDE_FILES = (
    "__init__.py", "_config.py", "PySide6_Essentials.json",
    "QtCore.pyd", "QtGui.pyd", "QtSvg.pyd", "QtWidgets.pyd",
    "pyside6.abi3.dll",
    "Qt6Core.dll", "Qt6Gui.dll", "Qt6Network.dll", "Qt6Svg.dll", "Qt6Widgets.dll",
    "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_codecvt_ids.dll",
    "vcruntime140.dll", "vcruntime140_1.dll",
)
PYSIDE_DIRS = ("glue", "metatypes", "resources", "support", "translations")
PYSIDE_PLUGINS = ("platforms", "styles", "imageformats", "iconengines", "generic")

# pywin32 311 + win10toast: full package dirs (binaries live in win32\ and
# pywin32_system32\; pywin32.pth wires the paths at interpreter startup)
SP_ITEMS = (
    "win32", "pywin32_system32", "win32com", "win32comext",
    "pythoncom.py", "pywin32.pth", "pywin32.version.txt",
    "pywin32-311.dist-info", "win10toast", "win10toast-0.9.dist-info",
    # win10toast needs pkg_resources (shipped with setuptools, absent from
    # the CPython embeddable package)
    "setuptools", "pkg_resources", "setuptools-80.10.2.dist-info",
)


def find_gcc() -> Path:
    env = os.environ.get("XXDOWN_GCC")
    if env:
        p = Path(env)
        if p.is_file():
            return p
        raise SystemExit("XXDOWN_GCC does not point to gcc.exe: %s" % env)
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if winget.is_dir():
        for d in sorted(winget.iterdir()):
            if "llvm-mingw" in d.name.lower():
                for gcc in d.rglob("bin/gcc.exe"):
                    return gcc
    raise SystemExit(
        "gcc.exe (LLVM MinGW) not found.\n"
        "Install it with:  winget install MartinStorsjo.LLVM-MinGW.UCRT\n"
        "or set XXDOWN_GCC to the full path of gcc.exe")


def require(path: Path, hint: str):
    if not path.exists():
        raise SystemExit("missing %s\n%s" % (path, hint))


def copy_tree_no_pycache(src, dst):
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))


def main() -> int:
    require(EMBED_ZIP,
            "Download python-3.13.11-embed-amd64.zip from python.org into vendor/")
    require(PYSIDE_SRC,
            "vendor/pyside/ must contain PySide6 6.11.2 Essentials + shiboken6\n"
            "(pip download PySide6-Essentials==6.11.2 shiboken6==6.11.2, unpack the wheels)")
    require(BASE_SP,
            "vendor/site-packages/ must contain pywin32 311, win10toast 0.9, setuptools 80.10.2\n"
            "(pip download pywin32==311 win10toast==0.9 setuptools==80.10.2, unpack the wheels)")
    for name in ("yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe", "deno.exe"):
        require(BIN_DIR / name,
                "vendor/bin/%s is missing (see README '构建' for download links)" % name)
    gcc = find_gcc()
    windres = gcc.parent / "windres.exe"
    require(windres, "windres.exe must sit next to gcc.exe")

    # 1) C launcher
    OUT.mkdir(parents=True, exist_ok=True)
    launcher = OUT / "XX Down.exe"
    if launcher.exists():
        launcher.unlink()
    rc = BT / "launcher.rc"
    rc.write_text('1 ICON "%s"\n' % (BT / "xlogo4.ico"), encoding="utf-8")
    res = BT / "launcher.res"
    r = subprocess.run([str(windres), str(rc), "-O", "coff", "-o", str(res)],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        print("windres failed")
        print(r.stderr[-2000:])
        return 3
    r = subprocess.run([str(gcc), "-O2", "-municode", "-mwindows", "-static",
                        "-o", str(launcher), str(ROOT / "launcher.c"), str(res),
                        "-lshell32"],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0 or not launcher.is_file():
        print("launcher build failed")
        print(r.stderr[-3000:])
        return 3

    # 2) official CPython embeddable runtime
    pydir = OUT / "python"
    if pydir.exists():
        shutil.rmtree(pydir)
    with zipfile.ZipFile(EMBED_ZIP) as z:
        z.extractall(pydir)
    (pydir / "python313._pth").write_text(
        "python313.zip\n.\nLib\\site-packages\nimport site\n", encoding="utf-8")
    sp = pydir / "Lib" / "site-packages"
    sp.mkdir(parents=True)

    # 3) curated PySide6
    ps = sp / "PySide6"
    ps.mkdir()
    for name in PYSIDE_FILES:
        src = PYSIDE_SRC / "PySide6" / name
        if not src.is_file():
            print("FATAL: missing PySide6 file:", src)
            return 4
        shutil.copy2(src, ps / name)
    for name in PYSIDE_DIRS:
        copy_tree_no_pycache(PYSIDE_SRC / "PySide6" / name, ps / name)
    plugins = ps / "plugins"
    for name in PYSIDE_PLUGINS:
        copy_tree_no_pycache(PYSIDE_SRC / "PySide6" / "plugins" / name, plugins / name)

    # 4) shiboken6
    copy_tree_no_pycache(PYSIDE_SRC / "shiboken6", sp / "shiboken6")

    # 5) pywin32 + win10toast
    for name in SP_ITEMS:
        src = BASE_SP / name
        if not src.exists():
            print("FATAL: missing site-packages item:", src)
            return 4
        if src.is_dir():
            copy_tree_no_pycache(src, sp / name)
        else:
            shutil.copy2(src, sp / name)

    # 6) stage the installer payload (explicit list; the dev root holds
    #    cookies/settings/logs/build tools that must not ship)
    stage = ROOT / ".install"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    shutil.copytree(pydir, stage / "python",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("XX Down.exe", "ytdlp_gui.py", "\u4f7f\u7528\u8bf4\u660e.txt"):
        shutil.copy2(ROOT / name, stage / name)
    for name in ("yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe", "deno.exe"):
        shutil.copy2(BIN_DIR / name, stage / name)

    # 7) smoke test (console python so output is visible)
    print("smoke test...")
    try:
        p = subprocess.run([str(pydir / "python.exe"), str(OUT / "ytdlp_gui.py"),
                            "--smoke"], capture_output=True, text=True,
                           errors="replace", timeout=30)
    except subprocess.TimeoutExpired:
        print("SMOKE TIMEOUT")
        return 5
    if p.returncode != 0:
        print("SMOKE FAILED rc", p.returncode)
        print(p.stdout[-3000:])
        print(p.stderr[-3000:])
        return 5
    print("SMOKE_OK")

    total = sum(f.stat().st_size for f in stage.rglob("*") if f.is_file())
    print("assembled -> %s (staged %.1f MB, launcher sha256 %s)"
          % (OUT, total / 1048576,
             hashlib.sha256(launcher.read_bytes()).hexdigest()[:16].upper()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
