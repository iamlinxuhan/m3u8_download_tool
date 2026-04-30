# -*- coding: utf-8 -*-
"""
M3U8视频下载器 2.0 - PyQt5 版本
功能：
  - 双模式切换：M3U8/普通视频下载 + 视频格式转换
  - M3U8下载策略与原版完全一致：
      Step1 用 requests 下载 .m3u8 到本地临时文件
      Step2 用 N_m3u8DL-CLI 消费临时文件，实时输出日志/进度
  - 支持本地 .m3u8 文件导入（跳过 Step1，直接进 Step2）
  - 当 URL 不是 .m3u8 时，可直接下载普通视频（requests 流式 + 断点续传）
  - 下载后若输出格式与实际格式不同，自动调用 ffmpeg 转换
  - 视频格式转换面板：ffmpeg，解析 time= 更新进度条
  - 所有子进程均在后台线程中运行，不阻塞 UI
  - 彩色日志（绿/红/黄/白），带时间戳
  - PyInstaller 打包兼容（sys.frozen / sys._MEIPASS）
"""

import os
import sys
import re
import threading
import subprocess
import time
import math
from datetime import datetime
from urllib.parse import urlparse

# ─── PyQt5 导入 ────────────────────────────────────────────────────────────────
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QStackedWidget,
    QMenuBar, QMenu, QAction, QStatusBar, QLabel, QLineEdit,
    QPushButton, QComboBox, QSpinBox, QProgressBar, QTextEdit,
    QFileDialog, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QVBoxLayout, QGroupBox,
    QMessageBox,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import (
    QFont, QTextCursor, QTextCharFormat, QColor, QIcon, QPalette
)

# ─── 可选 requests ─────────────────────────────────────────────────────────────
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


# ═══════════════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════════════

def get_base_dir() -> str:
    """获取程序基础目录（兼容 PyInstaller）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_tools_dir() -> str:
    return os.path.join(get_base_dir(), "tools")


def get_meipass_tools_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, "tools")
    return get_tools_dir()


def format_size(size_bytes: float) -> str:
    if size_bytes <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(max(size_bytes, 1), 1024)))
    i = min(i, len(units) - 1)
    return f"{size_bytes / math.pow(1024, i):.2f} {units[i]}"


def format_speed(bps: float) -> str:
    return format_size(bps) + "/s"


# ═══════════════════════════════════════════════════════════════════════════════
# DownloaderManager —— 自动查找 N_m3u8DL-CLI（保留原版逻辑）
# ═══════════════════════════════════════════════════════════════════════════════

class DownloaderManager:
    """管理 N_m3u8DL-CLI 下载器路径，查找逻辑与原版一致"""

    CANDIDATE_NAMES = [
        "N_m3u8DL-CLI_v3.0.2.exe",  # 带版本号的完整名称
        "N_m3u8DL-CLI.exe",          # 通用名称
        "N_m3u8DL-CLI-SimpleG.exe",  # SimpleG 版本
    ]

    def __init__(self):
        self._custom_path: str = ""

    def set_custom_path(self, path: str):
        self._custom_path = path.strip()

    def get_custom_path(self) -> str:
        return self._custom_path

    def get_downloader_path(self) -> str:
        """获取下载器路径，优先级：手动指定 > PyInstaller tools > 开发环境 tools"""
        # 0. 用户手动指定
        if self._custom_path and os.path.isfile(self._custom_path):
            return self._custom_path

        # 1. PyInstaller 打包环境（与原版逻辑完全对应）
        if getattr(sys, 'frozen', False):
            base_path = sys._MEIPASS
            # 先检查 tools 子目录
            tools_dir = os.path.join(base_path, 'tools')
            if os.path.exists(tools_dir):
                for exe_name in self.CANDIDATE_NAMES:
                    exe_path = os.path.join(tools_dir, exe_name)
                    if os.path.exists(exe_path):
                        return exe_path
            # 再检查根目录
            for exe_name in self.CANDIDATE_NAMES:
                exe_path = os.path.join(base_path, exe_name)
                if os.path.exists(exe_path):
                    return exe_path
        else:
            # 2. 开发环境：当前目录下的 tools 文件夹
            dev_tools_dir = os.path.join(os.getcwd(), "tools")
            if os.path.exists(dev_tools_dir):
                for exe_name in self.CANDIDATE_NAMES:
                    exe_path = os.path.join(dev_tools_dir, exe_name)
                    if os.path.exists(exe_path):
                        return exe_path

        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# FFmpegManager —— 查找 ffmpeg.exe
# ═══════════════════════════════════════════════════════════════════════════════

class FFmpegManager:
    def __init__(self):
        self._custom_path: str = ""

    def set_custom_path(self, path: str):
        self._custom_path = path.strip()

    def get_custom_path(self) -> str:
        return self._custom_path

    def get_ffmpeg_path(self) -> str:
        if self._custom_path and os.path.isfile(self._custom_path):
            return self._custom_path
        # PyInstaller 环境
        if getattr(sys, 'frozen', False):
            for candidate in [
                os.path.join(get_meipass_tools_dir(), "ffmpeg.exe"),
                os.path.join(sys._MEIPASS, "ffmpeg.exe"),
            ]:
                if os.path.isfile(candidate):
                    return candidate
        # tools 目录
        p = os.path.join(get_tools_dir(), "ffmpeg.exe")
        if os.path.isfile(p):
            return p
        # 同级目录
        p = os.path.join(get_base_dir(), "ffmpeg.exe")
        if os.path.isfile(p):
            return p
        return ""

    def is_available(self) -> bool:
        return bool(self.get_ffmpeg_path())


# ═══════════════════════════════════════════════════════════════════════════════
# DownloadWorker —— 后台下载线程
# ═══════════════════════════════════════════════════════════════════════════════

class DownloadWorker(QObject):
    """
    后台线程下载工作器。
    M3U8 下载策略与原版完全一致：
      Step1 requests 下载 .m3u8 到临时文件
      Step2 N_m3u8DL-CLI 读取临时文件执行下载
      完成后清理临时文件
    """
    log_signal      = pyqtSignal(str, str)   # (message, level)
    progress_signal = pyqtSignal(float)       # 0~100
    status_signal   = pyqtSignal(str)
    speed_signal    = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)   # (success, output_path)

    def __init__(
        self,
        url: str,
        work_dir: str,
        save_name: str,
        max_threads: int,
        min_threads: int,
        downloader_path: str,
        ffmpeg_path: str,
        output_format: str,
        is_local_m3u8: bool = False,
    ):
        super().__init__()
        self.url             = url
        self.work_dir        = work_dir
        self.save_name       = save_name
        self.max_threads     = max_threads
        self.min_threads     = min_threads
        self.downloader_path = downloader_path
        self.ffmpeg_path     = ffmpeg_path
        self.output_format   = output_format.lower().strip(".")
        self.is_local_m3u8   = is_local_m3u8
        self._stop_event     = threading.Event()
        self._process: subprocess.Popen = None

    def stop(self):
        self._stop_event.set()
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
            except Exception:
                pass

    def run(self):
        try:
            self._execute()
        except Exception as e:
            self.log_signal.emit(f"发生未预期错误: {e}", "error")
            self.finished_signal.emit(False, "")

    # ── 主调度 ──────────────────────────────────────────────────────────────────

    def _execute(self):
        os.makedirs(self.work_dir, exist_ok=True)

        parsed    = urlparse(self.url)
        is_http   = parsed.scheme in ("http", "https")
        # 只取 path 部分做后缀判断，忽略 ?query 参数（如 play.m3u8?_KS=xxx）
        path_lower = parsed.path.lower()
        is_m3u8 = self.is_local_m3u8 or path_lower.endswith(".m3u8")

        if is_m3u8:
            # ── M3U8 下载（使用与原版完全相同的策略）
            self._download_m3u8(self.url)
        elif is_http:
            # ── 普通视频 URL，直接下载
            self._download_direct(self.url)
        else:
            # ── 本地文件，直接走格式转换
            self.log_signal.emit("输入为本地文件，跳过下载", "info")
            self._maybe_convert(self.url)

    # ── M3U8 下载（完整保留原版策略） ──────────────────────────────────────────

    def _download_m3u8(self, m3u8_url: str):
        """
        与原版 download_m3u8() 逻辑完全一致：
          Step1: requests 下载 m3u8 到本地临时文件
          Step2: N_m3u8DL-CLI 消费临时文件
          完成后清理临时文件，检查退出码
        """
        if not self.downloader_path:
            self.log_signal.emit("错误: 无法找到下载器，请确保程序已正确打包。", "error")
            self.finished_signal.emit(False, "")
            return

        # 临时 M3U8 文件路径
        temp_m3u8 = os.path.join(
            self.work_dir,
            f"temp_playlist_{int(datetime.now().timestamp())}.m3u8"
        )

        # ──────────────────────────────────────────────────────
        # Step1: 用 requests 下载 m3u8 文件（本地文件跳过此步）
        # ──────────────────────────────────────────────────────
        if self.is_local_m3u8 or not (m3u8_url.startswith("http://") or m3u8_url.startswith("https://")):
            # 本地文件直接使用，无需下载
            temp_m3u8 = m3u8_url
            is_temp = False
            self.log_signal.emit(f"使用本地 M3U8 文件: {m3u8_url}", "info")
        else:
            is_temp = True
            self.log_signal.emit("步骤1: 下载M3U8文件", "info")
            self.log_signal.emit(f"URL: {m3u8_url}", "info")
            self.status_signal.emit("正在下载M3U8文件...")

            if not HAS_REQUESTS:
                self.log_signal.emit("错误: requests 库未安装！", "error")
                self.finished_signal.emit(False, "")
                return

            try:
                self.log_signal.emit("正在下载M3U8文件...", "info")
                headers = {
                    'User-Agent': (
                        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/91.0.4472.124 Safari/537.36'
                    )
                }
                response = requests.get(m3u8_url, headers=headers, timeout=30)
                response.raise_for_status()

                with open(temp_m3u8, 'wb') as f:
                    f.write(response.content)

                self.log_signal.emit("M3U8文件下载完成", "success")
                self.status_signal.emit("M3U8文件下载完成")

            except requests.exceptions.RequestException as e:
                self.log_signal.emit(f"M3U8文件下载失败: {e}", "error")
                self.status_signal.emit("M3U8下载失败")
                self.finished_signal.emit(False, "")
                return

        # ──────────────────────────────────────────────────────
        # Step2: 用 N_m3u8DL-CLI 下载视频
        # ──────────────────────────────────────────────────────
        self.log_signal.emit("步骤2: 下载视频", "info")
        self.log_signal.emit(f"输出目录: {self.work_dir}", "info")
        self.log_signal.emit(f"文件名: {self.save_name}", "info")
        self.status_signal.emit("正在启动视频下载...")

        # 获取下载器目录（确保 ffmpeg 在同一目录下能被找到）
        downloader_dir = os.path.dirname(self.downloader_path)

        cmd = [
            self.downloader_path,
            temp_m3u8,
            "--workDir",     self.work_dir,
            "--saveName",    self.save_name,
            "--maxThreads",  str(self.max_threads),
            "--minThreads",  str(self.min_threads),
            "--retryCount",  "99",
            "--enableDelAfterDone",
        ]

        self.log_signal.emit("正在执行下载命令...", "info")
        self.status_signal.emit("正在执行下载命令...")

        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                creationflags=creation_flags,
                shell=False,
                cwd=downloader_dir,
            )
            self.log_signal.emit("下载进程已启动", "info")
            self.status_signal.emit("下载进行中...")
        except Exception as e:
            self.log_signal.emit(f"启动下载进程失败: {e}", "error")
            self.status_signal.emit("启动进程失败")
            self.finished_signal.emit(False, "")
            return

        # 实时读取输出
        for line in iter(self._process.stdout.readline, ''):
            if self._stop_event.is_set():
                self._process.terminate()
                self.log_signal.emit("下载已被用户停止", "error")
                break
            line = line.strip()
            if line:
                self.log_signal.emit(line, self._classify_line(line))
                self._parse_m3u8dl_progress(line)

        self._process.wait()

        # 清理临时文件
        if is_temp:
            try:
                if os.path.exists(temp_m3u8):
                    os.remove(temp_m3u8)
                    self.log_signal.emit("已清理临时文件", "info")
            except Exception:
                pass

        if self._stop_event.is_set():
            self.finished_signal.emit(False, "")
            return

        # 检查退出码
        if self._process.returncode == 0:
            self.log_signal.emit("=" * 60, "info")
            self.log_signal.emit("下载完成！", "success")
            self.log_signal.emit(f"文件已保存到: {self.work_dir}", "success")
            self.log_signal.emit(f"文件名: {self.save_name}", "success")
            self.status_signal.emit("下载完成")
            # 检查是否需要格式转换
            raw_out = os.path.join(self.work_dir, f"{self.save_name}.mp4")
            self._maybe_convert(raw_out)
        else:
            self.log_signal.emit(f"下载失败，退出码: {self._process.returncode}", "error")
            self.status_signal.emit("下载失败")
            self.finished_signal.emit(False, "")

    # ── 普通视频直接下载（requests 流式 + 断点续传） ──────────────────────────

    def _download_direct(self, url: str):
        self.log_signal.emit("─" * 50, "info")
        self.log_signal.emit("检测到普通视频 URL，启动直接下载", "info")

        if not HAS_REQUESTS:
            self.log_signal.emit("错误：requests 库未安装，无法直接下载！", "error")
            self.finished_signal.emit(False, "")
            return

        path_part = urlparse(url).path
        ext       = os.path.splitext(path_part)[1] or ".mp4"
        save_file = os.path.join(self.work_dir, f"{self.save_name}{ext}")

        self.log_signal.emit(f"目标文件：{save_file}", "info")
        self.status_signal.emit("正在连接服务器...")

        try:
            head       = requests.head(url, timeout=15, allow_redirects=True)
            total_size = int(head.headers.get("Content-Length", 0))
            supports_range = "bytes" in head.headers.get("Accept-Ranges", "")
        except Exception as e:
            self.log_signal.emit(f"HEAD 请求失败：{e}，尝试直接下载", "info")
            total_size     = 0
            supports_range = False

        downloaded = os.path.getsize(save_file) if os.path.exists(save_file) else 0
        if downloaded >= total_size > 0:
            self.log_signal.emit("文件已存在且完整，跳过下载", "success")
            self.progress_signal.emit(100.0)
            self._maybe_convert(save_file)
            return

        headers = {}
        mode    = "ab"
        if supports_range and downloaded > 0:
            headers["Range"] = f"bytes={downloaded}-"
            self.log_signal.emit(f"断点续传，已下载：{format_size(downloaded)}", "info")
        elif downloaded > 0 and not supports_range:
            downloaded = 0
            mode       = "wb"

        self.status_signal.emit("正在下载...")

        try:
            resp = requests.get(url, headers=headers, stream=True, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            self.log_signal.emit(f"下载请求失败：{e}", "error")
            self.finished_signal.emit(False, "")
            return

        start_time       = time.time()
        chunk_downloaded = 0

        with open(save_file, mode) as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if self._stop_event.is_set():
                    self.log_signal.emit("下载已被用户停止", "error")
                    self.finished_signal.emit(False, "")
                    return
                if chunk:
                    f.write(chunk)
                    chunk_downloaded += len(chunk)
                    elapsed = time.time() - start_time
                    speed   = chunk_downloaded / elapsed if elapsed > 0 else 0
                    self.speed_signal.emit(format_speed(speed))
                    if total_size > 0:
                        done = downloaded + chunk_downloaded
                        pct  = min(done / total_size * 100, 100.0)
                        self.progress_signal.emit(pct)

        self.log_signal.emit(f"文件下载完成：{save_file}", "success")
        self.progress_signal.emit(100.0)
        self._maybe_convert(save_file)

    # ── 格式转换（ffmpeg） ──────────────────────────────────────────────────────

    def _maybe_convert(self, src_file: str):
        """若输出格式与当前文件格式不同，调用 ffmpeg 转换"""
        if not os.path.exists(src_file):
            self.log_signal.emit(f"错误：输出文件不存在：{src_file}", "error")
            self.finished_signal.emit(False, "")
            return

        src_ext = os.path.splitext(src_file)[1].lower().strip(".")
        if src_ext == self.output_format or not self.output_format:
            self.log_signal.emit("无需格式转换", "info")
            self.finished_signal.emit(True, src_file)
            return

        if not self.ffmpeg_path:
            self.log_signal.emit("警告：未找到 ffmpeg，跳过格式转换", "error")
            self.finished_signal.emit(True, src_file)
            return

        dst_file = os.path.join(self.work_dir, f"{self.save_name}.{self.output_format}")
        self.log_signal.emit(f"开始格式转换：{src_ext} -> {self.output_format}", "info")
        self._run_ffmpeg(src_file, dst_file)

    def _run_ffmpeg(self, src: str, dst: str, extra_args: list = None):
        cmd = [self.ffmpeg_path, "-i", src]
        if extra_args:
            cmd += extra_args
        cmd += ["-y", dst]

        self.log_signal.emit(f"ffmpeg 命令：{' '.join(cmd)}", "info")
        self.status_signal.emit("正在转换格式...")

        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
            )
        except Exception as e:
            self.log_signal.emit(f"启动 ffmpeg 失败：{e}", "error")
            self.finished_signal.emit(False, "")
            return

        duration_sec = None
        for line in iter(self._process.stdout.readline, ""):
            if self._stop_event.is_set():
                self._process.terminate()
                self.log_signal.emit("ffmpeg 已被用户终止", "error")
                self.finished_signal.emit(False, "")
                return
            line = line.rstrip()
            if not line:
                continue
            self.log_signal.emit(line, "info")
            dur_m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", line)
            if dur_m and duration_sec is None:
                h, m, s = int(dur_m.group(1)), int(dur_m.group(2)), float(dur_m.group(3))
                duration_sec = h * 3600 + m * 60 + s
            time_m = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
            if time_m and duration_sec and duration_sec > 0:
                h, m, s = int(time_m.group(1)), int(time_m.group(2)), float(time_m.group(3))
                current_sec = h * 3600 + m * 60 + s
                self.progress_signal.emit(min(current_sec / duration_sec * 100, 100.0))

        self._process.wait()
        if self._process.returncode == 0:
            self.log_signal.emit(f"格式转换完成：{dst}", "success")
            self.progress_signal.emit(100.0)
            self.finished_signal.emit(True, dst)
        else:
            self.log_signal.emit(f"ffmpeg 转换失败，退出码：{self._process.returncode}", "error")
            self.finished_signal.emit(False, "")

    # ── 工具方法 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _classify_line(line: str) -> str:
        l = line.lower()
        if any(k in l for k in ("error", "错误", "failed", "fail")):
            return "error"
        if any(k in l for k in ("完成", "success", "done", "finish")):
            return "success"
        if any(k in l for k in ("progress", "%", "kb/s", "mb/s", "完成数量")):
            return "progress"
        return "info"

    def _parse_m3u8dl_progress(self, line: str):
        # Progress: 118/127 (92.91%)
        m = re.search(r'Progress:\s*(\d+)/(\d+)\s*\((\d+\.?\d*)%\)', line)
        if m:
            self.progress_signal.emit(float(m.group(3)))
            return
        # 完成数量 58 / 127
        m2 = re.search(r'完成数量\s+(\d+)\s*/\s*(\d+)', line)
        if m2:
            cur, tot = int(m2.group(1)), int(m2.group(2))
            if tot > 0:
                self.progress_signal.emit(cur / tot * 100)
        # 速度
        m3 = re.search(r'(\d+\.?\d*)\s*(KB|MB)/s', line)
        if m3:
            self.speed_signal.emit(f"{m3.group(1)} {m3.group(2)}/s")


# ═══════════════════════════════════════════════════════════════════════════════
# ConvertWorker —— 纯 ffmpeg 格式转换后台线程
# ═══════════════════════════════════════════════════════════════════════════════

class ConvertWorker(QObject):
    log_signal      = pyqtSignal(str, str)
    progress_signal = pyqtSignal(float)
    status_signal   = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, ffmpeg_path: str, src: str, dst: str, extra_args: str = ""):
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.src         = src
        self.dst         = dst
        self.extra_args  = extra_args
        self._stop_event = threading.Event()
        self._process: subprocess.Popen = None

    def stop(self):
        self._stop_event.set()
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
            except Exception:
                pass

    def run(self):
        try:
            self._execute()
        except Exception as e:
            self.log_signal.emit(f"转换错误：{e}", "error")
            self.finished_signal.emit(False, "")

    def _execute(self):
        if not self.ffmpeg_path:
            self.log_signal.emit("错误：未找到 ffmpeg.exe！", "error")
            self.finished_signal.emit(False, "")
            return

        cmd = [self.ffmpeg_path, "-i", self.src]
        if self.extra_args.strip():
            import shlex
            cmd += shlex.split(self.extra_args)
        cmd += ["-y", self.dst]

        self.log_signal.emit(f"ffmpeg 命令：{' '.join(cmd)}", "info")
        self.status_signal.emit("正在转换...")

        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
            )
        except Exception as e:
            self.log_signal.emit(f"启动 ffmpeg 失败：{e}", "error")
            self.finished_signal.emit(False, "")
            return

        duration_sec = None
        for line in iter(self._process.stdout.readline, ""):
            if self._stop_event.is_set():
                self._process.terminate()
                self.log_signal.emit("转换已被用户停止", "error")
                self.finished_signal.emit(False, "")
                return
            line = line.rstrip()
            if not line:
                continue
            self.log_signal.emit(line, "info")
            dur_m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", line)
            if dur_m and duration_sec is None:
                h, m, s = int(dur_m.group(1)), int(dur_m.group(2)), float(dur_m.group(3))
                duration_sec = h * 3600 + m * 60 + s
            time_m = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
            if time_m and duration_sec and duration_sec > 0:
                h, m, s = int(time_m.group(1)), int(time_m.group(2)), float(time_m.group(3))
                current_sec = h * 3600 + m * 60 + s
                self.progress_signal.emit(min(current_sec / duration_sec * 100, 100.0))

        self._process.wait()
        if self._process.returncode == 0:
            self.log_signal.emit(f"转换完成：{self.dst}", "success")
            self.progress_signal.emit(100.0)
            self.finished_signal.emit(True, self.dst)
        else:
            self.log_signal.emit(f"ffmpeg 异常退出，退出码：{self._process.returncode}", "error")
            self.finished_signal.emit(False, "")


# ═══════════════════════════════════════════════════════════════════════════════
# 彩色日志组件
# ═══════════════════════════════════════════════════════════════════════════════

class ColorLogWidget(QTextEdit):
    COLORS = {
        "info":     "#e0e0e0",
        "success":  "#66bb6a",
        "error":    "#ef5350",
        "progress": "#ffca28",
        "ts":       "#9e9e9e",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Consolas", 9))
        self.setStyleSheet(
            "QTextEdit {"
            "  background-color: #1e1e1e;"
            "  color: #e0e0e0;"
            "  border: 1px solid #444;"
            "  border-radius: 4px;"
            "}"
        )

    def append_log(self, message: str, level: str = "info"):
        ts     = datetime.now().strftime("[%H:%M:%S] ")
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)

        fmt_ts = QTextCharFormat()
        fmt_ts.setForeground(QColor(self.COLORS["ts"]))
        cursor.insertText(ts, fmt_ts)

        fmt_msg = QTextCharFormat()
        fmt_msg.setForeground(QColor(self.COLORS.get(level, self.COLORS["info"])))
        cursor.insertText(message + "\n", fmt_msg)

        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def clear_log(self):
        self.clear()
        self.append_log("日志已清空", "info")


# ═══════════════════════════════════════════════════════════════════════════════
# 设置对话框
# ═══════════════════════════════════════════════════════════════════════════════

class SettingsDialog(QDialog):
    def __init__(self, dl_manager: DownloaderManager, ff_manager: FFmpegManager, parent=None):
        super().__init__(parent)
        self.dl_manager = dl_manager
        self.ff_manager = ff_manager
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        form   = QFormLayout()
        form.setSpacing(10)

        # N_m3u8DL-CLI 路径
        self.dl_path_edit = QLineEdit(self.dl_manager.get_custom_path())
        self.dl_path_edit.setPlaceholderText("留空则自动查找 tools/ 目录")
        dl_btn = QPushButton("浏览...")
        dl_btn.setFixedWidth(70)
        dl_btn.clicked.connect(self._browse_dl)
        dl_row = QHBoxLayout()
        dl_row.addWidget(self.dl_path_edit)
        dl_row.addWidget(dl_btn)
        form.addRow("N_m3u8DL-CLI 路径：", dl_row)

        # ffmpeg 路径
        self.ff_path_edit = QLineEdit(self.ff_manager.get_custom_path())
        self.ff_path_edit.setPlaceholderText("留空则自动查找 tools/ffmpeg.exe")
        ff_btn = QPushButton("浏览...")
        ff_btn.setFixedWidth(70)
        ff_btn.clicked.connect(self._browse_ff)
        ff_row = QHBoxLayout()
        ff_row.addWidget(self.ff_path_edit)
        ff_row.addWidget(ff_btn)
        form.addRow("FFmpeg 路径：", ff_row)

        layout.addLayout(form)

        hint = QLabel(
            "提示：若不手动指定，程序会自动在 tools/ 目录下查找对应可执行文件。\n"
            "若工具不存在，可从以下地址下载：\n"
            "  N_m3u8DL-CLI：https://github.com/nilaoda/N_m3u8DL-CLI/releases\n"
            "  FFmpeg：https://ffmpeg.org/download.html"
        )
        hint.setStyleSheet("color: #aaa; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _browse_dl(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择 N_m3u8DL-CLI", "", "可执行文件 (*.exe)")
        if p:
            self.dl_path_edit.setText(p)

    def _browse_ff(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择 ffmpeg.exe", "", "可执行文件 (*.exe)")
        if p:
            self.ff_path_edit.setText(p)

    def _accept(self):
        self.dl_manager.set_custom_path(self.dl_path_edit.text())
        self.ff_manager.set_custom_path(self.ff_path_edit.text())
        self.accept()


# ═══════════════════════════════════════════════════════════════════════════════
# 下载面板
# ═══════════════════════════════════════════════════════════════════════════════

class DownloadPanel(QWidget):
    status_signal = pyqtSignal(str)

    def __init__(self, dl_manager: DownloaderManager, ff_manager: FFmpegManager, parent=None):
        super().__init__(parent)
        self.dl_manager  = dl_manager
        self.ff_manager  = ff_manager
        self._worker: DownloadWorker = None
        self._thread: threading.Thread = None
        self._running      = False
        self._is_local_m3u8 = False
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── 设置区 ──────────────────────────────────────────────────────────────
        grp  = QGroupBox("下载设置")
        grp.setStyleSheet("QGroupBox { font-weight: bold; }")
        form = QFormLayout(grp)
        form.setSpacing(8)

        # URL + 导入本地 m3u8
        url_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("输入 M3U8 地址 或 普通视频 URL...")
        url_row.addWidget(self.url_edit)
        import_btn = QPushButton("导入本地 .m3u8")
        import_btn.setFixedWidth(130)
        import_btn.clicked.connect(self._import_local_m3u8)
        url_row.addWidget(import_btn)
        form.addRow("视频地址：", url_row)

        # 输出目录
        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit(
            os.path.join(os.path.expanduser("~"), "Downloads", "M3U8_Downloads")
        )
        dir_row.addWidget(self.dir_edit)
        dir_btn = QPushButton("浏览...")
        dir_btn.setFixedWidth(70)
        dir_btn.clicked.connect(self._browse_dir)
        dir_row.addWidget(dir_btn)
        form.addRow("输出目录：", dir_row)

        # 文件名
        self.name_edit = QLineEdit("output")
        form.addRow("文件名：", self.name_edit)

        # 输出格式
        fmt_row = QHBoxLayout()
        self.fmt_combo = QComboBox()
        self.fmt_combo.addItems(["mp4", "mkv", "mov", "avi", "ts", "mp3", "aac", "flac"])
        self.fmt_combo.setEditable(True)
        self.fmt_combo.setCurrentText("mp4")
        fmt_row.addWidget(self.fmt_combo)
        fmt_row.addStretch()
        form.addRow("输出格式：", fmt_row)

        # 线程设置（上限 2048，与原版一致）
        thread_row = QHBoxLayout()
        self.max_spin = QSpinBox()
        self.max_spin.setRange(1, 2048)
        self.max_spin.setValue(16)
        self.min_spin = QSpinBox()
        self.min_spin.setRange(1, 2048)
        self.min_spin.setValue(8)
        thread_row.addWidget(QLabel("最高线程："))
        thread_row.addWidget(self.max_spin)
        thread_row.addSpacing(20)
        thread_row.addWidget(QLabel("最低线程："))
        thread_row.addWidget(self.min_spin)
        thread_row.addStretch()
        form.addRow("线程设置：", thread_row)

        root.addWidget(grp)

        # ── 进度区 ──────────────────────────────────────────────────────────────
        grp_prog = QGroupBox("下载进度")
        grp_prog.setStyleSheet("QGroupBox { font-weight: bold; }")
        prog_layout = QVBoxLayout(grp_prog)

        self.progress_label = QLabel("就绪")
        prog_layout.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        prog_layout.addWidget(self.progress_bar)

        self.speed_label = QLabel("速度：—")
        self.speed_label.setStyleSheet("color: #4CAF50;")
        prog_layout.addWidget(self.speed_label)

        root.addWidget(grp_prog)

        # ── 按钮区 ──────────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("开始下载")
        self.start_btn.setStyleSheet(
            "background:#4CAF50;color:white;font-weight:bold;padding:8px 24px;"
        )
        self.start_btn.clicked.connect(self.start_download)

        self.stop_btn = QPushButton("停止下载")
        self.stop_btn.setStyleSheet(
            "background:#F44336;color:white;font-weight:bold;padding:8px 24px;"
        )
        self.stop_btn.clicked.connect(self.stop_download)
        self.stop_btn.setEnabled(False)

        open_btn = QPushButton("打开输出目录")
        open_btn.setStyleSheet("padding:8px 16px;")
        open_btn.clicked.connect(self._open_dir)

        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(open_btn)
        btn_row.addStretch()
        root.addLayout(btn_row)

        # ── 日志区 ──────────────────────────────────────────────────────────────
        grp_log = QGroupBox("实时日志")
        grp_log.setStyleSheet("QGroupBox { font-weight: bold; }")
        log_layout = QVBoxLayout(grp_log)

        self.log_widget = ColorLogWidget()
        log_layout.addWidget(self.log_widget)

        clear_btn = QPushButton("清空日志")
        clear_btn.setFixedWidth(90)
        clear_btn.clicked.connect(self.log_widget.clear_log)
        log_layout.addWidget(clear_btn, alignment=Qt.AlignRight)

        root.addWidget(grp_log, stretch=1)

    # ── 槽方法 ──────────────────────────────────────────────────────────────────

    def _import_local_m3u8(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择本地 .m3u8 文件", "",
            "M3U8 文件 (*.m3u8);;所有文件 (*)"
        )
        if path:
            self.url_edit.setText(path)
            self._is_local_m3u8 = True
            self.log_widget.append_log(f"已导入本地 M3U8：{path}", "success")

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def _open_dir(self):
        d = self.dir_edit.text().strip()
        if d and os.path.isdir(d):
            os.startfile(d)
        else:
            QMessageBox.information(self, "提示", "目录不存在，请先设置有效的输出目录。")

    def start_download(self):
        if self._running:
            QMessageBox.warning(self, "警告", "下载正在进行中！")
            return

        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.critical(self, "错误", "请输入视频地址！")
            return

        work_dir  = self.dir_edit.text().strip()
        save_name = self.name_edit.text().strip()

        if not work_dir:
            QMessageBox.critical(self, "错误", "请设置输出目录！")
            return
        if not save_name:
            QMessageBox.critical(self, "错误", "请输入文件名！")
            return

        illegal = set('<>:"|?*\\/').intersection(save_name)
        if illegal:
            QMessageBox.critical(self, "错误", f"文件名含非法字符：{''.join(illegal)}")
            return

        max_t = self.max_spin.value()
        min_t = self.min_spin.value()
        if min_t > max_t:
            QMessageBox.critical(self, "错误", "最低线程数不能大于最高线程数！")
            return

        dl_path  = self.dl_manager.get_downloader_path()
        ff_path  = self.ff_manager.get_ffmpeg_path()
        out_fmt  = self.fmt_combo.currentText().strip().lower().strip(".")

        # 判断是否为本地 m3u8
        is_local = self._is_local_m3u8 or (
            os.path.isfile(url) and url.lower().endswith(".m3u8")
        )
        # 若 URL 已被用户手动改为网络地址，重置标志，并用 path 部分判断后缀
        if not os.path.isfile(url):
            self._is_local_m3u8 = False
            from urllib.parse import urlparse as _up
            is_local = _up(url).path.lower().endswith(".m3u8")

        self._worker = DownloadWorker(
            url=url,
            work_dir=work_dir,
            save_name=save_name,
            max_threads=max_t,
            min_threads=min_t,
            downloader_path=dl_path,
            ffmpeg_path=ff_path,
            output_format=out_fmt,
            is_local_m3u8=is_local,
        )
        self._worker.log_signal.connect(self.log_widget.append_log)
        self._worker.progress_signal.connect(self._on_progress)
        self._worker.status_signal.connect(self._on_status)
        self._worker.speed_signal.connect(lambda s: self.speed_label.setText(f"速度：{s}"))
        self._worker.finished_signal.connect(self._on_finished)

        self._running = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        self.progress_label.setText("正在启动...")
        self.status_signal.emit("下载中...")

        self._thread = threading.Thread(target=self._worker.run, daemon=True)
        self._thread.start()

        self.log_widget.append_log("=" * 60, "info")
        self.log_widget.append_log(
            f"任务启动  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "info"
        )
        self.log_widget.append_log(f"目标地址：{url}", "info")
        self.log_widget.append_log(f"输出目录：{work_dir}", "info")
        self.log_widget.append_log(f"输出格式：{out_fmt}", "info")

    def stop_download(self):
        if self._worker:
            self._worker.stop()
        self.log_widget.append_log("已发送停止信号...", "error")
        self.status_signal.emit("正在停止...")

    def _on_progress(self, pct: float):
        self.progress_bar.setValue(int(pct))
        self.progress_label.setText(f"进度：{pct:.1f}%")

    def _on_status(self, msg: str):
        self.progress_label.setText(msg)
        self.status_signal.emit(msg)

    def _on_finished(self, success: bool, out_path: str):
        self._running = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if success:
            self.progress_bar.setValue(100)
            self.progress_label.setText("完成 ✓")
            self.log_widget.append_log(f"任务完成！输出文件：{out_path}", "success")
            self.status_signal.emit(f"完成：{os.path.basename(out_path)}")
            reply = QMessageBox.question(
                self, "下载完成",
                f"任务已完成！\n\n文件：{out_path}\n\n是否打开输出目录？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self._open_dir()
        else:
            self.progress_label.setText("失败 ✗")
            self.log_widget.append_log("任务失败，请查看上方日志", "error")
            self.status_signal.emit("下载失败")

    def terminate_all(self):
        if self._worker:
            self._worker.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# 转换面板
# ═══════════════════════════════════════════════════════════════════════════════

class ConvertPanel(QWidget):
    status_signal = pyqtSignal(str)

    def __init__(self, ff_manager: FFmpegManager, parent=None):
        super().__init__(parent)
        self.ff_manager = ff_manager
        self._worker: ConvertWorker = None
        self._thread: threading.Thread = None
        self._running = False
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── 设置区 ──────────────────────────────────────────────────────────────
        grp  = QGroupBox("转换设置")
        grp.setStyleSheet("QGroupBox { font-weight: bold; }")
        form = QFormLayout(grp)
        form.setSpacing(8)

        # 输入文件
        in_row = QHBoxLayout()
        self.in_edit = QLineEdit()
        self.in_edit.setPlaceholderText("选择或拖入视频/音频文件...")
        in_row.addWidget(self.in_edit)
        in_btn = QPushButton("浏览...")
        in_btn.setFixedWidth(70)
        in_btn.clicked.connect(self._browse_input)
        in_row.addWidget(in_btn)
        form.addRow("输入文件：", in_row)

        # 输出目录
        out_dir_row = QHBoxLayout()
        self.out_dir_edit = QLineEdit(
            os.path.join(os.path.expanduser("~"), "Downloads", "M3U8_Downloads")
        )
        out_dir_row.addWidget(self.out_dir_edit)
        out_dir_btn = QPushButton("浏览...")
        out_dir_btn.setFixedWidth(70)
        out_dir_btn.clicked.connect(self._browse_out_dir)
        out_dir_row.addWidget(out_dir_btn)
        form.addRow("输出目录：", out_dir_row)

        # 输出文件名
        self.out_name_edit = QLineEdit()
        self.out_name_edit.setPlaceholderText("留空则与输入文件同名")
        form.addRow("输出文件名：", self.out_name_edit)

        # 输出格式
        fmt_row = QHBoxLayout()
        self.fmt_combo = QComboBox()
        self.fmt_combo.addItems(
            ["mp4", "mkv", "mov", "avi", "ts", "mp3", "aac", "flac", "wav", "webm"]
        )
        self.fmt_combo.setEditable(True)
        self.fmt_combo.setCurrentText("mp4")
        fmt_row.addWidget(self.fmt_combo)
        fmt_row.addStretch()
        form.addRow("输出格式：", fmt_row)

        # 自定义 ffmpeg 参数
        self.extra_edit = QLineEdit()
        self.extra_edit.setPlaceholderText("可选，如：-vcodec libx264 -crf 23")
        form.addRow("自定义参数：", self.extra_edit)

        root.addWidget(grp)

        # ── 进度区 ──────────────────────────────────────────────────────────────
        grp_prog = QGroupBox("转换进度")
        grp_prog.setStyleSheet("QGroupBox { font-weight: bold; }")
        prog_layout = QVBoxLayout(grp_prog)

        self.progress_label = QLabel("就绪")
        prog_layout.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        prog_layout.addWidget(self.progress_bar)

        root.addWidget(grp_prog)

        # ── 按钮区 ──────────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("开始转换")
        self.start_btn.setStyleSheet(
            "background:#2196F3;color:white;font-weight:bold;padding:8px 24px;"
        )
        self.start_btn.clicked.connect(self.start_convert)

        self.stop_btn = QPushButton("停止转换")
        self.stop_btn.setStyleSheet(
            "background:#F44336;color:white;font-weight:bold;padding:8px 24px;"
        )
        self.stop_btn.clicked.connect(self.stop_convert)
        self.stop_btn.setEnabled(False)

        open_btn = QPushButton("打开输出目录")
        open_btn.setStyleSheet("padding:8px 16px;")
        open_btn.clicked.connect(self._open_out_dir)

        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(open_btn)
        btn_row.addStretch()
        root.addLayout(btn_row)

        # ── 日志区 ──────────────────────────────────────────────────────────────
        grp_log = QGroupBox("转换日志")
        grp_log.setStyleSheet("QGroupBox { font-weight: bold; }")
        log_layout = QVBoxLayout(grp_log)

        self.log_widget = ColorLogWidget()
        log_layout.addWidget(self.log_widget)

        clear_btn = QPushButton("清空日志")
        clear_btn.setFixedWidth(90)
        clear_btn.clicked.connect(self.log_widget.clear_log)
        log_layout.addWidget(clear_btn, alignment=Qt.AlignRight)

        root.addWidget(grp_log, stretch=1)

    # ── 槽方法 ──────────────────────────────────────────────────────────────────

    def _browse_input(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择输入文件", "",
            "视频/音频文件 (*.mp4 *.mkv *.mov *.avi *.ts *.flv *.mp3 *.aac *.flac *.wav *.webm *.m4v);;所有文件 (*)"
        )
        if path:
            self.in_edit.setText(path)
            base = os.path.splitext(os.path.basename(path))[0]
            self.out_name_edit.setText(base)

    def _browse_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录", self.out_dir_edit.text())
        if d:
            self.out_dir_edit.setText(d)

    def _open_out_dir(self):
        d = self.out_dir_edit.text().strip()
        if d and os.path.isdir(d):
            os.startfile(d)
        else:
            QMessageBox.information(self, "提示", "目录不存在，请先设置有效的输出目录。")

    def start_convert(self):
        if self._running:
            QMessageBox.warning(self, "警告", "转换正在进行中！")
            return

        src = self.in_edit.text().strip()
        if not src or not os.path.isfile(src):
            QMessageBox.critical(self, "错误", "请选择有效的输入文件！")
            return

        out_dir = self.out_dir_edit.text().strip()
        if not out_dir:
            QMessageBox.critical(self, "错误", "请设置输出目录！")
            return

        out_name = self.out_name_edit.text().strip() or os.path.splitext(os.path.basename(src))[0]
        fmt      = self.fmt_combo.currentText().strip().lower().strip(".")
        if not fmt:
            QMessageBox.critical(self, "错误", "请选择输出格式！")
            return

        ff_path = self.ff_manager.get_ffmpeg_path()
        if not ff_path:
            QMessageBox.critical(
                self, "错误",
                "未找到 ffmpeg.exe！\n请在[工具 - 设置]中指定 ffmpeg 路径。"
            )
            return

        os.makedirs(out_dir, exist_ok=True)
        dst   = os.path.join(out_dir, f"{out_name}.{fmt}")
        extra = self.extra_edit.text().strip()

        self._worker = ConvertWorker(ff_path, src, dst, extra)
        self._worker.log_signal.connect(self.log_widget.append_log)
        self._worker.progress_signal.connect(self._on_progress)
        self._worker.status_signal.connect(self._on_status)
        self._worker.finished_signal.connect(self._on_finished)

        self._running = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        self.progress_label.setText("正在启动...")
        self.status_signal.emit("转换中...")

        self._thread = threading.Thread(target=self._worker.run, daemon=True)
        self._thread.start()

        self.log_widget.append_log("=" * 60, "info")
        self.log_widget.append_log(
            f"转换任务启动  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "info"
        )
        self.log_widget.append_log(f"输入：{src}", "info")
        self.log_widget.append_log(f"输出：{dst}", "info")

    def stop_convert(self):
        if self._worker:
            self._worker.stop()
        self.log_widget.append_log("已发送停止信号...", "error")
        self.status_signal.emit("正在停止...")

    def _on_progress(self, pct: float):
        self.progress_bar.setValue(int(pct))
        self.progress_label.setText(f"进度：{pct:.1f}%")

    def _on_status(self, msg: str):
        self.progress_label.setText(msg)
        self.status_signal.emit(msg)

    def _on_finished(self, success: bool, out_path: str):
        self._running = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if success:
            self.progress_bar.setValue(100)
            self.progress_label.setText("完成 ✓")
            self.log_widget.append_log(f"转换完成！输出：{out_path}", "success")
            self.status_signal.emit(f"完成：{os.path.basename(out_path)}")
            reply = QMessageBox.question(
                self, "转换完成",
                f"转换完成！\n\n文件：{out_path}\n\n是否打开输出目录？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self._open_out_dir()
        else:
            self.progress_label.setText("失败 ✗")
            self.log_widget.append_log("转换失败，请查看上方日志", "error")
            self.status_signal.emit("转换失败")

    def terminate_all(self):
        if self._worker:
            self._worker.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# 主窗口
# ═══════════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("M3U8视频下载器 2.0")
        self.resize(960, 700)
        self.setMinimumSize(800, 580)

        icon_path = os.path.join(get_base_dir(), "fm.ico")
        if os.path.isfile(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.dl_manager = DownloaderManager()
        self.ff_manager = FFmpegManager()

        self._build_menu()
        self._build_central()
        self._build_statusbar()
        self._startup_check()

    # ── UI 构建 ──────────────────────────────────────────────────────────────────

    def _build_menu(self):
        mb = self.menuBar()

        func_menu = mb.addMenu("功能")
        a1 = QAction("下载视频", self)
        a1.setShortcut("Ctrl+1")
        a1.triggered.connect(lambda: self._switch_panel(0))
        func_menu.addAction(a1)

        a2 = QAction("视频格式转换", self)
        a2.setShortcut("Ctrl+2")
        a2.triggered.connect(lambda: self._switch_panel(1))
        func_menu.addAction(a2)

        tool_menu = mb.addMenu("工具")
        a_set = QAction("设置...", self)
        a_set.setShortcut("Ctrl+,")
        a_set.triggered.connect(self._open_settings)
        tool_menu.addAction(a_set)

        a_tools = QAction("打开 tools 目录", self)
        a_tools.triggered.connect(self._open_tools_dir)
        tool_menu.addAction(a_tools)

        help_menu = mb.addMenu("帮助")
        a_about = QAction("关于", self)
        a_about.triggered.connect(self._show_about)
        help_menu.addAction(a_about)

    def _build_central(self):
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.download_panel = DownloadPanel(self.dl_manager, self.ff_manager)
        self.download_panel.status_signal.connect(self._update_status)
        self.stack.addWidget(self.download_panel)   # index 0

        self.convert_panel = ConvertPanel(self.ff_manager)
        self.convert_panel.status_signal.connect(self._update_status)
        self.stack.addWidget(self.convert_panel)    # index 1

        self.stack.setCurrentIndex(0)

    def _build_statusbar(self):
        sb = self.statusBar()
        self.status_label = QLabel("就绪")
        sb.addWidget(self.status_label, 1)
        self.tool_status_label = QLabel()
        sb.addPermanentWidget(self.tool_status_label)

    # ── 启动检查 ──────────────────────────────────────────────────────────────────

    def _startup_check(self):
        dl_ok = bool(self.dl_manager.get_downloader_path())
        ff_ok = self.ff_manager.is_available()

        if dl_ok and ff_ok:
            self.tool_status_label.setText("✔ N_m3u8DL-CLI  ✔ ffmpeg")
            self.tool_status_label.setStyleSheet("color: #4CAF50;")
        else:
            parts = []
            if not dl_ok:
                parts.append("✘ N_m3u8DL-CLI 未找到")
            if not ff_ok:
                parts.append("✘ ffmpeg 未找到")
            self.tool_status_label.setText("  ".join(parts))
            self.tool_status_label.setStyleSheet("color: #F44336;")
            QTimer.singleShot(500, lambda: self._warn_missing_tools(dl_ok, ff_ok))

    def _warn_missing_tools(self, dl_ok: bool, ff_ok: bool):
        lines = ["以下工具未找到，部分功能将不可用：\n"]
        if not dl_ok:
            lines.append("• N_m3u8DL-CLI — M3U8 下载功能")
        if not ff_ok:
            lines.append("• ffmpeg.exe — 格式转换功能")
        lines.append("\n请将工具放至 tools/ 目录，或在[工具 - 设置]中手动指定路径。")
        QMessageBox.warning(self, "工具缺失", "\n".join(lines))

    # ── 面板切换 / 菜单响应 ──────────────────────────────────────────────────────

    def _switch_panel(self, idx: int):
        self.stack.setCurrentIndex(idx)
        self._update_status(["下载视频", "视频格式转换"][idx])

    def _open_settings(self):
        dlg = SettingsDialog(self.dl_manager, self.ff_manager, self)
        if dlg.exec_() == QDialog.Accepted:
            self._startup_check()
            self._update_status("设置已保存")

    def _open_tools_dir(self):
        d = get_tools_dir()
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    def _show_about(self):
        QMessageBox.about(
            self, "关于 M3U8视频下载器 2.0",
            "<b>M3U8视频下载器 2.0</b><br><br>"
            "基于 PyQt5 构建<br>"
            "M3U8 下载核心逻辑与原版完全一致<br><br>"
            "使用工具：<br>"
            "• N_m3u8DL-CLI（M3U8 下载）<br>"
            "• ffmpeg（格式转换）"
        )

    def _update_status(self, msg: str):
        self.status_label.setText(msg)

    # ── 窗口关闭 ──────────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self.download_panel.terminate_all()
        self.convert_panel.terminate_all()
        event.accept()


# ═══════════════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    if hasattr(Qt, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 暗色调色板
    palette = QPalette()
    palette.setColor(QPalette.Window,          QColor(45,  45,  45))
    palette.setColor(QPalette.WindowText,      QColor(220, 220, 220))
    palette.setColor(QPalette.Base,            QColor(35,  35,  35))
    palette.setColor(QPalette.AlternateBase,   QColor(53,  53,  53))
    palette.setColor(QPalette.ToolTipBase,     QColor(25,  25,  25))
    palette.setColor(QPalette.ToolTipText,     QColor(220, 220, 220))
    palette.setColor(QPalette.Text,            QColor(220, 220, 220))
    palette.setColor(QPalette.Button,          QColor(53,  53,  53))
    palette.setColor(QPalette.ButtonText,      QColor(220, 220, 220))
    palette.setColor(QPalette.BrightText,      Qt.red)
    palette.setColor(QPalette.Link,            QColor(42,  130, 218))
    palette.setColor(QPalette.Highlight,       QColor(42,  130, 218))
    palette.setColor(QPalette.HighlightedText, Qt.black)
    app.setPalette(palette)

    win = MainWindow()
    win.show()

    # 居中
    screen = app.primaryScreen().geometry()
    win.move(
        (screen.width()  - win.width())  // 2,
        (screen.height() - win.height()) // 2,
    )

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
