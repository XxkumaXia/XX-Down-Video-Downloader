# -*- coding: utf-8 -*-
"""xxdown_cli —— 给本地 agent / 脚本调用的命令行接口（不弹窗、不启动 GUI）

设计原则：
  * 复用 ytdlp_gui 里已有的下载引擎函数（yt-dlp/ffmpeg 调度、抖音、新片场、
    视频号、Cookie 获取），不重复实现，也不改动 GUI 的任何行为。
  * stdout 只输出一个 JSON 文档；进度和日志走 stderr，方便 agent 解析。
  * 自带下载记录（xxdown_archive.txt + xxdown_index.jsonl），同一个链接不会
    重复下载；工具（yt-dlp/deno/ffmpeg）和 Cookie 的状态可以直接查，
    省掉重复下载 yt-dlp、反复获取 Cookie。

用法：
  python\\python.exe xxdown_cli.py tools
  python\\python.exe xxdown_cli.py doctor
  python\\python.exe xxdown_cli.py resolve "https://www.douyin.com/video/123"
  python\\python.exe xxdown_cli.py download "URL1" "URL2" --quality 1080p
  python\\python.exe xxdown_cli.py history --url "URL1"
  python\\python.exe xxdown_cli.py cookies status
  python\\python.exe xxdown_cli.py cookies grab --browser edge
  python\\python.exe xxdown_cli.py wx status
  python\\python.exe xxdown_cli.py update
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import ytdlp_gui as G  # noqa: E402  复用现成引擎，不重复实现

APP_NAME = "xxdown_cli"
CLI_VERSION = "1.0"
APP_VERSION = getattr(G, "APP_VERSION", "0.0")
INVOCATION = "python\\python.exe xxdown_cli.py <command> [args]"
ARCHIVE_NAME = "xxdown_archive.txt"
INDEX_NAME = "xxdown_index.jsonl"
CLI_CONFIG_NAME = "xxdown_cli_settings.json"

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_ENV = 3

# 抖音 / 新片场的 CDN 直链需要浏览器 UA + Referer（缺一个就 403），与 GUI 一致
# manifest 的列分隔符用 chr(9)，避免源码里的转义歧义
MF_SEP = chr(9)

DY_HEADERS = ["--user-agent", G.DY_UA, "--add-header", "Referer: https://www.douyin.com/"]
XPC_HEADERS = ["--user-agent", G.XPC_UA, "--add-header", "Referer: " + G.XPC_REFERER,
               "--add-header", "Range: bytes=0-"]


# --------------------------------------------------------------- 输出约定
def _fix_streams():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log(msg):
    """日志和进度一律走 stderr，stdout 只留一个 JSON 文档"""
    print(msg, file=sys.stderr, flush=True)


def emit(payload, code=EXIT_OK):
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    sys.exit(code)


def fail(error, code=EXIT_PARTIAL, **extra):
    payload = {"ok": False, "tool": APP_NAME, "error": error}
    payload.update(extra)
    emit(payload, code)


# --------------------------------------------------------------- 路径与下载记录
def archive_path() -> Path:
    return G.app_dir() / ARCHIVE_NAME


def index_path() -> Path:
    return G.app_dir() / INDEX_NAME


def cli_config_path() -> Path:
    """CLI 自己的默认值文件（不改 GUI 的 ytdlp_gui_settings.json）"""
    return G.app_dir() / CLI_CONFIG_NAME


def load_cli_overrides() -> dict:
    p = cli_config_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def index_load():
    """返回（按链接的索引, 按记录键的索引）"""
    by_url, by_key = {}, {}
    p = index_path()
    if not p.exists():
        return by_url, by_key
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        if not isinstance(rec, dict) or not rec.get("key"):
            continue
        by_key[rec["key"]] = rec
        for u in rec.get("urls") or []:
            # 后写的记录覆盖先写的：--force 重下后要以最新文件为准
            by_url[u] = rec
    return by_url, by_key


def index_record(key, urls, paths, platform, title=None):
    if not key or not paths:
        return
    rec = {"key": key, "urls": [u for u in dict.fromkeys(urls) if u],
           "platform": platform, "title": title, "paths": list(paths),
           "ts": int(time.time())}
    try:
        with index_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log("（写入下载记录失败：%s）" % e)


def existing_paths(rec):
    """记录里的文件还在才算命中；文件被删了就应该重新下载"""
    alive = []
    for p in (rec or {}).get("paths") or []:
        try:
            if p and Path(p).exists():
                alive.append(p)
        except Exception:
            pass
    return alive


def dedupe_hit(url, args):
    """先查 CLI 下载记录（含文件是否仍在），命中就不起 yt-dlp 进程"""
    if getattr(args, "force", False):
        return None
    rec = index_load()[0].get(url)
    alive = existing_paths(rec)
    if not alive:
        return None
    return {"url": url, "kind": classify(url), "status": "deduped", "paths": alive,
            "title": rec.get("title"), "note": "命中下载记录，未重复下载"}

# --------------------------------------------------------------- 设置合并
_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


def parse_opt(pairs):
    """--opt key value 直接覆盖设置键；布尔/整数做类型归一"""
    out = {}
    for k, v in (pairs or []):
        low = str(v).strip().lower()
        if low in _TRUE:
            out[k] = True
        elif low in _FALSE:
            out[k] = False
        else:
            try:
                out[k] = int(v)
            except ValueError:
                out[k] = v
    return out


def build_settings(args):
    """GUI 现有设置为基线 < CLI 配置文件 < 命令行参数；不写回任何设置文件"""
    s = G.load_settings()
    for k, v in load_cli_overrides().items():
        s[k] = v
    for key, attr in (("quality", "quality"), ("audio_quality", "audio_quality"),
                      ("container", "container"), ("download_dir", "dir"),
                      ("template", "template"), ("sub_lang", "sub_lang"),
                      ("browser", "browser"), ("cookie_file", "cookie_file")):
        v = getattr(args, attr, None)
        if v:
            s[key] = v
    for k, v in (getattr(args, "opt", None) or {}).items():
        s[k] = v
    if getattr(args, "no_cookies", False):
        s["cookie_mode"] = "none"
    if getattr(args, "cookies_from_browser", None):
        s["cookie_mode"] = "browser"
        s["browser"] = args.cookies_from_browser.capitalize()
    elif getattr(args, "cookie_file", None):
        s["cookie_mode"] = "file"
    if getattr(args, "no_subs", False):
        s["subs"] = False
    if getattr(args, "subs", False):
        s["subs"] = True
    if getattr(args, "no_thumbnail", False):
        s["thumbnail"] = False
    if getattr(args, "playlist", False):
        s["no_playlist"] = False
    if getattr(args, "no_playlist", False):
        s["no_playlist"] = True
    s["download_dir"] = s.get("download_dir") or G.default_download_dir()
    s["quality"] = s.get("quality") or G.QUALITIES[0]
    s["audio_quality"] = s.get("audio_quality") or "最佳"
    s["container"] = s.get("container") or "自动"
    s["template"] = s.get("template") or G.DEFAULT_TEMPLATE
    return s


# --------------------------------------------------------------- 环境状态
def cookie_domains(path: Path):
    """统计 cookies.txt 里每个域名的条目数与最早过期时间"""
    info = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return None, str(e)
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        dom, exp = parts[0], parts[4]
        d = info.setdefault(dom, {"count": 0, "earliest": None})
        d["count"] += 1
        try:
            e = int(exp)
        except Exception:
            e = 0
        if e > 0 and (d["earliest"] is None or e < d["earliest"]):
            d["earliest"] = e
    return info, None


def cookies_status(s):
    out = {"cookie_mode": s.get("cookie_mode"), "browser": s.get("browser")}
    p = Path(s.get("cookie_file") or (G.app_dir() / "cookies.txt"))
    out["cookie_file"] = str(p)
    if not p.exists():
        out.update(cookie_file_exists=False, cookie_entries=0, cookie_domains={},
                   cookie_earliest_expiry=None, cookie_expired=None)
        return out
    domains, err = cookie_domains(p)
    out["cookie_file_exists"] = True
    if err:
        out["cookie_read_error"] = err
        return out
    exps = [d["earliest"] for d in domains.values() if d["earliest"]]
    out.update(cookie_entries=sum(d["count"] for d in domains.values()),
               cookie_domains={k: v["count"] for k, v in sorted(domains.items())},
               cookie_earliest_expiry=(min(exps) if exps else None),
               cookie_expired=(bool(exps) and min(exps) < time.time()))
    return out


def ttwid_status():
    """抖音的设备标识缓存（30 天有效）；有缓存就不用再注册"""
    p = G._dy_ttwid_path()
    if not p.exists():
        return {"cached": False}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"cached": False}
    return {"cached": bool(d.get("ttwid")), "expire": d.get("expire"),
            "valid": bool(d.get("ttwid")) and time.time() < (d.get("expire") or 0)}


def wx_status():
    if not G.wxchannels_engine_installed():
        return {"installed": False, "alive": False,
                "note": "未安装；download 视频号链接时会自动下载（或先跑 wx install）"}
    return {"installed": True, "version": G.wxchannels_engine_version(),
            "alive": G.wxchannels_api_alive(timeout=2)}


def versions():
    yt_local, deno_local = G.get_local_ytdlp_version(), G.get_local_deno_version()
    yt_latest = G.get_latest_version("yt-dlp/yt-dlp")
    deno_latest = G.get_latest_version("denoland/deno")
    return {
        "yt_dlp": {"local": yt_local, "latest": yt_latest,
                   "update_available": G.compare_versions(yt_local, yt_latest),
                   "path": G.find_ytdlp()},
        "deno": {"local": deno_local, "latest": deno_latest,
                 "update_available": G.compare_versions(deno_local, deno_latest),
                 "path": str(G.app_dir() / "deno.exe")},
        "wxchannels_engine": wx_status(),
    }


def env_report(s):
    ff = G.find_ffmpeg_dir()
    return {"ffmpeg_bundled": bool(ff), "ffmpeg_dir": ff,
            "cookies": cookies_status(s), "douyin_ttwid": ttwid_status(),
            "download_dir": s.get("download_dir"),
            "download_dir_exists": bool(s.get("download_dir") and os.path.isdir(s["download_dir"])),
            "archive_file": str(archive_path()), "archive_exists": archive_path().exists(),
            "index_file": str(index_path()), "index_exists": index_path().exists()}


# --------------------------------------------------------------- 子进程
def run_process(args, on_line=None, timeout=None):
    """执行子进程并逐行回调，返回 (退出码, 输出行, 是否被超时杀掉)"""
    lines = []
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=flags)
    killed = []

    def _kill():
        killed.append(True)
        try:
            proc.kill()
        except Exception:
            pass

    timer = threading.Timer(timeout, _kill) if timeout else None
    if timer:
        timer.daemon = True
        timer.start()
    try:
        for raw in iter(proc.stdout.readline, ""):
            line = raw.rstrip("\r\n")
            lines.append(line)
            if on_line:
                on_line(line)
        proc.wait()
    finally:
        if timer:
            timer.cancel()
    return proc.returncode, lines, (bool(killed) and proc.returncode != 0)


def forward_progress(line):
    """进度转发到 stderr，只转有意义的行，避免刷屏"""
    if line.strip() and (line.startswith("[") or "[download]" in line):
        log(line)


def tail_error(lines, n=6):
    keep = [l for l in lines if l.startswith(("ERROR", "WARNING"))]
    if keep:
        return " | ".join(keep[-3:])[:600]
    tail = [l for l in lines[-n:] if l.strip()]
    return (" / ".join(tail))[:600] or "子进程无输出"

# --------------------------------------------------------------- 链接分流
def classify(url):
    if G.is_wxchannels_url(url):
        return "wxchannels"
    if G.is_douyin_url(url):
        return "douyin"
    if G.is_xinpianchang_url(url):
        return "xinpianchang"
    return "generic"


def norm_url(url):
    url = url.strip()
    return G.dy_normalize_url(url) if G.is_douyin_url(url) else url


def split_urls(raw_list):
    """链接可以空格/逗号分隔，归一化后去重并保持顺序"""
    urls = []
    for raw in raw_list:
        for one in re.split(r"[\s,]+", raw):
            one = one.strip()
            if one.startswith(("http://", "https://")):
                urls.append(norm_url(one))
    return list(dict.fromkeys(urls))


def wait_devtools(browser, timeout=10):
    """等浏览器调试端口出现（抖音回退模式 / 新片场需要真实浏览器）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        port, path = G.read_devtools_active_port(browser)
        if port:
            return port, path
        cport, _ = G.find_running_debug()
        if cport:
            return cport, None
        time.sleep(1)
    return None, None


def browser_hint(browser):
    return ("未检测到 %s 的调试端口：请在 %s://inspect 打开 Remote debugging 开关，"
            "保持浏览器运行后重试" % ("Chrome" if browser == "chrome" else "Edge", browser))


def pick_browser(s, args):
    """抖音回退模式和新片场都用 GUI 的浏览器设置，且只支持 Chrome/Edge"""
    browser = (getattr(args, "browser", None) or s.get("browser") or "Edge").lower()
    if browser not in ("chrome", "edge"):
        return None, "需要 Chrome 或 Edge 读取会话 Cookie（当前 %s，可用 --browser 指定）" % browser
    return browser, None


# --------------------------------------------------------------- 产物路径
def read_manifest(path: Path):
    """解析 --print-to-file 落盘的 "原始链接<TAB>产物路径"。

    yt-dlp 对取不到的字段写 NA（跳过或没产出），这里只认磁盘上真实存在的文件。
    返回 {原始链接: [产物路径, ...]}，批量下载也能逐条对上链接。
    """
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split(MF_SEP)
        if len(parts) != 2:
            continue
        url, fp = parts[0].strip(), parts[1].strip()
        if not fp or fp == "NA" or not Path(fp).exists():
            continue
        bucket = out.setdefault(url, [])
        if fp not in bucket:
            bucket.append(fp)
    return out


def read_plain_manifest(path: Path):
    """模板只有 %(filepath)s 时（单链接场景）manifest 每行就是一个路径"""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fp = line.strip()
        if fp and fp != "NA" and Path(fp).exists() and fp not in out:
            out.append(fp)
    return out


def collect_products(main_paths):
    """主文件 + 同一次下载产生的字幕/封面附属文件"""
    all_paths = []
    for mp in main_paths:
        p = Path(mp)
        if str(p) not in all_paths:
            all_paths.append(str(p))
        for suffix in (".srt", ".vtt", ".ass", ".ssa", ".png", ".jpg", ".jpeg", ".webp"):
            sib = p.with_name(p.stem + suffix)
            if sib.exists() and str(sib) not in all_paths:
                all_paths.append(str(sib))
    return all_paths


def new_manifest():
    """--print-to-file 的目标文件；写文件而不是 stdout，否则 yt-dlp 会静音进度"""
    fd, name = tempfile.mkstemp(prefix="xxdown_mf_", suffix=".txt")
    os.close(fd)
    return Path(name)


# --------------------------------------------------------------- 通用站点下载
def download_generic(urls, s, args, results):
    """一个（必要时两个）yt-dlp 进程下载多个普通站点链接。

    画质/Cookie/字幕/封面参数完全复用 GUI 的 build_download_args。
    去重以本地索引为唯一裁判（dedupe_hit 已经拦下"文件还在"的链接）：
    能走到这里的只剩两类——没记录的新链接，以及记录在但文件被删了的链接。
    前者带 --download-archive 让 yt-dlp 顺便按视频 ID 兜住重复；
    后者必须不带 archive 且 --force-overwrites，否则 archive 会拦住重下。
    产物路径用 after_move 模板落盘，按 original_url 逐条对应。
    """
    by_url = index_load()[0]
    if args.force:
        fresh, revive = [], list(urls)
    else:
        fresh, revive = [], []
        for u in urls:
            hit = by_url.get(u) and not existing_paths(by_url.get(u))
            (revive if hit else fresh).append(u)

    mf = new_manifest()
    base_extra = (["--newline"] if not args.quiet else []) + \
        ["--print-to-file", "after_move:%(original_url)s" + MF_SEP + "%(filepath)s", str(mf)]
    runs = []
    if fresh:
        runs.append((fresh, [] if args.no_archive
                     else ["--download-archive", str(archive_path())]))
    if revive:
        runs.append((revive, ["--force-overwrites"]))
    manifest, finished = {}, []
    try:
        for group, extra in runs:
            # 链接必须放最后
            dl_args = G.build_download_args([], s) + base_extra + extra + group
            log("> " + " ".join(dl_args))
            rc, lines_, timed_out = run_process(
                dl_args, on_line=(None if args.quiet else forward_progress),
                timeout=args.timeout)
            if timed_out:
                err = "超时（%d 秒）" % args.timeout
                for u in group:
                    results.append({"url": u, "kind": "generic", "status": "failed",
                                    "error": err})
                continue
            finished.append((group, rc, lines_))
            for k, v in read_manifest(mf).items():
                bucket = manifest.setdefault(k, [])
                bucket.extend(x for x in v if x not in bucket)
    finally:
        try:
            mf.unlink(missing_ok=True)
        except Exception:
            pass

    for group, rc, lines_ in finished:
        if rc != 0:
            err = tail_error(lines_)
            for u in group:
                results.append({"url": u, "kind": "generic", "status": "failed",
                                "error": err, "exit_code": rc})
            continue
        for u in group:
            got = manifest.get(u) or []
            if not got:
                # 新链接被 yt-dlp 按视频 ID 判定为已下过（archive 命中）
                results.append({"url": u, "kind": "generic", "status": "deduped",
                                "paths": [],
                                "note": "yt-dlp 按视频 ID 判定该视频已下载；"
                                        "要重下加 --force"})
                continue
            all_paths = collect_products(got)
            stem = Path(got[0]).stem
            # 本地索引始终记账，archive 只负责 yt-dlp 自己的去重；
            # 否则 --force/--no-archive 下完仍然查不到，下次白跑一趟
            index_record(u, [u], all_paths, "generic", title=stem)
            results.append({"url": u, "kind": "generic", "status": "downloaded",
                            "title": stem, "paths": all_paths, "exit_code": rc})


# --------------------------------------------------------------- 抖音 / 新片场
def download_one_media(url, s, args, results, kind):
    """复用 GUI 的解析函数拿直链，再交给本地 yt-dlp 下载（参数与 GUI 流程一致）"""
    hit = dedupe_hit(url, args)
    if hit:
        results.append(hit)
        return
    quality = s.get("quality") or G.QUALITIES[0]
    audio_quality = s.get("audio_quality") or "最佳"

    if kind == "douyin":
        info, err = G.dy_resolve_video(url, log=log)
        if not info:
            log("抖音 API 解析失败：%s，回退浏览器拦截模式" % err)
            browser, berr = pick_browser(s, args)
            if berr:
                results.append({"url": url, "kind": kind, "status": "failed",
                                "error": "%s（API 失败原因：%s）" % (berr, err)})
                return
            port, _ = wait_devtools(browser, args.browser_timeout)
            if not port:
                results.append({"url": url, "kind": kind, "status": "failed",
                                "error": browser_hint(browser) + "（API 失败原因：%s）" % err})
                return
            grabbed, gerr = G.dy_grab_video_via_browser(browser, url, log=log)
            if gerr:
                results.append({"url": url, "kind": kind, "status": "failed", "error": gerr})
                return
            fmt = {"label": "默认", "url": grabbed["url"], "size": 0, "height": 0}
            audio_only = (quality == "仅音频")
            title = grabbed["title"]
        else:
            fmt, audio_only = G._dy_pick_format(info, quality)
            title = info["title"]
        headers = DY_HEADERS
    else:
        browser, berr = pick_browser(s, args)
        if berr:
            results.append({"url": url, "kind": kind, "status": "failed", "error": berr})
            return
        port, _ = wait_devtools(browser, args.browser_timeout)
        if not port:
            results.append({"url": url, "kind": kind, "status": "failed", "error": browser_hint(browser)})
            return
        info, err = G.xpc_resolve_video(browser, url, log=log)
        if not info:
            results.append({"url": url, "kind": kind, "status": "failed", "error": str(err)})
            return
        fmt, audio_only = G._xpc_pick_format(info, quality)
        title = info["title"]
        headers = XPC_HEADERS

    safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", title or "").strip()[:80]
    out = os.path.join(s["download_dir"], (safe or (kind + "_video")) + ".%(ext)s")
    mf = new_manifest()
    dl_args = [G.find_ytdlp(), "--ignore-config", "--no-playlist",
               "--paths", "home:" + s["download_dir"], "-o", out]
    ffdir = G.find_ffmpeg_dir()
    if ffdir:
        dl_args += ["--ffmpeg-location", ffdir]
    if not args.quiet:
        dl_args += ["--newline"]
    if args.force:
        dl_args += ["--force-overwrites"]
    dl_args += headers + ["--print-to-file", "after_move:%(filepath)s", str(mf)]
    if audio_only:
        dl_args += ["-x", "--audio-format", "mp3"]
        cap = G._AUDIO_BITRATE.get(audio_quality)
        dl_args += ["--audio-quality", "0" if not cap else "%dK" % cap]
    dl_args += [fmt["url"]]
    try:
        log("> " + " ".join(dl_args))
        rc, lines, timed_out = run_process(dl_args,
                                           on_line=(None if args.quiet else forward_progress),
                                           timeout=args.timeout)
        all_paths = collect_products(read_plain_manifest(mf))
        if rc != 0 or timed_out:
            err = ("超时（%d 秒）" % args.timeout) if timed_out else tail_error(lines)
            results.append({"url": url, "kind": kind, "status": "failed", "error": err,
                            "exit_code": rc, "format": fmt["label"]})
            return
        index_record(url, [url], all_paths, kind, title=title)
        results.append({"url": url, "kind": kind, "status": "downloaded", "title": title,
                        "format": fmt["label"], "paths": all_paths, "exit_code": rc})
    finally:
        try:
            mf.unlink(missing_ok=True)
        except Exception:
            pass

# --------------------------------------------------------------- 微信视频号
def prepare_wx(s, args):
    """与 GUI 的 _wx_prepare_service 同逻辑，只是不弹窗"""
    if not G.wxchannels_engine_installed():
        if getattr(args, "no_install", False):
            return False, "视频号引擎未安装（--no-install 时不会自动下载，可先跑 wx install）"
        ok, msg = G.install_wxchannels_engine(log=log)
        if not ok:
            return False, msg
    if G.wxchannels_api_alive(timeout=2):
        return True, None
    return G.start_wxchannels_engine(s["download_dir"],
                                     keep_system_proxy=(not getattr(args, "no_system_proxy", False)),
                                     log=log)


def wx_task_paths(task, s):
    """引擎不返回最终路径，任务完成后按任务名在目录里匹配产物"""
    name = (task or {}).get("name") or ""
    base = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip()[:20]
    if not base:
        return []
    out = []
    for d in (Path(s["download_dir"]), G.app_dir()):
        d = Path(d)
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.is_file() and f.name.startswith(base) and \
                    f.suffix.lower() not in (".part", ".tmp", ".json", ".yaml", ".log"):
                out.append(str(f))
    return sorted(set(out))


def wait_wx_task(task_id, s, timeout=900, log_enabled=True):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        tasks, err = G.wxchannels_list_tasks(page_size=100)
        if err:
            return {"status": "failed", "error": err}
        t = next((x for x in tasks if x.get("id") == task_id), None)
        if not t:
            return {"status": "failed", "error": "找不到视频号任务 %s" % task_id}
        st = t.get("status")
        if log_enabled and st != last:
            log("视频号任务 %s：%s %d%%" % (task_id, G.WXCHANNELS_TASK_STATUS.get(st, st),
                                            int(t.get("progress") or 0)))
            last = st
        if st == 5:
            return {"status": "downloaded", "paths": wx_task_paths(t, s), "task": t}
        if st in (6, 7):
            return {"status": "failed",
                    "error": t.get("error") or G.WXCHANNELS_TASK_STATUS.get(st, st), "task": t}
        time.sleep(2)
    return {"status": "timeout", "error": "等待视频号任务超时（%d 秒）" % timeout,
            "task_id": task_id}


def download_wx(urls, s, args, results):
    ok, err = prepare_wx(s, args)
    if not ok:
        for u in urls:
            results.append({"url": u, "kind": "wxchannels", "status": "failed", "error": err})
        return
    for u in urls:
        hit = dedupe_hit(u, args)
        if hit:
            results.append(hit)
            continue
        task, terr = G.wxchannels_create_task(u, download_dir=s["download_dir"],
                                              existing_action=args.existing, log=log)
        if terr:
            results.append({"url": u, "kind": "wxchannels", "status": "failed", "error": terr})
            continue
        entry = {"url": u, "kind": "wxchannels", "status": "task_created",
                 "task_id": task.get("id"), "name": task.get("name")}
        if not args.no_wait:
            entry.update(wait_wx_task(task.get("id"), s, timeout=args.timeout or 900,
                                      log_enabled=not args.quiet))
            if entry.get("status") == "downloaded" and entry.get("paths"):
                index_record(u, [u], entry["paths"], "wxchannels", title=task.get("name"))
        results.append(entry)


# --------------------------------------------------------------- 命令
TOOLS_DOC = {
    "tools": "输出本命令清单与取值范围（agent 自描述用）",
    "doctor": "查环境：yt-dlp/deno/ffmpeg/视频号引擎版本、cookies.txt 域名条目与过期情况、抖音 ttwid 缓存；带 problems",
    "resolve": "只解析链接：标题、时长、可用画质列表，不下载",
    "download": "下载链接，自动分流普通站点/抖音/新片场/微信视频号；下过的直接返回本地路径（--force 重下）",
    "history": "查下载记录：--url 判断某链接是否已下过并给出文件路径；--last N 列最近 N 条",
    "cookies": "status（状态/域名条目/是否过期）、grab（从运行中的 Chrome/Edge 导出 cookies.txt）、validate（用真实链接校验）",
    "wx": "视频号引擎：status / install / start / stop / tasks",
    "update": "检查并更新内置 yt-dlp 与 deno（更新的就是本地这份，agent 不用另外下载）",
}


def cmd_tools(args):
    emit({"ok": True, "tool": APP_NAME, "cli_version": CLI_VERSION,
          "invocation": INVOCATION,
          "output_contract": "stdout = 一个 JSON 文档（含 ok/tool）；进度与日志 = stderr；不弹窗、不启动 GUI",
          "exit_codes": {str(EXIT_OK): "成功（含命中下载记录直接返回本地文件）",
                         str(EXIT_PARTIAL): "有链接失败",
                         str(EXIT_USAGE): "参数错误",
                         str(EXIT_ENV): "环境缺失（调试端口未开、yt-dlp 不可用等）",
                         "130": "中断"},
          "commands": TOOLS_DOC,
          "quality_options": list(G.QUALITIES),
          "audio_quality_options": list(G.AUDIO_QUALITIES),
          "container_options": list(G.CONTAINERS)})


def cmd_doctor(args):
    s = build_settings(args)
    payload = {"ok": True, "tool": APP_NAME, "app_dir": str(G.app_dir())}
    payload.update(versions())
    payload["env"] = env_report(s)
    payload["settings"] = {k: s.get(k) for k in ("quality", "audio_quality", "container",
                                                 "download_dir", "template", "subs",
                                                 "sub_lang", "thumbnail", "no_playlist",
                                                 "cookie_mode")}
    problems = []
    if not payload["yt_dlp"]["local"]:
        problems.append("yt-dlp 不可用（路径 %s）" % payload["yt_dlp"]["path"])
    if not payload["env"]["ffmpeg_bundled"]:
        problems.append("未找到随附 ffmpeg，合并/转码可能失败")
    if not payload["env"]["download_dir_exists"]:
        problems.append("下载目录不存在（download 会自动创建）：%s" % payload["env"]["download_dir"])
    if payload["yt_dlp"]["update_available"]:
        problems.append("yt-dlp 有新版（跑 update）")
    if payload["deno"]["update_available"]:
        problems.append("deno 有新版（跑 update）")
    if payload["env"]["cookies"].get("cookie_expired"):
        problems.append("cookies.txt 有已过期条目，建议 cookies grab 重新导出")
    payload["problems"] = problems
    emit(payload, EXIT_OK if payload["yt_dlp"]["local"] else EXIT_ENV)


def cmd_resolve(args):
    s = build_settings(args)
    url = norm_url(args.url)
    kind = classify(url)
    if kind == "douyin":
        info, err = G.dy_resolve_video(url, log=log)
        if info:
            emit({"ok": True, "tool": APP_NAME, "url": url, "kind": kind, "id": info["id"],
                  "title": info["title"], "duration": info["duration"],
                  "uploader": info["uploader"],
                  "formats": [{"label": f["label"], "height": f["height"], "size": f["size"],
                               "fps": f["fps"]} for f in info["formats"]],
                  "quality_options": list(G.QUALITIES)})
        # API 失效（抖音已要求 a_bogus/msToken 签名）：回退浏览器拦截，与 download_one_media 一致
        log("\u6296\u97f3 API \u89e3\u6790\u5931\u8d25\uff1a%s\uff0c\u56de\u9000\u6d4f\u89c8\u5668\u62e6\u622a\u6a21\u5f0f" % err)
        browser, berr = pick_browser(s, args)
        if berr:
            fail("%s\uff08API \u5931\u8d25\u539f\u56e0\uff1a%s\uff09" % (berr, err), kind=kind, url=url)
        port, _ = wait_devtools(browser, args.browser_timeout)
        if not port:
            fail(browser_hint(browser) + "\uff08API \u5931\u8d25\u539f\u56e0\uff1a%s\uff09" % err,
                 EXIT_ENV, kind=kind, url=url)
        grabbed, gerr = G.dy_grab_video_via_browser(browser, url, log=log)
        if gerr:
            fail(str(gerr), kind=kind, url=url)
        emit({"ok": True, "tool": APP_NAME, "url": url, "kind": kind,
              "id": G.dy_extract_video_id(url),
              "title": grabbed.get("title"),
              "duration": None,
              "uploader": None,
              # \u6d4f\u89c8\u5668\u62e6\u622a\u53ea\u62ff\u5f97\u5230\u5f53\u524d\u64ad\u653e\u7684\u90a3\u4e00\u8def\uff0c\u6ca1\u6709\u5168\u753b\u8d28\u5217\u8868
              "formats": [{"label": "\u9ed8\u8ba4", "height": 0, "size": 0, "fps": 0,
                           "url": grabbed.get("url")}],
              "note": "\u6296\u97f3 API \u5931\u6548\uff0c\u672c\u6b21\u8d70\u6d4f\u89c8\u5668\u62e6\u622a\uff1b\u53ea\u6709\u4e00\u8def\u9ed8\u8ba4\u753b\u8d28\uff0c\u65e0\u753b\u8d28\u5217\u8868\uff0c--quality \u4e0d\u751f\u6548",
              "quality_options": list(G.QUALITIES)})
    if kind == "xinpianchang":
        browser, berr = pick_browser(s, args)
        if berr:
            fail(berr, EXIT_USAGE, kind=kind, url=url)
        port, _ = wait_devtools(browser, args.browser_timeout)
        if not port:
            fail(browser_hint(browser), EXIT_ENV, kind=kind, url=url)
        info, err = G.xpc_resolve_video(browser, url, log=log)
        if not info:
            fail(str(err), kind=kind, url=url)
        emit({"ok": True, "tool": APP_NAME, "url": url, "kind": kind, "id": info["id"],
              "title": info["title"], "duration": info["duration"],
              "uploader": info["uploader"],
              "formats": [{"label": f["label"], "height": f["height"], "size": f["size"],
                           "kind": f.get("kind")} for f in info["formats"]],
              "quality_options": list(G.QUALITIES)})
    if kind == "wxchannels":
        out, ferr = G.wxchannels_fetch(url, log=log, timeout=args.timeout or 180)
        if ferr:
            fail(ferr, kind=kind, url=url)
        emit({"ok": True, "tool": APP_NAME, "url": url, "kind": kind,
              "title": (out or {}).get("title"), "raw": out,
              "note": "视频号由后台引擎下载，画质信息见 raw"})
    jargs = G.build_test_args([], s) + ["-J", "--no-warnings"] + [url]
    rc, lines, timed_out = run_process(jargs, timeout=args.timeout)
    if rc != 0:
        fail(("超时（%d 秒）" % args.timeout) if timed_out else tail_error(lines),
             kind=kind, url=url, exit_code=rc)
    try:
        info = json.loads([l for l in lines if l.strip()][-1])
    except Exception:
        fail("无法解析 yt-dlp 的 JSON 输出", kind=kind, url=url)
    emit({"ok": True, "tool": APP_NAME, "url": url, "kind": kind, "id": info.get("id"),
          "title": info.get("title"), "duration": info.get("duration"),
          "uploader": info.get("uploader"),
          "formats": [{"format_id": f.get("format_id"), "ext": f.get("ext"),
                       "height": f.get("height"), "vcodec": f.get("vcodec"),
                       "acodec": f.get("acodec"),
                       "filesize": f.get("filesize") or f.get("filesize_approx"),
                       "note": f.get("format_note")} for f in info.get("formats") or []],
          "quality_options": list(G.QUALITIES)})


def cmd_download(args):
    s = build_settings(args)
    urls = split_urls(args.urls)
    if not urls:
        fail("没有可用的 http(s) 链接（每个链接必须以 http:// 或 https:// 开头）",
             EXIT_USAGE)
    Path(s["download_dir"]).mkdir(parents=True, exist_ok=True)

    groups = {}
    results = []
    for u in urls:
        hit = dedupe_hit(u, args)
        if hit:
            results.append(hit)
            continue
        groups.setdefault(classify(u), []).append(u)

    if groups.get("generic"):
        download_generic(groups["generic"], s, args, results)
    for u in groups.get("douyin", []):
        download_one_media(u, s, args, results, "douyin")
    for u in groups.get("xinpianchang", []):
        download_one_media(u, s, args, results, "xinpianchang")
    if groups.get("wxchannels"):
        download_wx(groups["wxchannels"], s, args, results)

    # deduped 也算成功：文件已在本地，正是"避免重复下载"要的结果
    bad = [r for r in results if r.get("status") not in ("downloaded", "deduped")]
    payload = {"ok": not bad, "tool": APP_NAME, "download_dir": s["download_dir"],
               "requested": len(urls), "results": results,
               "succeeded": len([r for r in results if r.get("status") == "downloaded"]),
               "deduped": len([r for r in results if r.get("status") == "deduped"])}
    if bad:
        payload["error"] = "；".join(str(r.get("error") or r.get("status")) for r in bad)[:800]
    emit(payload, EXIT_OK if not bad else EXIT_PARTIAL)


def cmd_history(args):
    by_url, _ = index_load()
    if args.url:
        u = norm_url(args.url)
        rec = by_url.get(u)
        if not rec:
            emit({"ok": True, "tool": APP_NAME, "url": u, "downloaded": False})
        alive = existing_paths(rec)
        payload = {"ok": True, "tool": APP_NAME, "url": u, "downloaded": bool(alive),
                   "paths": alive, "record": rec}
        if not alive:
            payload["note"] = "有记录但文件已不在，download 会重新下载"
        emit(payload)
    records = []
    if args.last:
        p = index_path()
        if p.exists():
            lines = [l for l in p.read_text(encoding="utf-8", errors="replace").splitlines()
                     if l.strip()]
            for l in reversed(lines[-int(args.last):]):
                try:
                    rec = json.loads(l)
                except Exception:
                    continue
                rec["paths_exist"] = [bool(Path(x).exists()) for x in rec.get("paths") or []]
                records.append(rec)
    emit({"ok": True, "tool": APP_NAME, "index": str(index_path()), "records": records})

def cmd_cookies(args):
    s = build_settings(args)
    cmd = args.cookies_cmd
    if cmd == "status":
        emit({"ok": True, "tool": APP_NAME, **cookies_status(s),
              "douyin_ttwid": ttwid_status(),
              "note": "抖音不需要 cookies，但官方 API 已被风控（403），需 Chrome/Edge 开着调试端口；"
                      "新片场需要浏览器保持登录并开着调试端口"})
    if cmd == "validate":
        url = args.url or "https://www.youtube.com"
        ck = s.get("cookie_file") or str(G.app_dir() / "cookies.txt")
        if not Path(ck).exists():
            fail("cookies 文件不存在：%s" % ck, EXIT_ENV, cookie_file=ck)
        rc, lines, timed_out = run_process(
            [G.find_ytdlp(), "--ignore-config", "--cookies", ck, "--simulate",
             "--skip-download", "--no-warnings", url], timeout=args.timeout or 90)
        emit({"ok": rc == 0, "tool": APP_NAME, "cookie_file": ck, "url": url,
              "valid": rc == 0, "exit_code": rc,
              "error": None if rc == 0 else tail_error(lines)},
             EXIT_OK if rc == 0 else EXIT_ENV)
    if cmd == "grab":
        browser = (args.browser or s.get("browser") or "Edge").lower()
        if browser not in ("chrome", "edge"):
            fail("grab 仅支持 chrome / edge（其他浏览器请用扩展导出 cookies.txt）", EXIT_USAGE)
        port, _ = wait_devtools(browser, timeout=args.timeout)
        if not port:
            fail(browser_hint(browser), EXIT_ENV)
        text_, gerr = G.grab_browser_cookies(browser, G.COOKIE_URLS)
        if gerr:
            fail(gerr, EXIT_ENV)
        out_path = Path(args.out) if args.out else (G.app_dir() / "cookies.txt")
        out_path.write_text(text_, encoding="utf-8")
        n = len([l for l in text_.splitlines() if l and not l.startswith("#")])
        emit({"ok": True, "tool": APP_NAME, "saved": str(out_path), "entries": n,
              "browser": browser})


def cmd_wx(args):
    s = build_settings(args)
    cmd = args.wx_cmd
    if cmd == "status":
        st = wx_status()
        emit({"ok": True, "tool": APP_NAME, **st})
    if cmd == "install":
        ok, msg = G.install_wxchannels_engine(log=log)
        emit({"ok": ok, "tool": APP_NAME, "version": msg, "error": None if ok else msg},
             EXIT_OK if ok else EXIT_PARTIAL)
    if cmd == "start":
        ok, err = prepare_wx(s, args)
        emit({"ok": ok, "tool": APP_NAME, "panel": G.WXCHANNELS_PANEL_URL, "error": err,
              "hint": "需在微信 PC 端把视频号双击分离成独立窗口并播放任意视频，"
                      "引擎才能拦截到页面"},
             EXIT_OK if ok else EXIT_ENV)
    if cmd == "stop":
        ok = G.stop_wxchannels_engine(log=log)
        emit({"ok": ok, "tool": APP_NAME}, EXIT_OK if ok else EXIT_PARTIAL)
    if cmd == "tasks":
        tasks, err = G.wxchannels_list_tasks(page_size=args.last or 50)
        emit({"ok": err is None, "tool": APP_NAME, "tasks": tasks, "error": err},
             EXIT_OK if err is None else EXIT_PARTIAL)


def cmd_update(args):
    v = versions()
    if not v["yt_dlp"]["local"]:
        fail("yt-dlp 不可用，无法更新：%s" % v["yt_dlp"]["path"], EXIT_ENV)
    if v["yt_dlp"]["update_available"]:
        log("正在更新 yt-dlp %s -> %s ..." % (v["yt_dlp"]["local"], v["yt_dlp"]["latest"]))
        rc, lines, _ = run_process([G.find_ytdlp(), "-U"], timeout=args.timeout)
        if rc != 0:
            log("yt-dlp 更新失败：%s" % tail_error(lines))
    else:
        log("yt-dlp %s 已是最新" % (v["yt_dlp"]["local"] or "?"))
    if v["deno"]["update_available"]:
        ok, msg = G.download_deno(v["deno"]["latest"], log=log)
        if not ok:
            log("deno 更新失败：%s" % msg)
    v2 = versions()
    ok = (not v2["yt_dlp"]["update_available"]) and (not v2["deno"]["update_available"])
    emit({"ok": ok, "tool": APP_NAME, **v2}, EXIT_OK if ok else EXIT_PARTIAL)


# --------------------------------------------------------------- 参数
def _add_common(p):
    p.add_argument("--dir", help="下载目录（默认沿用 GUI 设置）")
    p.add_argument("--quality", choices=G.QUALITIES, help="画质（默认沿用 GUI 设置）")
    p.add_argument("--audio-quality", choices=G.AUDIO_QUALITIES)
    p.add_argument("--container", choices=G.CONTAINERS)
    p.add_argument("--template", help="文件名模板，如 %%(title)s.%%(ext)s")
    p.add_argument("--cookie-file", help="使用指定 cookies.txt")
    p.add_argument("--cookies-from-browser", choices=["chrome", "edge"],
                   help="直接从浏览器读 Cookie（抖音不需要 cookies）")
    p.add_argument("--no-cookies", action="store_true", help="本次不用 Cookie")
    p.add_argument("--browser", choices=["Chrome", "Edge"],
                   help="浏览器选择（影响 Cookie 与浏览器拦截模式）")
    p.add_argument("--no-subs", action="store_true")
    p.add_argument("--subs", action="store_true")
    p.add_argument("--sub-lang", help="字幕语言，如 zh,en")
    p.add_argument("--no-thumbnail", action="store_true")
    p.add_argument("--playlist", action="store_true", help="允许播放列表")
    p.add_argument("--no-playlist", action="store_true")
    p.add_argument("--opt", nargs=2, action="append", metavar=("KEY", "VALUE"),
                   help="直接覆盖设置键值（可多次），如 --opt subs false")
    p.add_argument("--quiet", action="store_true", help="不向 stderr 输出进度")
    p.add_argument("--timeout", type=int, default=None, help="子进程超时秒数")
    p.add_argument("--browser-timeout", type=int, default=10,
                   help="等待浏览器调试端口的秒数")


def _build_parser():
    ap = argparse.ArgumentParser(
        prog="xxdown_cli",
        description="XX Down agent CLI：复用 XX Down 的下载引擎，JSON 输出，不弹窗")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tools", help=TOOLS_DOC["tools"])
    _add_common(sub.add_parser("doctor", help=TOOLS_DOC["doctor"]))

    p = sub.add_parser("resolve", help=TOOLS_DOC["resolve"])
    p.add_argument("url")
    _add_common(p)

    p = sub.add_parser("download", help=TOOLS_DOC["download"])
    p.add_argument("urls", nargs="+", help="一个或多个视频链接（可空格/逗号分隔）")
    p.add_argument("--force", action="store_true", help="忽略下载记录，强制重新下载")
    p.add_argument("--no-archive", action="store_true", help="本次不读写下载记录")
    p.add_argument("--existing", choices=G.WXCHANNELS_EXISTING_ACTIONS, default="skip",
                   help="视频号同名文件处理方式")
    p.add_argument("--no-install", action="store_true", help="视频号引擎缺失时不自动下载")
    p.add_argument("--no-system-proxy", action="store_true", help="视频号服务不接管系统代理")
    p.add_argument("--no-wait", action="store_true", help="视频号创建任务后不等待完成")
    _add_common(p)

    p = sub.add_parser("history", help=TOOLS_DOC["history"])
    p.add_argument("--url", help="查这条链接是否已下载过")
    p.add_argument("--last", type=int, help="列出最近 N 条记录")

    p = sub.add_parser("cookies", help=TOOLS_DOC["cookies"])
    csub = p.add_subparsers(dest="cookies_cmd", required=True)
    _add_common(csub.add_parser("status", help="Cookie / 抖音 ttwid 状态"))
    c2 = csub.add_parser("grab", help="从运行中的 Chrome/Edge 导出 cookies.txt")
    c2.add_argument("--browser", choices=["chrome", "edge"])
    c2.add_argument("--out", help="保存路径（默认 app 目录 cookies.txt）")
    c2.add_argument("--timeout", type=int, default=30, help="等待调试端口的秒数")
    c3 = csub.add_parser("validate", help="用真实链接校验 Cookie 是否可用")
    c3.add_argument("--url")
    _add_common(c3)

    p = sub.add_parser("wx", help=TOOLS_DOC["wx"])
    wsub = p.add_subparsers(dest="wx_cmd", required=True)
    _add_common(wsub.add_parser("status", help="引擎安装/运行状态"))
    _add_common(wsub.add_parser("install", help="下载/更新视频号引擎"))
    w2 = wsub.add_parser("start", help="启动引擎服务")
    _add_common(w2)
    w2.add_argument("--no-system-proxy", action="store_true")
    w2.add_argument("--no-install", action="store_true")
    wsub.add_parser("stop", help="停止服务并还原系统代理")
    w3 = wsub.add_parser("tasks", help="列出下载任务")
    w3.add_argument("--last", type=int, default=50)

    p = sub.add_parser("update", help=TOOLS_DOC["update"])
    p.add_argument("--timeout", type=int, default=None)
    return ap


_DISPATCH = {"tools": cmd_tools, "doctor": cmd_doctor, "resolve": cmd_resolve,
             "download": cmd_download, "history": cmd_history, "cookies": cmd_cookies,
             "wx": cmd_wx, "update": cmd_update}


def main():
    _fix_streams()
    args = _build_parser().parse_args()
    if getattr(args, "opt", None):
        args.opt = parse_opt(args.opt)
    try:
        _DISPATCH[args.cmd](args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        fail("已中断", 130)
    except BrokenPipeError:
        sys.exit(EXIT_OK)
    except Exception as e:
        fail("内部错误：%r" % (e,), EXIT_PARTIAL)


if __name__ == "__main__":
    main()
