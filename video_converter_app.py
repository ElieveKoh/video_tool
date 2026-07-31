#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import streamlit as st
import streamlit.components.v1 as components
import subprocess
import os
import threading
import time
import json
import re
import sys
import concurrent.futures
import functools
import hashlib
import urllib.request
import urllib.error
import urllib.parse
from html import escape as _esc
from datetime import datetime


def _file_check_key(path):
    """파일 체크박스의 안정적인 위젯 key.

    key에 토글 카운터를 섞으면 '전체 선택' 한 번에 모든 체크박스가 재생성되어
    파일이 많을 때 매우 느려진다. key는 파일 경로로 고정하고, 일괄 토글은
    session_state에 직접 대입해 처리한다.
    """
    return "file_check_" + hashlib.md5(path.encode('utf-8')).hexdigest()[:16]


def _yt_check_key(url):
    """유튜브 큐 체크박스의 안정적인 위젯 key(URL 기준).

    인덱스 기반 key는 항목 삭제 시 나머지 항목의 체크 상태가 밀리는 버그를 만든다.
    """
    return "yt_queue_check_" + hashlib.md5(url.encode('utf-8')).hexdigest()[:16]


# 유튜브 제목 조회 결과 캐시(성공만 저장). 같은 URL 재조회 시 네트워크 호출 없이 즉시 반환.
_YT_TITLE_CACHE = {}
_YT_TITLE_CACHE_LOCK = threading.Lock()

# ffprobe 결과 캐시. 키는 (경로, mtime, size)이므로 파일이 변경되면 자동 무효화된다.
_VIDEO_INFO_CACHE = {}


def _fetch_title_via_oembed(url):
    """YouTube oEmbed 엔드포인트로 제목/업로더만 가져온다.

    yt-dlp는 제목 한 줄을 위해 extractor 전체(webpage + player response + 포맷 목록)를
    실행하기 때문에 5~20초가 걸린다. oEmbed는 단순 JSON GET이라 보통 0.5초 이내다.
    비공개/연령제한 영상은 실패하므로 호출자가 yt-dlp로 폴백해야 한다.
    """
    endpoint = "https://www.youtube.com/oembed?" + urllib.parse.urlencode(
        {'url': url, 'format': 'json'}
    )
    req = urllib.request.Request(endpoint, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    title = (data.get('title') or '').strip()
    if not title:
        return None
    return {
        'title': title,
        'duration': 0,
        'uploader': (data.get('author_name') or 'Unknown').strip(),
        'view_count': 0,
        'upload_date': 'Unknown',
    }


def _fetch_video_title(yt_dlp_path, url):
    """제목만 빠르게 추출. 성공 결과는 프로세스 캐시에 저장한다.

    1차: YouTube oEmbed(HTTP GET, ~0.5s)
    2차: yt-dlp `--print title` (oEmbed가 못 다루는 URL/비공개 영상용)
    실패(None)는 캐시하지 않아 일시적 오류가 고정되지 않는다.
    """
    with _YT_TITLE_CACHE_LOCK:
        if url in _YT_TITLE_CACHE:
            return _YT_TITLE_CACHE[url]

    info = None
    if 'youtube.com' in url or 'youtu.be' in url:
        try:
            info = _fetch_title_via_oembed(url)
        except Exception as e:
            print(f"oEmbed 실패, yt-dlp로 폴백: {e}")

    if info is None:
        try:
            cmd = [
                yt_dlp_path, '--print', 'title', '--no-warnings',
                '--no-playlist', '--skip-download',
                '--socket-timeout', '10', url,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if result.returncode == 0:
                title = result.stdout.strip().split('\n')[0].strip()
                info = {
                    'title': title if title else 'Unknown',
                    'duration': 0,
                    'uploader': 'Unknown',
                    'view_count': 0,
                    'upload_date': 'Unknown',
                }
            else:
                print(f"yt-dlp error (returncode {result.returncode}): {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            print(f"yt-dlp timeout (20s) fetching: {url}")
        except Exception as e:
            print(f"비디오 제목 추출 오류: {e}")

    if info is not None:
        with _YT_TITLE_CACHE_LOCK:
            _YT_TITLE_CACHE[url] = info
    return info


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_yt_dlp_version(yt_dlp_path):
    """yt-dlp 버전 문자열을 캐싱(1시간).

    유튜브 탭이 그려질 때마다 `yt-dlp --version` subprocess를 실행하던 것을 방지한다.
    yt-dlp 업데이트 후에는 `_cached_yt_dlp_version.clear()`로 무효화한다.
    """
    try:
        result = subprocess.run(
            [yt_dlp_path, '--version'], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


@functools.lru_cache(maxsize=8)
def _detect_hw_accel(ffmpeg_path):
    """macOS VideoToolbox 인코더 지원 여부를 프로세스 단위로 1회만 감지.

    ffmpeg -encoders 호출은 비용이 커서 매 rerun마다 실행하면 앱이 느려진다.
    lru_cache로 감싸 프로세스 생명주기 동안 한 번만 실행되도록 한다.
    """
    if sys.platform != 'darwin':
        return False
    try:
        result = subprocess.run(
            [ffmpeg_path, '-hide_banner', '-encoders'],
            capture_output=True, text=True, timeout=5
        )
        return 'h264_videotoolbox' in result.stdout
    except Exception:
        return False


class VideoConverterCore:
    """비디오 변환 핵심 기능 클래스"""
    
    # 코덱별 설정 상수
    CODEC_CONFIG = {
        "h264": {
            "encoder": "libx264",
            "hw_encoder": "h264_videotoolbox",  # macOS 하드웨어 가속
            "param_name": "preset",
            "bitrates": {"4k": 20, "hd": 12, "sd": 5},
            "bitrate_bonus": {"4k": 5, "hd": 3, "sd": 2}
        },
        "h265": {
            "encoder": "libx265",
            "hw_encoder": "hevc_videotoolbox",  # macOS 하드웨어 가속
            "param_name": "preset",
            "bitrates": {"4k": 15, "hd": 8, "sd": 3},
            "bitrate_bonus": {"4k": 3, "hd": 2, "sd": 1}
        },
        "vp9": {
            "encoder": "libvpx-vp9",
            "hw_encoder": None,  # VP9는 하드웨어 가속 미지원
            "param_name": "speed",
            "bitrates": {"4k": 16, "hd": 8, "sd": 4},
            "bitrate_bonus": {"4k": 4, "hd": 2, "sd": 1}
        },
        "av1": {
            "encoder": "libaom-av1",
            "hw_encoder": None,  # AV1은 하드웨어 가속 미지원
            "param_name": "cpu-used",
            "bitrates": {"4k": 12, "hd": 6, "sd": 3},
            "bitrate_bonus": {"4k": 3, "hd": 2, "sd": 1}
        }
    }
    
    # 해상도별 설정
    RESOLUTION_CONFIG = {
        "4k": {"width": 3840, "height": 2160},
        "1440p": {"width": 2560, "height": 1440},
        "1080p": {"width": 1920, "height": 1080},
        "720p": {"width": 1280, "height": 720},
        "480p": {"width": 854, "height": 480}
    }
    
    # 품질 프리셋 매핑
    QUALITY_PRESETS = {
        "fast": {"h264": "fast", "h265": "fast", "vp9": 4, "av1": 8},
        "balanced": {"h264": "medium", "h265": "medium", "vp9": 2, "av1": 6},
        "high": {"h264": "slow", "h265": "slow", "vp9": 1, "av1": 4}
    }
    
    def __init__(self):
        self.current_process = None
        self.conversion_stopped = False
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.hw_accel_available = self._check_hw_accel()

    def _check_hw_accel(self):
        """하드웨어 가속 사용 가능 여부 확인 (macOS VideoToolbox)

        결과는 프로세스 단위로 캐싱된다(ffmpeg 실행은 최초 1회만).
        """
        return _detect_hw_accel(self._get_ffmpeg_path())
    
    def _get_ffmpeg_path(self):
        """FFmpeg 경로 찾기 (로컬 bin 우선)"""
        local_ffmpeg = os.path.join(self.base_dir, 'bin', 'ffmpeg')
        if os.path.exists(local_ffmpeg):
            return local_ffmpeg
        return 'ffmpeg'  # 시스템 PATH에서 찾기
    
    def _get_ffprobe_path(self):
        """FFprobe 경로 찾기 (로컬 bin 우선)"""
        local_ffprobe = os.path.join(self.base_dir, 'bin', 'ffprobe')
        if os.path.exists(local_ffprobe):
            return local_ffprobe
        return 'ffprobe'  # 시스템 PATH에서 찾기
        
    def get_video_info(self, file_path):
        """FFprobe를 사용해서 비디오 정보 추출.

        같은 파일에 대해 (경로, mtime, size)를 키로 결과를 메모이즈한다.
        메모이즈 이전에는 파일 1건 변환에 ffprobe 프로세스가 3번 떴다
        (호출자 → convert_video → get_codec_options). 파일이 바뀌면
        mtime/size가 달라져 자동으로 무효화된다.
        """
        cache_key = None
        try:
            stat = os.stat(file_path)
            cache_key = (file_path, stat.st_mtime, stat.st_size)
            if cache_key in _VIDEO_INFO_CACHE:
                return _VIDEO_INFO_CACHE[cache_key]
        except OSError:
            # URL 등 stat이 불가능한 입력은 캐시하지 않는다.
            pass

        try:
            # 로컬 바이너리 우선 사용
            ffprobe_path = self._get_ffprobe_path()

            cmd = [
                ffprobe_path, '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height,codec_name,bit_rate',
                '-show_entries', 'format=duration,bit_rate',
                '-of', 'json', file_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                
                video_stream = data.get('streams', [{}])[0]
                format_info = data.get('format', {})

                info = {
                    'width': int(video_stream.get('width', 0)),
                    'height': int(video_stream.get('height', 0)),
                    'codec': video_stream.get('codec_name', 'unknown'),
                    'duration': float(format_info.get('duration', 0)),
                    'bitrate': int(format_info.get('bit_rate', 0))
                }
                if cache_key is not None:
                    if len(_VIDEO_INFO_CACHE) > 512:
                        _VIDEO_INFO_CACHE.clear()
                    _VIDEO_INFO_CACHE[cache_key] = info
                return info
        except Exception as e:
            print(f"비디오 정보 추출 오류: {e}")
            return None
    
    def get_codec_options(self, file_path, target_codec, target_resolution, quality_preset, target_fps="original", scan_type="progressive", custom_video_bitrate=None, custom_audio_bitrate=None):
        """코덱별 변환 옵션 생성 (프레임레이트 및 스캔 타입 지원, 커스텀 비트레이트 옵션)"""
        # 비디오 정보 가져오기
        video_info = self.get_video_info(file_path)
        if not video_info:
            print(f"❌ 비디오 정보를 가져올 수 없습니다: {file_path}")
            video_info = {
                'width': 1920, 'height': 1080, 'codec': 'unknown',
                'duration': 0, 'bitrate': 0
            }

        # 원본 비트레이트를 Mbps로 변환
        orig_mbps = video_info['bitrate'] // 1000000 if video_info['bitrate'] > 0 else 0

        # 비디오 필터 리스트
        vf_filters = []

        # 타겟 해상도 설정
        if target_resolution in self.RESOLUTION_CONFIG:
            res_config = self.RESOLUTION_CONFIG[target_resolution]
            target_height = res_config["height"]
            vf_filters.append(f"scale={res_config['width']}:{res_config['height']}")
        else:
            target_height = video_info['height']

        # 디인터레이싱 필터 추가 (progressive 선택 시)
        if scan_type == "progressive":
            vf_filters.append("yadif=mode=send_frame:parity=auto:deint=all")

        # 해상도 카테고리 판단
        if target_height >= 2160:
            res_category = "4k"
        elif target_height >= 720:
            res_category = "hd"
        else:
            res_category = "sd"

        # 코덱 설정 가져오기
        if target_codec not in self.CODEC_CONFIG:
            return None

        codec_config = self.CODEC_CONFIG[target_codec]

        # 비트레이트 계산 (커스텀 비트레이트가 있으면 사용)
        if custom_video_bitrate:
            target_br = custom_video_bitrate
            max_br = int(target_br * 1.25)  # 커스텀 비트레이트의 125%를 maxrate로 설정
        else:
            default_bitrate = codec_config["bitrates"][res_category]
            target_br = max(orig_mbps, default_bitrate) if orig_mbps > 0 else default_bitrate
            max_br = target_br + codec_config["bitrate_bonus"][res_category]

        # 품질 프리셋 가져오기 (커스텀/crf인 경우 balanced 사용)
        if quality_preset in ("custom", "crf"):
            preset_value = self.QUALITY_PRESETS["balanced"][target_codec]
        else:
            preset_value = self.QUALITY_PRESETS[quality_preset][target_codec]

        # 하드웨어 가속 사용 여부 결정 (fast/balanced 프리셋이고 hw_encoder가 있으면 사용)
        use_hw_accel = (
            self.hw_accel_available and
            codec_config.get("hw_encoder") and
            quality_preset in ("fast", "balanced")
        )

        # CRF 모드 처리
        if quality_preset == "crf":
            crf_values = {"h264": 23, "h265": 28, "vp9": 31, "av1": 30}
            crf_val = custom_video_bitrate if custom_video_bitrate else crf_values.get(target_codec, 23)
            if target_codec in ("h264", "h265"):
                cmd_args = [
                    "-c:v", codec_config["encoder"],
                    "-crf", str(crf_val),
                    f"-{codec_config['param_name']}", str(preset_value)
                ]
            else:
                cmd_args = [
                    "-c:v", codec_config["encoder"],
                    "-crf", str(crf_val),
                    f"-{codec_config['param_name']}", str(preset_value),
                    "-b:v", "0"
                ]
        elif use_hw_accel:
            # 하드웨어 가속 사용 (VideoToolbox - 매우 빠름!)
            cmd_args = [
                "-c:v", codec_config["hw_encoder"],
                "-b:v", f"{target_br}M",
                "-maxrate", f"{max_br}M",
                "-bufsize", f"{max_br * 2}M"
            ]
            print(f"🚀 하드웨어 가속 사용: {codec_config['hw_encoder']}")
        else:
            # 소프트웨어 인코딩
            cmd_args = [
                "-c:v", codec_config["encoder"],
                "-b:v", f"{target_br}M",
                "-maxrate", f"{max_br}M",
                "-bufsize", f"{max_br * 2}M",
                f"-{codec_config['param_name']}", str(preset_value)
            ]

        # 멀티스레딩 (CPU 코어 수 활용)
        import multiprocessing
        cmd_args.extend(["-threads", str(max(1, multiprocessing.cpu_count()))])

        # 프레임레이트 설정
        if target_fps != "original":
            cmd_args.extend(["-r", target_fps])

        # 비디오 필터 추가
        if vf_filters:
            cmd_args.extend(["-vf", ",".join(vf_filters)])

        return cmd_args
    
    def convert_video(self, input_file, output_file, target_codec, target_resolution, quality_preset, target_fps="original", scan_type="progressive", custom_video_bitrate=None, custom_audio_bitrate=None, progress_callback=None):
        """비디오 변환 실행"""
        try:
            self.conversion_stopped = False

            # 코덱 옵션 생성
            codec_options = self.get_codec_options(input_file, target_codec, target_resolution, quality_preset, target_fps, scan_type, custom_video_bitrate, custom_audio_bitrate)
            if not codec_options:
                return False, f"코덱 옵션 생성 실패 - 파일: {input_file}, 코덱: {target_codec}"

            print(f"🎯 FFmpeg 명령어 옵션: {' '.join(codec_options)}")

            # 출력 디렉토리 생성
            os.makedirs(os.path.dirname(output_file), exist_ok=True)

            # 오디오 코덱 설정 (커스텀 비트레이트가 있으면 재인코딩, 없으면 복사)
            if custom_audio_bitrate:
                audio_options = ['-c:a', 'aac', '-b:a', f'{custom_audio_bitrate}k']
            else:
                audio_options = ['-c:a', 'copy']

            # FFmpeg 명령어 구성 (로컬 바이너리 사용)
            ffmpeg_path = self._get_ffmpeg_path()
            cmd = [ffmpeg_path, '-i', input_file] + codec_options + audio_options + ['-movflags', '+faststart', '-map', '0', output_file, '-y', '-progress', 'pipe:1']

            # 비디오 길이 추출 (진행률 계산용)
            video_info = self.get_video_info(input_file)
            total_duration = video_info['duration'] if video_info else 0

            # FFmpeg 프로세스 시작 (stderr도 STDOUT으로 리다이렉트하여 버퍼 문제 방지)
            self.current_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1
            )

            # 진행률 모니터링
            last_progress_time = time.time()
            stderr_lines = []

            while True:
                if self.conversion_stopped:
                    self.current_process.terminate()
                    return False, "사용자에 의해 중단됨"

                output = self.current_process.stdout.readline()
                if output == '' and self.current_process.poll() is not None:
                    break

                # stderr 출력 저장 (디버깅용)
                if output.strip():
                    stderr_lines.append(output.strip())
                    # 최근 50줄만 유지
                    if len(stderr_lines) > 50:
                        stderr_lines.pop(0)

                if output.strip().startswith('out_time_ms='):
                    try:
                        time_ms = int(output.strip().split('=')[1])
                        time_seconds = time_ms / 1000000

                        if total_duration > 0 and progress_callback:
                            progress = min(time_seconds / total_duration, 1.0)
                            progress_callback(progress, time_seconds, total_duration)
                            last_progress_time = time.time()
                    except Exception as e:
                        print(f"진행률 파싱 중 오류: {e}")

                # 30초 이상 진행률 업데이트가 없으면 경고
                if time.time() - last_progress_time > 30:
                    print(f"⚠️ 30초 동안 진행률 업데이트 없음")
                    last_progress_time = time.time()

            # 프로세스 완료 확인
            return_code = self.current_process.wait()

            if return_code == 0:
                return True, "변환 완료"
            else:
                # 마지막 오류 메시지 출력
                error_msg = '\n'.join(stderr_lines[-10:]) if stderr_lines else "알 수 없는 오류"
                return False, f"FFmpeg 오류: {error_msg}"

        except Exception as e:
            return False, f"변환 중 오류 발생: {str(e)}"
    
    def stop_conversion(self):
        """변환 중단"""
        self.conversion_stopped = True
        if self.current_process and self.current_process.poll() is None:
            try:
                self.current_process.terminate()
                self.current_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.current_process.kill()

    def probe_duration(self, input_source):
        """로컬 파일 또는 URL의 길이(초)를 반환. 알 수 없으면 0.

        ffprobe는 http(s) 입력도 처리하므로 URL도 시도한다. 원격 probe는
        느릴 수 있어 타임아웃을 짧게 두고, 실패하면 0을 돌려 호출자가
        불확정 진행 표시로 폴백하게 한다.
        """
        if not input_source.startswith(('http://', 'https://')):
            info = self.get_video_info(input_source)
            return info['duration'] if info else 0
        try:
            cmd = [
                self._get_ffprobe_path(), '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                input_source,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if result.returncode == 0:
                return float(result.stdout.strip() or 0)
        except Exception as e:
            print(f"원격 duration 조회 실패(불확정 진행률로 진행): {e}")
        return 0

    def strip_audio(self, input_source, output_file, progress_callback=None, total_duration=None):
        """비디오에서 오디오 트랙 제거 (비디오는 재인코딩 없이 복사)

        input_source: 로컬 파일 경로 또는 URL (ffmpeg가 직접 처리)
        total_duration: 미리 구한 길이(초). None이면 여기서 조회한다.
        progress_callback(progress, processed_seconds, speed):
            길이를 아는 경우 progress는 0~1, 모르면 None이 넘어간다.
            이전 구현은 URL일 때 duration을 0으로 두고 `total_duration > 0`
            가드에 걸려 콜백을 한 번도 호출하지 않았다(진행률이 안 보이던 원인).
        """
        try:
            self.conversion_stopped = False
            ffmpeg_path = self._get_ffmpeg_path()

            if total_duration is None:
                total_duration = self.probe_duration(input_source)

            cmd = [
                ffmpeg_path,
                '-i', input_source,
                '-c:v', 'copy',
                '-an',
                '-y',
                '-progress', 'pipe:1',
                output_file
            ]

            out_dir = os.path.dirname(output_file)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1
            )
            self.current_process = process

            speed = ''
            tail = []
            while True:
                if self.conversion_stopped:
                    process.terminate()
                    return False, "사용자에 의해 중단됨"

                output = process.stdout.readline()
                if output == '' and process.poll() is not None:
                    break

                line = output.strip()
                if not line:
                    continue
                tail.append(line)
                if len(tail) > 30:
                    tail.pop(0)

                if line.startswith('speed='):
                    speed = line.split('=', 1)[1].strip()
                elif line.startswith('out_time_ms=') and progress_callback:
                    try:
                        processed = int(line.split('=')[1]) / 1000000
                        progress = min(processed / total_duration, 1.0) if total_duration > 0 else None
                        progress_callback(progress, processed, speed)
                    except Exception:
                        pass

            return_code = process.wait()
            if return_code == 0 and os.path.exists(output_file):
                return True, "Audio removed successfully"
            return False, "FFmpeg 오류: " + ('\n'.join(tail[-6:]) or "알 수 없는 오류")
        except Exception as e:
            return False, str(e)

class YouTubeDownloader:
    """유튜브 다운로더 클래스"""
    
    def __init__(self):
        self.download_process = None
        self.download_stopped = False
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
    
    def _get_yt_dlp_path(self):
        """yt-dlp 경로 찾기 (로컬 bin 우선)"""
        local_yt_dlp = os.path.join(self.base_dir, 'bin', 'yt-dlp')
        if os.path.exists(local_yt_dlp):
            return local_yt_dlp
        return 'yt-dlp'  # 시스템 PATH에서 찾기
    
    def check_yt_dlp(self):
        """yt-dlp 설치 확인"""
        try:
            yt_dlp_path = self._get_yt_dlp_path()
            result = subprocess.run([yt_dlp_path, '--version'], capture_output=True, text=True, timeout=10)
            return result.returncode == 0
        except Exception as e:
            print(f"yt-dlp 설치 확인 오류: {e}")
            return False

    def get_yt_dlp_version(self):
        """yt-dlp 현재 버전 반환"""
        try:
            yt_dlp_path = self._get_yt_dlp_path()
            result = subprocess.run([yt_dlp_path, '--version'], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def update_yt_dlp(self):
        """yt-dlp 업데이트 (--update 시도 후 실패하면 pip upgrade)"""
        try:
            yt_dlp_path = self._get_yt_dlp_path()

            # 1차: 내장 --update 시도 (standalone 바이너리용)
            result = subprocess.run(
                [yt_dlp_path, '--update'],
                capture_output=True, text=True, timeout=120
            )
            output = (result.stdout + '\n' + result.stderr).strip()

            if result.returncode == 0 and 'ERROR' not in output:
                new_version = self.get_yt_dlp_version()
                return True, output, new_version

            # 2차: pip으로 설치된 경우 pip upgrade 시도
            if 'pip' in output or 'PyPi' in output:
                import sys
                pip_result = subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', '--upgrade', 'yt-dlp'],
                    capture_output=True, text=True, timeout=120
                )
                pip_output = (pip_result.stdout + '\n' + pip_result.stderr).strip()
                if pip_result.returncode == 0:
                    new_version = self.get_yt_dlp_version()
                    return True, pip_output, new_version
                else:
                    return False, pip_output, None

            return False, output, None
        except Exception as e:
            return False, str(e), None
    
    def get_video_title_fast(self, url):
        """유튜브 비디오 제목만 빠르게 추출 (큐 추가용).

        성공 결과는 프로세스 캐시에 저장되어 같은 URL 재조회 시 즉시 반환된다.
        """
        return _fetch_video_title(self._get_yt_dlp_path(), url)

    def get_video_info(self, url):
        """유튜브 비디오 정보 추출 (전체 정보)"""
        try:
            yt_dlp_path = self._get_yt_dlp_path()
            cmd = [yt_dlp_path, '--dump-json', '--no-download', url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode == 0:
                video_info = json.loads(result.stdout.split('\n')[0])  # 첫 번째 비디오만
                return {
                    'title': video_info.get('title', 'Unknown'),
                    'duration': video_info.get('duration', 0),
                    'uploader': video_info.get('uploader', 'Unknown'),
                    'view_count': video_info.get('view_count', 0),
                    'upload_date': video_info.get('upload_date', 'Unknown')
                }
        except Exception as e:
            print(f"비디오 정보 추출 오류: {e}")
            return None
    
    def download_video(self, url, output_path, progress_callback=None):
        """유튜브 비디오 다운로드"""
        try:
            self.download_stopped = False

            # 다운로드 전 폴더의 기존 파일 목록 저장
            existing_files = set(os.listdir(output_path)) if os.path.exists(output_path) else set()

            # 최고 품질 비디오 + 오디오 다운로드 (해상도 우선)
            yt_dlp_path = self._get_yt_dlp_path()
            cmd = [
                yt_dlp_path,
                '-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',  # 최고 해상도 우선
                '--merge-output-format', 'mp4',  # 최종 출력은 MP4로 병합
                '--no-playlist',  # 플레이리스트의 다른 영상 다운로드 방지
                '-o', os.path.join(output_path, '%(title)s.%(ext)s'),
                '--newline',  # 진행률 표시를 위한 개행
                url
            ]

            self.download_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1
            )

            downloaded_file = None

            # 진행률 모니터링
            while True:
                if self.download_stopped:
                    self.download_process.terminate()
                    return False, "사용자에 의해 중단됨", None

                output = self.download_process.stdout.readline()
                if output == '' and self.download_process.poll() is not None:
                    break

                # 디버깅을 위한 출력
                print(f"[yt-dlp] {output.strip()}")

                # 진행률 파싱
                if '[download]' in output and '%' in output:
                    try:
                        # 진행률 추출 (예: [download]  45.2% of 50.2MiB at 1.2MiB/s ETA 00:30)
                        progress_match = re.search(r'(\d+\.?\d*)%', output)
                        if progress_match and progress_callback:
                            progress = float(progress_match.group(1)) / 100
                            progress_callback(progress)
                    except Exception as e:
                        print(f"YouTube 진행률 파싱 오류: {e}")

                # 다운로드 완료된 파일명 추출 (여러 패턴 지원)
                if '[Merger]' in output and 'Merging formats into' in output:
                    try:
                        # [Merger] Merging formats into "filename.mp4"
                        file_match = re.search(r'Merging formats into "([^"]+)"', output)
                        if file_match:
                            downloaded_file = file_match.group(1).strip()
                            print(f"✅ 병합된 파일: {downloaded_file}")
                    except Exception as e:
                        print(f"병합 파일명 추출 오류: {e}")

                elif 'has already been downloaded' in output or 'Destination:' in output:
                    try:
                        # 파일 경로 추출
                        if 'has already been downloaded' in output:
                            # [download] /path/to/file.mp4 has already been downloaded
                            file_match = re.search(r'\[download\] (.+?) has already been downloaded', output)
                            if file_match:
                                downloaded_file = file_match.group(1).strip()
                                print(f"✅ 이미 다운로드된 파일: {downloaded_file}")
                        elif 'Destination:' in output:
                            file_match = re.search(r'Destination: (.+)', output)
                            if file_match:
                                downloaded_file = file_match.group(1).strip()
                                print(f"✅ 다운로드된 파일: {downloaded_file}")
                    except Exception as e:
                        print(f"다운로드 파일명 추출 오류: {e}")

            return_code = self.download_process.wait()

            if return_code == 0:
                # 다운로드된 파일 찾기 (파일명을 못 찾은 경우)
                if not downloaded_file or not os.path.exists(downloaded_file):
                    print("⚠️  파일 경로를 찾을 수 없어서 폴더에서 검색 중...")
                    # 새로 생성된 파일 찾기
                    current_files = set(os.listdir(output_path))
                    new_files = current_files - existing_files

                    # .mp4 파일만 필터링
                    mp4_files = [f for f in new_files if f.endswith('.mp4')]

                    if mp4_files:
                        # 가장 최근에 수정된 파일 선택
                        mp4_files_with_time = [(f, os.path.getmtime(os.path.join(output_path, f))) for f in mp4_files]
                        mp4_files_with_time.sort(key=lambda x: x[1], reverse=True)
                        downloaded_file = os.path.join(output_path, mp4_files_with_time[0][0])
                        print(f"✅ 폴더에서 찾은 파일: {downloaded_file}")

                if downloaded_file and os.path.exists(downloaded_file):
                    return True, "다운로드 완료", downloaded_file
                else:
                    return False, "다운로드 파일을 찾을 수 없음", None
            else:
                return False, "다운로드 실패", None

        except Exception as e:
            return False, f"다운로드 중 오류: {str(e)}", None
    
    def stop_download(self):
        """다운로드 중단"""
        self.download_stopped = True
        if self.download_process and self.download_process.poll() is None:
            try:
                self.download_process.terminate()
                self.download_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.download_process.kill()

class BackgroundJob:
    """장시간 작업(변환/다운로드/무음화)의 진행 상태를 담는 객체.

    설계 규칙: 워커 스레드는 이 객체만 갱신하고 Streamlit API(st.*)는 절대 호출하지
    않는다. st.* 는 ScriptRunContext가 있는 스크립트 실행 스레드에서만 유효하다.
    UI는 run_every 프래그먼트로 이 상태를 폴링해 그린다.

    이 구조로 바꾼 이유: 이전에는 변환 루프가 스크립트 실행 자체를 점유해서
    작업 중에는 화면이 먹통이 됐다. 그 상태에서 무언가를 클릭하면 실행 중인
    스크립트가 중단되어 ffmpeg 프로세스가 고아가 되고 변환이 중복 실행됐다.
    """

    def __init__(self, kind, total, label=""):
        self.kind = kind            # 'convert' | 'youtube' | 'mute'
        self.total = max(1, int(total))
        self.label = label
        self.cancel = threading.Event()
        self._lock = threading.Lock()
        self._state = {
            'overall': 0.0,
            'status': '준비 중...',
            'detail': '',
            'log': [],
            'done': False,
            'result_status': None,
            'result_summary': '',
            'processed_urls': [],
        }

    def update(self, **kwargs):
        with self._lock:
            self._state.update(kwargs)

    def add_log(self, line):
        with self._lock:
            self._state['log'].append(line)

    def mark_url_processed(self, url):
        with self._lock:
            self._state['processed_urls'].append(url)

    def finish(self, status, summary):
        with self._lock:
            self._state.update(
                done=True, result_status=status, result_summary=summary,
                overall=1.0, detail='',
            )

    def snapshot(self):
        """UI가 읽는 불변 스냅샷."""
        with self._lock:
            snap = dict(self._state)
            snap['log'] = list(snap['log'])
            snap['processed_urls'] = list(snap['processed_urls'])
        snap['kind'] = self.kind
        snap['total'] = self.total
        snap['label'] = self.label
        snap['cancelled'] = self.cancel.is_set()
        return snap


def _fmt_clock(seconds):
    seconds = int(max(0, seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _output_name(base_name, codec, resolution):
    if resolution != "original":
        return f"{base_name}_{codec}_{resolution}.mp4"
    return f"{base_name}_{codec}.mp4"


def _worker_convert(job, converter, files, output_path, settings):
    """로컬 파일 일괄 변환 (워커 스레드)."""
    total = len(files)
    success_count = failed_count = 0

    for idx, input_file in enumerate(files):
        if job.cancel.is_set():
            break

        file_name = os.path.basename(input_file)
        out_name = _output_name(os.path.splitext(file_name)[0],
                                settings['codec'], settings['resolution'])
        output_file = os.path.join(output_path, out_name)

        job.update(overall=idx / total, detail=file_name,
                   status=f"변환 중 ({idx + 1}/{total})")
        started = time.time()

        def progress_cb(progress, current, duration, _idx=idx, _name=file_name):
            elapsed = time.time() - started
            eta = ''
            if progress > 0.01:
                remaining = int(elapsed / progress - elapsed)
                if remaining > 0:
                    eta = f" · 남은 시간 약 {_fmt_clock(remaining)}"
            job.update(
                overall=(_idx + progress) / total,
                detail=f"{_name} — {progress * 100:.1f}% "
                       f"({_fmt_clock(current)} / {_fmt_clock(duration)}){eta}",
            )

        ok, message = converter.convert_video(
            input_file, output_file, settings['codec'], settings['resolution'],
            settings['quality'], settings['fps'], settings['scan'],
            settings['custom_video_br'], settings['custom_audio_br'], progress_cb,
        )
        if ok:
            success_count += 1
            job.add_log(f"✅ `{out_name}` ({int(time.time() - started)}초)")
        else:
            failed_count += 1
            job.add_log(f"❌ `{file_name}` — {message}")
        job.update(overall=(idx + 1) / total)

    tail = f"성공 {success_count}건, 실패 {failed_count}건 · 저장 위치: {output_path}"
    if job.cancel.is_set():
        job.finish('warning', f"⏹️ 중단됨 — {tail}")
    elif success_count:
        job.finish('success' if failed_count == 0 else 'warning', f"🎉 변환 완료 — {tail}")
    else:
        job.finish('error', "❌ 모든 변환이 실패했습니다.")


def _worker_youtube(job, converter, downloader, items, save_path, delete_original):
    """유튜브 일괄 다운로드(+변환) (워커 스레드)."""
    total = len(items)
    success_count = failed_count = 0

    for idx, item in enumerate(items):
        if job.cancel.is_set():
            break

        url = item['url']
        settings = item['settings']
        title = item['info'].get('title', url)
        base_overall = idx / total

        job.update(overall=base_overall, status=f"다운로드 중 ({idx + 1}/{total})", detail=title)

        def dl_cb(progress, _base=base_overall, _title=title):
            # 항목당 앞 절반은 다운로드, 뒤 절반은 변환에 배분한다
            job.update(overall=_base + (progress * 0.5) / total,
                       detail=f"{_title} — 다운로드 {progress * 100:.1f}%")

        ok, message, downloaded = downloader.download_video(url, save_path, dl_cb)
        if not ok:
            failed_count += 1
            job.add_log(f"❌ 다운로드 실패 — {title}: {message}")
            job.mark_url_processed(url)
            job.update(overall=(idx + 1) / total)
            continue

        if settings.get('download_only', False):
            success_count += 1
            job.add_log(f"📥 다운로드 완료 — {os.path.basename(downloaded)}")
            job.mark_url_processed(url)
            job.update(overall=(idx + 1) / total)
            continue

        info = converter.get_video_info(downloaded)
        current_codec = info['codec'] if info else 'unknown'
        needs_conversion = (
            current_codec != settings['codec']
            or settings['resolution'] != "original"
            or settings['fps'] != "original"
            or settings['quality'] == "custom"
        )

        if not needs_conversion:
            success_count += 1
            job.add_log(f"ℹ️ 이미 원하는 포맷 — {os.path.basename(downloaded)}")
            job.mark_url_processed(url)
            job.update(overall=(idx + 1) / total)
            continue

        out_name = _output_name(os.path.splitext(os.path.basename(downloaded))[0],
                                settings['codec'], settings['resolution'])
        output_file = os.path.join(save_path, out_name)
        job.update(status=f"변환 중 ({idx + 1}/{total})")

        def conv_cb(progress, current, duration, _base=base_overall, _title=title):
            job.update(
                overall=_base + (0.5 + progress * 0.5) / total,
                detail=f"{_title} — 변환 {progress * 100:.1f}% "
                       f"({_fmt_clock(current)} / {_fmt_clock(duration)})",
            )

        conv_ok, conv_msg = converter.convert_video(
            downloaded, output_file, settings['codec'], settings['resolution'],
            settings['quality'], settings['fps'], settings['scan'],
            settings['custom_video_br'], settings['custom_audio_br'], conv_cb,
        )
        if conv_ok:
            success_count += 1
            job.add_log(f"✅ 변환 완료 — {out_name}")
            if delete_original:
                try:
                    os.remove(downloaded)
                except Exception as e:
                    job.add_log(f"⚠️ 원본 삭제 실패 — {e}")
        else:
            failed_count += 1
            job.add_log(f"❌ 변환 실패 — {title}: {conv_msg}")

        job.mark_url_processed(url)
        job.update(overall=(idx + 1) / total)

    tail = f"성공 {success_count}건, 실패 {failed_count}건 · 저장 위치: {save_path}"
    if job.cancel.is_set():
        job.finish('warning', f"⏹️ 중단됨 — {tail}")
    elif success_count:
        job.finish('success' if failed_count == 0 else 'warning', f"🎉 배치 완료 — {tail}")
    else:
        job.finish('error', "❌ 모든 작업이 실패했습니다.")


def _worker_mute(job, converter, source, output_file):
    """무음 비디오 생성 (워커 스레드)."""
    job.update(status="길이 확인 중...", detail=os.path.basename(output_file))
    duration = converter.probe_duration(source)
    if duration <= 0:
        job.add_log("ℹ️ 길이를 확인할 수 없어 처리된 시간으로 표시합니다.")
    job.update(status="무음 비디오 생성 중...")

    def progress_cb(progress, processed, speed):
        suffix = f" · {speed}" if speed else ""
        if progress is None:
            job.update(detail=f"{_fmt_clock(processed)} 처리됨{suffix}")
        else:
            job.update(overall=progress,
                       detail=f"{progress * 100:.1f}% "
                              f"({_fmt_clock(processed)} / {_fmt_clock(duration)}){suffix}")

    ok, message = converter.strip_audio(source, output_file, progress_cb,
                                        total_duration=duration)
    if job.cancel.is_set():
        job.finish('warning', "⏹️ 중단됨")
    elif ok:
        size_mb = os.path.getsize(output_file) / (1024 * 1024)
        job.add_log(f"📁 {output_file}")
        job.finish('success',
                   f"🔇 무음 비디오 저장 완료 — {os.path.basename(output_file)} ({size_mb:.1f} MB)")
    else:
        job.finish('error', f"❌ 실패: {message}")


def _job_runner(job, target, args):
    """워커 예외를 삼키지 않고 작업을 반드시 종료 상태로 만든다.

    예외로 job이 done에 도달하지 못하면 UI가 영원히 진행 중으로 남는다.
    """
    try:
        target(job, *args)
    except Exception as e:
        job.finish('error', f"❌ 작업 중 예외가 발생했습니다: {e}")
    finally:
        if not job.snapshot()['done']:
            job.finish('error', "❌ 작업이 예기치 않게 종료되었습니다.")


def start_background_job(job, target, args):
    """작업을 데몬 스레드로 시작하고 세션에 등록한다."""
    threading.Thread(target=_job_runner, args=(job, target, args), daemon=True).start()
    st.session_state['job'] = job


# 앱 메타 정보
APP_NAME = "Video Tool"
APP_VERSION = "6.2.0"

# 섹션(탭) 식별자. st.tabs 대신 session_state 기반 네비게이션을 쓰기 때문에
# 라벨이 곧 상태값이다. 활성 섹션만 렌더하므로 rerun에도 선택이 유지된다.
TAB_CONVERT = "🎬 Convert"
TAB_YOUTUBE = "📥 YouTube"
TAB_MUTE = "🔇 Mute"
TAB_LABELS = [TAB_CONVERT, TAB_YOUTUBE, TAB_MUTE]

# 페이지 설정
st.set_page_config(
    page_title=f"{APP_NAME} v{APP_VERSION}",
    page_icon="🎥",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# 세션 상태 초기화 (한 곳에서 통합 관리)
def init_session_state():
    # 가벼운 기본값만 이곳에서 초기화한다.
    # 주의: 무거운 객체(VideoConverterCore/YouTubeDownloader)는 여기서 생성하지 않는다.
    #       이 함수는 매 rerun마다 호출되는데, 딕셔너리 리터럴에 인스턴스를 두면
    #       생성자가 매번 실행되어(ffmpeg/yt-dlp subprocess) 앱 전체가 느려진다.
    defaults = {
        'theme_mode': 'light',
        'selected_folder_path': "",
        'video_files_list': [],
        'file_selection_state': {},
        'job': None,
        'yt_queue': [],
        'yt_queue_selection': {},
        'yt_save_folder_path': os.path.join(os.path.expanduser("~"), "Downloads"),
        'sort_by': 'name',
        'sort_order': 'asc',
        'active_tab': TAB_CONVERT,
        'mute_save_folder_path': os.path.join(os.path.expanduser("~"), "Downloads"),
        'yt_delete_original': True,
        'op_result': None,
        'yt_notice': None,
        'yt_url_rows': [0],
        'yt_url_row_seq': 1,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    # 무거운 객체는 세션에 없을 때만 지연 생성한다(세션당 1회).
    if 'converter' not in st.session_state:
        st.session_state['converter'] = VideoConverterCore()
    if 'yt_downloader' not in st.session_state:
        st.session_state['yt_downloader'] = YouTubeDownloader()

init_session_state()

@st.cache_resource
def get_app_css():
    """CSS 문자열을 한 번만 생성하고 캐싱 — 매 rerun마다 재파싱 방지"""
    return """
<style>
/* 폰트는 CSS @import로 불러온다. st.html은 내용을 HTML로 정제하므로 stylesheet
   링크 태그는 제거되어 버린다(그 탓에 Material 아이콘이 폰트 없이
   'queue_play_next' 같은 리거처 이름 그대로 노출됐다). @import는 CSS라 살아남는다.
   @import는 반드시 스타일시트 최상단에 있어야 적용된다.

   경고: 이 문자열 안에서는 꺾쇠(less-than + 낱말) 조합을 절대 쓰지 말 것.
   태그처럼 보이는 조각이 하나라도 있으면 정제 과정에서 style 블록이 통째로
   삭제되어 앱 스타일 전체가 사라진다. tests/test_css_integrity.py 가 이를 검사한다. */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;900&display=swap');
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200&display=swap');
:root {
    /* Material 3 light tokens */
    --m-surface: #fcf8fb;
    --m-surface-low: #f6f3f5;
    --m-surface-container: #f0edef;
    --m-surface-high: #eae7ea;
    --m-surface-variant: #e4e2e4;
    --m-surface-dim: #dcd9dc;
    --m-on-surface: #1b1b1d;
    --m-on-surface-variant: #414755;
    --m-outline: #717786;
    --m-outline-variant: #c1c6d7;
    --m-primary: #0058bc;
    --m-primary-container: #0070eb;
    --m-on-primary: #ffffff;
    --m-primary-fixed: #d8e2ff;
    --m-tertiary: #8a2bb9;
    --m-tertiary-fixed: #f6d9ff;
    --m-secondary: #4c4aca;
    --m-secondary-fixed: #e2dfff;
    --m-error: #ba1a1a;

    /* Aliases used across the app */
    --bg-primary: var(--m-surface); --bg-secondary: var(--m-surface-low); --bg-tertiary: var(--m-surface-container); --bg-input: var(--m-surface-container-lowest, #ffffff);
    --border-default: var(--m-outline-variant); --border-accent: var(--m-primary);
    --text-primary: var(--m-on-surface); --text-secondary: var(--m-on-surface-variant); --text-muted: var(--m-outline);
    --accent: var(--m-primary); --accent-hover: var(--m-primary-container);
    --success: #2ea043; --warning: #d29922; --info: #58a6ff;

    /* Glass — Lumina spec */
    --glass-bg: rgba(255, 255, 255, 0.40);
    --glass-bg-strong: rgba(255, 255, 255, 0.62);
    --glass-border: rgba(255, 255, 255, 0.80);
    --glass-shadow: 0 10px 40px rgba(0, 0, 0, 0.05);
    --card-shadow: 0 8px 32px rgba(31, 38, 135, 0.07);

    /* Layout chrome */
    --topnav-h: 64px;
    --sidenav-w: 0px;
    --footer-h: 32px;
}
[data-theme="dark"] {
    --m-surface: #0f172a;
    --m-surface-low: #111827;
    --m-surface-container: #1e293b;
    --m-surface-high: #243044;
    --m-surface-variant: #334155;
    --m-on-surface: #e2e8f0;
    --m-on-surface-variant: #94a3b8;
    --m-outline: #64748b;
    --m-outline-variant: #334155;
    --m-primary: #adc6ff;
    --m-primary-container: #0070eb;
    --m-on-primary: #001a41;
    --m-primary-fixed: #d8e2ff;
    --m-tertiary: #e8b3ff;
    --m-tertiary-fixed: #7201a2;
    --m-secondary: #c2c1ff;
    --m-secondary-fixed: #3631b4;

    --bg-primary: var(--m-surface); --bg-secondary: var(--m-surface-low); --bg-tertiary: var(--m-surface-container); --bg-input: var(--m-surface-container);
    --border-default: var(--m-outline-variant); --border-accent: var(--m-primary);
    --text-primary: var(--m-on-surface); --text-secondary: var(--m-on-surface-variant); --text-muted: var(--m-outline);
    --accent: #6ea8ff; --accent-hover: #adc6ff;
    --success: #3fb950; --warning: #d29922; --info: #6ea8ff;

    --glass-bg: rgba(15, 23, 42, 0.40);
    --glass-bg-strong: rgba(15, 23, 42, 0.62);
    --glass-border: rgba(148, 163, 184, 0.24);
    --glass-shadow: 0 10px 40px rgba(2, 6, 23, 0.40);
    --card-shadow: 0 8px 32px rgba(0, 0, 0, 0.30);
}
* { font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important; }
.material-symbols-outlined, [data-testid="stIconMaterial"], .material-symbols-rounded {
    font-family: 'Material Symbols Outlined' !important;
    font-weight: normal; font-style: normal; line-height: 1;
    letter-spacing: normal; text-transform: none; display: inline-block;
    white-space: nowrap; word-wrap: normal; direction: ltr;
    -webkit-font-feature-settings: 'liga'; -webkit-font-smoothing: antialiased;
}
.stApp { background: var(--m-surface) !important; }
[data-theme="dark"] .stApp { background: var(--m-surface) !important; }

/* === Mesh background (3 orbs, Lumina spec) — will-change로 GPU 레이어 고정 === */
body::before, body::after, .vt-mesh { pointer-events: none; will-change: transform; }
body::before {
    content: "";
    position: fixed;
    top: -10%; left: -10%;
    width: 50vw; height: 50vw;
    border-radius: 9999px;
    background: radial-gradient(circle, rgba(216,226,255,0.6) 0%, rgba(216,226,255,0) 70%);
    filter: blur(12px);
    z-index: 0;
    transform: translateZ(0);
}
body::after {
    content: "";
    position: fixed;
    bottom: -20%; right: -10%;
    width: 60vw; height: 60vw;
    border-radius: 9999px;
    background: radial-gradient(circle, rgba(246,217,255,0.5) 0%, rgba(246,217,255,0) 70%);
    filter: blur(12px);
    z-index: 0;
    transform: translateZ(0);
}
[data-theme="dark"] body::before {
    background: radial-gradient(circle, rgba(0,88,188,0.30) 0%, rgba(0,88,188,0) 65%);
}
[data-theme="dark"] body::after {
    background: radial-gradient(circle, rgba(138,43,185,0.28) 0%, rgba(138,43,185,0) 65%);
}

.stApp > header { height: 0rem; background-color: transparent !important; }
header[data-testid="stHeader"] { background-color: transparent !important; position: relative; z-index: 1; }
[data-testid="stAppViewContainer"] { position: relative; z-index: 1; }

/* Push main content below fixed TopNav and right of fixed SideNav */
.main .block-container {
    padding-top: calc(var(--topnav-h) + 24px);
    padding-bottom: calc(var(--footer-h) + 32px);
    padding-left: 24px;
    padding-right: 24px;
    max-width: 100%;
}

/* === TopNavBar === */
.vt-topnav {
    position: fixed; top: 0; left: 0; right: 0; height: var(--topnav-h);
    z-index: 1000; padding: 0 24px;
    display: flex; align-items: center; justify-content: space-between;
    background: rgba(255,255,255,0.70);
    backdrop-filter: blur(24px) saturate(150%);
    -webkit-backdrop-filter: blur(24px) saturate(150%);
    border-bottom: 0.5px solid rgba(255,255,255,0.80);
    box-shadow: 0 10px 40px rgba(0,0,0,0.05);
}
[data-theme="dark"] .vt-topnav { background: rgba(15,23,42,0.60); border-bottom-color: rgba(255,255,255,0.10); }
.vt-topnav__brand { display: flex; align-items: center; gap: 12px; font-size: 18px; font-weight: 700; letter-spacing: -0.02em; color: var(--text-primary); }
.vt-topnav__brand-mark {
    width: 34px; height: 34px; border-radius: 11px;
    background: linear-gradient(135deg, #e0f2fe, #ddd6fe);
    display: inline-flex; align-items: center; justify-content: center;
    box-shadow: 0 4px 12px rgba(0,0,0,0.10);
    border: 0.5px solid rgba(255,255,255,0.75);
    overflow: hidden;
}
.vt-topnav__logo-emoji {
    font-size: 21px;
    line-height: 1;
    transform: translateY(-0.5px);
    filter: drop-shadow(0 1px 1px rgba(0,0,0,0.22));
}
[data-theme="dark"] .vt-topnav__brand-mark {
    background: linear-gradient(135deg, #dbeafe, #c4b5fd);
    border: 0.5px solid rgba(226,232,240,0.60);
}
.vt-topnav__right { display: flex; align-items: center; gap: 8px; }
.vt-topnav__icon-btn { width: 36px; height: 36px; border-radius: 9999px; display: inline-flex; align-items: center; justify-content: center; color: var(--text-secondary); background: transparent; border: none; cursor: pointer; transition: background-color 160ms ease; }
.vt-topnav__icon-btn:hover { background: rgba(255,255,255,0.50); color: var(--text-primary); }
[data-theme="dark"] .vt-topnav__icon-btn:hover { background: rgba(30,41,59,0.50); }

footer { visibility: hidden !important; }
.stAppDeployButton, button[data-testid="stAppDeployButton"] { display: none !important; }
[data-testid="stMainMenu"] { display: none !important; visibility: hidden !important; opacity: 0 !important; }
[data-theme="dark"] p, [data-theme="dark"] span, [data-theme="dark"] label, [data-theme="dark"] h1, [data-theme="dark"] h2, [data-theme="dark"] h3, [data-theme="dark"] h4, [data-theme="dark"] h5, [data-theme="dark"] h6, [data-theme="dark"] div, [data-theme="dark"] li, [data-theme="dark"] td, [data-theme="dark"] th { color: var(--text-primary) !important; }
[data-theme="dark"] .stMarkdown, [data-theme="dark"] .stMarkdown p, [data-theme="dark"] [data-testid="stMarkdownContainer"] p { color: var(--text-primary) !important; }
[data-theme="dark"] .element-container { color: var(--text-primary) !important; }
[data-theme="dark"] small { color: var(--text-secondary) !important; }
[data-theme="dark"] input, [data-theme="dark"] textarea, [data-theme="dark"] select, [data-theme="dark"] [data-baseweb="select"], [data-theme="dark"] [data-baseweb="input"] { background-color: var(--bg-tertiary) !important; color: var(--text-primary) !important; border-color: var(--border-default) !important; }
[data-theme="dark"] input:disabled, [data-theme="dark"] textarea:disabled { color: var(--text-secondary) !important; -webkit-text-fill-color: var(--text-secondary) !important; opacity: 1 !important; }
[data-theme="dark"] input::placeholder, [data-theme="dark"] textarea::placeholder { color: var(--text-muted) !important; }
[data-theme="dark"] input:focus, [data-theme="dark"] textarea:focus { border-color: var(--accent) !important; box-shadow: 0 0 0 1px var(--accent) !important; }
[data-theme="dark"] [data-baseweb="select"] > div { background-color: var(--bg-tertiary) !important; border-color: var(--border-default) !important; }
[data-theme="dark"] [data-baseweb="select"] svg { color: var(--text-primary) !important; fill: var(--text-primary) !important; }
[data-theme="dark"] [data-baseweb="select"] > div > div { color: var(--text-primary) !important; }
[data-theme="dark"] [data-baseweb="popover"], [data-theme="dark"] [data-baseweb="menu"] { background-color: var(--bg-tertiary) !important; }
[data-theme="dark"] [role="option"] { background-color: var(--bg-tertiary) !important; color: var(--text-primary) !important; }
[data-theme="dark"] [role="option"]:hover { background-color: var(--border-default) !important; }
[data-theme="dark"] [data-testid="stFileUploader"], [data-theme="dark"] [data-testid="stFileUploader"] section { background-color: var(--bg-tertiary) !important; border-color: var(--border-default) !important; }
[data-theme="dark"] [data-testid="stCheckbox"] label { color: var(--text-primary) !important; }
[data-theme="dark"] [data-testid="stTabContent"] { background-color: var(--bg-primary) !important; }
[data-theme="dark"] [data-testid="stExpander"] { background-color: var(--bg-secondary) !important; border-color: var(--border-default) !important; }
[data-theme="dark"] [data-testid="stExpanderDetails"] { background-color: var(--bg-tertiary) !important; }
[data-theme="dark"] [data-testid="stAlert"] { background-color: var(--bg-secondary) !important; color: var(--text-primary) !important; }
[data-theme="dark"] code { background-color: var(--bg-tertiary) !important; color: var(--text-primary) !important; }
[data-theme="dark"] pre { background-color: var(--bg-tertiary) !important; border-color: var(--border-default) !important; }
[data-theme="dark"] hr { border-color: var(--border-default) !important; }
/* === Glass panels (Lumina spec) — contain으로 리플로우 격리 === */
.st-key-config_panel > div, .st-key-file_source > div, .st-key-queue_panel > div, .st-key-mute_card > div, .st-key-yt_config_panel > div, .st-key-yt_queue_panel > div, .st-key-yt_save_panel > div {
    background: var(--glass-bg);
    border: 0.5px solid var(--glass-border);
    border-radius: 24px;
    box-shadow: var(--glass-shadow);
    backdrop-filter: blur(16px) saturate(160%);
    -webkit-backdrop-filter: blur(16px) saturate(160%);
    will-change: transform;
    contain: layout style;
}
.st-key-config_panel > div, .st-key-yt_config_panel > div { padding: 24px; }
.st-key-file_source > div { padding: 18px; margin-top: 12px; }
.st-key-queue_panel > div, .st-key-yt_queue_panel > div { padding: 24px; }
.st-key-mute_card > div { padding: 28px; max-width: 760px; margin: 0 auto; }

.vt-section-label { font-size: 12px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; color: var(--text-secondary); margin-bottom: 12px; }

/* === Stat cards (Lumina display-lg) === */
.vt-stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 20px; }
.vt-stats-card {
    position: relative; overflow: hidden;
    background: var(--glass-bg);
    border: 0.5px solid var(--glass-border);
    border-radius: 16px;
    padding: 24px;
    box-shadow: var(--card-shadow);
    backdrop-filter: blur(16px) saturate(160%);
    -webkit-backdrop-filter: blur(16px) saturate(160%);
    will-change: transform;
    contain: layout style;
}
.vt-stats-card::after {
    content: ""; position: absolute; top: -16px; right: -16px;
    width: 96px; height: 96px; border-radius: 9999px;
    background: var(--m-primary-fixed); opacity: 0.45;
    filter: blur(16px);
    transition: opacity 200ms ease;
    pointer-events: none;
}
.vt-stats-card:hover::after { opacity: 0.65; }
.vt-stats-card:nth-child(2)::after { background: var(--m-tertiary-fixed); }
.vt-stats-card:nth-child(3)::after { background: var(--m-secondary-fixed); }
.vt-stats-card__head { display: flex; align-items: center; gap: 6px; color: var(--text-secondary); margin-bottom: 8px; position: relative; z-index: 1; }
.vt-stats-card__head .material-symbols-outlined { font-size: 18px; }
.vt-stats-card__label { font-size: 12px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; }
.vt-stats-card__value { font-size: 40px; font-weight: 700; letter-spacing: -0.04em; line-height: 1.1; color: var(--text-primary); position: relative; z-index: 1; }
.vt-stats-card__unit { font-size: 18px; font-weight: 600; letter-spacing: -0.01em; color: var(--text-muted); margin-left: 8px; }
.vt-stats-card__value--accent { color: var(--m-primary); }

/* legacy class kept for YT row, now restyled to match */
.vt-stats-row--yt .vt-stats-card::after { background: var(--m-tertiary-fixed); }
.vt-badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 0.25rem; font-size: 0.65rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; }
.vt-badge--ready { background: rgba(46,160,67,0.15); color: var(--success); border: 1px solid rgba(46,160,67,0.3); }
.vt-badge--pending { background: rgba(210,153,34,0.15); color: var(--warning); border: 1px solid rgba(210,153,34,0.3); }
.vt-badge--processing { background: rgba(88,166,255,0.15); color: var(--info); border: 1px solid rgba(88,166,255,0.3); }
/* === Footer status bar (Lumina) === */
.vt-status-bar {
    position: fixed; bottom: 0; left: 0; right: 0; height: var(--footer-h);
    z-index: 999; padding: 0 24px;
    display: flex; align-items: center; justify-content: space-between;
    font-size: 10px; font-weight: 500; letter-spacing: 0.02em; color: var(--text-muted);
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace !important;
    background: rgba(248,250,252,0.80);
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    border-top: 0.5px solid rgba(255,255,255,0.40);
}
[data-theme="dark"] .vt-status-bar { background: rgba(15,23,42,0.80); border-top-color: rgba(255,255,255,0.05); }
.vt-status-bar strong { color: var(--text-secondary); font-weight: 600; }
.vt-status-bar__group { display: flex; align-items: center; gap: 16px; }

/* === Section header (renamed Lumina-style) === */
.vt-header { margin: 0 0 24px 0; display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.vt-header__title { font-size: 28px; font-weight: 700; letter-spacing: -0.03em; color: var(--text-primary); margin: 0; line-height: 1.15; }
.vt-header__subtitle { color: var(--text-secondary); font-size: 14px; font-weight: 400; margin: 4px 0 0 0; }
.vt-header__badges { display: flex; gap: 6px; flex-wrap: wrap; }
.vt-header__badge { padding: 4px 10px; border-radius: 9999px; font-size: 11px; font-weight: 700; letter-spacing: 0.04em; background: var(--m-primary-fixed); color: var(--m-primary); border: 0.5px solid rgba(0,88,188,0.20); }
[data-theme="dark"] .vt-header__badge {
    background: rgba(51, 65, 85, 0.72);
    color: #e2e8f0;
    border-color: rgba(148, 163, 184, 0.35);
}

/* === Buttons (Lumina pill + active scale) === */
/* primary는 불투명 단색으로 — 반투명 그라디언트는 연해 보여서
   비활성 버튼으로 오인됐다. 상단 섹션 네비게이션의 활성 pill과 같은 색을 쓴다. */
button[kind="primary"] {
    background: var(--m-primary) !important;
    border: 0.5px solid var(--m-primary) !important;
    color: var(--m-on-primary) !important;
    font-weight: 600 !important;
    border-radius: 14px !important;
    box-shadow: 0 6px 18px rgba(0,88,188,0.28) !important;
    transition: transform 140ms ease, filter 140ms ease, background-color 140ms ease !important;
}
button[kind="primary"]:hover { filter: brightness(1.12); }
button[kind="primary"]:active { transform: scale(0.98); }
/* 비활성은 명확히 회색으로 — 활성 상태와 헷갈리지 않게 */
button[kind="primary"]:disabled {
    background: var(--m-surface-variant) !important;
    border-color: var(--border-default) !important;
    color: var(--text-muted) !important;
    box-shadow: none !important;
}
/* 다크모드 primary는 M3 다크 팔레트대로 밝은 파랑 + 어두운 글자.
   주변 패널/보조 버튼이 모두 슬레이트 계열이라, primary도 슬레이트로 두면
   주 동작이 배경에 묻혀 구분되지 않는다. */
[data-theme="dark"] button[kind="primary"] {
    background: var(--m-primary) !important;
    border: 0.5px solid var(--m-primary) !important;
    color: var(--m-on-primary) !important;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.45) !important;
}
[data-theme="dark"] button[kind="primary"]:hover { filter: brightness(1.08); }
[data-theme="dark"] button[kind="primary"]:disabled {
    background: rgba(30, 41, 59, 0.55) !important;
    border-color: rgba(148, 163, 184, 0.20) !important;
    color: var(--text-muted) !important;
    box-shadow: none !important;
}
button[kind="secondary"] {
    background: rgba(0,0,0,0.05) !important;
    border: 0.5px solid rgba(255,255,255,0.40) !important;
    color: var(--text-primary) !important;
    font-weight: 500 !important;
    border-radius: 12px !important;
    transition: transform 140ms ease, background-color 140ms ease !important;
}
button[kind="secondary"]:hover { background: rgba(0,0,0,0.10) !important; }
button[kind="secondary"]:active { transform: scale(0.98); }
[data-theme="dark"] button[kind="secondary"] { background: rgba(255,255,255,0.05) !important; border-color: rgba(255,255,255,0.10) !important; }
[data-theme="dark"] button[kind="secondary"]:hover { background: rgba(255,255,255,0.10) !important; }

/* === Inputs / selects (Lumina shadow-inner) === */
input, textarea, [data-baseweb="select"] > div {
    background-color: rgba(0,0,0,0.05) !important;
    border: 0.5px solid rgba(255,255,255,0.40) !important;
    border-radius: 10px !important;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.04) !important;
    color: var(--text-primary) !important;
}
[data-theme="dark"] input, [data-theme="dark"] textarea, [data-theme="dark"] [data-baseweb="select"] > div {
    background-color: rgba(255,255,255,0.05) !important;
    border-color: rgba(255,255,255,0.10) !important;
}
input:focus, textarea:focus { border-color: var(--m-primary) !important; box-shadow: 0 0 0 1px var(--m-primary) !important; }
/* Stop 버튼 — 셀렉터를 .st-key-* 로 수정했다.
   Streamlit은 위젯 key를 button 속성이 아니라 컨테이너 클래스(st-key-KEY 형태)로
   내보내므로, 예전의 button[key="..."] 규칙은 한 번도 적용되지 않았다.
   주의: 이 CSS 문자열 안에 꺾쇠로 감싼 낱말을 쓰면 안 된다. st.html이 내용을
   HTML로 정제(sanitize)하기 때문에 태그처럼 보이는 조각이 있으면 style 블록이
   통째로 제거되어 앱 스타일이 전부 사라진다. */
.st-key-job_stop_btn button {
    background: var(--m-error) !important;
    border-color: var(--m-error) !important;
    color: #ffffff !important;
    font-weight: 600 !important;
}
.st-key-job_stop_btn button:hover {
    filter: brightness(1.12);
}
.st-key-job_stop_btn button:disabled {
    background: var(--m-surface-variant) !important;
    border-color: var(--border-default) !important;
    color: var(--text-muted) !important;
}
.stProgress > div > div > div > div { background: linear-gradient(90deg, var(--m-primary), var(--m-tertiary)); border-radius: 9999px; height: 6px; }
/* (st.tabs 제거됨 — 섹션 네비게이션 스타일은 .st-key-nav_section 참조) */

/* Theme toggle button — moved into TopNav area */
.st-key-theme_toggle { position: fixed !important; top: 14px; right: 16px; z-index: 1001; }
.st-key-theme_toggle button { background: rgba(255,255,255,0.50) !important; border: 0.5px solid rgba(255,255,255,0.80) !important; border-radius: 9999px !important; padding: 6px 12px !important; font-size: 16px !important; box-shadow: 0 4px 12px rgba(0,0,0,0.06) !important; }
.st-key-theme_toggle button:hover { background: rgba(255,255,255,0.70) !important; }
[data-theme="dark"] .st-key-theme_toggle button { background: rgba(15,23,42,0.50) !important; border-color: rgba(255,255,255,0.10) !important; color: var(--text-primary) !important; }
[data-theme="dark"] .st-key-theme_toggle button:hover { background: rgba(30,41,59,0.70) !important; }

/* Queue rows (Lumina) */
.st-key-queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox), .st-key-yt_queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox) {
    display: flex !important; align-items: center !important;
    min-height: 48px !important;
    padding: 8px 12px !important;
    border-radius: 12px !important;
    border: 0.5px solid transparent !important;
    transition: background 160ms ease, border-color 160ms ease !important;
}
.st-key-queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox):hover, .st-key-yt_queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox):hover {
    background: rgba(255,255,255,0.50) !important;
    border-color: rgba(255,255,255,0.60) !important;
}
[data-theme="dark"] .st-key-queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox):hover, [data-theme="dark"] .st-key-yt_queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox):hover {
    background: rgba(30,41,59,0.50) !important;
    border-color: rgba(255,255,255,0.10) !important;
}
.st-key-queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox) > div[data-testid="column"], .st-key-yt_queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox) > div[data-testid="column"] { display: flex !important; align-items: center !important; }
.st-key-queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox) p, .st-key-yt_queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox) p { margin: 0 !important; padding: 0 !important; line-height: 1.4 !important; font-size: 14px !important; }
.st-key-queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox) .stCheckbox, .st-key-yt_queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox) .stCheckbox { margin: 0 !important; padding: 0 !important; }

/* === File row (single html node per file — widget count 축소용) === */
.vt-file-row {
    display: grid;
    grid-template-columns: 3fr 1fr 1.5fr;
    align-items: center;
    gap: 8px;
    font-size: 14px;
    line-height: 1.4;
    color: var(--text-primary);
    min-height: 32px;
}
.vt-file-row__name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vt-file-row__size { text-align: center; font-weight: 600; font-variant-numeric: tabular-nums; }
.vt-file-row__date { text-align: center; color: var(--text-secondary); font-size: 12px; font-variant-numeric: tabular-nums; }

/* === Section nav — st.tabs 대체 (st.radio를 세그먼트 pill로 스타일링) === */
.st-key-nav_section { margin-bottom: 16px; }
.st-key-nav_section [role="radiogroup"] {
    display: inline-flex;
    gap: 4px;
    background: var(--glass-bg);
    border: 0.5px solid var(--glass-border);
    border-radius: 14px;
    padding: 4px;
    backdrop-filter: blur(16px) saturate(160%);
    -webkit-backdrop-filter: blur(16px) saturate(160%);
    box-shadow: var(--glass-shadow);
}
/* 라디오 동그라미 숨기고 라벨 자체를 pill로 만든다 */
.st-key-nav_section [role="radiogroup"] > label {
    margin: 0 !important;
    padding: 8px 18px !important;
    border-radius: 10px !important;
    cursor: pointer;
    transition: background 160ms ease, color 160ms ease !important;
}
.st-key-nav_section [role="radiogroup"] > label > div:first-child { display: none !important; }
.st-key-nav_section [role="radiogroup"] > label p {
    margin: 0 !important;
    font-size: 14px !important;
    font-weight: 600 !important;
    letter-spacing: -0.01em !important;
    color: var(--text-secondary) !important;
    white-space: nowrap;
}
.st-key-nav_section [role="radiogroup"] > label:hover { background: rgba(255,255,255,0.45); }
.st-key-nav_section [role="radiogroup"] > label:hover p { color: var(--text-primary) !important; }
[data-theme="dark"] .st-key-nav_section [role="radiogroup"] > label:hover { background: rgba(255,255,255,0.07); }
.st-key-nav_section [role="radiogroup"] > label:has(input:checked) {
    background: var(--m-primary);
    box-shadow: 0 4px 14px rgba(0,88,188,0.22);
}
.st-key-nav_section [role="radiogroup"] > label:has(input:checked) p { color: var(--m-on-primary) !important; }
[data-theme="dark"] .st-key-nav_section [role="radiogroup"] > label:has(input:checked) {
    background: rgba(51, 65, 85, 0.92);
}
[data-theme="dark"] .st-key-nav_section [role="radiogroup"] > label:has(input:checked) p { color: #e2e8f0 !important; }
.st-key-nav_section [role="radiogroup"]:has(input:disabled) { opacity: 0.55; }

/* === 진행 중 작업 패널 (전역, 섹션 위에 표시) === */
.st-key-job_panel > div {
    background: var(--glass-bg-strong);
    border: 0.5px solid var(--glass-border);
    border-radius: 16px;
    padding: 14px 18px;
    margin-bottom: 16px;
    box-shadow: var(--card-shadow);
    backdrop-filter: blur(16px) saturate(160%);
    -webkit-backdrop-filter: blur(16px) saturate(160%);
}
.st-key-job_panel div[data-testid="stHorizontalBlock"] { align-items: center; }
.vt-job__status {
    margin: 0; font-size: 14px; font-weight: 600; letter-spacing: -0.01em;
    color: var(--text-primary); display: flex; align-items: center; min-height: 32px;
}
.st-key-job_panel .stProgress { margin-top: 4px; }
.st-key-job_panel [data-testid="stCaptionContainer"] p { font-size: 12px !important; margin: 4px 0 0 !important; }

/* === URL 입력 행 + 큐 툴바 === */
/* 링크 칸 행: 입력과 삭제(✕)를 한 줄에 붙여 정렬 */
.st-key-yt_url_rows div[data-testid="stHorizontalBlock"] { gap: 4px; align-items: center; }
.st-key-yt_url_rows div[data-testid="stHorizontalBlock"] > div[data-testid="column"] { display: flex; align-items: center; }
.st-key-yt_url_rows div[data-testid="stVerticalBlock"] { gap: 6px; }
.st-key-yt_url_add_row { margin-top: 2px; }
.st-key-yt_url_add_row button {
    color: var(--m-primary) !important;
    font-weight: 600 !important;
    font-size: 13px !important;
    padding: 4px 6px !important;
}
[data-theme="dark"] .st-key-yt_url_add_row button { color: var(--accent) !important; }

/* 큐 관리 버튼은 주 동작(Add to Queue / Download Now)보다 작고 약하게 —
   같은 크기면 같은 위계로 읽힌다 */
.vt-queue-toolbar__title {
    font-size: 12px; font-weight: 700; letter-spacing: 0.05em;
    text-transform: uppercase; color: var(--text-secondary);
    margin: 0; display: flex; align-items: center; min-height: 30px;
}
/* 배경은 있고(칩처럼 눌러지는 게 보이게), 크기·굵기로만 주 동작과 구분한다 */
.st-key-yt_select_all button, .st-key-yt_clear_queue button {
    font-size: 12px !important;
    font-weight: 600 !important;
    color: var(--text-secondary) !important;
    background: var(--m-surface-variant) !important;
    border: 0.5px solid var(--border-default) !important;
    padding: 3px 10px !important;
    min-height: 28px !important;
    border-radius: 8px !important;
    transition: background 140ms ease, color 140ms ease !important;
}
.st-key-yt_select_all button:hover {
    color: var(--m-on-primary) !important;
    background: var(--m-primary) !important;
    border-color: var(--m-primary) !important;
}
.st-key-yt_clear_queue button:hover {
    color: #ffffff !important;
    background: var(--m-error) !important;
    border-color: var(--m-error) !important;
}
[data-theme="dark"] .st-key-yt_select_all button, [data-theme="dark"] .st-key-yt_clear_queue button {
    background: rgba(51, 65, 85, 0.70) !important;
    border-color: rgba(148, 163, 184, 0.28) !important;
    color: #cbd5e1 !important;
}
[data-theme="dark"] .st-key-yt_select_all button:hover {
    background: rgba(71, 92, 122, 0.95) !important;
    color: #ffffff !important;
}
[data-theme="dark"] .st-key-yt_clear_queue button:hover {
    background: rgba(155, 40, 40, 0.90) !important;
    border-color: rgba(255, 138, 138, 0.40) !important;
    color: #ffffff !important;
}

/* 큐 항목 삭제(✕) / 링크 칸 삭제(✕) — 아이콘 크기로 축소.
   '＋ 링크 칸 추가'는 컬럼 밖에 있으므로 column 하위로 한정해 제외한다. */
.st-key-yt_url_rows div[data-testid="column"] button,
[class*="st-key-yt_remove_"] button {
    color: var(--text-muted) !important;
    padding: 2px 6px !important;
    min-height: 26px !important;
}
.st-key-yt_url_rows div[data-testid="column"] button:hover,
[class*="st-key-yt_remove_"] button:hover {
    color: var(--m-error) !important;
    background: rgba(186,26,26,0.07) !important;
}

/* Sort buttons (Lumina label-caps) */
/* 정렬 헤더 버튼 — 위와 같은 이유로 .st-key-*로 수정 */
.st-key-sort_name button, .st-key-sort_size button, .st-key-sort_date button {
    border: none !important; background: transparent !important; box-shadow: none !important;
    outline: none !important; padding: 8px 6px !important; font-weight: 700 !important;
    font-size: 12px !important; letter-spacing: 0.05em !important; text-transform: uppercase !important;
    color: var(--text-secondary) !important; border-radius: 8px !important;
}
.st-key-sort_name button:hover, .st-key-sort_size button:hover, .st-key-sort_date button:hover {
    color: var(--m-primary) !important; background: rgba(0,88,188,0.06) !important;
}
.st-key-toggle_all_header button {
    font-size: 12px !important; font-weight: 700 !important; padding: 8px 4px !important;
}

/* Expander */
[data-testid="stExpander"] { border: 0.5px solid var(--border-default) !important; border-radius: 12px !important; background: rgba(255,255,255,0.30) !important; backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); overflow: visible !important; }
[data-theme="dark"] [data-testid="stExpander"] { background: rgba(15,23,42,0.30) !important; }
[data-testid="stExpander"] summary { font-size: 14px !important; font-weight: 600 !important; color: var(--text-secondary) !important; padding: 12px 16px !important; }
[data-theme="dark"] [data-testid="stExpander"] summary {
    background: rgba(15,23,42,0.68) !important;
    color: #cbd5e1 !important;
    border-radius: 10px !important;
}
[data-theme="dark"] [data-testid="stExpander"] summary p,
[data-theme="dark"] [data-testid="stExpander"] summary [data-testid="stMarkdownContainer"] p {
    color: #cbd5e1 !important;
}
[data-testid="stExpander"] summary::-webkit-details-marker { display: none !important; }
[data-testid="stExpander"] summary::marker { content: '' !important; }
[data-testid="stExpander"] summary > span { display: flex !important; align-items: center !important; gap: 8px !important; width: 100% !important; }
[data-testid="stExpander"] summary > span > div { flex: 1 !important; min-width: 0 !important; }
[data-testid="stExpander"] summary p { margin: 0 !important; line-height: 1.35 !important; white-space: normal !important; overflow: visible !important; text-overflow: clip !important; }
[data-testid="stExpander"] summary [data-testid="stIconMaterial"] { display: none !important; }
[data-testid="stExpander"] summary > span > span { display: none !important; }

.feature-box { background: var(--glass-bg); border: 0.5px solid var(--glass-border); border-radius: 12px; padding: 16px; backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); }
div[data-testid="stHorizontalBlock"] { gap: 8px; }

/* Scrollable queue panels - config stays visible */
.st-key-queue_panel > div, .st-key-yt_queue_panel > div { max-height: 65vh; overflow-y: auto; }
.st-key-yt_save_panel > div { padding: 16px; margin-top: 8px; }
/* Fix expander text overlap */
[data-testid="stExpanderDetails"] > div { padding: 0.5rem 0 !important; }
[data-theme="dark"] .st-key-mute_card [data-baseweb="input"] > div,
[data-theme="dark"] .st-key-mute_card [data-baseweb="base-input"],
[data-theme="dark"] .st-key-mute_card input {
    background: rgba(15, 23, 42, 0.78) !important;
    border-color: rgba(148, 163, 184, 0.40) !important;
    color: #e2e8f0 !important;
}
.vt-chip-row { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-top: 0.25rem; }
.vt-chip { display: inline-flex; align-items: center; padding: 0.1rem 0.4rem; border-radius: 9999px; font-size: 0.65rem; font-weight: 600; letter-spacing: 0.01em; border: 1px solid var(--border-default); background: var(--bg-tertiary); color: var(--text-secondary); }
.vt-chip--accent { border-color: rgba(88, 166, 255, 0.35); color: var(--info); background: rgba(88, 166, 255, 0.12); }
.vt-chip--warn { border-color: rgba(210, 153, 34, 0.35); color: var(--warning); background: rgba(210, 153, 34, 0.12); }
.vt-chip--mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace !important; font-size: 0.62rem; }
/* old tab-hero variant block removed (unified to glass) */
.vt-kbd { display: inline-flex; align-items: center; padding: 0.08rem 0.32rem; border-radius: 0.35rem; border: 1px solid var(--border-default); background: var(--bg-tertiary); font-size: 0.68rem; }
/* === Chips / pills (Lumina format-conversion style) === */
.vt-chip-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; align-items: center; }
.vt-chip { display: inline-flex; align-items: center; padding: 4px 8px; border-radius: 6px; font-size: 12px; font-weight: 500; letter-spacing: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace !important; background: rgba(0,0,0,0.05); color: var(--text-secondary); border: 0.5px solid var(--border-default); }
.vt-chip--accent { background: rgba(0,88,188,0.10); color: var(--m-primary); border-color: rgba(0,88,188,0.20); }
.vt-chip--tertiary { background: rgba(138,43,185,0.10); color: var(--m-tertiary); border-color: rgba(138,43,185,0.20); }
.vt-chip--warn { background: rgba(210,153,34,0.10); color: var(--warning); border-color: rgba(210,153,34,0.25); }
.vt-chip--mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace !important; }
.vt-chip-arrow { color: var(--text-muted); font-size: 14px; }

/* === Tab hero banner (kept, restyled to Lumina) === */
.vt-tab-hero { border-radius: 16px; padding: 16px 20px; margin-bottom: 16px; border: 0.5px solid var(--glass-border); background: var(--glass-bg); backdrop-filter: blur(20px) saturate(180%); -webkit-backdrop-filter: blur(20px) saturate(180%); box-shadow: var(--card-shadow); }
.vt-tab-hero__title { margin: 0; font-size: 18px; font-weight: 600; letter-spacing: -0.01em; color: var(--text-primary); }
.vt-tab-hero__sub { margin: 4px 0 0; font-size: 13px; color: var(--text-secondary); }
.vt-tab-hero--light, .vt-tab-hero--dark { /* unified now */ }
.vt-kbd { display: inline-flex; align-items: center; padding: 2px 6px; border-radius: 6px; border: 0.5px solid var(--border-default); background: rgba(0,0,0,0.05); font-size: 11px; font-family: ui-monospace, monospace; color: var(--text-secondary); }

/* keep mute card centered */
.st-key-mute_card > div { max-width: 760px; margin: 0 auto; }

/* === Loading overlay — Streamlit 기본 블러 대체 === */
@keyframes vt-spin { to { transform: rotate(360deg); } }
@keyframes vt-fade-in { from { opacity: 0; } to { opacity: 1; } }

/* 기본 상태 위젯 숨기기 */
[data-testid="stStatusWidget"] {
    visibility: hidden !important;
    height: 0 !important;
    overflow: hidden !important;
    position: absolute !important;
}

/* 로딩 배경 오버레이 */
.stApp:has([data-testid="stStatusWidget"]:not(:empty))::before {
    content: "";
    position: fixed;
    inset: 0;
    background: rgba(252, 248, 251, 0.55);
    z-index: 9990;
    pointer-events: none;
    animation: vt-fade-in 120ms ease-out;
}
[data-theme="dark"] .stApp:has([data-testid="stStatusWidget"]:not(:empty))::before {
    background: rgba(15, 23, 42, 0.55);
}

/* 중앙 스피너 */
.stApp:has([data-testid="stStatusWidget"]:not(:empty))::after {
    content: "";
    position: fixed;
    top: 50%;
    left: 50%;
    width: 40px;
    height: 40px;
    margin: -44px 0 0 -20px;
    border: 3px solid rgba(0, 88, 188, 0.15);
    border-top-color: var(--m-primary);
    border-radius: 50%;
    animation: vt-spin 0.65s linear infinite;
    z-index: 9991;
    pointer-events: none;
}
[data-theme="dark"] .stApp:has([data-testid="stStatusWidget"]:not(:empty))::after {
    border-color: rgba(173, 198, 255, 0.15);
    border-top-color: var(--m-primary);
}

/* 스피너 아래 "Loading" 텍스트 */
.stApp:has([data-testid="stStatusWidget"]:not(:empty)) .main .block-container::before {
    content: "Loading\2026";
    position: fixed;
    top: calc(50% + 12px);
    left: 50%;
    transform: translateX(-50%);
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
    letter-spacing: 0.02em;
    z-index: 9991;
    pointer-events: none;
    animation: vt-fade-in 180ms ease-out;
}

/* st.spinner 스타일 강화 */
.stSpinner > div {
    border-radius: 12px;
    padding: 12px 20px !important;
    background: var(--glass-bg-strong) !important;
    border: 0.5px solid var(--glass-border) !important;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    box-shadow: var(--card-shadow) !important;
}
</style>
"""

st.html(get_app_css())


# 공통 함수들
def open_folder_dialog(scan=True):
    """플랫폼별 폴더 선택 다이얼로그 (macOS는 osascript, 기타는 수동 입력)

    scan=False면 소스 폴더로 채택하지 않고 경로만 반환한다(저장 위치 선택용).
    """
    import sys

    # macOS에서만 osascript 사용
    if sys.platform == 'darwin':
        try:
            prompt = "Select folder containing video files" if scan else "Select save folder"
            apple_script = f'''
            tell application "System Events"
                activate
                set selectedFolder to choose folder with prompt "{prompt}"
                return POSIX path of selectedFolder
            end tell
            '''

            result = subprocess.run(
                ['osascript', '-e', apple_script],
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                folder_selected = result.stdout.strip()
                if folder_selected and os.path.exists(folder_selected):
                    if scan:
                        st.session_state['selected_folder_path'] = folder_selected
                        scan_folder_files(folder_selected)
                    return folder_selected
            return None
        except Exception as e:
            st.error(f"Folder selection error: {e}")
            return None
    else:
        # macOS가 아닌 환경에서는 수동 입력 사용
        st.info("💡 Non-macOS environment: Please enter the folder path manually below.")
        return None

def open_file_dialog():
    """파일 선택 다이얼로그 (macOS 전용)"""
    import sys

    if sys.platform == 'darwin':
        try:
            apple_script = '''
            tell application "System Events"
                activate
                set selectedFiles to choose file with prompt "Select video files" with multiple selections allowed
                set filePaths to {}
                repeat with aFile in selectedFiles
                    set end of filePaths to POSIX path of aFile
                end repeat
                return filePaths
            end tell
            '''

            result = subprocess.run(
                ['osascript', '-e', apple_script],
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                files_str = result.stdout.strip()
                if files_str:
                    file_paths = [f.strip() for f in files_str.split(', ') if f.strip()]

                    # 비디오 파일만 필터링
                    video_extensions = ['.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg', '.3gp']
                    valid_files = [f for f in file_paths if any(f.lower().endswith(ext) for ext in video_extensions) and os.path.exists(f)]

                    if valid_files:
                        st.session_state['video_files_list'] = valid_files
                        st.session_state['file_selection_state'] = {file: True for file in valid_files}
                        st.session_state['selected_folder_path'] = os.path.dirname(valid_files[0])
                        meta = {}
                        for f in valid_files:
                            try:
                                stat = os.stat(f)
                                meta[f] = {'name': os.path.basename(f), 'size': stat.st_size, 'date': stat.st_mtime}
                            except OSError:
                                meta[f] = {'name': os.path.basename(f), 'size': 0, 'date': 0}
                        st.session_state['file_meta_cache'] = meta
                        return valid_files
            return None
        except Exception as e:
            st.error(f"File selection error: {e}")
            return None
    return None

# 섹션 전환에도 값을 유지해야 하는 위젯 key.
# Streamlit은 '이번 실행에서 만들어지지 않은' 위젯의 state를 정리한다. 활성 섹션만
# 렌더하기 때문에, 따로 보관하지 않으면 섹션을 옮길 때마다 그 섹션의 입력값과
# 설정이 초기화된다. (파일/큐 체크박스는 file_selection_state·yt_queue_selection
# 에서 스스로 복구하므로 여기 넣지 않는다.)
_PINNED_WIDGET_KEYS = frozenset({
    'mute_input_source', 'mute_output_name',
    'download_only_checkbox', 'yt_delete_original',
    'yt_codec', 'yt_resolution', 'yt_quality', 'yt_fps', 'yt_scan',
    'yt_crf_slider', 'yt_custom_vbr', 'yt_custom_abr',
    'vc_codec', 'vc_resolution', 'vc_quality', 'vc_fps', 'vc_scan',
    'vc_crf', 'vc_custom_vbr', 'vc_custom_abr',
})
_PINNED_WIDGET_PREFIXES = ('yt_url_row_',)


def _is_pinned_key(key):
    return key in _PINNED_WIDGET_KEYS or key.startswith(_PINNED_WIDGET_PREFIXES)


def restore_pinned_widgets():
    """보관해둔 위젯 값을 복원한다. 위젯 생성 전에 호출해야 한다."""
    for key, value in st.session_state.setdefault('_pinned', {}).items():
        if key not in st.session_state:
            st.session_state[key] = value


def remember_pinned_widgets():
    """이번 실행에 존재하는 위젯 값을 보관한다. 스크립트 마지막에 호출한다."""
    stash = st.session_state.setdefault('_pinned', {})
    for key in list(st.session_state.keys()):
        if _is_pinned_key(key):
            stash[key] = st.session_state[key]


def _url_row_key(row_id):
    """URL 입력 행의 위젯 key. 행 id 기준이라 행을 지워도 다른 행 값이 밀리지 않는다."""
    return f"yt_url_row_{row_id}"


def _add_url_row():
    """URL 입력 행 추가 (on_click 콜백 — 본문 재실행 전에 상태가 확정된다)."""
    st.session_state['yt_url_rows'].append(st.session_state['yt_url_row_seq'])
    st.session_state['yt_url_row_seq'] += 1


def _remove_url_row(row_id):
    """URL 입력 행 제거. 마지막 한 행은 남긴다."""
    rows = st.session_state['yt_url_rows']
    if len(rows) > 1 and row_id in rows:
        rows.remove(row_id)
        # 콜백은 위젯 생성 전에 실행되므로 여기서 key를 지우는 것은 안전하다.
        st.session_state.pop(_url_row_key(row_id), None)


def _reset_url_rows():
    """입력 행을 빈 한 행으로 되돌린다.

    기존 key를 지우지 않고 '새 id'를 발급한다. 이미 생성된 위젯의 session_state를
    수정하면 Streamlit이 예외를 던지기 때문이다(이 함수는 본문에서 호출된다).
    """
    st.session_state['yt_url_rows'] = [st.session_state['yt_url_row_seq']]
    st.session_state['yt_url_row_seq'] += 1


def _collect_url_rows():
    """입력 행에서 URL 목록을 모은다(입력 순서 유지, 중복 제거).

    한 행에 공백/줄바꿈/콤마로 여러 링크를 붙여넣어도 분리한다.
    """
    urls, seen = [], set()
    for row_id in st.session_state['yt_url_rows']:
        raw = (st.session_state.get(_url_row_key(row_id)) or '').strip()
        if not raw:
            continue
        for token in re.split(r'[\s,]+', raw):
            token = token.strip()
            if token and token not in seen:
                seen.add(token)
                urls.append(token)
    return urls


def add_urls_to_queue(urls, settings):
    """여러 URL의 제목을 병렬 조회해 큐에 추가한다.

    제목 조회는 URL당 한 번의 네트워크 왕복이므로 순차로 하면 개수에 비례해 느려진다.
    스레드로 동시에 조회한다(_fetch_video_title은 캐시에 락이 있어 스레드 안전).
    반환: (added, skipped, failed) — 각각 URL 리스트.
    """
    existing = {item['url'] for item in st.session_state['yt_queue']}
    todo = [u for u in urls if u not in existing]
    skipped = [u for u in urls if u in existing]
    if not todo:
        return [], skipped, []

    yt_dlp_path = st.session_state['yt_downloader']._get_yt_dlp_path()
    fetched = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
        futures = {pool.submit(_fetch_video_title, yt_dlp_path, u): u for u in todo}
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            try:
                fetched[url] = future.result()
            except Exception as e:
                print(f"제목 조회 실패 {url}: {e}")
                fetched[url] = None

    added, failed = [], []
    for url in todo:  # 입력 순서대로 큐에 넣는다
        info = fetched.get(url)
        if not info:
            failed.append(url)
            continue
        st.session_state['yt_queue'].append({
            'url': url, 'info': info, 'settings': dict(settings),
        })
        st.session_state['yt_queue_selection'][url] = True
        st.session_state[_yt_check_key(url)] = True
        added.append(url)
    return added, skipped, failed


def _toggle_all_queue(new_state):
    """유튜브 큐 일괄 선택/해제. 체크박스 위젯 state를 직접 갱신한다."""
    for item in st.session_state['yt_queue']:
        st.session_state['yt_queue_selection'][item['url']] = new_state
        st.session_state[_yt_check_key(item['url'])] = new_state


def _apply_sort(column):
    """정렬 열 토글. on_click 콜백이므로 본문 재실행 전에 상태가 확정된다."""
    if st.session_state['sort_by'] == column:
        st.session_state['sort_order'] = 'desc' if st.session_state['sort_order'] == 'asc' else 'asc'
    else:
        st.session_state['sort_by'] = column
        st.session_state['sort_order'] = 'asc' if column == 'name' else 'desc'


def _toggle_all_files(new_state):
    """파일 목록 일괄 선택/해제. 체크박스 위젯 state를 직접 갱신한다."""
    for vf in st.session_state['video_files_list']:
        st.session_state['file_selection_state'][vf] = new_state
        st.session_state[_file_check_key(vf)] = new_state


def scan_folder_files(folder_path):
    """선택된 폴더에서 비디오 파일 스캔 + 메타데이터 캐싱"""
    video_extensions = {'.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg', '.3gp'}
    video_files = []
    file_meta_cache = {}
    
    try:
        for file in os.listdir(folder_path):
            if os.path.splitext(file)[1].lower() in video_extensions:
                full_path = os.path.join(folder_path, file)
                video_files.append(full_path)
                try:
                    stat = os.stat(full_path)
                    file_meta_cache[full_path] = {
                        'name': file,
                        'size': stat.st_size,
                        'date': stat.st_mtime,
                    }
                except OSError:
                    file_meta_cache[full_path] = {
                        'name': file, 'size': 0, 'date': 0,
                    }
        
        st.session_state['video_files_list'] = video_files
        st.session_state['file_selection_state'] = {file: True for file in video_files}
        st.session_state['file_meta_cache'] = file_meta_cache
        
    except Exception as e:
        st.error(f"Folder scan error: {e}")

def render_op_result():
    """직전 작업 결과를 렌더하고 소비한다(한 번만 표시)."""
    result = st.session_state.get('op_result')
    if not result:
        return
    st.session_state['op_result'] = None

    if result['status'] == 'success':
        st.success(result['summary'])
    elif result['status'] == 'warning':
        st.warning(result['summary'])
    else:
        st.error(result['summary'])

    if result['log']:
        with st.expander(f"자세히 ({len(result['log'])}건)", expanded=False):
            for line in result['log']:
                st.markdown(f"- {line}")


@st.fragment(run_every="0.5s")
def render_job_panel():
    """진행 중인 작업 패널. 0.5초마다 이 프래그먼트만 다시 그린다.

    프래그먼트라서 폴링이 페이지 전체를 다시 그리지 않는다. 실제 작업은
    워커 스레드에서 돌기 때문에 이 동안에도 섹션 전환·설정 변경이 자유롭다.
    """
    job = st.session_state.get('job')
    if job is None:
        return

    snap = job.snapshot()

    with st.container(key="job_panel"):
        head, stop_col = st.columns([5, 1])
        with head:
            st.markdown(f'<p class="vt-job__status">{_esc(snap["status"])}</p>',
                        unsafe_allow_html=True)
        with stop_col:
            if st.button("■ Stop", key="job_stop_btn", use_container_width=True,
                         disabled=snap['done'] or snap['cancelled']):
                # 플래그만 세우고 실행 중인 프로세스를 종료시킨다(둘 다 st.* 미사용)
                job.cancel.set()
                st.session_state['converter'].stop_conversion()
                st.session_state['yt_downloader'].stop_download()
                st.rerun(scope="fragment")

        st.progress(min(max(snap['overall'], 0.0), 1.0))
        if snap['cancelled'] and not snap['done']:
            st.caption("중단 중입니다...")
        elif snap['detail']:
            st.caption(snap['detail'])

        if snap['log']:
            with st.expander(f"진행 로그 ({len(snap['log'])}건)", expanded=False):
                for line in snap['log'][-30:]:
                    st.markdown(f"- {line}")

    if snap['done']:
        # 결과를 세션에 넘기고 전체 rerun으로 idle 상태로 복귀한다.
        st.session_state['op_result'] = {
            'status': snap['result_status'],
            'summary': snap['result_summary'],
            'log': snap['log'],
        }
        st.session_state['job'] = None

        if snap['kind'] == 'youtube':
            # 처리된 항목만 큐에서 제거한다(중단된 경우 남은 항목은 큐에 유지).
            done_urls = set(snap['processed_urls'])
            st.session_state['yt_queue'] = [
                it for it in st.session_state['yt_queue'] if it['url'] not in done_urls
            ]
            for url in done_urls:
                st.session_state['yt_queue_selection'].pop(url, None)
                st.session_state.pop(_yt_check_key(url), None)

        st.rerun()


def resolve_writable_save_path(configured_path):
    """저장 경로가 유효/쓰기 가능한지 확인하고 안전한 경로를 반환"""
    fallback_path = os.path.join(os.path.expanduser("~"), "Downloads")
    target_path = configured_path or fallback_path

    def _is_writable(path):
        try:
            os.makedirs(path, exist_ok=True)
            probe_file = os.path.join(path, ".videotool_write_probe")
            with open(probe_file, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe_file)
            return True
        except Exception:
            return False

    if _is_writable(target_path):
        return target_path, None

    os.makedirs(fallback_path, exist_ok=True)
    return fallback_path, f"⚠️ Save path is not writable: `{target_path}`. Switched to `{fallback_path}`."

# 테마 적용 스크립트 — 1회 적용, MutationObserver/setTimeout 제거
theme_html = f"""
<script>
(function() {{
    var theme = '{st.session_state['theme_mode']}';
    var targets = [
        window.parent.document.documentElement,
        window.parent.document.body,
        window.parent.document.querySelector('[data-testid="stApp"]')
    ];
    for (var i = 0; i < targets.length; i++) {{
        if (targets[i]) targets[i].setAttribute('data-theme', theme);
    }}
}})();
</script>
"""
components.html(theme_html, height=0)

# 섹션 전환으로 사라진 위젯 값을 되돌린다. 첫 위젯(테마 토글)보다 먼저 실행해야 한다.
restore_pinned_widgets()

# 작업 진행 여부. 진행 중에는 rerun을 유발하는 컨트롤(섹션 전환, 테마 토글)을 잠근다.
# 작업 중 rerun이 걸리면 실행 중인 스크립트 런이 끊겨 ffmpeg 프로세스가 고아가 되고
# 변환이 중복 실행된다. 예전 코드는 "탭을 전환하지 마세요"라는 경고문으로 때웠다.
APP_BUSY = st.session_state.get('job') is not None

# 테마 토글 버튼 (스타일은 통합 CSS에 정의됨)
theme_icon = "☀" if st.session_state['theme_mode'] == 'dark' else "☾"
if st.button(theme_icon, key="theme_toggle", help="Toggle Light/Dark Mode"):
    new_mode = 'dark' if st.session_state['theme_mode'] == 'light' else 'light'
    st.session_state['theme_mode'] = new_mode
    st.toast(f"{'Dark' if new_mode == 'dark' else 'Light'} mode", icon="☀️" if new_mode == 'light' else "🌙")
    st.rerun()

# Lumina TopNavBar (decorative — segmented control below handles real navigation)
st.html(f'''
<div class="vt-topnav">
    <div class="vt-topnav__brand">
        <span class="vt-topnav__brand-mark"><span class="vt-topnav__logo-emoji">🎥</span></span>
        <span>{APP_NAME}</span>
        <span style="font-size:11px;font-weight:600;letter-spacing:0.04em;color:var(--text-secondary);padding:3px 8px;border-radius:9999px;background:var(--glass-bg-strong);border:0.5px solid var(--glass-border);margin-left:4px;">v{APP_VERSION}</span>
    </div>
</div>
''')

# 페이지 내 섹션 헤더 (Lumina headline)
st.html('''
<div class="vt-header">
    <div>
        <h1 class="vt-header__title">Video Workspace</h1>
        <p class="vt-header__subtitle">Convert local files, download from YouTube, or mute clips — all in one place.</p>
    </div>
    <div class="vt-header__badges">
        <span class="vt-header__badge">CRF MODE</span>
        <span class="vt-header__badge">HW ACCEL</span>
        <span class="vt-header__badge">AUTO UPDATE</span>
    </div>
</div>
''')

# --- 옵션 딕셔너리 (모듈 스코프) ---
# 주의: 활성 섹션만 렌더하므로 이 딕셔너리들이 특정 탭 블록 안에 있으면
#       다른 탭에서 NameError가 난다. 반드시 탭 밖에 유지할 것.
codec_options = {
    "h264": "H.264 - Universal",
    "h265": "H.265 - High Compression",
    "vp9": "VP9 - Web Optimized",
    "av1": "AV1 - Next-Gen Efficiency"
}
resolution_options = {
    "original": "Keep Original",
    "4k": "4K (3840x2160)",
    "1440p": "QHD (2560x1440)",
    "1080p": "Full HD (1920x1080)",
    "720p": "HD (1280x720)",
    "480p": "SD (854x480)"
}
quality_presets = {
    "fast": "Fast Conversion (Normal Quality)",
    "balanced": "Balanced (Recommended)",
    "high": "High Quality (Slow Conversion)",
    "crf": "CRF Mode (Constant Quality)",
    "custom": "Custom Bitrate"
}
fps_options = {
    "original": "Keep Original",
    "23.976": "23.976 fps (Film)",
    "24": "24 fps (Cinema)",
    "25": "25 fps (PAL)",
    "29.97": "29.97 fps (NTSC)",
    "30": "30 fps",
    "50": "50 fps",
    "59.94": "59.94 fps",
    "60": "60 fps"
}
scan_options = {
    "progressive": "Progressive",
    "interlaced": "Interlaced"
}

# --- 섹션 네비게이션 ---
# st.tabs를 쓰지 않는 이유: st.tabs의 선택 상태는 프론트엔드 전용이라
# st.rerun()이 발생하면 항상 첫 탭으로 되돌아간다(탭이 튀는 원인).
# 위젯 key를 가진 segmented_control은 값이 session_state에 남아 rerun에도 유지된다.
if 'nav_section' not in st.session_state:
    st.session_state['nav_section'] = st.session_state.get('active_tab', TAB_CONVERT)

# st.radio를 쓰는 이유(segmented_control 대신): 항상 하나가 선택되어 해제 상태가 없고,
# streamlit.testing(AppTest)이 지원하므로 '탭이 튀는' 회귀를 자동 테스트할 수 있다.
# 외형은 CSS(.st-key-nav_section)로 세그먼트 pill과 동일하게 만든다.
active_tab = st.radio(
    "Section",
    TAB_LABELS,
    key="nav_section",
    horizontal=True,
    label_visibility="collapsed",
)
st.session_state['active_tab'] = active_tab

# 진행 중인 작업 패널 — 모든 섹션 위에 표시해서 섹션을 옮겨도 진행 상황이 보인다.
# 작업이 없을 때는 프래그먼트를 아예 만들지 않아 불필요한 폴링이 없다.
if APP_BUSY:
    render_job_panel()

# 직전 작업 결과를 표시한다(작업 종료 시 rerun하므로 여기서 렌더된다).
render_op_result()


# Tab 1: Local File Conversion
if active_tab == TAB_CONVERT:
    st.html("""
    <div class="vt-tab-hero">
        <p class="vt-tab-hero__title">Local Conversion Workspace</p>
        <p class="vt-tab-hero__sub">폴더/파일 선택 → 정렬/선택 → 일괄 변환 흐름으로 작동합니다.</p>
    </div>
    """)

    # --- Stats cards ---
    _files = st.session_state['video_files_list']
    _meta = st.session_state.get('file_meta_cache', {})
    _sel_count = sum(1 for f in _files if st.session_state['file_selection_state'].get(f, True)) if _files else 0
    _total_mb = sum(_meta.get(f, {}).get('size', 0) / (1024*1024) for f in _files) if _files else 0
    if _total_mb < 1024:
        _size_num, _size_unit = f"{_total_mb:.1f}", "MB"
    else:
        _size_num, _size_unit = f"{_total_mb/1024:.1f}", "GB"
    st.html(f'''
    <div class="vt-stats-row">
        <div class="vt-stats-card">
            <div class="vt-stats-card__head"><span class="material-symbols-outlined">queue_play_next</span><span class="vt-stats-card__label">Queue Total</span></div>
            <div class="vt-stats-card__value">{len(_files)}<span class="vt-stats-card__unit">Files</span></div>
        </div>
        <div class="vt-stats-card">
            <div class="vt-stats-card__head"><span class="material-symbols-outlined">check_circle</span><span class="vt-stats-card__label">Selected</span></div>
            <div class="vt-stats-card__value vt-stats-card__value--accent">{_sel_count}<span class="vt-stats-card__unit">Ready</span></div>
        </div>
        <div class="vt-stats-card">
            <div class="vt-stats-card__head"><span class="material-symbols-outlined">data_usage</span><span class="vt-stats-card__label">Total Size</span></div>
            <div class="vt-stats-card__value">{_size_num}<span class="vt-stats-card__unit">{_size_unit}</span></div>
        </div>
    </div>
    ''')

    # --- Pre-init variables ---
    selected_files = []
    custom_video_br = None
    custom_audio_br = None

    # --- 2-column layout ---
    left_col, right_col = st.columns([2, 3])

    with left_col:
        st.markdown('<p class="vt-section-label">CONFIGURATION</p>', unsafe_allow_html=True)

        with st.container(key="config_panel"):
            selected_codec = st.selectbox(
                "Codec", options=list(codec_options.keys()),
                format_func=lambda x: codec_options[x], index=0, key="vc_codec"
            )
            selected_resolution = st.selectbox(
                "Resolution", options=list(resolution_options.keys()),
                format_func=lambda x: resolution_options[x], index=0, key="vc_resolution"
            )
            selected_quality = st.selectbox(
                "Quality", options=list(quality_presets.keys()),
                format_func=lambda x: quality_presets[x], index=1, key="vc_quality"
            )

            if selected_quality == "crf":
                crf_defaults = {"h264": 23, "h265": 28, "vp9": 31, "av1": 30}
                custom_video_br = st.slider(
                    "CRF Value (lower = better quality):",
                    min_value=0, max_value=51,
                    value=crf_defaults.get(selected_codec, 23), step=1, key="vc_crf"
                )

            with st.expander("Advanced Settings"):
                selected_fps = st.selectbox(
                    "Frame Rate", options=list(fps_options.keys()),
                    format_func=lambda x: fps_options[x], index=0, key="vc_fps"
                )
                selected_scan = st.selectbox(
                    "Scan Type", options=list(scan_options.keys()),
                    format_func=lambda x: scan_options[x], index=0, key="vc_scan"
                )

                if selected_quality == "custom":
                    custom_video_br = st.number_input(
                        "Video Bitrate (Mbps):", min_value=1, max_value=100, value=10, step=1,
                        key="vc_custom_vbr"
                    )
                    custom_audio_br = st.number_input(
                        "Audio Bitrate (kbps):", min_value=64, max_value=320, value=192, step=32,
                        key="vc_custom_abr"
                    )

        # st.expander의 본문은 접혀 있어도 항상 실행되므로 selected_fps/selected_scan은
        # 언제나 정의된다. 기존의 `if 'x' not in dir():` 폴백은 동작하지 않는 죽은 코드여서 제거했다.

        st.markdown('<p class="vt-section-label">FILE SOURCE</p>', unsafe_allow_html=True)
        with st.container(key="file_source"):
            import sys
            current_path = st.session_state.get('selected_folder_path', 'No files or folder selected')
            st.text_input("Location:", value=current_path, disabled=True, label_visibility="collapsed")

            if sys.platform == 'darwin':
                fc1, fc2 = st.columns(2)
                with fc1:
                    if st.button("Select Folder", use_container_width=True, key="btn_folder"):
                        with st.spinner("📂 Waiting for folder selection..."):
                            selected = open_folder_dialog()
                        if selected:
                            file_count = len(st.session_state.get('video_files_list', []))
                            st.toast(f"📂 {file_count} video files loaded", icon="✅")
                            st.rerun()
                with fc2:
                    if st.button("Select Files", use_container_width=True, key="btn_files"):
                        with st.spinner("📂 Waiting for file selection..."):
                            selected = open_file_dialog()
                        if selected:
                            st.toast(f"📂 {len(selected)} files selected", icon="✅")
                            st.rerun()
            else:
                manual_path = st.text_input(
                    "Folder path:", value=st.session_state.get('selected_folder_path', ''),
                    placeholder="e.g., C:\\Users\\user\\Videos"
                )
                if manual_path and manual_path != st.session_state.get('selected_folder_path', ''):
                    if os.path.exists(manual_path) and os.path.isdir(manual_path):
                        st.session_state['selected_folder_path'] = manual_path
                        scan_folder_files(manual_path)
                        st.rerun()
                    else:
                        st.error("❌ Invalid folder path.")

        # 시작 버튼. 중단은 전역 작업 패널의 Stop 하나로 통일했다.
        if st.session_state['video_files_list']:
            start_conversion = st.button(
                "▶ Start Conversion", type="primary", use_container_width=True,
                disabled=APP_BUSY, key="start_conversion_btn",
            )
        else:
            start_conversion = False

    with right_col:
        st.markdown('<p class="vt-section-label">PROJECT FILES</p>', unsafe_allow_html=True)

        @st.fragment
        def render_file_queue():
            """파일 큐 패널 — fragment로 분리하여 체크박스 토글 시 전체 페이지 rerun 방지"""
            with st.container(key="queue_panel"):
                if st.session_state['video_files_list']:
                    st.success(f"📹 {len(st.session_state['video_files_list'])} video files")

                    _meta = st.session_state.get('file_meta_cache', {})
                    file_info_list = []
                    for video_file in st.session_state['video_files_list']:
                        cached = _meta.get(video_file)
                        if cached:
                            file_info_list.append({'path': video_file, **cached})
                        else:
                            file_info_list.append({
                                'path': video_file,
                                'name': os.path.basename(video_file),
                                'size': os.path.getsize(video_file) if os.path.exists(video_file) else 0,
                                'date': os.path.getmtime(video_file) if os.path.exists(video_file) else 0,
                            })

                    reverse = (st.session_state['sort_order'] == 'desc')
                    if st.session_state['sort_by'] == 'name':
                        file_info_list.sort(key=lambda x: x['name'].lower(), reverse=reverse)
                    elif st.session_state['sort_by'] == 'date':
                        file_info_list.sort(key=lambda x: x['date'], reverse=reverse)
                    elif st.session_state['sort_by'] == 'size':
                        file_info_list.sort(key=lambda x: x['size'], reverse=reverse)

                    name_arrow = (" ↑" if st.session_state['sort_order'] == 'asc' else " ↓") if st.session_state['sort_by'] == 'name' else " ↕"
                    size_arrow = (" ↑" if st.session_state['sort_order'] == 'asc' else " ↓") if st.session_state['sort_by'] == 'size' else " ↕"
                    date_arrow = (" ↑" if st.session_state['sort_order'] == 'asc' else " ↓") if st.session_state['sort_by'] == 'date' else " ↕"

                    # 정렬/일괄선택은 on_click 콜백으로 처리한다.
                    # 콜백은 재실행 '전에' 실행되므로 st.rerun()을 부를 필요가 없고
                    # (예전 코드의 st.rerun()은 fragment 밖 전체 rerun을 유발했다),
                    # 같은 실행에서 정렬 결과와 화살표 표시가 일치한다.
                    col_check_h, col_meta_h = st.columns([0.5, 5.5])
                    with col_check_h:
                        all_selected = all(st.session_state['file_selection_state'].get(f, True) for f in st.session_state['video_files_list'])
                        st.button(
                            "☑ All" if all_selected else "☐ All",
                            key="toggle_all_header", use_container_width=True,
                            on_click=_toggle_all_files, args=(not all_selected,),
                        )
                    with col_meta_h:
                        h_name, h_size, h_date = st.columns([3, 1, 1.5])
                        with h_name:
                            st.button(f"File Name{name_arrow}", key="sort_name", use_container_width=True,
                                      on_click=_apply_sort, args=("name",))
                        with h_size:
                            st.button(f"Size{size_arrow}", key="sort_size", use_container_width=True,
                                      on_click=_apply_sort, args=("size",))
                        with h_date:
                            st.button(f"Modified{date_arrow}", key="sort_date", use_container_width=True,
                                      on_click=_apply_sort, args=("date",))

                    # 파일당 위젯 수를 줄인다(기존 columns4+checkbox+markdown3 → columns2+checkbox+html1).
                    # 파일 수가 많을 때 렌더/전송 비용이 지배적이므로 이 축소가 체감 반응성에 직결된다.
                    for file_info in file_info_list:
                        video_file = file_info['path']
                        file_size_mb = file_info['size'] / (1024 * 1024)
                        file_date = datetime.fromtimestamp(file_info['date']).strftime('%Y-%m-%d %H:%M')

                        col_check, col_meta = st.columns([0.5, 5.5])
                        with col_check:
                            file_key = _file_check_key(video_file)
                            if file_key not in st.session_state:
                                st.session_state[file_key] = st.session_state['file_selection_state'].get(video_file, True)
                            is_selected = st.checkbox("✓", key=file_key, label_visibility="collapsed")
                            st.session_state['file_selection_state'][video_file] = is_selected
                        with col_meta:
                            st.html(
                                f'<div class="vt-file-row">'
                                f'<span class="vt-file-row__name" title="{_esc(video_file)}">📄 {_esc(file_info["name"])}</span>'
                                f'<span class="vt-file-row__size">{file_size_mb:.1f} MB</span>'
                                f'<span class="vt-file-row__date">{file_date}</span>'
                                f'</div>'
                            )
                else:
                    st.info("📂 Select a folder or files to begin")

        render_file_queue()

        selected_files = [f for f in st.session_state['video_files_list']
                          if st.session_state['file_selection_state'].get(f, True)]

    # --- 변환 시작 (백그라운드 스레드) ---
    if start_conversion and not APP_BUSY and selected_files:
        _output_path = os.path.join(
            st.session_state.get('selected_folder_path') or os.getcwd(),
            f"converted_{selected_codec}",
        )
        os.makedirs(_output_path, exist_ok=True)
        _job = BackgroundJob('convert', len(selected_files),
                             label=f"로컬 변환 {len(selected_files)}건")
        start_background_job(_job, _worker_convert, (
            st.session_state['converter'],
            list(selected_files),
            _output_path,
            {
                'codec': selected_codec, 'resolution': selected_resolution,
                'quality': selected_quality, 'fps': selected_fps,
                'scan': selected_scan, 'custom_video_br': custom_video_br,
                'custom_audio_br': custom_audio_br,
            },
        ))
        st.toast(f"변환 시작 — {len(selected_files)}개 파일", icon="🎬")
        st.rerun()

    # --- Status bar ---
    _codec_display = selected_codec.upper()
    _res_display = selected_resolution.upper() if selected_resolution != "original" else "ORIGINAL"
    _quality_display = selected_quality.upper()
    _selected_count = len(selected_files)
    _run_state = "RUNNING" if APP_BUSY else "IDLE"
    st.html(f'''
    <div class="vt-status-bar">
        <div class="vt-status-bar__group">
            <span>State: <strong>{_run_state}</strong></span>
            <span>Selected: <strong>{_selected_count}</strong></span>
        </div>
        <div class="vt-status-bar__group">
            <span>Codec: <strong>{_codec_display}</strong></span>
            <span>Res: <strong>{_res_display}</strong></span>
            <span>Quality: <strong>{_quality_display}</strong></span>
        </div>
    </div>
    ''')


# 탭 2: 유튜브 다운로더
if active_tab == TAB_YOUTUBE:
    st.html("""
    <div class="vt-tab-hero">
        <p class="vt-tab-hero__title">YouTube Queue Console</p>
        <p class="vt-tab-hero__sub">URL을 추가하고 큐를 선택해 일괄 실행하세요. <span class="vt-kbd">Queue</span> <span class="vt-kbd">Batch</span></p>
    </div>
    """)
    # Stats cards
    _yt_queue = st.session_state['yt_queue']
    _yt_sel = sum(1 for item in _yt_queue if st.session_state['yt_queue_selection'].get(item['url'], True)) if _yt_queue else 0
    st.html(f'''
    <div class="vt-stats-row">
        <div class="vt-stats-card">
            <div class="vt-stats-card__head"><span class="material-symbols-outlined">playlist_play</span><span class="vt-stats-card__label">Queue Total</span></div>
            <div class="vt-stats-card__value">{len(_yt_queue)}<span class="vt-stats-card__unit">Items</span></div>
        </div>
        <div class="vt-stats-card">
            <div class="vt-stats-card__head"><span class="material-symbols-outlined">check_circle</span><span class="vt-stats-card__label">Selected</span></div>
            <div class="vt-stats-card__value vt-stats-card__value--accent">{_yt_sel}<span class="vt-stats-card__unit">Ready</span></div>
        </div>
    </div>
    ''')

    # Pre-init variables
    add_to_queue_btn = False
    yt_custom_video_br = None
    yt_custom_audio_br = None
    start_batch = False

    # 2-column layout (matches Tab 1)
    yt_left_col, yt_right_col = st.columns([2, 3])

    with yt_left_col:
        st.markdown('<p class="vt-section-label">DOWNLOAD SETTINGS</p>', unsafe_allow_html=True)

        with st.container(key="yt_config_panel"):
            # yt-dlp version info (캐싱 — 매 rerun subprocess 실행 방지)
            current_ver = _cached_yt_dlp_version(st.session_state['yt_downloader']._get_yt_dlp_path())
            ver_display = current_ver or 'Not found'
            st.markdown(f'<p style="font-size:0.8rem;color:var(--text-secondary);margin-bottom:0.5rem;">yt-dlp <strong>v{ver_display}</strong></p>', unsafe_allow_html=True)
            if st.button("Update yt-dlp", key="ytdlp_update_btn", use_container_width=True):
                with st.spinner("Updating yt-dlp..."):
                    success, output, new_ver = st.session_state['yt_downloader'].update_yt_dlp()
                    _cached_yt_dlp_version.clear()  # 버전 캐시 무효화
                    if success:
                        st.success(f"Updated to {new_ver}" if new_ver else "yt-dlp is up to date!")
                    else:
                        st.error(f"Update failed: {output}")

            download_only = st.checkbox(
                "Download Only (Skip Conversion)",
                value=False,
                key="download_only_checkbox",
                help="Download in original format without conversion"
            )

            if not download_only:
                yt_selected_codec = st.selectbox(
                    "Codec", options=list(codec_options.keys()),
                    format_func=lambda x: codec_options[x], index=0, key="yt_codec"
                )
                yt_selected_resolution = st.selectbox(
                    "Resolution", options=list(resolution_options.keys()),
                    format_func=lambda x: resolution_options[x], index=0, key="yt_resolution"
                )
                yt_selected_quality = st.selectbox(
                    "Quality", options=list(quality_presets.keys()),
                    format_func=lambda x: quality_presets[x], index=1, key="yt_quality"
                )

                if yt_selected_quality == "crf":
                    crf_defaults = {"h264": 23, "h265": 28, "vp9": 31, "av1": 30}
                    yt_custom_video_br = st.slider(
                        "CRF Value (lower = better quality):",
                        min_value=0, max_value=51,
                        value=crf_defaults.get(yt_selected_codec, 23), step=1, key="yt_crf_slider"
                    )

                with st.expander("Advanced Settings"):
                    yt_selected_fps = st.selectbox(
                        "Frame Rate", options=list(fps_options.keys()),
                        format_func=lambda x: fps_options[x], index=0, key="yt_fps"
                    )
                    yt_selected_scan = st.selectbox(
                        "Scan Type", options=list(scan_options.keys()),
                        format_func=lambda x: scan_options[x], index=0, key="yt_scan"
                    )
                    if yt_selected_quality == "custom":
                        yt_custom_video_br = st.number_input(
                            "Video Bitrate (Mbps):", min_value=1, max_value=100, value=10, step=1, key="yt_custom_vbr"
                        )
                        yt_custom_audio_br = st.number_input(
                            "Audio Bitrate (kbps):", min_value=64, max_value=320, value=192, step=32, key="yt_custom_abr"
                        )

                # 원본 삭제 여부는 반드시 변환 '전에' 물어본다.
                st.checkbox(
                    "변환 후 원본 다운로드 파일 삭제",
                    key="yt_delete_original",
                    help="끄면 다운로드된 원본과 변환 결과를 모두 보관합니다",
                )
            else:
                yt_selected_codec = "h264"
                yt_selected_resolution = "original"
                yt_selected_quality = "balanced"
                yt_selected_fps = "original"
                yt_selected_scan = "progressive"

        # (동작하지 않던 `if 'x' not in dir():` 폴백 제거 — 위 if/else 양쪽에서 항상 대입된다)

        st.markdown('<p class="vt-section-label">SAVE LOCATION</p>', unsafe_allow_html=True)
        with st.container(key="yt_save_panel"):
            st.text_input("Location:", value=st.session_state['yt_save_folder_path'], disabled=True, label_visibility="collapsed", key="yt_save_path_display")
            if st.button("Select Folder", key="yt_folder_select", use_container_width=True):
                # scan=False: 저장 위치 선택이므로 로컬 변환 탭의 소스 폴더를 덮어쓰지 않는다.
                selected = open_folder_dialog(scan=False)
                if selected:
                    st.session_state['yt_save_folder_path'] = selected
                    st.rerun()

        # (배치 다운로드 버튼은 큐 아래로 이동 — 링크 입력 → 큐 → 다운로드 흐름을 따른다)

    with yt_right_col:
        st.markdown('<p class="vt-section-label">DOWNLOAD QUEUE</p>', unsafe_allow_html=True)

        with st.container(key="yt_queue_panel"):
            # 직전 '큐 추가' 결과 알림 (한 번만 표시하고 소비)
            _notice = st.session_state.pop('yt_notice', None)
            if _notice:
                (st.warning if _notice['type'] == 'warning' else st.success)(_notice['msg'])

            # --- URL 입력: 여러 링크를 행으로 모은 뒤 한 번에 큐로 보낸다 ---
            _busy = APP_BUSY
            _rows = st.session_state['yt_url_rows']

            with st.container(key="yt_url_rows"):
                for _row_id in _rows:
                    _c_in, _c_rm = st.columns([12, 1])
                    with _c_in:
                        # label은 행 id로 고정한다. Streamlit 위젯 ID는 key뿐 아니라
                        # label도 반영하므로, 위치 번호(URL 1/2/3)를 쓰면 행을 삭제해
                        # 번호가 밀릴 때 위젯이 새로 만들어져 입력값이 사라진다.
                        st.text_input(
                            f"YouTube URL (row {_row_id})",
                            placeholder="https://www.youtube.com/watch?v=...",
                            key=_url_row_key(_row_id),
                            label_visibility="collapsed",
                            disabled=_busy,
                        )
                    with _c_rm:
                        st.button(
                            "✕", key=f"yt_url_rm_{_row_id}", help="이 링크 칸 삭제",
                            type="tertiary", disabled=_busy or len(_rows) == 1,
                            on_click=_remove_url_row, args=(_row_id,),
                        )

            _pending_n = len(_collect_url_rows())

            # '링크 칸 추가'와 'Add to Queue'를 한 줄에 둔다(왼쪽 보조 / 오른쪽 주 동작).
            # 이 줄은 yt_url_rows 컨테이너 밖에 있어야 한다 — 안에 있으면 ✕ 버튼용
            # CSS(.st-key-yt_url_rows div[column] button)에 같이 걸린다.
            _c_add, _c_queue = st.columns([1.4, 1])
            with _c_add:
                st.button(
                    "＋ 링크 칸 추가", key="yt_url_add_row", type="tertiary",
                    help="여러 영상을 한 번에 넣으려면 칸을 추가하세요 (한 칸에 여러 링크를 붙여넣어도 됩니다)",
                    disabled=_busy, on_click=_add_url_row,
                )
            with _c_queue:
                add_to_queue_btn = st.button(
                    f"＋ Add to Queue ({_pending_n})" if _pending_n else "＋ Add to Queue",
                    type="primary", use_container_width=True,
                    disabled=_pending_n == 0 or _busy,
                    key="yt_add_to_queue",
                )

            # Queue items
            if st.session_state['yt_queue']:
                all_selected = all(st.session_state['yt_queue_selection'].get(item['url'], True)
                                   for item in st.session_state['yt_queue'])
                # 너비도 위계다 — use_container_width를 주면 주 동작과 같은 폭이 되어
                # 크기를 줄인 효과가 사라진다. 내용 크기로만 잡는다.
                _q_title, _q_sel, _q_clr = st.columns([5, 1.15, 1])
                with _q_title:
                    st.markdown(
                        f'<p class="vt-queue-toolbar__title">QUEUE · {len(st.session_state["yt_queue"])}</p>',
                        unsafe_allow_html=True,
                    )
                with _q_sel:
                    st.button(
                        "전체 해제" if all_selected else "전체 선택",
                        key="yt_select_all", type="tertiary",
                        on_click=_toggle_all_queue, args=(not all_selected,),
                    )
                with _q_clr:
                    if st.button("큐 비우기", key="yt_clear_queue", type="tertiary"):
                        for _item in st.session_state['yt_queue']:
                            st.session_state.pop(_yt_check_key(_item['url']), None)
                        st.session_state['yt_queue'] = []
                        st.session_state['yt_queue_selection'] = {}
                        st.rerun()

                items_to_remove = []
                for idx, item in enumerate(st.session_state['yt_queue']):
                    url = item['url']
                    info = item['info']
                    settings = item['settings']

                    col_check, col_info, col_remove = st.columns([0.5, 5, 0.8])
                    with col_check:
                        # key를 인덱스가 아닌 URL 기반으로 고정한다. 인덱스 기반이면
                        # 항목을 삭제할 때 나머지 항목의 체크 상태가 한 칸씩 밀린다.
                        _chk_key = _yt_check_key(url)
                        if _chk_key not in st.session_state:
                            st.session_state[_chk_key] = st.session_state['yt_queue_selection'].get(url, True)
                        is_selected = st.checkbox("sel", key=_chk_key, label_visibility="collapsed")
                        st.session_state['yt_queue_selection'][url] = is_selected
                    with col_info:
                        st.markdown(f"**{info['title']}**")
                        info_parts = []
                        if info.get('uploader') and info['uploader'] != 'Unknown':
                            info_parts.append(info['uploader'])
                        if info.get('duration', 0) > 0:
                            dm = info['duration'] // 60
                            ds = info['duration'] % 60
                            info_parts.append(f"{dm:02d}:{ds:02d}")
                        if info.get('view_count', 0) > 0:
                            vc = info['view_count']
                            info_parts.append(f"{vc/1000000:.1f}M" if vc > 1000000 else f"{vc/1000:.1f}K" if vc > 1000 else str(vc))
                        if info_parts:
                            st.markdown(f'<p style="font-size:0.8rem;color:var(--text-secondary);margin:0;">{" | ".join(info_parts)}</p>', unsafe_allow_html=True)
                        if settings['download_only']:
                            st.markdown(
                                '<div class="vt-chip-row"><span class="vt-chip vt-chip--warn">DOWNLOAD ONLY</span></div>',
                                unsafe_allow_html=True
                            )
                        else:
                            codec_name = codec_options[settings['codec']].split(' - ')[0]
                            res_name = resolution_options[settings['resolution']]
                            st.markdown(
                                f'<div class="vt-chip-row">'
                                f'<span class="vt-chip vt-chip--accent">CONVERT</span>'
                                f'<span class="vt-chip vt-chip--mono">{codec_name}</span>'
                                f'<span class="vt-chip vt-chip--mono">{res_name}</span>'
                                f'</div>',
                                unsafe_allow_html=True
                            )
                    with col_remove:
                        if st.button("✕", key=f"yt_remove_{idx}", help="큐에서 제거",
                                     type="tertiary"):
                            items_to_remove.append(idx)

                if items_to_remove:
                    for idx in sorted(items_to_remove, reverse=True):
                        removed_url = st.session_state['yt_queue'][idx]['url']
                        st.session_state['yt_queue'].pop(idx)
                        st.session_state['yt_queue_selection'].pop(removed_url, None)
                        st.session_state.pop(_yt_check_key(removed_url), None)
                    st.rerun()
            else:
                st.info("위에 링크를 넣고 **Add to Queue**를 누르세요. 여러 개면 **＋ 링크 칸 추가**로 칸을 늘리거나, 한 칸에 여러 링크를 붙여넣어도 됩니다.")

        # --- 배치 다운로드 (큐 패널 아래) ---
        # 큐 패널 안에 두면 패널의 max-height/overflow 때문에 스크롤 밖으로 밀려
        # 주 동작이 안 보일 수 있다. 그래서 패널 바깥에 둔다.
        if st.session_state['yt_queue']:
            _sel_count = sum(1 for item in st.session_state['yt_queue']
                             if st.session_state['yt_queue_selection'].get(item['url'], True))
            start_batch = st.button(
                f"▶ Batch Download ({_sel_count})" if _sel_count else "▶ Batch Download",
                type="primary", use_container_width=True,
                disabled=APP_BUSY or _sel_count == 0,
                key="yt_start_batch",
            )

    # --- 배치 다운로드 시작 (백그라운드 스레드) ---
    if start_batch and not APP_BUSY:
        _items = [item for item in st.session_state['yt_queue']
                  if st.session_state['yt_queue_selection'].get(item['url'], True)]
        if _items:
            _save_path, _warn = resolve_writable_save_path(
                st.session_state.get('yt_save_folder_path'))
            st.session_state['yt_save_folder_path'] = _save_path
            if _warn:
                st.session_state['yt_notice'] = {'type': 'warning', 'msg': _warn}
            _job = BackgroundJob('youtube', len(_items),
                                 label=f"유튜브 {len(_items)}건")
            start_background_job(_job, _worker_youtube, (
                st.session_state['converter'],
                st.session_state['yt_downloader'],
                [dict(it) for it in _items],
                _save_path,
                st.session_state.get('yt_delete_original', True),
            ))
            st.toast(f"배치 다운로드 시작 — {len(_items)}건", icon="📥")
            st.rerun()

    # 링크 → 큐 추가. 'Download Now'는 제거했다 — 배치 다운로드와 역할이 같아
    # 두 버튼이 헷갈렸고, 링크가 여러 개일 때 '지금 다운로드'의 의미도 모호했다.
    # 흐름은 하나로 통일했다: 링크 입력 → Add to Queue → Batch Download.
    if add_to_queue_btn:
        urls = _collect_url_rows()
        if urls:
            queue_settings = {
                'codec': yt_selected_codec, 'resolution': yt_selected_resolution,
                'quality': yt_selected_quality, 'fps': yt_selected_fps,
                'scan': yt_selected_scan, 'custom_video_br': yt_custom_video_br,
                'custom_audio_br': yt_custom_audio_br, 'download_only': download_only,
            }
            with st.spinner(f"{len(urls)}개 링크 정보를 가져오는 중..."):
                added, skipped, failed = add_urls_to_queue(urls, queue_settings)

            notes = []
            if skipped:
                notes.append(f"이미 큐에 있어 건너뜀: {len(skipped)}개")
            if failed:
                notes.append(
                    f"정보를 가져올 수 없는 링크 {len(failed)}개 (URL을 확인하세요): "
                    + ", ".join(f"`{u}`" for u in failed)
                )

            if added:
                # 알림을 세션에 실어 보낸다. st.warning을 그린 뒤 st.rerun()하면
                # 그 메시지가 지워져 사용자가 건너뜀/실패를 볼 수 없다.
                st.session_state['yt_notice'] = {
                    'type': 'warning' if notes else 'success',
                    'msg': f"큐에 {len(added)}개 추가" + (" · " + " · ".join(notes) if notes else ""),
                }
                _reset_url_rows()
                st.rerun()
            else:
                # 추가된 게 없으면 rerun하지 않는다 — 입력을 그대로 남겨 사용자가
                # 고칠 수 있게 하고, 이유를 바로 보여준다.
                for note in notes:
                    st.warning(note)


# Tab 3: Mute Video (무음 비디오 생성)
if active_tab == TAB_MUTE:
    st.html("""
    <div class="vt-tab-hero">
        <p class="vt-tab-hero__title">Mute Track Utility</p>
        <p class="vt-tab-hero__sub">URL/로컬 입력 후 오디오 트랙만 제거해 빠르게 무음 비디오를 생성합니다.</p>
    </div>
    """)
    with st.container(key="mute_card"):
        st.html('''
        <h3 style="font-size: 1.17em; margin-bottom: 0.5rem;">
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display: inline-block; vertical-align: middle; margin-right: 8px;">
                <path d="M11 5L6 9H2v6h4l5 4V5z"></path>
                <line x1="23" y1="9" x2="17" y2="15"></line>
                <line x1="17" y1="9" x2="23" y2="15"></line>
            </svg>
            Mute Video Generator
        </h3>
        ''')

        st.html('''
        <div class="feature-box" style="margin-bottom: 1.5rem;">
            <div style="display: flex; align-items: flex-start; gap: 0.75rem;">
                <span style="font-size: 1.5rem; line-height: 1;">🔇</span>
                <div>
                    <h4 style="margin: 0 0 0.25rem 0; font-size: 0.95rem;">Remove Audio Track from Video</h4>
                    <ul style="font-size: 0.85rem; margin: 0; padding-left: 1.25rem;">
                        <li>Paste a direct video URL (S3, CDN, archive) or local file path</li>
                        <li>Video stream is copied without re-encoding — <b>extremely fast</b></li>
                        <li>Progress is shown for both local files and URLs</li>
                    </ul>
                </div>
            </div>
        </div>
        ''')

        mute_input = st.text_input(
            "Video URL or local file path:",
            placeholder="https://example.com/video.mp4",
            key="mute_input_source"
        )

        default_output_name = "output_muted.mp4"
        if mute_input:
            if mute_input.startswith(('http://', 'https://')):
                parsed_path = urllib.parse.urlparse(mute_input).path
                src_name = os.path.splitext(os.path.basename(urllib.parse.unquote(parsed_path)))[0]
            else:
                src_name = os.path.splitext(os.path.basename(mute_input))[0]
            if src_name:
                default_output_name = f"{src_name}_muted.mp4"

        # key를 가진 위젯은 session_state가 value= 인자를 이기므로, 소스가 바뀌었을 때만
        # 출력 파일명을 다시 채운다(사용자가 직접 고친 이름은 보존).
        if st.session_state.get('_mute_input_seen') != mute_input:
            st.session_state['_mute_input_seen'] = mute_input
            st.session_state['mute_output_name'] = default_output_name

        mute_output_name = st.text_input("Output filename:", key="mute_output_name")

        # 저장 위치를 다른 탭과 동일하게 선택 가능하게 한다(기존에는 ~/Downloads 하드코딩).
        mute_save_path = st.session_state['mute_save_folder_path']
        st.html(f'''
        <div style="display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; padding: 0.5rem 0.75rem; background-color: var(--bg-tertiary); border-radius: 0.375rem;">
            <span style="font-size: 0.85rem;">📁</span>
            <span style="font-size: 0.8rem; color: var(--text-secondary);">Save to: <b>{_esc(mute_save_path)}</b></span>
        </div>
        ''')
        if sys.platform == 'darwin':
            if st.button("Change Folder", key="mute_folder_select", use_container_width=True,
                         disabled=APP_BUSY):
                chosen = open_folder_dialog(scan=False)
                if chosen:
                    st.session_state['mute_save_folder_path'] = chosen
                    st.rerun()

        # 중단은 전역 작업 패널의 Stop 하나로 통일했다.
        mute_start = st.button(
            "🔇 Generate Muted Video", type="primary", key="mute_generate_btn",
            disabled=not mute_input or APP_BUSY, use_container_width=True,
        )

        if mute_start and mute_input:
            save_path, path_warning = resolve_writable_save_path(
                st.session_state['mute_save_folder_path'])
            st.session_state['mute_save_folder_path'] = save_path
            if path_warning:
                st.session_state['yt_notice'] = {'type': 'warning', 'msg': path_warning}
            _job = BackgroundJob('mute', 1, label=mute_output_name)
            start_background_job(_job, _worker_mute, (
                st.session_state['converter'],
                mute_input,
                os.path.join(save_path, mute_output_name),
            ))
            st.toast("무음 비디오 생성 시작", icon="🔇")
            st.rerun()

# 푸터
st.markdown("---")
st.html(f"""
<div style="text-align: center; color: var(--text-secondary); padding: 2rem;">
    <p>🎥 {APP_NAME} v{APP_VERSION} - Made by Channy</p>
    <p>Video conversion tool with batch YouTube download support.</p>
</div>
""")


# 이번 실행의 위젯 값을 보관한다. 섹션을 옮기면 렌더되지 않은 위젯의 state가
# 정리되므로, 여기서 보관해두고 다음 실행 시작에 복원한다.
remember_pinned_widgets()
