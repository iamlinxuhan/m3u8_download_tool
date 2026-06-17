# -*- coding: utf-8 -*-
"""
M3U8视频下载器 2.0 - PyQt5 跨平台版本
功能：
  - 双模式切换：M3U8/普通视频下载 + 视频格式转换
  - 原生 Python M3U8 下载引擎（不再依赖 N_m3u8DL-CLI）
    · Master Playlist 解析，自动选最高码率
    · AES-128-CBC/ECB 解密
    · 多线程分片下载 + 断点续传
    · 完整性检查 + 自动重试
    · 二进制合并 / ffmpeg concat 合并
  - 普通视频直接下载（requests 流式 + 断点续传）
  - 自动检测系统 ffmpeg，支持 Linux/Windows 双平台
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
import hashlib
import json
import shutil
import queue
from datetime import datetime
from urllib.parse import urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import OrderedDict
import logging

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

# ─── 可选依赖 ─────────────────────────────────────────────────────────────────
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from Crypto.Cipher import AES
    HAS_PYCRYPTODOME = True
except ImportError:
    HAS_PYCRYPTODOME = False


# ═══════════════════════════════════════════════════════════════════════════════
# 平台工具函数
# ═══════════════════════════════════════════════════════════════════════════════

IS_WINDOWS = sys.platform == 'win32'
IS_LINUX = sys.platform.startswith('linux')
IS_MAC = sys.platform == 'darwin'


def get_base_dir() -> str:
    """获取程序基础目录（兼容 PyInstaller）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_resource_dir() -> str:
    """获取资源目录（PyInstaller 兼容）"""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return get_base_dir()


def get_downloads_dir() -> str:
    """获取用户下载目录"""
    return os.path.join(os.path.expanduser("~"), "Downloads", "M3U8_Downloads")


def open_file_explorer(path: str):
    """跨平台打开文件管理器"""
    if not os.path.exists(path):
        return
    if IS_WINDOWS:
        os.startfile(path)
    elif IS_LINUX:
        subprocess.Popen(['xdg-open', path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif IS_MAC:
        subprocess.Popen(['open', path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def format_size(size_bytes: float) -> str:
    if size_bytes <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(max(size_bytes, 1), 1024)))
    i = min(i, len(units) - 1)
    return f"{size_bytes / math.pow(1024, i):.2f} {units[i]}"


def format_speed(bps: float) -> str:
    return format_size(bps) + "/s"


def get_creation_flags():
    """获取子进程创建标志（Windows 下隐藏控制台窗口）"""
    return subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0


# ═══════════════════════════════════════════════════════════════════════════════
# FFmpeg 查找（跨平台）
# ═══════════════════════════════════════════════════════════════════════════════

class FFmpegManager:
    """跨平台 FFmpeg 查找器"""

    # Windows 候选名称
    WIN_NAMES = ["ffmpeg.exe"]
    # Linux/Mac 候选名称
    NIX_NAMES = ["ffmpeg"]

    def __init__(self):
        self._custom_path: str = ""

    def set_custom_path(self, path: str):
        self._custom_path = path.strip()

    def get_custom_path(self) -> str:
        return self._custom_path

    def _find_in_path(self, name: str) -> str:
        """在系统 PATH 中查找可执行文件"""
        for p in os.environ.get("PATH", "").split(os.pathsep):
            candidate = os.path.join(p, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return ""

    def _find_bundled(self) -> str:
        """查找打包的 ffmpeg"""
        names = self.WIN_NAMES if IS_WINDOWS else self.NIX_NAMES

        search_dirs = []
        if getattr(sys, 'frozen', False):
            search_dirs.extend([
                sys._MEIPASS,
                os.path.join(sys._MEIPASS, "ffmpeg"),
                os.path.join(sys._MEIPASS, "bin"),
            ])

        search_dirs.extend([
            get_base_dir(),
            os.path.join(get_base_dir(), "ffmpeg"),
            os.path.join(get_base_dir(), "bin"),
            os.path.join(get_base_dir(), "FFmpeg"),
        ])

        for d in search_dirs:
            if not os.path.isdir(d):
                continue
            for name in names:
                candidate = os.path.join(d, name)
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    return candidate
        return ""

    def get_ffmpeg_path(self) -> str:
        """获取 ffmpeg 路径，优先级：自定义 > 打包目录 > 系统 PATH"""
        # 1. 用户手动指定
        if self._custom_path and os.path.isfile(self._custom_path):
            return self._custom_path

        # 2. 打包目录
        bundled = self._find_bundled()
        if bundled:
            return bundled

        # 3. 系统 PATH
        names = self.WIN_NAMES if IS_WINDOWS else self.NIX_NAMES
        for name in names:
            found = self._find_in_path(name)
            if found:
                return found

        return ""

    def is_available(self) -> bool:
        return bool(self.get_ffmpeg_path())


# ═══════════════════════════════════════════════════════════════════════════════
# 原生 Python M3U8 下载引擎
# ═══════════════════════════════════════════════════════════════════════════════

class M3U8Parser:
    """M3U8 解析器：支持 Master Playlist 和 Media Playlist"""

    DEFAULT_UA = (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    )

    def __init__(self, url_or_path: str, headers: dict = None, log_callback=None):
        self.url_or_path = url_or_path
        self.headers = headers or {'User-Agent': self.DEFAULT_UA}
        self.log = log_callback or (lambda msg, level: None)
        self.base_url = ""
        self.is_master = False
        self.media_playlists = []  # 多码率列表
        self.segments = []         # 分片列表
        self.keys = {}             # key 缓存: {key_url: key_bytes}
        self.target_duration = 0
        self.media_sequence = 0
        self.endlist = False
        self.total_duration = 0.0
        self.version = 3
        self.map_uri = None        # EXT-X-MAP URI (fMP4)
        self.parts = []            # 按 DISCONTINUITY 分组
        self.audio_groups = []
        self.subtitle_groups = []

    def fetch_content(self, url: str) -> str:
        """获取 m3u8 内容"""
        if os.path.isfile(url):
            with open(url, 'r', encoding='utf-8') as f:
                return f.read()

        if not HAS_REQUESTS:
            raise RuntimeError("requests 库未安装，无法下载 m3u8")

        resp = requests.get(url, headers=self.headers, timeout=30)
        resp.raise_for_status()
        resp.encoding = 'utf-8'
        return resp.text

    def parse(self) -> list:
        """
        解析 m3u8，返回分片列表。
        每项: {
            'uri': str, 'duration': float, 'index': int,
            'key_method': str, 'key_uri': str, 'key_bytes': bytes,
            'iv': bytes, 'byterange': str, 'map_uri': str,
            'part': int
        }
        """
        content = self.fetch_content(self.url_or_path)
        self.base_url = self._resolve_base_url(self.url_or_path)

        lines = content.strip().split('\n')
        return self._parse_lines(lines)

    def _resolve_base_url(self, url: str) -> str:
        """解析 Base URL"""
        if os.path.isfile(url):
            return os.path.dirname(os.path.abspath(url)) + '/'
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{os.path.dirname(parsed.path)}/"

    def _parse_lines(self, lines: list) -> list:
        """逐行解析"""
        # 检查是否为 Master Playlist
        for line in lines:
            line = line.strip()
            if line.startswith('#EXT-X-STREAM-INF'):
                self.is_master = True
                break

        if self.is_master:
            return self._parse_master(lines)
        else:
            return self._parse_media(lines)

    def _parse_master(self, lines: list) -> list:
        """解析 Master Playlist，选最高码率"""
        streams = []
        current = {}

        for line in lines:
            line = line.strip()
            if line.startswith('#EXT-X-STREAM-INF'):
                # 解析属性
                for attr in self._parse_attrs(line[18:].strip()):
                    if attr[0] == 'BANDWIDTH':
                        current['bandwidth'] = int(attr[1])
                    elif attr[0] == 'RESOLUTION':
                        current['resolution'] = attr[1]
                    elif attr[0] == 'CODECS':
                        current['codecs'] = attr[1]
                    elif attr[0] == 'NAME':
                        current['name'] = attr[1]
            elif line and not line.startswith('#'):
                current['uri'] = line.strip()
                if 'bandwidth' in current:
                    streams.append(current.copy())
                current = {}
            elif line.startswith('#EXT-X-MEDIA'):
                attrs = dict(self._parse_attrs(line))
                if attrs.get('TYPE') == 'AUDIO':
                    self.audio_groups.append(attrs)

        if not streams:
            self.log("警告：Master Playlist 中未找到任何流", "error")
            return []

        # 按带宽降序排列
        streams.sort(key=lambda x: x.get('bandwidth', 0), reverse=True)

        self.log(f"找到 {len(streams)} 个清晰度选项", "info")
        for s in streams:
            self.log(
                f"  - {s.get('name', 'Unknown')}: "
                f"{s.get('resolution', '?')} @ {s.get('bandwidth', 0) // 1000}kbps",
                "info"
            )

        # 选择最高码率
        best = streams[0]
        self.log(f"自动选择: {best.get('name', 'Unknown')}", "success")

        # 递归解析子 m3u8
        sub_url = urljoin(self.base_url, best['uri'])
        self.log(f"解析子 m3u8: {sub_url}", "info")

        self.url_or_path = sub_url
        self.base_url = self._resolve_base_url(sub_url)
        self.is_master = False

        content = self.fetch_content(sub_url)
        return self._parse_media(content.strip().split('\n'))

    def _parse_attrs(self, line: str) -> list:
        """解析属性字符串，如 BANDWIDTH=123,RESOLUTION=1920x1080"""
        attrs = []
        current_key = ""
        current_val = ""
        in_quotes = False
        i = 0

        while i < len(line):
            ch = line[i]
            if not in_quotes:
                if ch == '=':
                    current_key = current_val.strip()
                    current_val = ""
                    i += 1
                    continue
                elif ch == ',':
                    if current_key:
                        attrs.append((current_key, current_val.strip()))
                    current_key = ""
                    current_val = ""
                    i += 1
                    continue
                elif ch == '"':
                    in_quotes = True
                    i += 1
                    continue
            else:
                if ch == '"':
                    in_quotes = False
                    i += 1
                    continue
            current_val += ch
            i += 1

        if current_key:
            attrs.append((current_key, current_val.strip()))
        return attrs

    def _parse_media(self, lines: list) -> list:
        """解析 Media Playlist"""
        segments = []
        current_key = {'method': 'NONE', 'uri': '', 'iv': None}
        part_index = 0
        seg_index = 0
        has_map = False

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if line.startswith('#EXT-X-TARGETDURATION'):
                try:
                    self.target_duration = int(line.split(':')[1])
                except:
                    pass

            elif line.startswith('#EXT-X-MEDIA-SEQUENCE'):
                try:
                    self.media_sequence = int(line.split(':')[1])
                except:
                    pass

            elif line.startswith('#EXT-X-ENDLIST'):
                self.endlist = True

            elif line.startswith('#EXT-X-VERSION'):
                try:
                    self.version = int(line.split(':')[1])
                except:
                    pass

            elif line.startswith('#EXT-X-KEY'):
                key_str = line[len('#EXT-X-KEY:'):].strip()
                attrs = dict(self._parse_attrs(key_str))
                current_key = {
                    'method': attrs.get('METHOD', 'NONE'),
                    'uri': attrs.get('URI', ''),
                    'iv': attrs.get('IV', None),
                }

            elif line.startswith('#EXT-X-MAP'):
                map_str = line[len('#EXT-X-MAP:'):].strip()
                attrs = dict(self._parse_attrs(map_str))
                has_map = True
                map_uri = attrs.get('URI', '')
                if map_uri:
                    self.map_uri = urljoin(self.base_url, map_uri)

            elif line.startswith('#EXT-X-DISCONTINUITY'):
                part_index += 1

            elif line.startswith('#EXTINF'):
                duration_str = line.split(':')[1].rstrip(',')
                try:
                    duration = float(duration_str)
                except:
                    duration = 0
                self.total_duration += duration

            elif line.startswith('#EXT-X-BYTERANGE'):
                # 字节范围，记录给下一行分片
                byterange = line.split(':')[1] if ':' in line else ''
            elif not line.startswith('#'):
                # 这是一个分片 URI
                uri = line.strip()
                if not uri:
                    continue

                # 完整 URL
                full_uri = urljoin(self.base_url, uri) if not uri.startswith('http') else uri

                # 获取解密密钥
                key_bytes = None
                iv_bytes = None

                if current_key['method'] == 'AES-128':
                    # 下载 KEY
                    key_uri = current_key['uri']
                    if key_uri and not key_uri.startswith('http'):
                        key_uri = urljoin(self.base_url, key_uri)

                    key_bytes = self._get_key_bytes(key_uri, current_key['method'])

                    # 处理 IV
                    if current_key['iv']:
                        iv_hex = current_key['iv'].lstrip('0x').lstrip('0X')
                        iv_bytes = bytes.fromhex(iv_hex.zfill(32))
                    else:
                        # 自动生成 IV: 序列号的大端序 16 字节
                        seq = seg_index + self.media_sequence
                        iv_bytes = seq.to_bytes(16, 'big')

                seg = {
                    'uri': full_uri,
                    'duration': duration,
                    'index': seg_index,
                    'key_method': current_key['method'],
                    'key_uri': current_key.get('uri', ''),
                    'key_bytes': key_bytes,
                    'iv': iv_bytes,
                    'map_uri': self.map_uri if has_map and seg_index == 0 else None,
                    'part': part_index,
                }
                segments.append(seg)
                seg_index += 1
                has_map = False  # MAP 只对后续第一个分片有效

        self.segments = segments
        self._group_parts()
        return segments

    def _group_parts(self):
        """按 DISCONTINUITY 分组"""
        self.parts = OrderedDict()
        for seg in self.segments:
            p = seg['part']
            if p not in self.parts:
                self.parts[p] = []
            self.parts[p].append(seg)

    def _get_key_bytes(self, key_uri: str, method: str) -> bytes:
        """下载并缓存 AES 密钥"""
        if key_uri in self.keys:
            return self.keys[key_uri]

        if not HAS_REQUESTS:
            raise RuntimeError("requests 库未安装")

        try:
            self.log(f"下载解密密钥: {key_uri}", "info")
            resp = requests.get(key_uri, headers=self.headers, timeout=15)
            resp.raise_for_status()
            key_data = resp.content

            if len(key_data) == 16:
                self.keys[key_uri] = key_data
                return key_data
            else:
                self.log(f"警告：密钥长度异常 ({len(key_data)} bytes)，尝试截取前 16 字节", "error")
                key_data = key_data[:16]
                self.keys[key_uri] = key_data
                return key_data
        except Exception as e:
            self.log(f"获取密钥失败: {e}", "error")
            raise


class SegmentDownloader:
    """单个分片下载器（支持 AES 解密）"""

    def __init__(self, segment: dict, save_dir: str, headers: dict,
                 retry: int = 5, timeout: int = 15, log_callback=None):
        self.segment = segment
        self.save_dir = save_dir
        self.headers = headers
        self.retry = retry
        self.timeout = timeout
        self.log = log_callback or (lambda msg, level: None)

    @property
    def index(self):
        return self.segment['index']

    @property
    def part(self):
        return self.segment['part']

    def get_output_path(self) -> str:
        """获取分片保存路径"""
        part_dir = os.path.join(self.save_dir, f"part_{self.part}")
        os.makedirs(part_dir, exist_ok=True)
        return os.path.join(part_dir, f"{self.index:06d}.ts")

    def download(self) -> bool:
        """下载并解密单个分片，返回是否成功"""
        out_path = self.get_output_path()

        # 已存在则跳过
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True

        # 临时文件路径
        tmp_path = out_path + ".tmp"

        for attempt in range(self.retry):
            try:
                resp = requests.get(
                    self.segment['uri'],
                    headers=self.headers,
                    timeout=self.timeout,
                    stream=True
                )
                resp.raise_for_status()

                with open(tmp_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)

                # 解密
                if self.segment['key_method'] == 'AES-128' and self.segment['key_bytes']:
                    self._decrypt_aes(tmp_path, out_path)
                    os.remove(tmp_path)
                else:
                    os.rename(tmp_path, out_path)

                return True

            except Exception as e:
                if attempt < self.retry - 1:
                    time.sleep(1 * (attempt + 1))
                else:
                    self.log(f"分片 {self.index} 下载失败（重试 {self.retry} 次）: {e}", "error")
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    return False
        return False

    def _decrypt_aes(self, src: str, dst: str):
        """AES-128-CBC 解密"""
        if not HAS_PYCRYPTODOME:
            raise RuntimeError("pycryptodome 库未安装")

        with open(src, 'rb') as f:
            data = f.read()

        cipher = AES.new(self.segment['key_bytes'], AES.MODE_CBC, iv=self.segment['iv'])
        decrypted = cipher.decrypt(data)

        # 去除 PKCS7 填充
        pad_len = decrypted[-1]
        if pad_len <= 16:
            decrypted = decrypted[:-pad_len]

        with open(dst, 'wb') as f:
            f.write(decrypted)


class NativeM3U8Downloader(QObject):
    """原生 Python M3U8 下载引擎（替代 N_m3u8DL-CLI）"""

    log_signal = pyqtSignal(str, str)
    progress_signal = pyqtSignal(float)
    status_signal = pyqtSignal(str)
    speed_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(
        self,
        url: str,
        work_dir: str,
        save_name: str,
        max_threads: int = 16,
        min_threads: int = 8,
        retry_count: int = 99,
        ffmpeg_path: str = "",
        output_format: str = "mp4",
        is_local_m3u8: bool = False,
        headers: dict = None,
    ):
        super().__init__()
        self.url = url
        self.work_dir = work_dir
        self.save_name = save_name
        self.max_threads = max_threads
        self.min_threads = min_threads
        self.retry_count = retry_count
        self.ffmpeg_path = ffmpeg_path
        self.output_format = output_format.lower().strip(".")
        self.is_local_m3u8 = is_local_m3u8
        self.headers = headers or {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            )
        }
        self._stop_event = threading.Event()
        self._process: subprocess.Popen = None

    def _log(self, msg: str, level: str = "info"):
        self.log_signal.emit(msg, level)

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
            self._log(f"发生未预期错误: {e}", "error")
            self.finished_signal.emit(False, "")

    def _execute(self):
        os.makedirs(self.work_dir, exist_ok=True)

        parsed = urlparse(self.url)
        is_http = parsed.scheme in ("http", "https")
        path_lower = parsed.path.lower()
        is_m3u8 = self.is_local_m3u8 or path_lower.endswith(".m3u8")

        if is_m3u8:
            self._download_m3u8()
        elif is_http:
            self._download_direct(self.url)
        else:
            self._log("输入为本地文件，跳过下载", "info")
            self._maybe_convert(self.url)

    # ── 原生 M3U8 下载 ────────────────────────────────────────────────────────

    def _download_m3u8(self):
        """使用原生 Python 引擎下载 M3U8"""
        self._log("=" * 60, "info")
        self._log("使用原生 Python M3U8 下载引擎", "info")

        # 如果为远程 m3u8 且非本地文件，先下载 m3u8
        m3u8_source = self.url
        temp_m3u8 = None
        is_temp = False

        if not self.is_local_m3u8 and self.url.startswith(("http://", "https://")):
            if not HAS_REQUESTS:
                self._log("错误: requests 库未安装！", "error")
                self.finished_signal.emit(False, "")
                return

            self._log("步骤1: 下载M3U8文件", "info")
            self.status_signal.emit("正在下载M3U8文件...")

            try:
                temp_m3u8 = os.path.join(
                    self.work_dir,
                    f"temp_playlist_{int(time.time())}.m3u8"
                )
                resp = requests.get(self.url, headers=self.headers, timeout=30)
                resp.raise_for_status()
                with open(temp_m3u8, 'w', encoding='utf-8') as f:
                    f.write(resp.text)
                m3u8_source = temp_m3u8
                is_temp = True
                self._log("M3U8文件下载完成", "success")
            except Exception as e:
                self._log(f"M3U8文件下载失败: {e}", "error")
                self.finished_signal.emit(False, "")
                return

        # ── 解析 M3U8 ────────────────────────────────────────────────────────
        self._log("步骤2: 解析M3U8", "info")
        self.status_signal.emit("正在解析M3U8...")

        try:
            parser = M3U8Parser(
                m3u8_source,
                headers=self.headers,
                log_callback=self._log
            )
            segments = parser.parse()

            if not segments:
                self._log("错误: M3U8 中未找到任何分片", "error")
                self.finished_signal.emit(False, "")
                return

            self._log(f"解析完成: 共 {len(segments)} 个分片, "
                      f"总时长 {parser.total_duration:.1f}s", "success")

            if parser.parts:
                self._log(f"分为 {len(parser.parts)} 个 Part", "info")

        except Exception as e:
            self._log(f"M3U8解析失败: {e}", "error")
            self.finished_signal.emit(False, "")
            if is_temp and temp_m3u8 and os.path.exists(temp_m3u8):
                os.remove(temp_m3u8)
            return

        # ── 下载分片 ─────────────────────────────────────────────────────────
        self._log("步骤3: 下载视频分片", "info")
        self.status_signal.emit("正在下载分片...")

        save_dir = os.path.join(self.work_dir, f"_{self.save_name}_segments")
        os.makedirs(save_dir, exist_ok=True)

        total_segs = len(segments)
        completed = 0
        failed_segs = []
        download_start = time.time()
        bytes_downloaded = [0]  # 用列表以便在闭包中修改
        bytes_lock = threading.Lock()

        # 使用线程池并行下载
        actual_threads = max(self.min_threads, min(self.max_threads, total_segs))
        actual_threads = min(actual_threads, 32)  # 限制最大32线程

        self._log(f"使用 {actual_threads} 个线程并行下载", "info")

        # 下载所有分片（带完整性检查和重试）
        for retry_round in range(self.retry_count + 1):
            if self._stop_event.is_set():
                self._log("下载已被用户停止", "error")
                self._cleanup_temp(save_dir, temp_m3u8, is_temp)
                self.finished_signal.emit(False, "")
                return

            remaining = [s for s in segments if s['index'] not in {f['index'] for f in failed_segs}]
            # 检查已存在的文件
            to_download = []
            for seg in remaining:
                part_dir = os.path.join(save_dir, f"part_{seg['part']}")
                out_path = os.path.join(part_dir, f"{seg['index']:06d}.ts")
                if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                    to_download.append(seg)

            if not to_download:
                self._log("所有分片已存在，跳过下载", "info")
                completed = total_segs
                break

            if retry_round == 0:
                self._log(f"需要下载 {len(to_download)} 个分片", "info")
            else:
                self._log(f"重试第 {retry_round} 轮: 还有 {len(to_download)} 个分片待下载", "info")

            completed = 0
            failed_segs = []

            with ThreadPoolExecutor(max_workers=actual_threads) as executor:
                futures = {}
                for seg in to_download:
                    if self._stop_event.is_set():
                        break
                    dl = SegmentDownloader(
                        seg, save_dir, self.headers,
                        retry=3, timeout=15,
                        log_callback=self._log
                    )
                    future = executor.submit(dl.download)
                    futures[future] = seg['index']

                for future in as_completed(futures):
                    if self._stop_event.is_set():
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    seg_idx = futures[future]
                    try:
                        success = future.result()
                        if success:
                            completed += 1
                        else:
                            failed_segs.append({'index': seg_idx})
                    except Exception:
                        failed_segs.append({'index': seg_idx})

                    # 更新进度
                    pct = (completed / total_segs) * 100
                    self.progress_signal.emit(min(pct, 100.0))

                    # 速度
                    elapsed = time.time() - download_start
                    if elapsed > 0:
                        speed = (completed * 1024 * 1024) / elapsed  # 估算
                        self.speed_signal.emit(format_speed(speed))

            if not failed_segs:
                break
            else:
                self._log(f"本轮 {len(failed_segs)} 个分片失败", "error")
                time.sleep(2)

        # 清理临时 m3u8
        if is_temp and temp_m3u8 and os.path.exists(temp_m3u8):
            try:
                os.remove(temp_m3u8)
            except Exception:
                pass

        if failed_segs:
            self._log(f"下载失败: {len(failed_segs)} 个分片无法完成", "error")
            self._cleanup_temp(save_dir)
            self.finished_signal.emit(False, "")
            return

        self._log(f"所有 {total_segs} 个分片下载完成", "success")

        # ── 合并分片 ─────────────────────────────────────────────────────────
        self._log("步骤4: 合并分片", "info")
        self.status_signal.emit("正在合并分片...")

        output_file = self._merge_segments(save_dir, parser.parts, segments)

        if output_file:
            self._log(f"合并完成: {output_file}", "success")
            # 清理分片目录
            try:
                shutil.rmtree(save_dir)
            except Exception:
                pass
            self._maybe_convert(output_file)
        else:
            self._log("合并失败", "error")
            self.finished_signal.emit(False, "")

    def _merge_segments(self, save_dir: str, parts: OrderedDict,
                        segments: list) -> str:
        """合并分片，支持多 Part 和 ffmpeg concat"""
        output_file = os.path.join(self.work_dir, f"{self.save_name}.mp4")

        if not self.ffmpeg_path:
            # 纯二进制合并
            self._log("未找到 ffmpeg，使用二进制合并", "info")
            return self._binary_merge(save_dir, parts, output_file)

        # 使用 ffmpeg concat
        if len(parts) == 1:
            # 单 Part：直接 concat
            part_idx = list(parts.keys())[0]
            return self._ffmpeg_concat_part(save_dir, part_idx, len(parts[part_idx]), output_file)
        else:
            # 多 Part：先分别合并，再最终合并
            part_files = []
            for part_idx, segs in parts.items():
                part_out = os.path.join(save_dir, f"part_{part_idx}_merged.ts")
                result = self._ffmpeg_concat_part(
                    save_dir, part_idx, len(segs), part_out
                )
                if result:
                    part_files.append(part_out)

            if part_files:
                # 二进制拼接所有 Part
                return self._binary_merge_files(part_files, output_file)

        return ""

    def _binary_merge(self, save_dir: str, parts: OrderedDict,
                      output_file: str) -> str:
        """二进制直接拼接"""
        with open(output_file, 'wb') as out:
            for part_idx in sorted(parts.keys()):
                segs = parts[part_idx]
                part_dir = os.path.join(save_dir, f"part_{part_idx}")
                for seg in sorted(segs, key=lambda s: s['index']):
                    seg_file = os.path.join(part_dir, f"{seg['index']:06d}.ts")
                    if os.path.exists(seg_file):
                        with open(seg_file, 'rb') as f:
                            out.write(f.read())

        if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
            return output_file
        return ""

    def _binary_merge_files(self, files: list, output_file: str) -> str:
        """二进制拼接多个文件"""
        with open(output_file, 'wb') as out:
            for f in files:
                if os.path.exists(f):
                    with open(f, 'rb') as inf:
                        out.write(inf.read())
        if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
            return output_file
        return ""

    def _ffmpeg_concat_part(self, save_dir: str, part_idx: int,
                            seg_count: int, output_file: str) -> str:
        """使用 ffmpeg concat 协议合并一个 Part"""
        part_dir = os.path.join(save_dir, f"part_{part_idx}")

        # 生成文件列表
        filelist_path = os.path.join(save_dir, f"filelist_{part_idx}.txt")
        with open(filelist_path, 'w') as f:
            for i in range(seg_count):
                seg_file = os.path.join(part_dir, f"{i:06d}.ts")
                if os.path.exists(seg_file):
                    # ffmpeg concat 需要转义路径
                    f.write(f"file '{seg_file.replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'\n")

        cmd = [
            self.ffmpeg_path,
            "-f", "concat",
            "-safe", "0",
            "-i", filelist_path,
            "-c", "copy",
            "-y",
            output_file
        ]

        self._log(f"ffmpeg 合并命令: {' '.join(cmd)}", "info")
        self.status_signal.emit("正在合并分片...")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=get_creation_flags(),
            )
            for line in iter(self._process.stdout.readline, ''):
                if self._stop_event.is_set():
                    self._process.terminate()
                    return ""
                line = line.strip()
                if line:
                    self._log(line, "info")

            self._process.wait()

            # 清理文件列表
            try:
                os.remove(filelist_path)
            except:
                pass

            if self._process.returncode == 0:
                return output_file
            else:
                self._log(f"ffmpeg 合并失败，退出码: {self._process.returncode}", "error")
                return ""
        except Exception as e:
            self._log(f"ffmpeg 合并异常: {e}", "error")
            return ""

    def _cleanup_temp(self, save_dir: str, temp_m3u8: str = None, is_temp: bool = False):
        """清理临时文件"""
        if is_temp and temp_m3u8 and os.path.exists(temp_m3u8):
            try:
                os.remove(temp_m3u8)
            except:
                pass
        if save_dir and os.path.exists(save_dir):
            try:
                shutil.rmtree(save_dir)
            except:
                pass

    # ── 普通视频直接下载 ──────────────────────────────────────────────────────

    def _download_direct(self, url: str):
        self._log("─" * 50, "info")
        self._log("检测到普通视频 URL，启动直接下载", "info")

        if not HAS_REQUESTS:
            self._log("错误：requests 库未安装，无法直接下载！", "error")
            self.finished_signal.emit(False, "")
            return

        path_part = urlparse(url).path
        ext = os.path.splitext(path_part)[1] or ".mp4"
        save_file = os.path.join(self.work_dir, f"{self.save_name}{ext}")

        self._log(f"目标文件：{save_file}", "info")
        self.status_signal.emit("正在连接服务器...")

        try:
            head = requests.head(url, timeout=15, allow_redirects=True)
            total_size = int(head.headers.get("Content-Length", 0))
            supports_range = "bytes" in head.headers.get("Accept-Ranges", "")
        except Exception as e:
            self._log(f"HEAD 请求失败：{e}，尝试直接下载", "info")
            total_size = 0
            supports_range = False

        downloaded = os.path.getsize(save_file) if os.path.exists(save_file) else 0
        if downloaded >= total_size > 0:
            self._log("文件已存在且完整，跳过下载", "success")
            self.progress_signal.emit(100.0)
            self._maybe_convert(save_file)
            return

        headers = self.headers.copy()
        mode = "ab"
        if supports_range and downloaded > 0:
            headers["Range"] = f"bytes={downloaded}-"
            self._log(f"断点续传，已下载：{format_size(downloaded)}", "info")
        elif downloaded > 0 and not supports_range:
            downloaded = 0
            mode = "wb"

        self.status_signal.emit("正在下载...")

        try:
            resp = requests.get(url, headers=headers, stream=True, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            self._log(f"下载请求失败：{e}", "error")
            self.finished_signal.emit(False, "")
            return

        start_time = time.time()
        chunk_downloaded = 0

        with open(save_file, mode) as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if self._stop_event.is_set():
                    self._log("下载已被用户停止", "error")
                    self.finished_signal.emit(False, "")
                    return
                if chunk:
                    f.write(chunk)
                    chunk_downloaded += len(chunk)
                    elapsed = time.time() - start_time
                    speed = chunk_downloaded / elapsed if elapsed > 0 else 0
                    self.speed_signal.emit(format_speed(speed))
                    if total_size > 0:
                        done = downloaded + chunk_downloaded
                        pct = min(done / total_size * 100, 100.0)
                        self.progress_signal.emit(pct)

        self._log(f"文件下载完成：{save_file}", "success")
        self.progress_signal.emit(100.0)
        self._maybe_convert(save_file)

    # ── 格式转换 ──────────────────────────────────────────────────────────────

    def _maybe_convert(self, src_file: str):
        """若输出格式与当前文件格式不同，调用 ffmpeg 转换"""
        if not os.path.exists(src_file):
            self._log(f"错误：输出文件不存在：{src_file}", "error")
            self.finished_signal.emit(False, "")
            return

        src_ext = os.path.splitext(src_file)[1].lower().strip(".")
        if src_ext == self.output_format or not self.output_format:
            self._log("无需格式转换", "info")
            self.finished_signal.emit(True, src_file)
            return

        if not self.ffmpeg_path:
            self._log("警告：未找到 ffmpeg，跳过格式转换", "error")
            self.finished_signal.emit(True, src_file)
            return

        dst_file = os.path.join(self.work_dir, f"{self.save_name}.{self.output_format}")
        self._log(f"开始格式转换：{src_ext} -> {self.output_format}", "info")
        self._run_ffmpeg(src_file, dst_file)

    def _run_ffmpeg(self, src: str, dst: str, extra_args: list = None):
        cmd = [self.ffmpeg_path, "-i", src]
        if extra_args:
            cmd += extra_args
        cmd += ["-y", dst]

        self._log(f"ffmpeg 命令：{' '.join(cmd)}", "info")
        self.status_signal.emit("正在转换格式...")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=get_creation_flags(),
            )
        except Exception as e:
            self._log(f"启动 ffmpeg 失败：{e}", "error")
            self.finished_signal.emit(False, "")
            return

        duration_sec = None
        for line in iter(self._process.stdout.readline, ""):
            if self._stop_event.is_set():
                self._process.terminate()
                self._log("ffmpeg 已被用户终止", "error")
                self.finished_signal.emit(False, "")
                return
            line = line.rstrip()
            if not line:
                continue
            self._log(line, "info")
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
            self._log(f"格式转换完成：{dst}", "success")
            self.progress_signal.emit(100.0)
            self.finished_signal.emit(True, dst)
        else:
            self._log(f"ffmpeg 转换失败，退出码：{self._process.returncode}", "error")
            self.finished_signal.emit(False, "")


# ═══════════════════════════════════════════════════════════════════════════════
# ConvertWorker —— 纯 ffmpeg 格式转换后台线程
# ═══════════════════════════════════════════════════════════════════════════════

class ConvertWorker(QObject):
    log_signal = pyqtSignal(str, str)
    progress_signal = pyqtSignal(float)
    status_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, ffmpeg_path: str, src: str, dst: str, extra_args: str = ""):
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.src = src
        self.dst = dst
        self.extra_args = extra_args
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
            self.log_signal.emit("错误：未找到 ffmpeg！", "error")
            self.finished_signal.emit(False, "")
            return

        cmd = [self.ffmpeg_path, "-i", self.src]
        if self.extra_args.strip():
            import shlex
            cmd += shlex.split(self.extra_args)
        cmd += ["-y", self.dst]

        self.log_signal.emit(f"ffmpeg 命令：{' '.join(cmd)}", "info")
        self.status_signal.emit("正在转换...")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=get_creation_flags(),
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
        "info": "#e0e0e0",
        "success": "#66bb6a",
        "error": "#ef5350",
        "progress": "#ffca28",
        "ts": "#9e9e9e",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        # 跨平台等宽字体
        if IS_WINDOWS:
            self.setFont(QFont("Consolas", 9))
        elif IS_MAC:
            self.setFont(QFont("Menlo", 11))
        else:
            self.setFont(QFont("monospace", 9))
        self.setStyleSheet(
            "QTextEdit {"
            "  background-color: #1e1e1e;"
            "  color: #e0e0e0;"
            "  border: 1px solid #444;"
            "  border-radius: 4px;"
            "}"
        )

    def append_log(self, message: str, level: str = "info"):
        ts = datetime.now().strftime("[%H:%M:%S] ")
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
    def __init__(self, ff_manager: FFmpegManager, parent=None):
        super().__init__(parent)
        self.ff_manager = ff_manager
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        # ffmpeg 路径
        self.ff_path_edit = QLineEdit(self.ff_manager.get_custom_path())
        placeholder = "留空则自动查找（系统PATH 或 程序目录）"
        self.ff_path_edit.setPlaceholderText(placeholder)
        ff_btn = QPushButton("浏览...")
        ff_btn.setFixedWidth(70)
        ff_btn.clicked.connect(self._browse_ff)
        ff_row = QHBoxLayout()
        ff_row.addWidget(self.ff_path_edit)
        ff_row.addWidget(ff_btn)
        form.addRow("FFmpeg 路径：", ff_row)

        layout.addLayout(form)

        hint = QLabel(
            "提示：若不手动指定，程序会自动查找系统 PATH 中的 ffmpeg。\n"
            "若系统中未安装 ffmpeg，可从以下地址下载：\n"
            "  Windows: https://ffmpeg.org/download.html\n"
            "  Linux: sudo apt install ffmpeg / sudo dnf install ffmpeg\n"
            "  macOS: brew install ffmpeg"
        )
        hint.setStyleSheet("color: #aaa; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _browse_ff(self):
        if IS_WINDOWS:
            filters = "可执行文件 (*.exe);;所有文件 (*)"
        else:
            filters = "所有文件 (*)"
        p, _ = QFileDialog.getOpenFileName(self, "选择 ffmpeg", "", filters)
        if p:
            self.ff_path_edit.setText(p)

    def _accept(self):
        self.ff_manager.set_custom_path(self.ff_path_edit.text())
        self.accept()


# ═══════════════════════════════════════════════════════════════════════════════
# 下载面板
# ═══════════════════════════════════════════════════════════════════════════════

class DownloadPanel(QWidget):
    status_signal = pyqtSignal(str)

    def __init__(self, ff_manager: FFmpegManager, parent=None):
        super().__init__(parent)
        self.ff_manager = ff_manager
        self._worker: NativeM3U8Downloader = None
        self._thread: threading.Thread = None
        self._running = False
        self._is_local_m3u8 = False
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── 设置区 ──────────────────────────────────────────────────────────
        grp = QGroupBox("下载设置")
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
        self.dir_edit = QLineEdit(get_downloads_dir())
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

        # 线程设置
        thread_row = QHBoxLayout()
        self.max_spin = QSpinBox()
        self.max_spin.setRange(1, 64)
        self.max_spin.setValue(16)
        self.min_spin = QSpinBox()
        self.min_spin.setRange(1, 64)
        self.min_spin.setValue(8)
        thread_row.addWidget(QLabel("最高线程："))
        thread_row.addWidget(self.max_spin)
        thread_row.addSpacing(20)
        thread_row.addWidget(QLabel("最低线程："))
        thread_row.addWidget(self.min_spin)
        thread_row.addStretch()
        form.addRow("线程设置：", thread_row)

        root.addWidget(grp)

        # ── 进度区 ──────────────────────────────────────────────────────────
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

        # ── 按钮区 ──────────────────────────────────────────────────────────
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

        # ── 日志区 ──────────────────────────────────────────────────────────
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

    # ── 槽方法 ──────────────────────────────────────────────────────────────

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
            open_file_explorer(d)
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

        work_dir = self.dir_edit.text().strip()
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

        ff_path = self.ff_manager.get_ffmpeg_path()
        out_fmt = self.fmt_combo.currentText().strip().lower().strip(".")

        # 判断是否为本地 m3u8
        is_local = self._is_local_m3u8 or (
            os.path.isfile(url) and url.lower().endswith(".m3u8")
        )
        if not os.path.isfile(url):
            self._is_local_m3u8 = False
            from urllib.parse import urlparse as _up
            is_local = _up(url).path.lower().endswith(".m3u8")

        self._worker = NativeM3U8Downloader(
            url=url,
            work_dir=work_dir,
            save_name=save_name,
            max_threads=max_t,
            min_threads=min_t,
            retry_count=99,
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

        # ── 设置区 ──────────────────────────────────────────────────────────
        grp = QGroupBox("转换设置")
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
        self.out_dir_edit = QLineEdit(get_downloads_dir())
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

        # ── 进度区 ──────────────────────────────────────────────────────────
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

        # ── 按钮区 ──────────────────────────────────────────────────────────
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

        # ── 日志区 ──────────────────────────────────────────────────────────
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

    # ── 槽方法 ──────────────────────────────────────────────────────────────

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
            open_file_explorer(d)
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
        fmt = self.fmt_combo.currentText().strip().lower().strip(".")
        if not fmt:
            QMessageBox.critical(self, "错误", "请选择输出格式！")
            return

        ff_path = self.ff_manager.get_ffmpeg_path()
        if not ff_path:
            QMessageBox.critical(
                self, "错误",
                "未找到 ffmpeg！\n请在[工具 - 设置]中指定 ffmpeg 路径，\n"
                "或确保系统 PATH 中有 ffmpeg。"
            )
            return

        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, f"{out_name}.{fmt}")
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

        self.ff_manager = FFmpegManager()

        self._build_menu()
        self._build_central()
        self._build_statusbar()
        self._startup_check()

    # ── UI 构建 ──────────────────────────────────────────────────────────────

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

        help_menu = mb.addMenu("帮助")
        a_about = QAction("关于", self)
        a_about.triggered.connect(self._show_about)
        help_menu.addAction(a_about)

    def _build_central(self):
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.download_panel = DownloadPanel(self.ff_manager)
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

    # ── 启动检查 ──────────────────────────────────────────────────────────────

    def _startup_check(self):
        ff_ok = self.ff_manager.is_available()

        if ff_ok:
            self.tool_status_label.setText("✔ ffmpeg")
            self.tool_status_label.setStyleSheet("color: #4CAF50;")
        else:
            self.tool_status_label.setText("✘ ffmpeg 未找到")
            self.tool_status_label.setStyleSheet("color: #F44336;")
            QTimer.singleShot(500, self._warn_missing_ffmpeg)

    def _warn_missing_ffmpeg(self):
        QMessageBox.warning(
            self, "工具缺失",
            "未找到 ffmpeg！格式转换功能将不可用。\n\n"
            "请安装 ffmpeg 或在[工具 - 设置]中指定路径。\n\n"
            "安装方法：\n"
            "  • Windows: 下载 ffmpeg.exe 放到程序目录\n"
            "  • Linux: sudo apt install ffmpeg\n"
            "  • macOS: brew install ffmpeg"
        )

    # ── 面板切换 / 菜单响应 ──────────────────────────────────────────────────

    def _switch_panel(self, idx: int):
        self.stack.setCurrentIndex(idx)
        self._update_status(["下载视频", "视频格式转换"][idx])

    def _open_settings(self):
        dlg = SettingsDialog(self.ff_manager, self)
        if dlg.exec_() == QDialog.Accepted:
            self._startup_check()
            self._update_status("设置已保存")

    def _show_about(self):
        QMessageBox.about(
            self, "关于 M3U8视频下载器 2.0",
            "<b>M3U8视频下载器 2.0</b><br><br>"
            "基于 PyQt5 构建<br>"
            "使用原生 Python M3U8 下载引擎<br><br>"
            "支持平台：Windows / Linux / macOS<br>"
            "依赖工具：ffmpeg（格式转换）<br><br>"
            "GitHub: https://github.com/iamlinxuhan/m3u8_download_tool"
        )

    def _update_status(self, msg: str):
        self.status_label.setText(msg)

    # ── 窗口关闭 ──────────────────────────────────────────────────────────────

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
    palette.setColor(QPalette.Window, QColor(45, 45, 45))
    palette.setColor(QPalette.WindowText, QColor(220, 220, 220))
    palette.setColor(QPalette.Base, QColor(35, 35, 35))
    palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ToolTipBase, QColor(25, 25, 25))
    palette.setColor(QPalette.ToolTipText, QColor(220, 220, 220))
    palette.setColor(QPalette.Text, QColor(220, 220, 220))
    palette.setColor(QPalette.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ButtonText, QColor(220, 220, 220))
    palette.setColor(QPalette.BrightText, Qt.red)
    palette.setColor(QPalette.Link, QColor(42, 130, 218))
    palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.HighlightedText, Qt.black)
    app.setPalette(palette)

    win = MainWindow()
    win.show()

    # 居中
    screen = app.primaryScreen().geometry()
    win.move(
        (screen.width() - win.width()) // 2,
        (screen.height() - win.height()) // 2,
    )

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
