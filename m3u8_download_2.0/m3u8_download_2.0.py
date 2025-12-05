import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import threading
import subprocess
import os
import sys
import re
from datetime import datetime
import requests

class M3U8DownloaderGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("M3U8视频下载器")
        self.root.geometry("900x650")

        # 实例化下载器管理器
        self.downloader_manager = DownloaderManager(self.log_message)

        # 检查必要库
        self.check_dependencies()

        # 设置样式
        self.setup_styles()

        # 创建主框架
        self.create_widgets()

        # 初始化下载状态
        self.downloading = False
        self.process = None

    def check_dependencies(self):
        """检查必要的依赖库"""
        try:
            import requests
        except ImportError:
            messagebox.showerror("缺少依赖", "请先安装requests库：\n\npip install requests")
            self.root.quit()

    def setup_styles(self):
        """设置界面样式"""
        style = ttk.Style()
        style.theme_use('clam')

        # 自定义颜色
        self.bg_color = "#f0f0f0"
        self.primary_color = "#2196F3"
        self.secondary_color = "#3F51B5"
        self.success_color = "#4CAF50"
        self.warning_color = "#FF9800"
        self.danger_color = "#F44336"

        self.root.configure(bg=self.bg_color)

    def create_widgets(self):
        """创建界面组件"""
        # 标题
        title_label = tk.Label(
            self.root,
            text="M3U8视频下载器",
            font=("微软雅黑", 24, "bold"),
            bg=self.primary_color,
            fg="white",
            pady=10
        )
        title_label.pack(fill=tk.X)

        # 主容器
        main_frame = tk.Frame(self.root, bg=self.bg_color, padx=20, pady=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 创建左侧输入区域和右侧进度区域
        left_frame = tk.Frame(main_frame, bg=self.bg_color)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

        right_frame = tk.Frame(main_frame, bg=self.bg_color)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # 左侧：输入设置区域
        settings_frame = tk.LabelFrame(
            left_frame,
            text="下载设置",
            font=("微软雅黑", 12, "bold"),
            bg=self.bg_color,
            padx=15,
            pady=15
        )
        settings_frame.pack(fill=tk.BOTH, expand=True)

        # M3U8地址输入
        m3u8_frame = tk.Frame(settings_frame, bg=self.bg_color)
        m3u8_frame.pack(fill=tk.X, pady=(0, 15))

        tk.Label(
            m3u8_frame,
            text="M3U8地址:",
            font=("微软雅黑", 11),
            bg=self.bg_color
        ).pack(side=tk.LEFT, padx=(0, 10), anchor=tk.W)

        self.m3u8_url = tk.Entry(
            m3u8_frame,
            font=("微软雅黑", 11),
            width=40
        )
        self.m3u8_url.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 工作目录设置
        workdir_frame = tk.Frame(settings_frame, bg=self.bg_color)
        workdir_frame.pack(fill=tk.X, pady=(0, 15))

        tk.Label(
            workdir_frame,
            text="输出目录:",
            font=("微软雅黑", 11),
            bg=self.bg_color
        ).pack(side=tk.LEFT, padx=(0, 10), anchor=tk.W)

        self.work_dir = tk.Entry(
            workdir_frame,
            font=("微软雅黑", 11),
            width=40
        )
        self.work_dir.insert(0, os.path.join(os.path.expanduser("~"), "Downloads", "M3U8_Downloads"))
        self.work_dir.pack(side=tk.LEFT, fill=tk.X, expand=True)

        browse_dir_btn = tk.Button(
            workdir_frame,
            text="浏览",
            font=("微软雅黑", 9),
            bg=self.secondary_color,
            fg="white",
            command=self.browse_directory,
            padx=10,
            pady=3,
            cursor="hand2"
        )
        browse_dir_btn.pack(side=tk.LEFT, padx=(5, 0))

        # 保存文件名设置
        filename_frame = tk.Frame(settings_frame, bg=self.bg_color)
        filename_frame.pack(fill=tk.X, pady=(0, 15))

        tk.Label(
            filename_frame,
            text="文件名:",
            font=("微软雅黑", 11),
            bg=self.bg_color
        ).pack(side=tk.LEFT, padx=(0, 10), anchor=tk.W)

        self.save_name = tk.Entry(
            filename_frame,
            font=("微软雅黑", 11),
            width=40
        )
        self.save_name.insert(0, "output")
        self.save_name.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 线程设置
        thread_frame = tk.Frame(settings_frame, bg=self.bg_color)
        thread_frame.pack(fill=tk.X, pady=(0, 15))

        tk.Label(
            thread_frame,
            text="线程设置:",
            font=("微软雅黑", 11),
            bg=self.bg_color
        ).pack(side=tk.LEFT, padx=(0, 10), anchor=tk.W)

        # 最高线程数
        tk.Label(
            thread_frame,
            text="最高线程:",
            font=("微软雅黑", 10),
            bg=self.bg_color
        ).pack(side=tk.LEFT, padx=(0, 5))

        self.max_threads = ttk.Spinbox(
            thread_frame,
            from_=1,
            to=32,
            width=8,
            font=("微软雅黑", 10)
        )
        self.max_threads.set("16")
        self.max_threads.pack(side=tk.LEFT, padx=(0, 15))

        # 最低线程数
        tk.Label(
            thread_frame,
            text="最低线程:",
            font=("微软雅黑", 10),
            bg=self.bg_color
        ).pack(side=tk.LEFT, padx=(0, 5))

        self.min_threads = ttk.Spinbox(
            thread_frame,
            from_=1,
            to=32,
            width=8,
            font=("微软雅黑", 10)
        )
        self.min_threads.set("8")
        self.min_threads.pack(side=tk.LEFT)

        # 控制按钮区域
        button_frame = tk.Frame(settings_frame, bg=self.bg_color)
        button_frame.pack(fill=tk.X, pady=(20, 0))

        self.start_button = tk.Button(
            button_frame,
            text="开始下载",
            font=("微软雅黑", 12, "bold"),
            bg=self.success_color,
            fg="white",
            command=self.start_download,
            padx=30,
            pady=10,
            cursor="hand2"
        )
        self.start_button.pack(side=tk.LEFT, padx=(0, 10))

        self.stop_button = tk.Button(
            button_frame,
            text="停止下载",
            font=("微软雅黑", 12, "bold"),
            bg=self.danger_color,
            fg="white",
            command=self.stop_download,
            padx=30,
            pady=10,
            cursor="hand2",
            state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT, padx=(0, 10))

        self.clear_button = tk.Button(
            button_frame,
            text="清空日志",
            font=("微软雅黑", 11),
            bg=self.primary_color,
            fg="white",
            command=self.clear_logs,
            padx=20,
            pady=8,
            cursor="hand2"
        )
        self.clear_button.pack(side=tk.LEFT)

        # 右侧：进度显示区域
        progress_frame = tk.LabelFrame(
            right_frame,
            text="下载进度",
            font=("微软雅黑", 12, "bold"),
            bg=self.bg_color,
            padx=15,
            pady=15
        )
        progress_frame.pack(fill=tk.BOTH, expand=True)

        # 总进度条
        self.total_progress_label = tk.Label(
            progress_frame,
            text="准备下载...",
            font=("微软雅黑", 10),
            bg=self.bg_color
        )
        self.total_progress_label.pack(anchor=tk.W)

        self.total_progress = ttk.Progressbar(
            progress_frame,
            length=400,
            mode='determinate'
        )
        self.total_progress.pack(fill=tk.X, pady=(5, 15))

        # 下载进度
        download_frame = tk.Frame(progress_frame, bg=self.bg_color)
        download_frame.pack(fill=tk.X, pady=(0, 10))

        tk.Label(
            download_frame,
            text="下载进度:",
            font=("微软雅黑", 10, "bold"),
            bg=self.bg_color
        ).pack(side=tk.LEFT, padx=(0, 10))

        self.download_progress_label = tk.Label(
            download_frame,
            text="0/0 (0.00%)",
            font=("微软雅黑", 10),
            bg=self.bg_color,
            fg=self.primary_color
        )
        self.download_progress_label.pack(side=tk.LEFT)

        # 合并进度
        merge_frame = tk.Frame(progress_frame, bg=self.bg_color)
        merge_frame.pack(fill=tk.X, pady=(0, 10))

        tk.Label(
            merge_frame,
            text="合并进度:",
            font=("微软雅黑", 10, "bold"),
            bg=self.bg_color
        ).pack(side=tk.LEFT, padx=(0, 10))

        self.merge_progress_label = tk.Label(
            merge_frame,
            text="等待合并...",
            font=("微软雅黑", 10),
            bg=self.bg_color,
            fg=self.warning_color
        )
        self.merge_progress_label.pack(side=tk.LEFT)

        # 速度显示
        self.speed_label = tk.Label(
            progress_frame,
            text="速度: 0 KB/s",
            font=("微软雅黑", 10),
            bg=self.bg_color,
            fg=self.success_color
        )
        self.speed_label.pack(anchor=tk.W)

        # 文件信息显示
        self.file_info_label = tk.Label(
            progress_frame,
            text="文件信息: 等待获取...",
            font=("微软雅黑", 9),
            bg=self.bg_color,
            fg="gray",
            wraplength=400,
            justify=tk.LEFT
        )
        self.file_info_label.pack(anchor=tk.W, pady=(10, 0))

        # 工具状态显示
        self.tool_status_label = tk.Label(
            progress_frame,
            text="工具状态: 正在检查...",
            font=("微软雅黑", 9),
            bg=self.bg_color,
            fg="blue",
            wraplength=400,
            justify=tk.LEFT
        )
        self.tool_status_label.pack(anchor=tk.W, pady=(5, 0))

        # 日志输出区域
        log_frame = tk.LabelFrame(
            right_frame,
            text="实时日志",
            font=("微软雅黑", 12, "bold"),
            bg=self.bg_color,
            padx=15,
            pady=15
        )
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=12,
            font=("Consolas", 9),
            wrap=tk.WORD,
            bg="#1e1e1e",
            fg="white",
            insertbackground="white"
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # 状态栏
        self.status_bar = tk.Label(
            self.root,
            text="就绪 | 输出目录: 请设置 | 文件名: output",
            bd=1,
            relief=tk.SUNKEN,
            anchor=tk.W,
            font=("微软雅黑", 9),
            bg="#e0e0e0"
        )
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        # 启动后检查工具状态
        self.root.after(100, self.check_tool_status)

        # 添加时间戳和欢迎信息
        self.log_message("=" * 60)
        self.log_message("M3U8视频下载器 已启动")
        self.log_message(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.log_message("=" * 60)
        self.update_status("就绪")

    def check_tool_status(self):
        """检查工具状态并更新UI"""
        tool_path = self.downloader_manager.get_downloader_path()
        if tool_path:
            self.tool_status_label.config(text=f"工具状态: 就绪", fg=self.success_color)
        else:
            self.tool_status_label.config(text="工具状态: 未找到必要组件", fg=self.danger_color)

    def browse_directory(self):
        """浏览并选择输出目录"""
        directory = filedialog.askdirectory(title="选择输出目录")
        if directory:
            self.work_dir.delete(0, tk.END)
            self.work_dir.insert(0, directory)
            self.update_status(f"输出目录: {directory}")

    def log_message(self, message):
        """向日志框添加消息"""
        timestamp = datetime.now().strftime("[%H:%M:%S] ")
        self.log_text.insert(tk.END, timestamp + message + "\n")
        self.log_text.see(tk.END)
        self.root.update()

    def update_status(self, message):
        """更新状态栏"""
        work_dir = self.work_dir.get().strip()
        save_name = self.save_name.get().strip()
        status_text = f"{message} | 输出目录: {work_dir if work_dir else '未设置'} | 文件名: {save_name if save_name else '未设置'}"
        self.status_bar.config(text=status_text)
        self.root.update()

    def start_download(self):
        """开始下载"""
        if self.downloading:
            messagebox.showwarning("警告", "下载正在进行中！")
            return
            
        # 验证输入
        m3u8_url = self.m3u8_url.get().strip()
        if not m3u8_url:
            messagebox.showerror("错误", "请输入M3U8地址！")
            return
            
        # 验证URL格式
        if not (m3u8_url.startswith("http://") or m3u8_url.startswith("https://")):
            messagebox.showerror("错误", "请输入有效的HTTP/HTTPS地址！")
            return
        
        # 验证输出目录
        work_dir = self.work_dir.get().strip()
        if not work_dir:
            messagebox.showerror("错误", "请输入输出目录！")
            return
            
        # 验证文件名
        save_name = self.save_name.get().strip()
        if not save_name:
            messagebox.showerror("错误", "请输入保存文件名！")
            return
            
        # 检查文件名是否包含非法字符
        illegal_chars = ['<', '>', ':', '"', '|', '?', '*', '\\', '/']
        for char in illegal_chars:
            if char in save_name:
                messagebox.showerror("错误", f"文件名包含非法字符: {char}")
                return
            
        try:
            max_threads = int(self.max_threads.get())
            min_threads = int(self.min_threads.get())
            
            if min_threads > max_threads:
                messagebox.showerror("错误", "最低线程数不能大于最高线程数！")
                return
                
            if max_threads < 1 or min_threads < 1:
                messagebox.showerror("错误", "线程数必须大于0！")
                return
                
        except ValueError:
            messagebox.showerror("错误", "请输入有效的线程数！")
            return

        # 获取下载器路径
        downloader_path = self.downloader_manager.get_downloader_path()
        if not downloader_path:
            messagebox.showerror("错误", "无法找到下载器，请确保程序已正确打包。")
            return

        # 更新UI状态
        self.downloading = True
        self.start_button.config(state=tk.DISABLED, bg="#cccccc")
        self.stop_button.config(state=tk.NORMAL)

        # 重置进度条
        self.total_progress['value'] = 0
        self.download_progress_label.config(text="0/0 (0.00%)")
        self.merge_progress_label.config(text="等待合并...", fg=self.warning_color)
        self.speed_label.config(text="速度: 0 KB/s")
        self.file_info_label.config(text="文件信息: 等待获取...")
        self.total_progress_label.config(text="正在初始化下载...")
        self.update_status("正在启动下载...")

        # 在新线程中开始下载
        download_thread = threading.Thread(
            target=self.download_m3u8,
            args=(m3u8_url, work_dir, save_name, max_threads, min_threads, downloader_path),
            daemon=True
        )
        download_thread.start()

    def stop_download(self):
        """停止下载"""
        if self.downloading and self.process:
            try:
                self.process.terminate()
                self.log_message("正在停止下载进程...")
                self.update_status("正在停止下载...")
            except:
                pass
            finally:
                self.downloading = False
                self.log_message("下载已停止")
                self.total_progress_label.config(text="下载已停止")
                self.merge_progress_label.config(text="已停止", fg=self.danger_color)
                self.update_status("下载已停止")
                self.reset_ui_state()
                
    def reset_ui_state(self):
        """重置UI状态"""
        self.start_button.config(state=tk.NORMAL, bg=self.success_color)
        self.stop_button.config(state=tk.DISABLED)
        
    def download_m3u8(self, m3u8_url, work_dir, save_name, max_threads, min_threads, downloader_path):
        """执行下载操作"""
        try:
            # 临时M3U8文件路径
            temp_m3u8 = os.path.join(work_dir, f"temp_playlist_{int(datetime.now().timestamp())}.m3u8")

            # 步骤1: 使用requests库下载m3u8文件
            self.log_message(f"步骤1: 下载M3U8文件")
            self.log_message(f"URL: {m3u8_url}")
            self.update_status("正在下载M3U8文件...")

            try:
                self.log_message("正在下载M3U8文件...")
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                }
                response = requests.get(m3u8_url, headers=headers, timeout=30)
                response.raise_for_status()

                with open(temp_m3u8, 'wb') as f:
                    f.write(response.content)

                self.log_message("M3U8文件下载完成")
                self.update_status("M3U8文件下载完成")

            except requests.exceptions.RequestException as e:
                error_msg = f"M3U8文件下载失败: {e}"
                self.log_message(error_msg)
                self.update_status("M3U8下载失败")
                self.on_download_failed()
                return

            # 步骤2: 使用N_m3u8DL-CLI下载视频
            self.log_message(f"步骤2: 下载视频")
            self.log_message(f"输出目录: {work_dir}")
            self.log_message(f"文件名: {save_name}")
            self.update_status("正在启动视频下载...")

            # 创建工作目录（如果不存在）
            if not os.path.exists(work_dir):
                try:
                    os.makedirs(work_dir)
                    self.log_message(f"创建工作目录: {work_dir}")
                except Exception as e:
                    self.log_message(f"创建工作目录失败: {str(e)}")

            # 获取下载器目录，确保ffmpeg在同一目录下能被找到
            downloader_dir = os.path.dirname(downloader_path)

            # 构建命令
            cmd = [
                downloader_path,
                temp_m3u8,
                "--workDir", work_dir,
                "--saveName", save_name,
                "--maxThreads", str(max_threads),
                "--minThreads", str(min_threads),
                "--retryCount", "99",
                "--enableDelAfterDone"
            ]

            self.log_message("正在执行下载命令...")
            self.update_status("正在执行下载命令...")

            # 执行下载命令
            try:
                if sys.platform == 'win32':
                    creation_flags = subprocess.CREATE_NO_WINDOW
                else:
                    creation_flags = 0

                # 设置工作目录到下载器所在目录，确保ffmpeg能被找到
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    creationflags=creation_flags,
                    shell=False,
                    cwd=downloader_dir
                )

                self.log_message("下载进程已启动")
                self.update_status("下载进行中...")

            except Exception as e:
                error_msg = f"启动下载进程失败: {str(e)}"
                self.log_message(error_msg)
                self.update_status("启动进程失败")
                self.on_download_failed()
                return

            # 实时读取输出
            for line in iter(self.process.stdout.readline, ''):
                if not line:
                    break

                # 显示在日志中
                line = line.strip()
                if line:
                    self.log_message(line)
                    # 解析进度信息
                    self.parse_progress(line)

            # 等待进程完成
            self.process.wait()

            # 清理临时文件
            try:
                if os.path.exists(temp_m3u8):
                    os.remove(temp_m3u8)
                    self.log_message(f"已清理临时文件")
            except:
                pass

            # 检查退出码
            if self.process.returncode == 0:
                self.log_message("=" * 60)
                self.log_message("下载完成！")
                self.log_message(f"文件已保存到: {work_dir}")
                self.log_message(f"文件名: {save_name}")
                self.update_status("下载完成")
                self.on_download_complete()
            else:
                error_msg = f"下载失败，退出码: {self.process.returncode}"
                self.log_message(error_msg)
                self.update_status("下载失败")
                self.on_download_failed()

        except Exception as e:
            error_msg = f"发生错误: {str(e)}"
            self.log_message(error_msg)
            self.update_status("发生错误")
            self.on_download_failed()
            
    def parse_progress(self, line):
        """解析进度信息并更新UI"""
        try:
            # 更新总进度标签
            if "开始解析" in line or "开始下载" in line:
                self.total_progress_label.config(text=line)
                self.update_status(line)
            elif "文件时长" in line:
                self.total_progress_label.config(text=line)
                self.file_info_label.config(text=f"文件信息: {line}")
            elif "总分片" in line:
                self.total_progress_label.config(text=line)
                
            # 解析下载进度
            if "完成数量" in line and "/" in line:
                # 例如: "完成数量 58 / 127"
                match = re.search(r'完成数量\s+(\d+)\s+/\s+(\d+)', line)
                if match:
                    current = int(match.group(1))
                    total = int(match.group(2))
                    if total > 0:
                        percentage = (current / total) * 100
                        self.download_progress_label.config(
                            text=f"{current}/{total} ({percentage:.2f}%)"
                        )
                        self.total_progress['value'] = percentage
                        self.update_status(f"下载中: {current}/{total} ({percentage:.1f}%)")
                        
            # 解析合并进度
            if "等待下载完成" in line:
                self.merge_progress_label.config(text="等待下载完成...", fg=self.warning_color)
            elif "开始合并" in line or "正在合并" in line:
                self.merge_progress_label.config(text="正在合并文件...", fg=self.primary_color)
                self.update_status("正在合并文件...")
            elif "合并完成" in line or "完成合并" in line:
                self.merge_progress_label.config(text="合并完成 ✓", fg=self.success_color)
                self.total_progress['value'] = 100
                self.update_status("合并完成")
                
            # 解析下载速度
            if "KB/s" in line:
                speed_match = re.search(r'(\d+\.?\d*)\s+KB/s', line)
                if speed_match:
                    speed = speed_match.group(1)
                    self.speed_label.config(text=f"速度: {speed} KB/s")
                    
            # 解析文件信息
            if "Video" in line or "Audio" in line:
                self.file_info_label.config(text=f"文件信息: {line}")
                    
            # 解析总进度条
            if "Progress:" in line:
                # 例如: "Progress: 118/127 (92.91%) -- 27.61 MB/29.72 MB"
                match = re.search(r'Progress:\s+(\d+)/(\d+)\s+\((\d+\.?\d*)%\)', line)
                if match:
                    current = int(match.group(1))
                    total = int(match.group(2))
                    percentage = float(match.group(3))
                    self.download_progress_label.config(
                        text=f"{current}/{total} ({percentage:.2f}%)"
                    )
                    self.total_progress['value'] = percentage
                    self.update_status(f"进度: {percentage:.1f}%")
                    
        except Exception as e:
            # 解析出错时不中断程序
            pass
            
    def on_download_complete(self):
        """下载完成时的处理"""
        self.downloading = False
        self.reset_ui_state()
        work_dir = self.work_dir.get().strip()
        save_name = self.save_name.get().strip()
        self.total_progress_label.config(text=f"下载完成！文件: {save_name}", fg=self.success_color)
        self.merge_progress_label.config(text="合并完成！ ✓", fg=self.success_color)
        self.update_status(f"下载完成: {save_name}")
        
    def on_download_failed(self):
        """下载失败时的处理"""
        self.downloading = False
        self.reset_ui_state()
        self.total_progress_label.config(text="下载失败 ✗", fg=self.danger_color)
        self.merge_progress_label.config(text="失败 ✗", fg=self.danger_color)
        self.update_status("下载失败")
        
    def clear_logs(self):
        """清空日志"""
        self.log_text.delete(1.0, tk.END)
        self.log_message("日志已清空")
        self.update_status("日志已清空")
        
    def on_closing(self):
        """关闭窗口时的处理"""
        if self.downloading and self.process:
            try:
                self.process.terminate()
                self.log_message("程序关闭，已终止下载进程")
            except:
                pass
        self.root.destroy()


class DownloaderManager:
    """管理 N_m3u8DL-CLI 下载器的自动查找"""
    
    def __init__(self, gui_callback=None):
        self.gui_log = gui_callback if gui_callback else print
        
    def get_downloader_path(self):
        """获取下载器路径"""
        # 定义所有可能的文件名（按优先级排序）
        possible_names = [
            "N_m3u8DL-CLI_v3.0.2.exe",  # 带版本号的完整名称
            "N_m3u8DL-CLI.exe",         # 通用名称
            "N_m3u8DL-CLI-SimpleG.exe", # SimpleG版本
        ]
        
        # 1. 优先检查 PyInstaller 打包环境
        if getattr(sys, 'frozen', False):
            base_path = sys._MEIPASS
            
            # 首先检查 tools 子目录（根据您的日志，文件在这里）
            tools_dir = os.path.join(base_path, 'tools')
            if os.path.exists(tools_dir):
                for exe_name in possible_names:
                    exe_path = os.path.join(tools_dir, exe_name)
                    if os.path.exists(exe_path):
                        #self.gui_log(f"找到下载器: {exe_name}")
                        return exe_path
            
            # 如果没找到，尝试根目录
            for exe_name in possible_names:
                exe_path = os.path.join(base_path, exe_name)
                if os.path.exists(exe_path):
                    #self.gui_log(f"找到下载器: {exe_name}")
                    return exe_path
        
        else:
            # 2. 开发环境：当前目录下的 tools 文件夹
            dev_tools_dir = os.path.join(os.getcwd(), "tools")
            if os.path.exists(dev_tools_dir):
                for exe_name in possible_names:
                    exe_path = os.path.join(dev_tools_dir, exe_name)
                    if os.path.exists(exe_path):
                        #self.gui_log(f"找到下载器: {exe_name}")
                        return exe_path
        
        # 3. 如果以上都未找到
        self.gui_log("错误: 未找到N_m3u8DL-CLI可执行文件")
        return None


def main():
    root = tk.Tk()
    app = M3U8DownloaderGUI(root)
    
    # 设置关闭窗口时的处理
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    
    # 窗口居中显示
    root.update_idletasks()
    width = root.winfo_width()
    height = root.winfo_height()
    x = (root.winfo_screenwidth() // 2) - (width // 2)
    y = (root.winfo_screenheight() // 2) - (height // 2)
    root.geometry(f'{width}x{height}+{x}+{y}')
    
    # 运行程序
    root.mainloop()


if __name__ == "__main__":
    main()