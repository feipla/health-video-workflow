"""
泛健康科普视频自动化工作流 - Web 网页版
一键部署到魔搭社区 ModelScope / Hugging Face Spaces
"""

import gradio as gr
import os
import sys
import json
import re
import time
import shutil
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from datetime import datetime

# 导入信息图生成器
from infographic_generator import generate_infographics_from_script, parse_script_sections

# 白板动画脚本路径（本地 bundled，无需外部依赖）
_THIS_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
WHITEBOARD_GEN_SCRIPT = str(_THIS_DIR / "whiteboard_engine.py")
WHITEBOARD_ASSETS_DIR = str(_THIS_DIR / "assets")

# ============ 启动自检 ============
def _check_environment():
    """启动时检查依赖环境"""
    missing = []

    # 检查 ffmpeg
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            missing.append("ffmpeg")
    except:
        missing.append("ffmpeg")

    if missing:
        print(f"[启动自检] 缺少系统依赖: {', '.join(missing)}，尝试安装...")
        for pkg in missing:
            try:
                subprocess.run(["apt-get", "update", "-qq"], capture_output=True, timeout=60)
                subprocess.run(["apt-get", "install", "-y", "-qq", pkg], capture_output=True, timeout=120)
                print(f"[OK] {pkg} 安装成功")
            except Exception as e:
                print(f"[WARN] 无法安装 {pkg}: {e}")

    # 检查 Python 依赖
    import_errors = []
    for name, import_name in [("opencv-python", "cv2"), ("Pillow", "PIL"), ("av", "av")]:
        try:
            __import__(import_name)
        except ImportError:
            import_errors.append(name)

    if import_errors:
        print(f"[启动自检] 缺少 Python 依赖: {', '.join(import_errors)}")
        subprocess.run([sys.executable, "-m", "pip", "install"] + import_errors,
                       capture_output=True, timeout=120)
        print(f"[OK] Python 依赖安装完成")

    print("[启动自检] 环境检查完成")

_check_environment()

# ============ 配置 ============
OUTPUT_BASE = "./output"
TEMP_BASE = "./temp"

# 确保输出目录存在
Path(OUTPUT_BASE).mkdir(parents=True, exist_ok=True)
Path(TEMP_BASE).mkdir(parents=True, exist_ok=True)

# ============ 工具函数 ============

def log(msg):
    """带时间戳的日志"""
    ts = datetime.now().strftime("%H:%M:%S")
    return f"[{ts}] {msg}"

def run_cmd(cmd, cwd=None, timeout=300):
    """运行 shell 命令并返回输出"""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout
        )
        if result.returncode != 0:
            return False, result.stderr[:500]
        return True, result.stdout[:500]
    except subprocess.TimeoutExpired:
        return False, "命令执行超时"
    except Exception as e:
        return False, str(e)

def extract_video_id(url):
    """从 YouTube URL 提取视频 ID"""
    parsed = urlparse(url)
    if parsed.hostname in ("www.youtube.com", "youtube.com"):
        return parse_qs(parsed.query).get("v", [None])[0]
    elif parsed.hostname == "youtu.be":
        return parsed.path[1:]
    return None

def clean_old_outputs(max_dirs=10):
    """清理旧的输出目录，避免磁盘占满"""
    output_dirs = sorted(Path(OUTPUT_BASE).iterdir(), key=os.path.getmtime)
    while len(output_dirs) > max_dirs:
        shutil.rmtree(output_dirs[0], ignore_errors=True)
        output_dirs = sorted(Path(OUTPUT_BASE).iterdir(), key=os.path.getmtime)

# ============ 核心工作流（支持进度回调）============

class VideoWorkflow:
    """视频工作流，带进度回调"""

    def __init__(self, video_url, tts_voice="zh-CN-XiaoxiaoNeural"):
        self.video_url = video_url
        self.tts_voice = tts_voice
        self.video_id = extract_video_id(video_url)
        self.work_dir = f"{OUTPUT_BASE}/{self.video_id}" if self.video_id else None
        self.metadata = None
        self.transcript = None
        self.script = None
        self.audio_path = None
        self.duration = 0
        self.final_video = None
        self.llm_api_key = os.getenv("LLM_API_KEY", "")
        self.whiteboard_clips = []  # 白板动画片段路径列表
        # 代理配置：优先使用 UI 传入的 proxy，其次环境变量 HTTP_PROXY
        self.proxy = os.getenv("HTTP_PROXY", "") or os.getenv("HTTPS_PROXY", "")

    def step1_fetch_info(self, progress_callback):
        """Step 1: 获取 YouTube 视频信息"""
        progress_callback(log("Step 1/6: 正在获取视频信息..."))

        if not self.video_id:
            progress_callback(log("❌ 无法解析 YouTube 链接"))
            return False

        Path(self.work_dir).mkdir(parents=True, exist_ok=True)

        # 构建 yt-dlp 基础命令
        ytdlp_base = ["yt-dlp"]
        if self.proxy:
            ytdlp_base += ["--proxy", self.proxy]
            progress_callback(log(f"   使用代理: {self.proxy[:30]}..."))

        ok, info_json = run_cmd(ytdlp_base + [
            "--dump-json", "--skip-download",
            "--write-thumbnail",
            self.video_url
        ], cwd=self.work_dir)

        if not ok:
            progress_callback(log(f"❌ yt-dlp 获取失败: {info_json}"))
            return False

        try:
            info = json.loads(info_json.strip().split("\n")[0])
        except:
            progress_callback(log("❌ 解析视频信息失败"))
            return False

        self.metadata = {
            "video_id": self.video_id,
            "title": info.get("title", ""),
            "description": info.get("description", "")[:500],
            "channel": info.get("channel", ""),
            "duration": info.get("duration", 0),
            "upload_date": info.get("upload_date", ""),
            "tags": info.get("tags", []),
            "original_url": self.video_url,
        }

        with open(f"{self.work_dir}/metadata.json", "w", encoding="utf-8") as f:
            json.dump(self.metadata, f, ensure_ascii=False, indent=2)

        title_short = self.metadata["title"][:50]
        dur_min = self.metadata["duration"] // 60
        progress_callback(log(f"✅ 视频: {title_short}"))
        progress_callback(log(f"   频道: {self.metadata['channel']} | 时长: {dur_min}分钟"))
        return True

    def step2_transcribe(self, progress_callback):
        """Step 2: 下载音频并转写字幕"""
        progress_callback(log("Step 2/6: 正在下载音频..."))

        audio_path = f"{self.work_dir}/audio.mp3"
        # 复用 proxy 配置
        ytdlp_base = ["yt-dlp"]
        if self.proxy:
            ytdlp_base += ["--proxy", self.proxy]
        ok, msg = run_cmd(ytdlp_base + [
            "-f", "bestaudio", "--extract-audio",
            "--audio-format", "mp3", "--audio-quality", "0",
            "-o", audio_path, self.video_url
        ])
        if not ok or not os.path.exists(audio_path):
            progress_callback(log(f"❌ 音频下载失败: {msg}"))
            return False

        file_size = os.path.getsize(audio_path) / 1024 / 1024
        progress_callback(log(f"✅ 音频下载完成 ({file_size:.1f} MB)"))
        progress_callback(log("正在转写英文语音 → 文本 (Whisper base, 约1-3分钟)..."))

        ok, msg = run_cmd([
            "whisper", audio_path, "--model", "base",
            "--language", "en", "--output_format", "srt",
            "--output_dir", self.work_dir
        ], timeout=600)

        if not ok:
            progress_callback(log(f"❌ Whisper 转写失败: {msg}"))
            return False

        # 获取转写文件
        srt_path = f"{self.work_dir}/audio.srt"
        if not os.path.exists(srt_path):
            # 尝试其他可能文件名
            srt_files = list(Path(self.work_dir).glob("*.srt"))
            if srt_files:
                srt_path = str(srt_files[0])

        if not os.path.exists(srt_path):
            progress_callback(log("❌ 未找到字幕文件"))
            return False

        with open(srt_path, "r", encoding="utf-8") as f:
            transcript_raw = f.read()

        self.transcript = re.sub(r"\d+\n\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}\n", "", transcript_raw)
        self.transcript = re.sub(r"\n+", " ", self.transcript).strip()

        with open(f"{self.work_dir}/transcript.txt", "w", encoding="utf-8") as f:
            f.write(self.transcript)

        word_count = len(self.transcript.split())
        progress_callback(log(f"✅ 转写完成! 共 {word_count} 个英文单词"))
        return True

    def step3_generate_script(self, progress_callback):
        """Step 3: 生成中文脚本"""
        progress_callback(log("Step 3/6: 正在生成中文脚本..."))

        if self.llm_api_key:
            progress_callback(log("   使用 DeepSeek AI 生成..."))
            return self._step3_with_api(progress_callback)
        else:
            progress_callback(log("⚠️ 未配置 API Key，使用本地模板"))
            progress_callback(log("   脚本将需要手动完善"))
            return self._step3_local(progress_callback)

    def _step3_with_api(self, progress_callback):
        """使用 DeepSeek API 生成脚本"""
        prompt = f"""你是一位专业的健康科普视频脚本创作者。请根据以下YouTube视频的内容,创作一段适合中国受众的中文科普视频脚本。

【原始视频信息】
标题: {self.metadata['title']}
频道: {self.metadata['channel']}
描述: {self.metadata['description'][:500]}

【视频转写内容】(英文)
{self.transcript[:3000]}

【创作要求 - 必须严格遵守】
1. 字数: 严格控制在 950-1150 个中文字（不含标点）。这个字数配音后正好是 5-7 分钟。
2. 风格: 通俗易懂,像朋友聊天一样,避免学术腔
3. 结构:
   - 开头(约150字, 45秒): 用一个问题或场景引入,抓住观众注意力
   - 正文(约700-850字, 4-5分钟): 分3-4个要点,每个要点先讲核心理念,再讲科学依据
   - 结尾(约100-150字, 30-45秒): 总结+1-2个可立即执行的实操建议
4. 语言: 中文口语化,适合视频配音,句子要短
5. 合规:
   - 只讲健康生活方式建议,不涉及疾病诊断或治疗
   - 避免绝对化表述("一定""绝对""100%")
   - 涉及的研究数据要标注来源
   - 结尾必须加免责声明
6. 格式: 直接输出脚本正文,每段开头标注 [预计时长]

请直接输出脚本内容,严格控制字数在 950-1150 字之间。"""

        req_data = json.dumps({
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "你是一位专业的中文健康科普视频脚本创作者。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
            "max_tokens": 2000
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=req_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.llm_api_key}"
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                self.script = result["choices"][0]["message"]["content"]
        except Exception as e:
            progress_callback(log(f"⚠️ API 调用失败: {e}，使用本地模板"))
            return self._step3_local(progress_callback)

        with open(f"{self.work_dir}/script.txt", "w", encoding="utf-8") as f:
            f.write(self.script)

        char_count = len(self.script)
        progress_callback(log(f"✅ 脚本生成完成! 约 {char_count} 字符"))
        progress_callback(log("   ⏱ 预计配音时长: 5-7 分钟"))
        return True

    def _step3_local(self, progress_callback):
        """本地模板（无 API Key 时使用）"""
        self.script = f"""【开头 - 45秒】
大家好,今天我们来聊一个和每个人健康息息相关的话题。看完这期视频,你会对这个问题有一个全新的认识。

【要点1 - 90秒】
首先,让我们看看科学研究是怎么说的。最新的研究发现,这个问题比我们想象的更加普遍。研究人员对数千人进行了长期跟踪,得出了令人惊讶的结论。

【要点2 - 90秒】
那么,为什么会这样呢?科学家们认为,这和我们日常生活中的几个习惯密切相关。第一个是...第二个是...第三个是...

【要点3 - 90秒】
好消息是,我们完全可以通过一些简单的调整来改善这一状况。不需要昂贵的设备,也不需要复杂的计划,只需要从今天开始做出一些小改变。

【要点4 - 60秒】
这里有一个常见的误区——很多人认为必须做到完美才有用。但实际上,科学研究告诉我们,循序渐进的改变才是最有效、最可持续的方式。

【结尾 - 45秒】
总结一下,今天分享的三个核心观点是...如果你觉得有用,不妨从今天开始尝试其中一个小改变。记住,健康不是目的,而是一种生活方式。

【免责声明】
本视频内容仅供健康科普参考,不构成任何医疗诊断或治疗建议。如有健康问题,请咨询专业医生。
"""

        with open(f"{self.work_dir}/script.txt", "w", encoding="utf-8") as f:
            f.write(self.script)

        progress_callback(log("✅ 本地模板脚本已生成"))
        progress_callback(log("⚠️ 建议: 打开 script.txt 手动完善内容以获得更好效果"))
        return True

    def step4_generate_voice(self, progress_callback):
        """Step 4: AI 配音"""
        progress_callback(log("Step 4/6: 正在生成 AI 配音..."))

        script_path = f"{self.work_dir}/script.txt"
        with open(script_path, "r", encoding="utf-8") as f:
            script_text = f.read()

        # 清理脚本中的标记行
        lines = []
        for line in script_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if re.match(r"^[【\[]", line):
                continue
            if line.startswith("---"):
                continue
            lines.append(line)

        clean_text = "\n".join(lines)
        clean_path = f"{self.work_dir}/script_clean.txt"
        with open(clean_path, "w", encoding="utf-8") as f:
            f.write(clean_text)

        self.audio_path = f"{self.work_dir}/voice.mp3"
        progress_callback(log("   正在合成语音 (Edge TTS, 约1-3分钟)..."))

        ok, msg = run_cmd([
            "edge-tts", "--file", clean_path,
            "--voice", self.tts_voice, "--rate", "+0%",
            "--write-media", self.audio_path,
            "--write-subtitles", f"{self.work_dir}/voice_subtitles.vtt"
        ], timeout=120)

        if not ok or not os.path.exists(self.audio_path):
            progress_callback(log(f"❌ 配音生成失败: {msg}"))
            return False

        # 获取音频时长
        ok, dur_str = run_cmd([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            self.audio_path
        ])
        self.duration = float(dur_str.strip()) if ok else 0

        progress_callback(log(f"✅ 配音完成! 时长: {self.duration/60:.1f} 分钟"))
        return True

    def step5_generate_infographics(self, progress_callback):
        """Step 5: 根据脚本生成信息图"""
        progress_callback(log("Step 5/8: 正在生成信息图..."))

        if not self.script:
            progress_callback(log("❌ 无脚本内容，无法生成信息图"))
            return False

        visuals_dir = f"{self.work_dir}/visuals"
        Path(visuals_dir).mkdir(parents=True, exist_ok=True)

        # 从脚本生成信息图
        video_title = self.metadata.get("title", "健康科普") if self.metadata else "健康科普"
        infographics = generate_infographics_from_script(
            self.script,
            visuals_dir,
            theme_name="default",
            video_title=video_title,
        )

        if not infographics:
            progress_callback(log("⚠️ 信息图生成失败，使用备用纯色背景"))
            return False

        progress_callback(log(f"✅ 生成 {len(infographics)} 张信息图"))
        for path, dur in infographics:
            progress_callback(log(f"   📄 {path} ({dur/1000:.0f}秒)"))

        self.infographics = infographics
        return True

    def step6_generate_whiteboard(self, progress_callback):
        """Step 6: 将信息图转为白板手绘动画"""
        progress_callback(log("Step 6/8: 正在生成白板动画 (此步较慢)..."))

        if not hasattr(self, 'infographics') or not self.infographics:
            progress_callback(log("⚠️ 无信息图，跳过白板动画"))
            return False

        whiteboard_dir = f"{self.work_dir}/whiteboard_clips"
        Path(whiteboard_dir).mkdir(parents=True, exist_ok=True)

        self.whiteboard_clips = []

        # 检查白板动画脚本是否存在
        if not os.path.exists(WHITEBOARD_GEN_SCRIPT):
            progress_callback(log(f"❌ 白板动画脚本不存在: {WHITEBOARD_GEN_SCRIPT}"))
            return False

        total = len(self.infographics)
        for idx, (img_path, duration_ms) in enumerate(self.infographics):
            progress_callback(log(f"   处理信息图 {idx+1}/{total} ({duration_ms/1000:.0f}秒)..."))

            output_path = f"{whiteboard_dir}/clip_{idx+1:02d}.mp4"

            # 白板动画的时长：信息图展示时长 * 0.6（手绘部分），剩下 40% 作为停留展示
            draw_duration = int(duration_ms * 0.6)

            ok, msg = run_cmd([
                sys.executable, WHITEBOARD_GEN_SCRIPT,
                img_path,
                "--output-dir", whiteboard_dir,
                "--duration", str(draw_duration),
            ], timeout=300)

            if ok:
                # 查找生成的视频文件
                clip_files = sorted(Path(whiteboard_dir).glob("vid_*.mp4"))
                if clip_files:
                    latest = str(clip_files[-1])
                    # 重命名为有序名称
                    os.rename(latest, output_path)
                    self.whiteboard_clips.append(output_path)
                    progress_callback(log(f"   ✅ 白板动画片段 {idx+1} 完成"))

                    # 还需要一个停留片段（展示完整信息图）
                    hold_path = f"{whiteboard_dir}/hold_{idx+1:02d}.mp4"
                    hold_duration = max(duration_ms - draw_duration, 2000)  # 最少2秒
                    ok2, _ = run_cmd([
                        "ffmpeg", "-y",
                        "-loop", "1",
                        "-i", img_path,
                        "-c:v", "libx264",
                        "-t", f"{hold_duration/1000:.2f}",
                        "-pix_fmt", "yuv420p",
                        "-preset", "fast",
                        "-crf", "23",
                        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
                        hold_path
                    ], timeout=60)

                    if ok2:
                        self.whiteboard_clips.append(hold_path)
                else:
                    progress_callback(log(f"   ⚠️ 未找到白板动画输出文件"))
            else:
                progress_callback(log(f"   ⚠️ 白板动画失败，使用静态图替代"))

        if not self.whiteboard_clips:
            progress_callback(log("⚠️ 所有白板动画均失败"))
            return False

        progress_callback(log(f"✅ 完成 {len(self.whiteboard_clips)} 个白板动画片段"))
        return True

    def step7_compose_video(self, progress_callback):
        """Step 7: 合成最终视频（白板动画片段 + 配音 + 字幕）"""
        progress_callback(log("Step 7/8: 正在合成最终视频..."))

        output_video = f"{self.work_dir}/final_video.mp4"
        srt_path = f"{self.work_dir}/voice_subtitles.srt"
        vtt_path = f"{self.work_dir}/voice_subtitles.vtt"

        # VTT → SRT 转换
        if os.path.exists(vtt_path) and not os.path.exists(srt_path):
            with open(vtt_path, "r", encoding="utf-8") as f:
                vtt_content = f.read()
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(vtt_content.replace("WEBVTT\n\n", ""))

        # 如果有白板动画片段，使用 concat 拼接
        if self.whiteboard_clips:
            progress_callback(log("   拼接白板动画片段..."))

            # 创建 concat 文件列表
            concat_file = f"{self.work_dir}/concat_list.txt"
            with open(concat_file, "w") as f:
                for clip in self.whiteboard_clips:
                    f.write(f"file '{clip}'\n")

            # 先拼接所有视频片段（无音频）
            concat_video = f"{self.work_dir}/concat_video.mp4"
            ok, msg = run_cmd([
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_file,
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                concat_video
            ], timeout=300)

            if ok and os.path.exists(concat_video):
                # 叠加音频和字幕
                if os.path.exists(srt_path):
                    srt_escaped = srt_path.replace("\\", "/").replace(":", "\\:")
                    cmd = [
                        "ffmpeg", "-y",
                        "-i", concat_video,
                        "-i", self.audio_path,
                        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                        "-c:a", "aac", "-b:a", "192k",
                        "-vf",
                        f"subtitles={srt_escaped}:"
                        f"force_style='FontName=Noto Sans CJK SC,FontSize=28,"
                        f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2'",
                        "-shortest",
                        "-movflags", "+faststart",
                        output_video
                    ]
                else:
                    cmd = [
                        "ffmpeg", "-y",
                        "-i", concat_video,
                        "-i", self.audio_path,
                        "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "192k",
                        "-shortest",
                        "-movflags", "+faststart",
                        output_video
                    ]

                ok, msg = run_cmd(cmd, timeout=300)
                if not ok:
                    progress_callback(log("⚠️ 视频+音频合成失败，使用纯色背景方案"))
                    ok = False
            else:
                progress_callback(log("⚠️ 视频拼接失败，使用纯色背景方案"))
                ok = False

            if ok and os.path.exists(output_video):
                file_size = os.path.getsize(output_video) / 1024 / 1024
                self.final_video = output_video
                progress_callback(log(f"✅ 最终视频合成完成!"))
                progress_callback(log(f"   时长: {self.duration/60:.1f}分钟 | 大小: {file_size:.0f} MB"))
                return True

        # 降级方案：纯色背景 + 音频 + 字幕
        progress_callback(log("   使用降级方案：纯色背景..."))
        if os.path.exists(srt_path):
            srt_escaped = srt_path.replace("\\", "/").replace(":", "\\:")
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=c=0x131b2e:s=1920x1080:r=30",
                "-i", self.audio_path,
                "-shortest",
                "-vf",
                f"subtitles={srt_escaped}:"
                f"force_style='FontName=Noto Sans CJK SC,FontSize=28,"
                f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2'",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                output_video
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=c=0x131b2e:s=1920x1080:r=30",
                "-i", self.audio_path,
                "-shortest",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                output_video
            ]

        ok, msg = run_cmd(cmd, timeout=300)
        if ok and os.path.exists(output_video):
            file_size = os.path.getsize(output_video) / 1024 / 1024
            self.final_video = output_video
            progress_callback(log(f"✅ 视频合成完成（降级方案）"))
            progress_callback(log(f"   时长: {self.duration/60:.1f}分钟 | 大小: {file_size:.0f} MB"))
            return True

        progress_callback(log(f"❌ 视频合成失败: {msg}"))
        return False

    def run(self, progress_callback):
        """运行完整工作流"""
        start_time = time.time()

        yield progress_callback(log("🚀 工作流启动"))
        yield progress_callback(log(f"   输入: {self.video_url}"))

        if not self.step1_fetch_info(progress_callback):
            yield progress_callback(log("❌ 工作流终止: Step 1 失败"))
            return

        if not self.step2_transcribe(progress_callback):
            yield progress_callback(log("❌ 工作流终止: Step 2 失败"))
            return

        if not self.step3_generate_script(progress_callback):
            yield progress_callback(log("❌ 工作流终止: Step 3 失败"))
            return

        if not self.step4_generate_voice(progress_callback):
            yield progress_callback(log("❌ 工作流终止: Step 4 失败"))
            return

        if not self.step5_generate_infographics(progress_callback):
            yield progress_callback(log("⚠️ Step 5 信息图失败，继续流程"))

        if not self.step6_generate_whiteboard(progress_callback):
            yield progress_callback(log("⚠️ Step 6 白板动画失败，使用降级方案"))

        if not self.step7_compose_video(progress_callback):
            yield progress_callback(log("❌ 工作流终止: Step 7 失败"))
            return

        elapsed = int(time.time() - start_time)
        yield progress_callback(log(f"🎉 全部完成! 耗时 {elapsed//60}分{elapsed%60}秒"))
        yield progress_callback(log(f"📁 输出目录: {self.work_dir}"))


# ============ Gradio Web 界面 ============

def run_workflow_ui(video_url, tts_voice, proxy_url=""):
    """Gradio 接口: 运行工作流并返回日志和结果"""
    if not video_url or not video_url.strip():
        yield ["请输入 YouTube 视频链接", None, None, None, None]
        return

    video_url = video_url.strip()
    proxy_url = proxy_url.strip()
    logs = []
    script_content = ""
    video_path = None
    audio_path = None
    video_id = extract_video_id(video_url)

    def emit(msg):
        logs.append(msg)
        return "\n".join(logs)

    workflow = VideoWorkflow(video_url, tts_voice)
    if proxy_url:
        workflow.proxy = proxy_url

    for _ in workflow.run(emit):
        script_path = f"{workflow.work_dir}/script.txt" if workflow.work_dir else None
        if script_path and os.path.exists(script_path):
            with open(script_path, "r", encoding="utf-8") as f:
                script_content = f.read()[:2000]
        if workflow.final_video and os.path.exists(workflow.final_video):
            video_path = workflow.final_video
        if workflow.audio_path and os.path.exists(workflow.audio_path):
            audio_path = workflow.audio_path

        yield ["\n".join(logs), script_content, video_path, audio_path, None]

    script_path = f"{workflow.work_dir}/script.txt" if workflow.work_dir else None
    if script_path and os.path.exists(script_path):
        with open(script_path, "r", encoding="utf-8") as f:
            script_content = f.read()
    if workflow.final_video and os.path.exists(workflow.final_video):
        video_path = workflow.final_video
    if workflow.audio_path and os.path.exists(workflow.audio_path):
        audio_path = workflow.audio_path

    yield ["\n".join(logs), script_content, video_path, audio_path, None]


# ============ UI 构建 ============

CSS = """
.gradio-container { max-width: 1200px !important; margin: 0 auto; }
h1 { text-align: center; background: linear-gradient(135deg, #667eea, #764ba2); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.status-box { border-left: 4px solid #667eea; }
"""

with gr.Blocks(title="泛健康视频自动化工作流") as demo:
    gr.HTML("""
    <div style="text-align:center; padding: 20px 0 10px 0;">
        <h1 style="font-size: 2em; margin:0;">🎬 泛健康科普视频自动化工作流</h1>
        <p style="color: #666; margin-top:8px;">粘贴 YouTube 链接 → 自动生成 5-7 分钟中文科普视频</p>
    </div>
    """)

    with gr.Row():
        with gr.Column(scale=1, min_width=350):
            gr.Markdown("### ⚙️ 输入设置")

            video_url = gr.Textbox(
                label="YouTube 视频链接",
                placeholder="https://www.youtube.com/watch?v=...",
                lines=2,
            )

            tts_voice = gr.Dropdown(
                label="配音声音",
                choices=[
                    ("🎤 女声 - Xiaoxiao (推荐)", "zh-CN-XiaoxiaoNeural"),
                    ("🎤 男声 - Yunxi", "zh-CN-YunxiNeural"),
                    ("🎤 新闻 - Yunyang", "zh-CN-YunyangNeural"),
                ],
                value="zh-CN-XiaoxiaoNeural",
                type="value",
            )

            proxy_url = gr.Textbox(
                label="🌐 代理地址（可选，国内访问YouTube需要）",
                placeholder="http://127.0.0.1:7890 或 socks5://127.0.0.1:1080",
                lines=1,
            )

            run_btn = gr.Button("🚀 开始生成", variant="primary", size="lg")

            gr.Markdown("""
            ---
            ### 💡 说明
            1. 粘贴 YouTube 健康视频链接
            2. 选择配音声音
            3. 如有代理工具，填代理地址（国内访问YouTube必备）
            4. 点击"开始生成"
            5. 等待处理完成（约 10-20 分钟）

            ### 🔑 API Key（可选）
            设置环境变量 `LLM_API_KEY` 可使用 DeepSeek AI 生成高质量脚本。
            未设置时使用本地模板。
            """)

        with gr.Column(scale=2):
            gr.Markdown("### 📋 运行日志")
            log_output = gr.Textbox(
                label="",
                lines=12,
                interactive=False,
                elem_classes="status-box",
            )

            with gr.Tabs():
                with gr.TabItem("📝 脚本预览"):
                    script_output = gr.Textbox(
                        label="",
                        lines=15,
                        interactive=False,
                    )
                    download_script_btn = gr.File(
                        label="下载脚本文件",
                        visible=False,
                    )

                with gr.TabItem("🎬 生成结果"):
                    with gr.Row():
                        video_output = gr.Video(
                            label="最终视频",
                            width=640,
                        )
                    download_audio_btn = gr.File(
                        label="下载音频文件",
                        visible=False,
                    )

            # 状态指示
            status_text = gr.Markdown("🟢 就绪，等待输入...")

    # 事件绑定
    def on_submit(url, voice, proxy):
        if not url or not url.strip():
            yield [None, "请输入 YouTube 视频链接", None, None, None, "🔴 错误: 链接为空"]
            return

        vid = extract_video_id(url.strip())
        if not vid:
            yield [None, "❌ 无效的 YouTube 链接，请检查格式", None, None, None, "🔴 错误: 链接格式不正确"]
            return

        if proxy and proxy.strip():
            yield [None, f"🟡 工作流启动中（使用代理）...", None, None, None, "🟡 处理中..."]
        else:
            yield [None, "🟡 工作流启动中（无代理）...", None, None, None, "🟡 处理中..."]

        for logs, script, video, audio, _ in run_workflow_ui(url.strip(), voice, proxy.strip() if proxy else ""):
            is_done = "全部完成" in logs
            status = "🟢 完成! ✅" if is_done else "🟡 处理中..."
            script_file = None
            audio_file = None

            if script:
                script_file_path = f"{TEMP_BASE}/script_preview.txt"
                with open(script_file_path, "w", encoding="utf-8") as f:
                    f.write(script)
                script_file = script_file_path

            if audio and os.path.exists(audio):
                audio_file = audio

            yield [video, logs, script, script_file, audio_file, status]

    run_btn.click(
        fn=on_submit,
        inputs=[video_url, tts_voice, proxy_url],
        outputs=[
            video_output,
            log_output,
            script_output,
            download_script_btn,
            download_audio_btn,
            status_text,
        ],
    )

    # 清空状态
    def clear_all():
        return [None, "", None, None, None, "🟢 就绪，等待输入..."]

    demo.load(clear_all, outputs=[
        video_output, log_output, script_output,
        download_script_btn, download_audio_btn, status_text
    ])


# ============ 启动 ============

if __name__ == "__main__":
    print("=" * 60)
    print("泛健康科普视频自动化工作流 - Web 服务")
    print("=" * 60)
    print()
    print("本地访问: http://localhost:7860")
    print("远程访问: 使用 --share 参数生成公共链接")
    print()
    print("环境变量:")
    print("  LLM_API_KEY - DeepSeek API Key (可选)")
    print()

    # 检查依赖
    missing = []
    try:
        import yt_dlp
    except:
        missing.append("yt-dlp")
    try:
        import whisper
    except:
        missing.append("openai-whisper")
    try:
        import edge_tts
    except:
        missing.append("edge-tts")

    if missing:
        print(f"⚠️ 缺少依赖: {', '.join(missing)}")
        print(f"请运行: pip install {' '.join(missing)}")
        print()

    # 检查 ffmpeg
    ok, _ = run_cmd(["ffmpeg", "-version"])
    if not ok:
        print("⚠️ FFmpeg 未安装，请先安装 FFmpeg")
        print()

    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        theme=gr.themes.Soft(),
        css=CSS,
    )