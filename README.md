# 媒体文件只读损坏检测器（Python + ffmpeg/ffprobe + exiftool）

> 一个**只读**、**多线程**的图片/视频损坏扫描工具。支持递归子目录、中文文件名、三档检测模式（快/中/慢），并在终端显示实时进度。使用外部工具 `ffprobe/ffmpeg` 与 `exiftool` 进行容器与解码层面的校验。

---

## 目录
- [特性一览](#特性一览)
- [工作原理](#工作原理)
- [环境要求](#环境要求)
- [安装与准备](#安装与准备)
- [使用方法](#使用方法)
  - [基础示例](#基础示例)
  - [参数说明](#参数说明)
  - [模式与判定标准](#模式与判定标准)
  - [输出与进度](#输出与进度)
- [支持的文件类型](#支持的文件类型)
- [性能与并发建议](#性能与并发建议)
- [安全性与只读保证](#安全性与只读保证)
- [常见问题（FAQ）与故障排查](#常见问题faq与故障排查)
- [已知局限](#已知局限)


---

## 特性一览
- **只读操作**：不写入被扫描目录中的任何文件。
- **递归遍历**：自动深入所有子目录扫描媒体文件。
- **三档检测模式**：
  - **fast**：容器/元数据探测（`ffprobe` + `exiftool`），速度最快。
  - **medium**：在 fast 基础上**解码首帧**（`ffmpeg`），能发现多数解码错误。
  - **slow**：视频进行**全轨道完整解码**（`ffmpeg -f null -`），最严格但最慢。
- **进度显示**：实时显示已检/总数、百分比、OK/损坏统计。
- **多线程**：`ThreadPoolExecutor` 并发扫描，充分利用多核；线程数可配置。
- **中文路径**：完整支持中文/空格等特殊字符文件名。
- **可扩展**：支持自定义扩展名过滤与单文件超时。

---

## 工作原理
程序对每个文件进行若干级别的“可解析/可解码”验证：
1. **容器/元数据可读（fast）**：
   - `ffprobe -v error -show_entries format,stream -of json <file>`：检查容器/流元数据能否被解析且无错误输出。
   - `exiftool -fast -fast2 -n -S -s -s -s <file>`：读取基础元数据，若输出中含 `Error:` 或进程报错，则视为失败。
   - 任一工具成功，即认为容器基本可读。
2. **解码首帧（medium 新增）**：
   - `ffmpeg -v error -i <file> -frames:v 1 -an -f null -`：尝试解码第一帧像素（视频/图像均适用）。
3. **完整解码（slow 替换）**：
   - `ffmpeg -v error -i <file> -map 0 -f null -`：对所有轨道进行完整解码（视频帧与音频样本）。

> **判定逻辑概要**：
> - **fast**：容器/元数据可读即判 **OK**；否则 **damaged**。
> - **medium**：**容器可读 且 首帧可解码** → **OK**；否则 **damaged**。
> - **slow**：以**完整解码**结果为准；成功 → **OK**，失败 → **damaged**（同时回报 fast/首帧诊断供参考）。

---

## 环境要求
- **Python**：3.8+（已在 3.12 上测试）
- **外部工具**（需在系统 `PATH` 中）：
  - FFmpeg 套件：`ffprobe` 与 `ffmpeg`
  - `exiftool`
- 操作系统：Windows / macOS / Linux

### 安装外部工具（示例）
**Windows**（任选其一）：
- [Chocolatey](https://chocolatey.org/)：`choco install ffmpeg exiftool`
- [Scoop](https://scoop.sh/)：`scoop install ffmpeg exiftool`

**macOS**：
- Homebrew：`brew install ffmpeg exiftool`

**Ubuntu/Debian**：
```bash
sudo apt-get update
sudo apt-get install -y ffmpeg exiftool
```

安装完成后，确保在终端能直接执行 `ffprobe -version`、`ffmpeg -version`、`exiftool -ver`。

---

## 安装与准备
1. 将脚本保存为 `check_media_integrity.py`（已随本项目提供）。
2. 确认外部工具已安装并在 `PATH`。
3. 打开终端（Windows 可用 PowerShell），切换到脚本所在目录。

---

## 使用方法

### 基础示例
**Windows（PowerShell）**
```powershell
python .\check_media_integrity.py --root "D:\\珍贵相册" --mode medium --workers 4 --timeout 120 --list-damaged
```

**Linux/macOS（Bash）**
```bash
python ./check_media_integrity.py --root "/mnt/nas/Family/相册" --mode slow --workers 8 --timeout 300 --list-damaged
```

### 参数说明
| 参数 | 必填 | 说明 | 默认值 |
|---|---:|---|---|
| `--root` | 是 | 要扫描的根目录 | 无 |
| `--mode` | 否 | 检测档位：`fast`/`medium`/`slow` | `medium` |
| `--workers` | 否 | 并发线程数 | `min(8, max(2, CPU核数))` |
| `--timeout` | 否 | **单文件**检测超时（秒） | 120 |
| `--include-exts` | 否 | 自定义扩展名（逗号分隔，带或不带点均可） | 使用内置集合 |
| `--list-damaged` | 否 | 检测结束后打印损坏/错误文件的详细原因 | 关闭 |

> `--include-exts` 一旦指定，将**同时**作为图片与视频扩展名集合使用（等同于“白名单”过滤），便于针对性排查某些格式。

### 模式与判定标准
- **fast（快）**
  - **依据**：容器/元数据可被解析（`ffprobe`/`exiftool` 二者任一成功）。
  - **优点**：最快；适合初筛/大范围健康检查。
  - **缺点**：不解码像素，可能漏掉编码层损坏。
- **medium（中）**
  - **依据**：**容器可读 且 首帧可解码**（更严格）。
  - **优点**：能发现多数实际解码错误，速度与覆盖度平衡。
  - **缺点**：比 fast 慢；极端尾部损坏的视频可能仍需 slow 识别。
- **slow（慢）**
  - **依据**：**完整解码**（视频全帧/音频全样本）；图像与 medium 等效（单帧即完整）。
  - **优点**：最严格，能发现绝大多数问题。
  - **缺点**：最慢、最耗 CPU/IO；建议只在疑难集上使用。

### 输出与进度
运行开始会打印外部工具可用性与所选模式依据，例如：
```
外部工具可用性：
  ffprobe:   OK
  ffmpeg:    OK
  exiftool:  OK

检测模式：medium
依据：medium：在 fast 基础上，额外用 ffmpeg 解码首帧（图像/视频），能发现大多数解码层错误。
```
扫描过程中会显示单行进度（持续刷新）：
```
进度: 128/1024 (12.5%) | OK: 120 | 损坏: 8
```
结束后给出汇总，并在 `--list-damaged` 打开时列出详情：
```
==== 检测完成 ====
根目录：D:\珍贵相册
模式：medium
总数：1024 | OK/跳过：1000 | 损坏/错误：24

-- 损坏/错误文件清单 --
[DAMAGED] D:\珍贵相册\2020\旅拍\clip001.mp4 | medium：... | fast: ffprobe rc=0 ... | first-frame: ffmpeg[first-frame] rc=1 err_len=96
...
```

> 说明：被标记为 `skipped` 的文件（如不在扩展名白名单内）会计入“OK/跳过”统计，但不会出现在损坏清单中。

---

## 支持的文件类型
默认图片扩展名：
```
.jpg .jpeg .png .gif .bmp .tif .tiff .webp .heic .heif .dng .cr2 .cr3 .nef .arw .raf .rw2 .orf
```
默认视频扩展名：
```
.mp4 .m4v .mov .avi .mkv .webm .wmv .flv .mts .m2ts .ts .3gp .3gpp .mxf .mpg .mpeg .vob
```
> 你也可以用 `--include-exts` 完全自定义需要扫描的扩展名集合（逗号分隔）。

---

## 性能与并发建议
- **线程数（--workers）**：
  - 若瓶颈在**磁盘 IO**（NAS/HDD），线程过多会引发抖动；可从 `CPU核数` 或 `4~8` 之间试探。
  - 若大多是**CPU 解码**（slow 模式），可接近 `CPU核数`，但别忽略温控与散热。
- **超时（--timeout）**：
  - 用于避免个别问题文件或驱动导致的“卡死”。大型 4K/8K 或超长视频在 slow 模式可能需要更长超时。
- **存储介质**：SSD 明显优于 HDD/NAS；尽量避免同时进行大文件拷贝或渲染。

---

## 安全性与只读保证
- 程序对目标目录仅执行**读取**（列表、打开、解码）操作，不会写入文件、修改时间戳或元数据。
- 所有外部工具调用都以**空设备输出**（`-f null -`）验证，不会产生临时媒体文件。
- 若需进一步背书：
  - Linux 可用 `strace -f -e trace=file -o trace.log python check_media_integrity.py ...` 观察系统调用；
  - Windows 可使用 **Process Monitor** 过滤 `CreateFile` 的写入标志进行旁证。

---

## 常见问题（FAQ）与故障排查
**Q1：出现 `UnicodeDecodeError: 'gbk' codec can't decode ...`（Windows）？**  
A：已在脚本中升级了子进程输出的处理方式：统一以**字节读取**，并按 **UTF‑8 → 系统首选编码 → GBK/CP936 → Latin‑1** 顺序**回退解码**，避免该问题。请使用当前版本脚本重新运行。

**Q2：提示 `ffprobe/ffmpeg/exiftool` not found？**  
A：未安装或未加入 `PATH`。请按上文安装方式安装，并重新打开终端；用 `ffprobe -version` 等命令确认可执行。

**Q3：为什么 `fast` 通过但 `medium/slow` 判为损坏？**  
A：容器可读≠内容可解码。`medium` 增加了解码首帧，`slow` 要求完整解码，能暴露编码层/轨道级问题（如关键帧损坏、音频流损坏）。

**Q4：`slow` 太慢怎么办？**  
A：建议先用 `fast` 全量初筛，`medium` 二次聚焦，最后对疑难集用 `slow` 逐个击破；或增大 `--workers` 并适当提升 `--timeout`。

**Q5：HEIC/RAW/某些专有编码报告解码失败？**  
A：取决于你安装的 FFmpeg/`exiftool` 对相应编解码器/容器的支持。更换/升级 FFmpeg 构建，或改用厂商工具验证。

**Q6：损坏清单中的 `rc=124/125` 是什么？**  
A：`124` 表示**超时**（`TimeoutExpired`），`125` 表示本地**异常**（例如进程启动失败）。

**Q7：输出里偶有“乱码/问号”字符？**  
A：这是为保证不因编码问题中断扫描所采取的“**宽松回退解码**”策略，不影响判定结论。

**Q8：能导出 JSON/CSV 报告吗？**  
A：当前版本仅打印到控制台。

---

## 已知局限
- **真伪判定范围**：
  - 本工具以“**能否被常用工具解析/解码**”为健康标准；对内容语义（如人脸是否完整、画面是否偏色）不做判断。
- **Codec 依赖**：不同平台/构建的 FFmpeg 对编解码器支持不完全一致，极少数文件可能出现“误报损坏”。
- **首帧充分性**：`medium` 模式仅解码首帧，若损坏发生在视频后段，需 `slow` 模式才能发现。
