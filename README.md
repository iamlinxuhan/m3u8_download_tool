# M3U8视频下载器 2.0

基于 PyQt5 的跨平台 M3U8 视频下载工具，内置原生 Python M3U8 下载引擎。

## 功能特性

- **原生 M3U8 下载引擎**：无需依赖第三方下载器，纯 Python 实现
  - Master Playlist 自动解析（选择最高码率流）
  - AES-128-CBC 加密分片解密
  - 多线程并行下载 + 断点续传
  - 完整性检查 + 自动重试
  - ffmpeg concat 合并 / 二进制合并
- **普通视频下载**：支持直接下载 mp4/mkv 等格式视频（流式下载 + 断点续传）
- **视频格式转换**：基于 ffmpeg 的格式转换，支持 mp4/mkv/mov/avi/ts/mp3/aac/flac/wav/webm 等
- **跨平台支持**：Windows / Linux / macOS
- **PyQt5 界面**：暗色主题，彩色日志，实时进度显示
- **本地 M3U8 导入**：支持导入本地 .m3u8 文件直接下载

## 安装

### 依赖

- Python 3.7+
- PyQt5
- requests
- pycryptodome
- ffmpeg（系统安装或放置于程序目录）

### 安装步骤

```bash
# 1. 克隆仓库
git clone https://github.com/iamlinxuhan/m3u8_download_tool.git
cd m3u8_download_tool/m3u8_download_2.0

# 2. 安装 Python 依赖
pip install -r requirements.txt

# 3. 安装 ffmpeg（任选一种）
# Windows: 下载 ffmpeg.exe 放到项目目录
# Linux (Debian/Ubuntu):
sudo apt install ffmpeg
# Linux (Fedora):
sudo dnf install ffmpeg
# macOS:
brew install ffmpeg

# 4. 运行
python m3u8_download_2.0.py
```

## 使用说明

### 下载 M3U8 视频

1. 在「下载视频」面板中，输入 M3U8 链接（或点击「导入本地 .m3u8」选择本地文件）
2. 设置输出目录和文件名
3. 选择输出格式（mp4/mkv 等）
4. 调整线程数（默认 8~16 线程）
5. 点击「开始下载」

### 视频格式转换

1. 切换到「视频格式转换」面板
2. 选择输入文件
3. 设置输出目录、文件名和格式
4. 可选：添加自定义 ffmpeg 参数（如 `-vcodec libx264 -crf 23`）
5. 点击「开始转换」

### 设置

通过「工具 → 设置」可以手动指定 ffmpeg 的路径。如果 ffmpeg 已在系统 PATH 中，程序会自动检测。

## 项目结构

```
m3u8_download_tool/
├── .github/workflows/       # GitHub Actions 自动打包
│   └── build.yml
├── m3u8_download_2.0/       # 主程序目录
│   ├── m3u8_download_2.0.py # 主程序
│   ├── requirements.txt     # Python 依赖
│   ├── fm.ico               # 程序图标
│   └── README.md            # 详细文档
└── README.md                # 本文件
```

## 技术架构

### M3U8 下载引擎

```
M3U8Parser          → 解析 m3u8 文件（Master/Media Playlist）
  ├── 自动选择最高码率流
  ├── 解析 EXT-X-KEY（AES-128 加密信息）
  ├── 自动下载并缓存解密密钥
  └── 处理 EXT-X-DISCONTINUITY 分组

SegmentDownloader   → 单分片下载器
  ├── HTTP 下载（支持重试）
  └── AES-128-CBC 解密

NativeM3U8Downloader → 下载调度器
  ├── ThreadPoolExecutor 多线程并行下载
  ├── 完整性检查 + 自动重试
  ├── ffmpeg concat 合并
  └── 格式转换
```

### 格式转换

使用 ffmpeg 进行格式转换，支持解析 `Duration` 和 `time=` 输出来更新进度条。

## 打包发布

本项目支持通过 GitHub Actions 自动打包，推送 `v*` 标签即可触发：

| 平台 | 格式 | 说明 |
|------|------|------|
| Windows | `.exe` 安装包 | 使用 NSIS 打包 |
| Linux x86_64 | `.deb` 安装包 | Debian/Ubuntu 系 |
| Linux ARM | `.pkg.tar.xz` | Arch Linux ARM 系 |

## 常见问题

### Q: 提示 "requests 库未安装"？

```bash
pip install requests
```

### Q: 提示 "pycryptodome 库未安装"？

```bash
pip install pycryptodome
```

部分加密的 M3U8 视频需要此库进行 AES-128 解密。

### Q: 提示 "未找到 ffmpeg"？

请安装 ffmpeg 或在「设置」中手动指定路径。Windows 用户可将 `ffmpeg.exe` 放到程序所在目录。

### Q: 下载速度慢？

适当调高线程数（建议不超过 32）。注意部分服务器可能限制并发连接数。

### Q: 部分分片下载失败？

程序会自动重试最多 99 轮，每轮对失败的分片重新下载。

## License

MIT License

## 致谢

- [PyQt5](https://www.riverbankcomputing.com/software/pyqt/) - GUI 框架
- [pycryptodome](https://www.pycryptodome.org/) - AES 解密
- [ffmpeg](https://ffmpeg.org/) - 视频处理
- [N_m3u8DL-CLI](https://github.com/nilaoda/N_m3u8DL-CLI) - 原始参考项目
