#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
媒体文件只读损坏检测器
=================================

特性：
- **只读操作**：仅进行读取与解码验证，不写入/修改任何目标目录下的文件。
- **递归遍历**：支持多级目录递归扫描图片与视频。
- **三档检测模式**：
  - fast（快）：仅做**容器/元数据探测**（ffprobe & exiftool），不实际解码像素。速度最快，但可能漏检深层损坏。
  - medium（中）：在 fast 的基础上，利用 **ffmpeg 解码首帧**（图像或视频），能发现多数解码层面的错误。
  - slow（慢）：对视频做 **全轨道完整解码**（所有帧/音频样本）至空设备（-f null -），最严格但最慢；图像与 medium 等效（单帧已完整）。
- **进度显示**：主线程汇总完成数与百分比，实时输出。
- **多线程**：ThreadPoolExecutor 并行执行文件检查任务（默认 worker 与 CPU 核数相关，可自定义）。
- **中文路径**：基于 Python 3 的 Unicode 字符串与子进程参数传递，良好支持中文文件名。
- **外部工具**：
  - 依赖 `ffprobe/ffmpeg`（来自 FFmpeg），和 `exiftool`（已在 PATH 中）。

用法示例：
    python check_media_integrity.py \
        \
        --mode medium \
        --workers 4 \
        --timeout 120 \
        --root "/path/到/你的/文件夹"

参数说明：
- --root ROOT           要扫描的根目录
- --mode {fast,medium,slow}
- --workers N           并发工作线程数（默认：min(8, max(2, CPU数)))
- --timeout SECONDS     单文件检测超时，避免卡死（默认：120 秒）
- --include-exts CSV    自定义扩展名（逗号分隔，不区分大小写）。未提供则使用内置常见图片/视频扩展名。
- --list-damaged        检测结束后逐行列出损坏文件的详细原因

仅打印报告到标准输出；**不会**在目标目录写任何文件。
"""

from __future__ import annotations
import argparse
import concurrent.futures as futures
import os
import sys
import time
import subprocess
import locale
from pathlib import Path
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional

# ----------------------------- 工具可用性检查 -----------------------------

def cmd_exists(cmd: str) -> bool:
    try:
        subprocess.run([cmd, '-version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

HAS_FFPROBE = cmd_exists('ffprobe')
HAS_FFMPEG  = cmd_exists('ffmpeg')

# exiftool 的版本参数是 -ver；若失败，不抛出异常，仅视为不可用
try:
    _ = subprocess.run(['exiftool', '-ver'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    HAS_EXIFTOOL = (_ and _.returncode == 0) or cmd_exists('exiftool')
except Exception:
    HAS_EXIFTOOL = False

# ----------------------------- 文件类型 -----------------------------

DEFAULT_IMAGE_EXTS = {
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tif', '.tiff', '.webp',
    '.heic', '.heif', '.dng', '.cr2', '.cr3', '.nef', '.arw', '.raf', '.rw2', '.orf'
}

DEFAULT_VIDEO_EXTS = {
    '.mp4', '.m4v', '.mov', '.avi', '.mkv', '.webm', '.wmv', '.flv', '.mts', '.m2ts', '.ts',
    '.3gp', '.3gpp', '.mxf', '.mpg', '.mpeg', '.vob'
}

# ----------------------------- 数据结构 -----------------------------

@dataclass
class FileResult:
    path: Path
    ok: bool
    status: str  # 'ok'|'damaged'|'error'|'skipped'
    reason: str
    mode: str
    duration_ms: int

# ----------------------------- 子进程运行 -----------------------------

def run(cmd: List[str], timeout: int) -> Tuple[int, str, str]:
    """运行子进程，返回 (returncode, stdout, stderr)。
    Windows 下避免因控制台本地编码（如 GBK）与工具输出（常为 UTF-8）不一致而解码失败。
    统一以二进制捕获，再按 UTF-8 → 系统首选编码 → GBK/CP936 → Latin-1 顺序回退解码。
    """
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            text=False,              # 以字节读取，避免在 reader 线程中用本地编码解码
            stdin=subprocess.DEVNULL # 不从标准输入读取，防止阻塞
        )
        stdout_b = p.stdout or b""
        stderr_b = p.stderr or b""

        def _decode(buf: bytes) -> str:
            for enc in ("utf-8", locale.getpreferredencoding(False) or "utf-8", "gbk", "cp936", "latin-1"):
                try:
                    return buf.decode(enc)
                except Exception:
                    pass
            return buf.decode("utf-8", errors="replace")

        return p.returncode, _decode(stdout_b), _decode(stderr_b)
    except subprocess.TimeoutExpired as e:
        return 124, "", f"Timeout: {e}"
    except Exception as e:
        return 125, "", f"Exception: {e!r}"

# ----------------------------- 检测核心 -----------------------------

FAST_BASIS = (
    "fast：仅进行容器/元数据探测（ffprobe + exiftool），不解码像素；速度快，可能漏检深层损坏。"
)
MEDIUM_BASIS = (
    "medium：在 fast 基础上，额外用 ffmpeg 解码**首帧**（图像/视频），能发现大多数解码层错误。"
)
SLOW_BASIS = (
    "slow：对视频执行**全轨道完整解码**（所有帧/音频样本）到空设备，最严格但最慢；图像等效于 medium（单帧即完整）。"
)


def is_image(path: Path, image_exts: set) -> bool:
    return path.suffix.lower() in image_exts


def is_video(path: Path, video_exts: set) -> bool:
    return path.suffix.lower() in video_exts


def check_fast(path: Path, timeout: int) -> Tuple[bool, str]:
    """仅探测容器/元数据是否可被解析。任一工具成功且无错误即判定通过。"""
    diagnostics = []
    ok_flags = []

    if HAS_FFPROBE:
        # -v error：仅输出错误；-show_entries/-show_format 可尽量覆盖图片/视频
        rc, out, err = run([
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=format_name:stream=codec_name,codec_type',
            '-of', 'json', str(path)
        ], timeout)
        ffprobe_ok = (rc == 0 and (not err.strip()))
        ok_flags.append(ffprobe_ok)
        diagnostics.append(f"ffprobe rc={rc} err_len={len(err.strip())}")
    else:
        diagnostics.append("ffprobe unavailable")

    if HAS_EXIFTOOL:
        # 纯读取元数据（-fast -fast2 加速）；出现 Error:* 视为失败
        rc2, out2, err2 = run(['exiftool', '-fast', '-fast2', '-n', '-S', '-s', '-s', '-s', str(path)], timeout)
        exif_ok = (rc2 == 0 and (('Error' not in out2) and (not err2.strip())))
        ok_flags.append(exif_ok)
        diagnostics.append(f"exiftool rc={rc2} has_error_token={'Error' in out2}")
    else:
        diagnostics.append("exiftool unavailable")

    # 任一工具成功即可认为容器可解析
    ok = any(ok_flags) if ok_flags else False
    reason = "; ".join(diagnostics)
    return ok, reason


def check_decode_first_frame(path: Path, timeout: int) -> Tuple[bool, str]:
    """解码首帧到空设备；图像和视频都适用。"""
    if not HAS_FFMPEG:
        return False, 'ffmpeg unavailable'

    # -v error：仅显示错误；-frames:v 1 解码一帧；-an 关闭音频；-f null - 输出到空
    rc, out, err = run([
        'ffmpeg', '-v', 'error', '-hide_banner', '-nostdin',
        '-i', str(path), '-frames:v', '1', '-an', '-f', 'null', '-'
    ], timeout)
    ok = (rc == 0 and (not err.strip()))
    return ok, f"ffmpeg[first-frame] rc={rc} err_len={len(err.strip())}"


def check_full_decode(path: Path, timeout: int) -> Tuple[bool, str]:
    """完整解码所有轨道（视频/音频）到空设备；图像相当于解码一帧。"""
    if not HAS_FFMPEG:
        return False, 'ffmpeg unavailable'

    rc, out, err = run([
        'ffmpeg', '-v', 'error', '-hide_banner', '-nostdin',
        '-i', str(path), '-map', '0', '-f', 'null', '-'
    ], timeout)
    ok = (rc == 0 and (not err.strip()))
    return ok, f"ffmpeg[full-decode] rc={rc} err_len={len(err.strip())}"


def audit_one(path: Path, mode: str, timeout: int, image_exts: set, video_exts: set) -> FileResult:
    t0 = time.time()
    try:
        if not (is_image(path, image_exts) or is_video(path, video_exts)):
            # 非支持扩展名：跳过
            return FileResult(path, True, 'skipped', 'unsupported extension', mode, int((time.time()-t0)*1000))

        # — fast：容器/元数据探测
        ok_fast, why_fast = check_fast(path, timeout)
        if mode == 'fast':
            status = 'ok' if ok_fast else 'damaged'
            reason = f"{FAST_BASIS} | diag: {why_fast}"
            return FileResult(path, ok_fast, status, reason, mode, int((time.time()-t0)*1000))

        # — medium：在 fast 成功的基础上尝试首帧解码；若 fast 已失败仍继续尝试解码，以提高召回
        ok_first, why_first = check_decode_first_frame(path, timeout)
        if mode == 'medium':
            ok = ok_fast and ok_first  # 两者都过更稳妥；允许根据需要调整为 ok_fast or ok_first
            # 解释：medium 要求容器解析+首帧解码均无报错
            status = 'ok' if ok else 'damaged'
            reason = f"{MEDIUM_BASIS} | fast: {why_fast} | first-frame: {why_first}"
            return FileResult(path, ok, status, reason, mode, int((time.time()-t0)*1000))

        # — slow：完整解码（最严格）；为了更可解释，同时给出 fast 与 first-frame 的诊断
        ok_full, why_full = check_full_decode(path, timeout)
        # 慎重起见，slow 模式以完整解码为准
        ok = ok_full
        status = 'ok' if ok else 'damaged'
        reason = f"{SLOW_BASIS} | fast: {why_fast} | first-frame: {why_first} | full: {why_full}"
        return FileResult(path, ok, status, reason, mode, int((time.time()-t0)*1000))

    except Exception as e:
        return FileResult(path, False, 'error', f'Exception: {e!r}', mode, int((time.time()-t0)*1000))

# ----------------------------- 扫描与进度 -----------------------------

def iter_files(root: Path) -> List[Path]:
    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 不修改任何属性，仅列举
        for fn in filenames:
            try:
                files.append(Path(dirpath) / fn)
            except Exception:
                # 防御性处理异常文件名
                pass
    return files


def format_progress(done: int, total: int, ok: int, bad: int) -> str:
    pct = (done / total * 100) if total else 100.0
    return f"\r进度: {done}/{total} ({pct:5.1f}%) | OK: {ok} | 损坏: {bad}"

# ----------------------------- 主程序 -----------------------------

def main():
    parser = argparse.ArgumentParser(description='媒体文件只读损坏检测器（ffprobe/ffmpeg/exiftool）')
    parser.add_argument('--root', required=True, help='要扫描的根目录')
    parser.add_argument('--mode', default='medium', choices=['fast', 'medium', 'slow'], help='检测强度档位')
    parser.add_argument('--workers', type=int, default=max(2, min(8, (os.cpu_count() or 4))), help='并发工作线程数')
    parser.add_argument('--timeout', type=int, default=120, help='单文件超时（秒）')
    parser.add_argument('--include-exts', type=str, default='', help='自定义扩展名（逗号分隔），例如: .jpg,.png,.mp4')
    parser.add_argument('--list-damaged', action='store_true', help='结束后列出损坏文件详情')

    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        print(f"根目录不存在或不可用：{root}")
        sys.exit(2)

    # 工具提示
    print("外部工具可用性：")
    print(f"  ffprobe:   {'OK' if HAS_FFPROBE else 'MISSING'}")
    print(f"  ffmpeg:    {'OK' if HAS_FFMPEG else 'MISSING'}")
    print(f"  exiftool:  {'OK' if HAS_EXIFTOOL else 'MISSING'}")

    # 依据提示
    basis = {'fast': FAST_BASIS, 'medium': MEDIUM_BASIS, 'slow': SLOW_BASIS}[args.mode]
    print(f"\n检测模式：{args.mode}\n依据：{basis}\n")

    # 扩展名集合
    if args.include_exts:
        exts = {e.strip().lower() if e.strip().startswith('.') else f'.{e.strip().lower()}' for e in args.include_exts.split(',') if e.strip()}
        image_exts = exts
        video_exts = exts
    else:
        image_exts = DEFAULT_IMAGE_EXTS
        video_exts = DEFAULT_VIDEO_EXTS

    # 收集文件
    all_files = iter_files(root)
    total = len(all_files)
    if total == 0:
        print('未找到任何文件。')
        return

    print(f"待检文件总数：{total}（其中将按扩展名过滤图片/视频）\n")

    checked = 0
    ok_count = 0
    bad_count = 0
    damaged_list: List[FileResult] = []

    # 任务提交
    with futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        future_map = {}
        for p in all_files:
            future = ex.submit(audit_one, p, args.mode, args.timeout, image_exts, video_exts)
            future_map[future] = p

        try:
            for fut in futures.as_completed(future_map):
                res: FileResult = fut.result()
                checked += 1

                if res.status == 'ok' or (res.ok and res.status == 'skipped'):
                    ok_count += 1
                elif res.status in ('damaged', 'error'):
                    bad_count += 1
                    damaged_list.append(res)
                # 进度条（单行覆盖输出）
                print(format_progress(checked, total, ok_count, bad_count), end='', flush=True)
        finally:
            print()  # 换行

    # 汇总报告
    print('\n==== 检测完成 ====')
    print(f"根目录：{root}")
    print(f"模式：{args.mode}")
    print(f"总数：{total} | OK/跳过：{ok_count} | 损坏/错误：{bad_count}")

    if args.list_damaged and damaged_list:
        print('\n-- 损坏/错误文件清单 --')
        for r in damaged_list:
            # 仅读输出，包含原因摘要
            print(f"[DAMAGED] {r.path} | {r.reason}")


if __name__ == '__main__':
    main()
