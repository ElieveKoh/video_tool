#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import streamlit as st
import streamlit.components.v1 as components
import subprocess
import os
import tempfile
import shutil
import threading
import time
import json
from pathlib import Path
import webbrowser
from threading import Thread
import re
import signal
from datetime import datetime

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
        """하드웨어 가속 사용 가능 여부 확인 (macOS VideoToolbox)"""
        import sys
        if sys.platform != 'darwin':
            return False

        try:
            ffmpeg_path = self._get_ffmpeg_path()
            result = subprocess.run(
                [ffmpeg_path, '-hide_banner', '-encoders'],
                capture_output=True, text=True, timeout=5
            )
            # VideoToolbox 인코더가 있는지 확인
            return 'h264_videotoolbox' in result.stdout
        except Exception:
            return False
    
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
        """FFprobe를 사용해서 비디오 정보 추출"""
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
                
                return {
                    'width': int(video_stream.get('width', 0)),
                    'height': int(video_stream.get('height', 0)),
                    'codec': video_stream.get('codec_name', 'unknown'),
                    'duration': float(format_info.get('duration', 0)),
                    'bitrate': int(format_info.get('bit_rate', 0))
                }
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

    def strip_audio(self, input_source, output_file, progress_callback=None):
        """비디오에서 오디오 트랙 제거 (비디오는 재인코딩 없이 복사)
        input_source: 로컬 파일 경로 또는 URL (ffmpeg가 직접 처리)
        """
        try:
            ffmpeg_path = self._get_ffmpeg_path()
            is_url = input_source.startswith(('http://', 'https://'))

            # 로컬 파일이면 duration 추출 가능, URL이면 불가
            total_duration = 0
            if not is_url:
                video_info = self.get_video_info(input_source)
                total_duration = video_info['duration'] if video_info else 0

            cmd = [
                ffmpeg_path,
                '-i', input_source,
                '-c:v', 'copy',
                '-an',
                '-y',
                '-progress', 'pipe:1',
                output_file
            ]

            os.makedirs(os.path.dirname(output_file), exist_ok=True)

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1
            )

            while True:
                output = process.stdout.readline()
                if output == '' and process.poll() is not None:
                    break
                if output.strip().startswith('out_time_ms=') and total_duration > 0:
                    try:
                        time_ms = int(output.strip().split('=')[1])
                        progress = min((time_ms / 1000000) / total_duration, 1.0)
                        if progress_callback:
                            progress_callback(progress)
                    except Exception:
                        pass

            return_code = process.wait()
            if return_code == 0 and os.path.exists(output_file):
                return True, "Audio removed successfully"
            return False, "FFmpeg returned error"
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
        """유튜브 비디오 제목만 빠르게 추출 (큐 추가용)"""
        try:
            yt_dlp_path = self._get_yt_dlp_path()
            cmd = [yt_dlp_path, '--get-title', '--no-warnings', url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode == 0:
                title = result.stdout.strip()
                return {
                    'title': title if title else 'Unknown',
                    'duration': 0,  # 나중에 다운로드 시 확인
                    'uploader': 'Unknown',
                    'view_count': 0,
                    'upload_date': 'Unknown'
                }
            else:
                print(f"yt-dlp error (returncode {result.returncode}): {result.stderr.strip()}")
                return None
        except subprocess.TimeoutExpired:
            print(f"yt-dlp timeout (30s) fetching: {url}")
            return None
        except Exception as e:
            print(f"비디오 제목 추출 오류: {e}")
            return None

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

# 페이지 설정
st.set_page_config(
    page_title="Video Tool v6.0",
    page_icon="🎥",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# 세션 상태 초기화 (한 곳에서 통합 관리)
def init_session_state():
    defaults = {
        'theme_mode': 'light',
        'selected_folder_path': "",
        'video_files_list': [],
        'file_selection_state': {},
        'converter': VideoConverterCore(),
        'conversion_running': False,
        'yt_downloader': YouTubeDownloader(),
        'yt_download_running': False,
        'yt_queue': [],
        'yt_queue_selection': {},
        'yt_url_input': "",
        'yt_save_folder_path': os.path.join(os.path.expanduser("~"), "Downloads"),
        'sort_by': 'name',
        'sort_order': 'asc',
        'vc_toggle_counter': 0,
        'yt_toggle_counter': 0,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

init_session_state()

# === VideoTool v6.0 통합 CSS ===
st.markdown("""
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg-primary: #ffffff; --bg-secondary: #f6f8fa; --bg-tertiary: #eef1f5; --bg-input: #ffffff;
    --border-default: #d0d7de; --border-accent: #ff4b4b;
    --text-primary: #1f2328; --text-secondary: #656d76; --text-muted: #8b949e;
    --accent: #ff4b4b; --accent-hover: #e03e3e;
    --success: #2ea043; --warning: #d29922; --info: #58a6ff;
    --card-shadow: 0 1px 3px rgba(0,0,0,0.06);
}
[data-theme="dark"] {
    --bg-primary: #0d1117; --bg-secondary: #161b22; --bg-tertiary: #21262d; --bg-input: #0d1117;
    --border-default: #30363d; --border-accent: #ff4b4b;
    --text-primary: #f0f6fc; --text-secondary: #8b949e; --text-muted: #6e7681;
    --accent: #ff6b6b; --accent-hover: #ff4b4b;
    --success: #3fb950; --warning: #d29922; --info: #58a6ff;
    --card-shadow: 0 1px 3px rgba(0,0,0,0.3);
}
* { font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important; }
[data-testid="stIconMaterial"], .material-symbols-rounded { font-family: 'Material Symbols Rounded' !important; }
.stApp { background-color: var(--bg-primary) !important; }
.stApp > header { height: 0rem; background-color: var(--bg-primary) !important; }
header[data-testid="stHeader"] { background-color: var(--bg-primary) !important; }
[data-theme="dark"] header[data-testid="stHeader"] { background-color: #0d1117 !important; }
.main .block-container { padding-top: 1rem; padding-bottom: 3.5rem; padding-left: 2rem; padding-right: 2rem; max-width: 100%; }
footer { visibility: hidden !important; }
.stAppDeployButton, button[data-testid="stAppDeployButton"] { display: none !important; }
[data-testid="stMainMenu"] { display: block !important; visibility: visible !important; opacity: 1 !important; position: fixed !important; top: 0.75rem !important; right: 1rem !important; z-index: 999999 !important; }
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
.st-key-config_panel > div { background: var(--bg-secondary); border: 1px solid var(--border-default); border-left: 4px solid var(--border-accent); border-radius: 0.75rem; padding: 1.25rem; box-shadow: var(--card-shadow); }
.st-key-file_source > div { background: var(--bg-secondary); border: 1px solid var(--border-default); border-radius: 0.75rem; padding: 1rem; margin-top: 1rem; box-shadow: var(--card-shadow); }
.st-key-queue_panel > div { background: var(--bg-secondary); border: 1px solid var(--border-default); border-radius: 0.75rem; padding: 1.25rem; box-shadow: var(--card-shadow); }
.st-key-mute_card > div { background: var(--bg-secondary); border: 1px solid var(--border-default); border-left: 4px solid var(--border-accent); border-radius: 0.75rem; padding: 1.5rem; box-shadow: var(--card-shadow); max-width: 700px; margin: 0 auto; }
.st-key-yt_config_panel > div { background: var(--bg-secondary); border: 1px solid var(--border-default); border-left: 4px solid var(--border-accent); border-radius: 0.75rem; padding: 1.25rem; box-shadow: var(--card-shadow); }
.st-key-yt_queue_panel > div { background: var(--bg-secondary); border: 1px solid var(--border-default); border-radius: 0.75rem; padding: 1.25rem; box-shadow: var(--card-shadow); }
.vt-section-label { font-size: 0.7rem; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; color: var(--text-secondary); margin-bottom: 0.75rem; }
.vt-stats-row { display: flex; gap: 0.75rem; margin-bottom: 1.25rem; }
.vt-stats-card { flex: 1; background: var(--bg-secondary); border: 1px solid var(--border-default); border-radius: 0.75rem; padding: 0.85rem 1rem; text-align: center; box-shadow: var(--card-shadow); }
.vt-stats-card__label { font-size: 0.6rem; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.25rem; }
.vt-stats-card__value { font-size: 1.3rem; font-weight: 700; color: var(--text-primary); }
.vt-stats-card__value--accent { color: var(--accent); }
.vt-badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 0.25rem; font-size: 0.65rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; }
.vt-badge--ready { background: rgba(46,160,67,0.15); color: var(--success); border: 1px solid rgba(46,160,67,0.3); }
.vt-badge--pending { background: rgba(210,153,34,0.15); color: var(--warning); border: 1px solid rgba(210,153,34,0.3); }
.vt-badge--processing { background: rgba(88,166,255,0.15); color: var(--info); border: 1px solid rgba(88,166,255,0.3); }
.vt-status-bar { position: fixed; bottom: 0; left: 0; right: 0; height: 2.5rem; z-index: 999; background: var(--bg-secondary); border-top: 1px solid var(--border-default); display: flex; align-items: center; justify-content: center; gap: 1.5rem; font-size: 0.72rem; color: var(--text-secondary); }
.vt-status-bar strong { color: var(--text-primary); font-weight: 600; }
.vt-header { text-align: center; margin-bottom: 1.5rem; margin-top: -0.5rem; }
.vt-header__title { font-size: 1.5rem; font-weight: 800; letter-spacing: -0.03em; background: linear-gradient(135deg, #ff4b4b, #f97316); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; margin: 0; }
.vt-header__subtitle { color: var(--text-secondary); font-size: 0.8rem; font-weight: 500; margin-top: 0.25rem; }
.vt-header__badges { display: flex; justify-content: center; gap: 0.4rem; margin-top: 0.6rem; flex-wrap: wrap; }
.vt-header__badge { color: white; padding: 0.15rem 0.5rem; border-radius: 9999px; font-size: 0.6rem; font-weight: 700; letter-spacing: 0.05em; }
button[kind="primary"] { background-color: var(--accent) !important; border-color: var(--accent) !important; color: white !important; font-weight: 600 !important; border-radius: 0.5rem !important; }
button[kind="primary"]:hover { background-color: var(--accent-hover) !important; }
button[kind="secondary"] { background-color: var(--bg-secondary) !important; border: 1px solid var(--border-default) !important; color: var(--text-primary) !important; font-weight: 500 !important; border-radius: 0.5rem !important; }
button[kind="secondary"]:hover { background-color: var(--bg-tertiary) !important; }
button[key="stop_conversion_btn"], button[key="yt_stop_batch"] { background-color: #ff4b4b !important; color: white !important; }
button[key="stop_conversion_btn"]:hover, button[key="yt_stop_batch"]:hover { background-color: #ff0000 !important; }
button[key="stop_conversion_btn"]:disabled, button[key="yt_stop_batch"]:disabled { background-color: #cccccc !important; color: #666666 !important; }
.stProgress > div > div > div > div { background: linear-gradient(90deg, #ff4b4b, #f97316); border-radius: 9999px; height: 0.5rem; }
.stTabs [data-baseweb="tab-list"] { gap: 0; border-bottom: 2px solid var(--border-default); padding: 0 0.5rem; }
.stTabs [data-baseweb="tab"] { height: 2.75rem; padding: 0 1.25rem; background-color: transparent; border: none; color: var(--text-secondary); font-weight: 500; font-size: 0.85rem; border-bottom: 2px solid transparent; margin-bottom: -2px; }
.stTabs [data-baseweb="tab"]:hover { color: var(--text-primary); background-color: transparent; }
.stTabs [aria-selected="true"] { color: var(--accent) !important; border-bottom-color: var(--accent) !important; font-weight: 700 !important; }
button[data-baseweb="tab"]:nth-child(1)::before, button[data-baseweb="tab"]:nth-child(2)::before, button[data-baseweb="tab"]:nth-child(3)::before { content: ''; display: inline-block; width: 14px; height: 14px; margin-right: 6px; vertical-align: middle; background-size: contain; background-repeat: no-repeat; }
button[data-baseweb="tab"]:nth-child(1)::before { background-image: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="%236b7280" stroke-width="2"><rect x="2" y="2" width="20" height="20" rx="2.18" ry="2.18"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/></svg>'); }
button[data-baseweb="tab"]:nth-child(2)::before { background-image: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="%236b7280" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'); }
button[data-baseweb="tab"]:nth-child(3)::before { background-image: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="%236b7280" stroke-width="2"><path d="M11 5L6 9H2v6h4l5 4V5z"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>'); }
[data-theme="dark"] button[data-baseweb="tab"]::before { filter: brightness(1.5); }
button[data-baseweb="tab"]:nth-child(3)::after { content: 'NEW'; display: inline-block; font-size: 0.5rem; font-weight: 700; color: white; background: linear-gradient(135deg, #f59e0b, #d97706); padding: 0.08rem 0.3rem; border-radius: 0.2rem; margin-left: 6px; vertical-align: middle; }
.st-key-theme_toggle { position: fixed !important; top: 0.75rem; right: 3.5rem; z-index: 999998; }
.st-key-theme_toggle button { background-color: var(--bg-secondary) !important; border: 1px solid var(--border-default) !important; border-radius: 0.375rem !important; padding: 0.25rem 0.5rem !important; font-size: 1.1rem !important; }
.st-key-theme_toggle button:hover { background-color: var(--bg-tertiary) !important; }
.st-key-queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox) { display: flex !important; align-items: center !important; min-height: 2.75rem !important; border-bottom: 1px solid var(--border-default); }
.st-key-queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox):hover { background-color: var(--bg-tertiary) !important; }
.st-key-queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox) > div[data-testid="column"] { display: flex !important; align-items: center !important; }
.st-key-queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox) p { margin: 0 !important; padding: 0 !important; line-height: 2rem !important; font-size: 0.85rem !important; }
.st-key-queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox) .stCheckbox { margin: 0 !important; padding: 0 !important; }
.st-key-yt_queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox) { display: flex !important; align-items: flex-start !important; padding: 0.5rem 0 !important; border-bottom: 1px solid var(--border-default); }
.st-key-yt_queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox):hover { background-color: var(--bg-tertiary) !important; }
.st-key-yt_queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox) > div[data-testid="column"] { display: flex !important; align-items: flex-start !important; }
.st-key-yt_queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox) .stCheckbox { margin: 0 !important; padding: 0 !important; }
.st-key-yt_queue_panel div[data-testid="stHorizontalBlock"]:has(.stCheckbox) p { margin: 0 !important; padding: 0 !important; }
button[key="sort_name"], button[key="sort_size"], button[key="sort_date"] { border: none !important; background-color: transparent !important; box-shadow: none !important; outline: none !important; padding: 0.5rem !important; font-weight: 700 !important; font-size: 0.7rem !important; letter-spacing: 0.08em !important; text-transform: uppercase !important; color: var(--text-secondary) !important; }
button[key="sort_name"]:hover, button[key="sort_size"]:hover, button[key="sort_date"]:hover { color: var(--accent) !important; }
[data-testid="stExpander"] { border: 1px solid var(--border-default) !important; border-radius: 0.5rem !important; background: transparent !important; overflow: visible !important; }
[data-testid="stExpander"] summary { font-size: 0.85rem !important; font-weight: 600 !important; color: var(--text-secondary) !important; padding: 0.75rem 1rem !important; }
[data-testid="stExpander"] summary::-webkit-details-marker { display: none !important; }
[data-testid="stExpander"] summary::marker { content: '' !important; }
.feature-box { background-color: var(--bg-secondary); border: 1px solid var(--border-default); border-radius: 0.5rem; padding: 1rem; }
div[data-testid="stHorizontalBlock"] { gap: 0.5rem; }
/* Scrollable queue panels - config stays visible */
.st-key-queue_panel > div, .st-key-yt_queue_panel > div { max-height: 65vh; overflow-y: auto; }
/* YT save location panel */
.st-key-yt_save_panel > div { background: var(--bg-secondary); border: 1px solid var(--border-default); border-radius: 0.75rem; padding: 1rem; margin-top: 0.5rem; box-shadow: var(--card-shadow); }
/* Fix expander text overlap */
[data-testid="stExpander"] summary > span { display: flex !important; align-items: center !important; width: 100% !important; }
[data-testid="stExpander"] summary > span > div { flex: 1 !important; min-width: 0 !important; }
[data-testid="stExpander"] summary p { margin: 0 !important; line-height: 1.5 !important; white-space: nowrap !important; overflow: hidden !important; text-overflow: ellipsis !important; }
[data-testid="stExpanderDetails"] > div { padding: 0.5rem 0 !important; }
</style>
""", unsafe_allow_html=True)


# 공통 함수들
def open_folder_dialog():
    """플랫폼별 폴더 선택 다이얼로그 (macOS는 osascript, 기타는 수동 입력)"""
    import sys

    # macOS에서만 osascript 사용
    if sys.platform == 'darwin':
        try:
            apple_script = '''
            tell application "System Events"
                activate
                set selectedFolder to choose folder with prompt "Select folder containing video files"
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
                        return valid_files
            return None
        except Exception as e:
            st.error(f"File selection error: {e}")
            return None
    return None

def scan_folder_files(folder_path):
    """선택된 폴더에서 비디오 파일 스캔"""
    video_extensions = ['.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg', '.3gp']
    video_files = []
    
    try:
        for file in os.listdir(folder_path):
            if any(file.lower().endswith(ext) for ext in video_extensions):
                video_files.append(os.path.join(folder_path, file))
        
        st.session_state['video_files_list'] = video_files
        st.session_state['file_selection_state'] = {file: True for file in video_files}
        
    except Exception as e:
        st.error(f"Folder scan error: {e}")

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

def convert_videos_realtime(selected_files, target_codec, target_resolution, quality_preset, selected_fps="original", selected_scan="progressive", custom_video_br=None, custom_audio_br=None):
    """실시간 비디오 변환 함수"""
    total_files = len(selected_files)
    
    # 출력 폴더 생성
    output_folder = f"converted_{target_codec}"
    current_dir = st.session_state.get('selected_folder_path', os.getcwd())
    output_path = os.path.join(current_dir, output_folder)
    
    st.markdown(f"### 🎬 Conversion Progress")
    st.info(f"📂 Output folder: {output_path}")
    
    overall_progress = st.progress(0)
    current_file_info = st.empty()
    current_file_progress = st.progress(0)
    conversion_log = st.empty()
    
    successful_conversions = 0
    failed_conversions = 0
    
    for file_idx, input_file in enumerate(selected_files):
        if not st.session_state.get('conversion_running', False):
            break
            
        file_name = os.path.basename(input_file)
        base_name = os.path.splitext(file_name)[0]
        
        # 출력 파일명 생성
        if target_resolution != "original":
            output_filename = f"{base_name}_{target_codec}_{target_resolution}.mp4"
        else:
            output_filename = f"{base_name}_{target_codec}.mp4"
        
        output_file = os.path.join(output_path, output_filename)
        
        # 현재 파일 정보 표시
        current_file_info.markdown(f"**📁 [{file_idx + 1}/{total_files}] 변환 중:** `{file_name}`")
        
        # 진행률 콜백 함수 (예상 시간 포함)
        conversion_start_time = time.time()

        def progress_callback(progress, current_time, total_time):
            current_file_progress.progress(progress)
            min_current = int(current_time // 60)
            sec_current = int(current_time % 60)
            min_total = int(total_time // 60)
            sec_total = int(total_time % 60)

            # 예상 남은 시간 계산
            if progress > 0.01:  # 1% 이상 진행되었을 때만 계산
                elapsed = time.time() - conversion_start_time
                estimated_total = elapsed / progress
                remaining = int(estimated_total - elapsed)

                if remaining > 0:
                    min_remaining = remaining // 60
                    sec_remaining = remaining % 60
                    conversion_log.text(
                        f"⏱️ 진행률: {progress*100:.1f}% | {min_current:02d}:{sec_current:02d} / {min_total:02d}:{sec_total:02d} | "
                        f"예상 남은 시간: {min_remaining}분 {sec_remaining}초"
                    )
                else:
                    conversion_log.text(f"⏱️ 진행률: {progress*100:.1f}% | {min_current:02d}:{sec_current:02d} / {min_total:02d}:{sec_total:02d}")
            else:
                conversion_log.text(f"⏱️ 진행률: {progress*100:.1f}% | {min_current:02d}:{sec_current:02d} / {min_total:02d}:{sec_total:02d}")
        
        # 현재 파일의 정보 표시
        try:
            video_info = st.session_state['converter'].get_video_info(input_file)
            if video_info:
                width, height = video_info['width'], video_info['height']
                codec = video_info['codec']
                duration_min = int(video_info['duration'] // 60)
                duration_sec = int(video_info['duration'] % 60)
                bitrate_mbps = video_info['bitrate'] // 1000000 if video_info['bitrate'] > 0 else 0
                
                st.write(f"📺 Resolution: {width}x{height} | Codec: {codec} | Duration: {duration_min:02d}:{duration_sec:02d} | Bitrate: {bitrate_mbps}Mbps")
        except Exception as e:
            print(f"비디오 정보 표시 오류: {e}")
        
        # 변환 실행
        start_time = time.time()
        success, message = st.session_state['converter'].convert_video(
            input_file, output_file, target_codec, target_resolution, quality_preset, selected_fps, selected_scan, custom_video_br, custom_audio_br, progress_callback
        )
        end_time = time.time()
        
        if success:
            successful_conversions += 1
            elapsed_time = int(end_time - start_time)
            st.success(f"✅ Conversion complete: `{output_filename}` (Duration: {elapsed_time}s)")
        else:
            failed_conversions += 1
            st.error(f"❌ Conversion failed: `{file_name}` - {message}")
        
        # 전체 진행률 업데이트
        overall_progress.progress((file_idx + 1) / total_files)
        
        # 잠깐 대기
        time.sleep(0.1)
    
    # 변환 완료
    st.session_state['conversion_running'] = False
    current_file_info.empty()
    current_file_progress.empty()
    conversion_log.empty()
    
    if successful_conversions > 0:
        st.success(f"🎉 Conversion complete! Success: {successful_conversions}, Failed: {failed_conversions}")
        st.balloons()
    else:
        st.error("❌ All conversions failed.")

def batch_download_and_convert(selected_items):
    """유튜브 배치 다운로드 + 변환 함수"""
    total_videos = len(selected_items)

    st.markdown(f"### 🎬 Batch Download + Conversion Progress ({total_videos} videos)")

    # 저장 경로 설정
    configured_path = st.session_state.get('yt_save_folder_path')
    save_path, save_path_warning = resolve_writable_save_path(configured_path)
    st.session_state['yt_save_folder_path'] = save_path

    st.info(f"📂 Save location: {save_path}")
    if save_path_warning:
        st.warning(save_path_warning)

    # 전체 진행률
    overall_progress = st.progress(0)
    current_video_info = st.empty()

    # 개별 비디오 진행률
    download_progress = st.progress(0)
    conversion_progress = st.progress(0)
    status_text = st.empty()

    successful_count = 0
    failed_count = 0

    for video_idx, item in enumerate(selected_items):
        if not st.session_state.get('yt_download_running', False):
            break

        url = item['url']
        info = item['info']
        settings = item['settings']

        # 현재 비디오 정보 표시
        current_video_info.markdown(f"**📥 [{video_idx + 1}/{total_videos}] {info['title']}**")

        # 다운로드 진행률 콜백
        def download_progress_callback(progress):
            overall = (video_idx + progress * 0.5) / total_videos
            overall_progress.progress(overall)
            download_progress.progress(progress)

        # 다운로드 실행
        status_text.markdown("**📥 Downloading from YouTube...**")
        success, message, downloaded_file = st.session_state['yt_downloader'].download_video(
            url, save_path, download_progress_callback
        )

        if not success:
            st.error(f"❌ Download failed: {info['title']} - {message}")
            failed_count += 1
            overall_progress.progress((video_idx + 1) / total_videos)
            continue

        st.success(f"✅ Downloaded: {os.path.basename(downloaded_file)}")

        # 다운로드 성공 시 큐의 해당 아이템 정보 업데이트
        for queue_item in st.session_state['yt_queue']:
            if queue_item['url'] == url:
                # 전체 정보 가져오기
                full_info = st.session_state['yt_downloader'].get_video_info(url)
                if full_info:
                    queue_item['info'] = full_info
                break

        # Download Only 모드면 변환 건너뛰기
        if settings.get('download_only', False):
            st.info("📥 Download Only mode - Skipping conversion")
            successful_count += 1
            overall_progress.progress((video_idx + 1) / total_videos)
            download_progress.progress(0)
            continue

        # 변환 필요 여부 확인
        video_info = st.session_state['converter'].get_video_info(downloaded_file)
        current_codec = video_info['codec'] if video_info else 'unknown'

        needs_conversion = (
            current_codec != settings['codec'] or
            settings['resolution'] != "original" or
            settings['fps'] != "original" or
            settings['quality'] == "custom"
        )

        if needs_conversion:
            status_text.markdown("**🎬 Converting video...**")

            # 출력 파일명 생성
            base_name = os.path.splitext(os.path.basename(downloaded_file))[0]
            if settings['resolution'] != "original":
                output_filename = f"{base_name}_{settings['codec']}_{settings['resolution']}.mp4"
            else:
                output_filename = f"{base_name}_{settings['codec']}.mp4"

            output_file = os.path.join(save_path, output_filename)

            # 변환 진행률 콜백
            def conversion_progress_callback(progress, current_time, total_time):
                overall = (video_idx + 0.5 + progress * 0.5) / total_videos
                overall_progress.progress(overall)
                conversion_progress.progress(progress)

                min_current = int(current_time // 60)
                sec_current = int(current_time % 60)
                min_total = int(total_time // 60)
                sec_total = int(total_time % 60)

                status_text.markdown(f"**🎬 Converting: {progress*100:.1f}% | {min_current:02d}:{sec_current:02d} / {min_total:02d}:{sec_total:02d}**")

            # 변환 실행
            conversion_success, conversion_message = st.session_state['converter'].convert_video(
                downloaded_file,
                output_file,
                settings['codec'],
                settings['resolution'],
                settings['quality'],
                settings['fps'],
                settings['scan'],
                settings['custom_video_br'],
                settings['custom_audio_br'],
                conversion_progress_callback
            )

            if conversion_success:
                st.success(f"✅ Converted: {output_filename}")
                successful_count += 1

                # 원본 파일 삭제
                try:
                    os.remove(downloaded_file)
                except Exception as e:
                    print(f"원본 파일 삭제 실패: {e}")
            else:
                st.error(f"❌ Conversion failed: {info['title']} - {conversion_message}")
                failed_count += 1
        else:
            st.info(f"ℹ️ Already desired format. Skipping conversion.")
            successful_count += 1

        # 전체 진행률 업데이트
        overall_progress.progress((video_idx + 1) / total_videos)
        download_progress.progress(0)
        conversion_progress.progress(0)

    # 완료
    st.session_state['yt_download_running'] = False
    current_video_info.empty()
    download_progress.empty()
    conversion_progress.empty()
    status_text.empty()
    overall_progress.empty()

    # 배치 완료 후 큐 비우기
    st.session_state['yt_queue'] = []
    st.session_state['yt_queue_selection'] = {}

    if successful_count > 0:
        st.success(f"🎉 Batch operation completed! Success: {successful_count}, Failed: {failed_count}")
        st.balloons()
    else:
        st.error("❌ All operations failed.")

def download_and_convert_youtube(url, target_codec, target_resolution, quality_preset, target_fps="original", target_scan="progressive", custom_video_br=None, custom_audio_br=None, download_only=False):
    """유튜브 다운로드 + 변환 함수"""

    if download_only:
        st.markdown("### 📥 YouTube Download Progress")
    else:
        st.markdown("### 📥 YouTube Download + Conversion Progress")
    
    # 저장 경로 설정
    configured_path = st.session_state.get('yt_save_folder_path')
    save_path, save_path_warning = resolve_writable_save_path(configured_path)
    st.session_state['yt_save_folder_path'] = save_path
    
    st.info(f"📂 Save location: {save_path}")
    if save_path_warning:
        st.warning(save_path_warning)
    
    # 진행률 표시
    download_progress = st.progress(0)
    status_text = st.empty()
    conversion_progress = st.progress(0)
    
    # 1단계: 다운로드
    status_text.markdown("**📥 1단계: 유튜브에서 다운로드 중...**")
    
    def download_progress_callback(progress):
        download_progress.progress(progress * 0.5)  # 전체의 50%까지
    
    success, message, downloaded_file = st.session_state['yt_downloader'].download_video(
        url, save_path, download_progress_callback
    )
    
    if not success:
        st.session_state['yt_download_running'] = False
        download_progress.empty()
        conversion_progress.empty()
        status_text.empty()
        st.error(f"❌ Download failed: {message}")
        return
    
    download_progress.progress(0.5)
    st.success(f"✅ Download complete: {os.path.basename(downloaded_file)}")

    # Download Only 모드면 변환 건너뛰기
    if download_only:
        download_progress.progress(1.0)
        st.session_state['yt_download_running'] = False
        download_progress.empty()
        conversion_progress.empty()
        status_text.empty()

        st.success(f"🎉 Download completed! File saved to: `{downloaded_file}`")
        st.balloons()
        return

    # 2단계: 변환 (필요한 경우만)
    if downloaded_file:
        # 현재 코덱 확인
        video_info = st.session_state['converter'].get_video_info(downloaded_file)
        current_codec = video_info['codec'] if video_info else 'unknown'
        
        # 변환이 필요한지 확인
        needs_conversion = (
            current_codec != target_codec or 
            target_resolution != "original"
        )
        
        if needs_conversion:
            status_text.markdown("**🎬 2단계: 비디오 변환 중...**")
            
            # 출력 파일명 생성
            base_name = os.path.splitext(os.path.basename(downloaded_file))[0]
            if target_resolution != "original":
                output_filename = f"{base_name}_{target_codec}_{target_resolution}.mp4"
            else:
                output_filename = f"{base_name}_{target_codec}.mp4"
            
            output_file = os.path.join(save_path, output_filename)
            
            # 변환 진행률 콜백
            def conversion_progress_callback(progress, current_time, total_time):
                overall_progress = 0.5 + (progress * 0.5)  # 50%부터 100%까지
                download_progress.progress(overall_progress)
                conversion_progress.progress(progress)
                
                min_current = int(current_time // 60)
                sec_current = int(current_time % 60)
                min_total = int(total_time // 60)
                sec_total = int(total_time % 60)
                
                status_text.markdown(f"**🎬 변환 진행률: {progress*100:.1f}% | {min_current:02d}:{sec_current:02d} / {min_total:02d}:{sec_total:02d}**")
            
            # 변환 실행
            conversion_success, conversion_message = st.session_state['converter'].convert_video(
                downloaded_file, output_file, target_codec, target_resolution, quality_preset, target_fps, target_scan, custom_video_br, custom_audio_br, conversion_progress_callback
            )
            
            if conversion_success:
                st.success(f"✅ Conversion complete: {output_filename}")
                
                # 원본 파일 삭제 여부 확인
                if st.checkbox("Delete original downloaded file", value=True):
                    try:
                        os.remove(downloaded_file)
                        st.info("🗑️ Original file has been deleted.")
                    except Exception as e:
                        st.warning(f"⚠️ Failed to delete original file: {e}")
                
                final_file = output_file
            else:
                st.error(f"❌ Conversion failed: {conversion_message}")
                final_file = downloaded_file
        else:
            download_progress.progress(1.0)
            st.info("ℹ️ Already the desired codec and resolution. Skipping conversion.")
            final_file = downloaded_file
        
        # 완료
        st.session_state['yt_download_running'] = False
        download_progress.empty()
        conversion_progress.empty()
        status_text.empty()

        st.success(f"🎉 All tasks completed! File saved to: `{final_file}`")
        st.balloons()

# 테마 적용 스크립트 (components.html 사용)
theme_html = f"""
<script>
(function() {{
    const theme = '{st.session_state['theme_mode']}';
    console.log('VideoTool: Applying theme:', theme);

    // 테마 적용 함수
    const applyTheme = () => {{
        try {{
            // 메인 앱 요소 찾기
            const stApp = window.parent.document.querySelector('[data-testid="stApp"]');
            if (stApp) {{
                stApp.setAttribute('data-theme', theme);
                console.log('VideoTool: Theme applied to stApp:', theme);
            }} else {{
                console.warn('VideoTool: stApp element not found');
            }}

            // body에도 적용
            const body = window.parent.document.body;
            if (body) {{
                body.setAttribute('data-theme', theme);
                console.log('VideoTool: Theme applied to body:', theme);
            }}

            // html에도 적용
            const html = window.parent.document.documentElement;
            if (html) {{
                html.setAttribute('data-theme', theme);
                console.log('VideoTool: Theme applied to html:', theme);
            }}
        }} catch (e) {{
            console.error('VideoTool: Error applying theme:', e);
        }}
    }};

    // 즉시 적용
    applyTheme();

    // 100ms 후 재적용 (DOM 로드 지연 대비)
    setTimeout(applyTheme, 100);

    // 500ms 후 재적용 (추가 안전장치)
    setTimeout(applyTheme, 500);

    // DOM 변경 감지하여 재적용
    try {{
        const observer = new MutationObserver(() => {{
            applyTheme();
        }});

        if (window.parent.document.body) {{
            observer.observe(window.parent.document.body, {{
                childList: true,
                subtree: false,
                attributes: false
            }});
        }}
    }} catch (e) {{
        console.error('VideoTool: Error setting up observer:', e);
    }}
}})();
</script>
"""
components.html(theme_html, height=0)

# 테마 토글 버튼 (스타일은 통합 CSS에 정의됨)
theme_icon = "☀" if st.session_state['theme_mode'] == 'dark' else "☾"
if st.button(theme_icon, key="theme_toggle", help="Toggle Light/Dark Mode"):
    st.session_state['theme_mode'] = 'dark' if st.session_state['theme_mode'] == 'light' else 'light'
    st.rerun()

# 진행 중 경고 표시
if st.session_state.get('conversion_running', False) or st.session_state.get('yt_download_running', False):
    st.warning("⚠️ **Conversion/Download in progress.** Please do not switch tabs or refresh the page until it's complete.")

# 메인 헤더
st.markdown('''
<div class="vt-header">
    <h1 class="vt-header__title">🎥 Video Tool v6.0</h1>
    <p class="vt-header__subtitle">Video Conversion &bull; YouTube Download &bull; Mute Video</p>
    <div class="vt-header__badges">
        <span class="vt-header__badge" style="background: linear-gradient(135deg, #3b82f6, #2563eb);">CRF MODE</span>
        <span class="vt-header__badge" style="background: linear-gradient(135deg, #10b981, #059669);">HW ACCEL</span>
        <span class="vt-header__badge" style="background: linear-gradient(135deg, #f59e0b, #d97706);">MUTE VIDEO</span>
        <span class="vt-header__badge" style="background: linear-gradient(135deg, #ef4444, #dc2626);">AUTO UPDATE</span>
    </div>
</div>
''', unsafe_allow_html=True)

# 탭 생성
tab1, tab2, tab3 = st.tabs(["Video Conversion", "YouTube Download", "Mute Video"])


# Tab 1: Local File Conversion
with tab1:
    # Option dictionaries (defined at tab scope for Tab 2 reuse)
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

    # --- Stats cards ---
    _files = st.session_state['video_files_list']
    _sel_count = sum(1 for f in _files if st.session_state['file_selection_state'].get(f, True)) if _files else 0
    _total_mb = sum(os.path.getsize(f) / (1024*1024) for f in _files if os.path.exists(f)) if _files else 0
    _total_display = f"{_total_mb:.1f} MB" if _total_mb < 1024 else f"{_total_mb/1024:.1f} GB"
    st.markdown(f'''
    <div class="vt-stats-row">
        <div class="vt-stats-card"><div class="vt-stats-card__label">QUEUE TOTAL</div><div class="vt-stats-card__value">{len(_files)}</div></div>
        <div class="vt-stats-card"><div class="vt-stats-card__label">SELECTED</div><div class="vt-stats-card__value vt-stats-card__value--accent">{_sel_count}</div></div>
        <div class="vt-stats-card"><div class="vt-stats-card__label">TOTAL SIZE</div><div class="vt-stats-card__value">{_total_display}</div></div>
        <div class="vt-stats-card"><div class="vt-stats-card__label">EST. DURATION</div><div class="vt-stats-card__value">--</div></div>
    </div>
    ''', unsafe_allow_html=True)

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
                format_func=lambda x: codec_options[x], index=0
            )
            selected_resolution = st.selectbox(
                "Resolution", options=list(resolution_options.keys()),
                format_func=lambda x: resolution_options[x], index=0
            )
            selected_quality = st.selectbox(
                "Quality", options=list(quality_presets.keys()),
                format_func=lambda x: quality_presets[x], index=1
            )

            if selected_quality == "crf":
                crf_defaults = {"h264": 23, "h265": 28, "vp9": 31, "av1": 30}
                custom_video_br = st.slider(
                    "CRF Value (lower = better quality):",
                    min_value=0, max_value=51,
                    value=crf_defaults.get(selected_codec, 23), step=1
                )

            with st.expander("Advanced Settings"):
                selected_fps = st.selectbox(
                    "Frame Rate", options=list(fps_options.keys()),
                    format_func=lambda x: fps_options[x], index=0
                )
                selected_scan = st.selectbox(
                    "Scan Type", options=list(scan_options.keys()),
                    format_func=lambda x: scan_options[x], index=0
                )

                if selected_quality == "custom":
                    custom_video_br = st.number_input(
                        "Video Bitrate (Mbps):", min_value=1, max_value=100, value=10, step=1
                    )
                    custom_audio_br = st.number_input(
                        "Audio Bitrate (kbps):", min_value=64, max_value=320, value=192, step=32
                    )

        # Default values if expander not opened
        if 'selected_fps' not in dir():
            selected_fps = "original"
        if 'selected_scan' not in dir():
            selected_scan = "progressive"

        st.markdown('<p class="vt-section-label">FILE SOURCE</p>', unsafe_allow_html=True)
        with st.container(key="file_source"):
            import sys
            current_path = st.session_state.get('selected_folder_path', 'No files or folder selected')
            st.text_input("Location:", value=current_path, disabled=True, label_visibility="collapsed")

            if sys.platform == 'darwin':
                fc1, fc2 = st.columns(2)
                with fc1:
                    if st.button("Select Folder", use_container_width=True, key="btn_folder"):
                        with st.spinner("📂 Opening folder dialog..."):
                            selected = open_folder_dialog()
                        if selected:
                            st.rerun()
                with fc2:
                    if st.button("Select Files", use_container_width=True, key="btn_files"):
                        with st.spinner("📂 Opening file dialog..."):
                            selected = open_file_dialog()
                        if selected:
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

        # Start/Stop buttons
        if st.session_state['video_files_list']:
            col_start, col_stop = st.columns([3, 1])
            with col_start:
                start_conversion = st.button("▶ Start Conversion", type="primary", use_container_width=True, disabled=st.session_state['conversion_running'])
            with col_stop:
                stop_conversion = st.button("■ Stop", use_container_width=True, disabled=not st.session_state['conversion_running'], key="stop_conversion_btn")
        else:
            start_conversion = False
            stop_conversion = False

    with right_col:
        st.markdown('<p class="vt-section-label">PROJECT FILES</p>', unsafe_allow_html=True)

        with st.container(key="queue_panel"):
            if st.session_state['video_files_list']:
                st.success(f"📹 {len(st.session_state['video_files_list'])} video files")

                # File info collection
                file_info_list = []
                for video_file in st.session_state['video_files_list']:
                    file_info_list.append({
                        'path': video_file,
                        'name': os.path.basename(video_file),
                        'size': os.path.getsize(video_file),
                        'date': os.path.getmtime(video_file)
                    })

                # Sorting
                reverse = (st.session_state['sort_order'] == 'desc')
                if st.session_state['sort_by'] == 'name':
                    file_info_list.sort(key=lambda x: x['name'].lower(), reverse=reverse)
                elif st.session_state['sort_by'] == 'date':
                    file_info_list.sort(key=lambda x: x['date'], reverse=reverse)
                elif st.session_state['sort_by'] == 'size':
                    file_info_list.sort(key=lambda x: x['size'], reverse=reverse)

                # Sort arrows
                name_arrow = (" ↑" if st.session_state['sort_order'] == 'asc' else " ↓") if st.session_state['sort_by'] == 'name' else " ↕"
                size_arrow = (" ↑" if st.session_state['sort_order'] == 'asc' else " ↓") if st.session_state['sort_by'] == 'size' else " ↕"
                date_arrow = (" ↑" if st.session_state['sort_order'] == 'asc' else " ↓") if st.session_state['sort_by'] == 'date' else " ↕"

                # Table header
                col_check_h, col_name_h, col_size_h, col_date_h = st.columns([0.5, 3, 1, 1.5])
                with col_check_h:
                    all_selected = all(st.session_state['file_selection_state'].get(f, True) for f in st.session_state['video_files_list'])
                    toggle_label = "☑ All" if all_selected else "☐ All"
                    if st.button(toggle_label, key="toggle_all_header", use_container_width=True):
                        new_state = not all_selected
                        for vf in st.session_state['video_files_list']:
                            st.session_state['file_selection_state'][vf] = new_state
                        st.session_state['vc_toggle_counter'] += 1
                        st.rerun()
                with col_name_h:
                    if st.button(f"File Name{name_arrow}", key="sort_name", use_container_width=True):
                        if st.session_state['sort_by'] == 'name':
                            st.session_state['sort_order'] = 'desc' if st.session_state['sort_order'] == 'asc' else 'asc'
                        else:
                            st.session_state['sort_by'] = 'name'
                            st.session_state['sort_order'] = 'asc'
                        st.rerun()
                with col_size_h:
                    if st.button(f"Size{size_arrow}", key="sort_size", use_container_width=True):
                        if st.session_state['sort_by'] == 'size':
                            st.session_state['sort_order'] = 'desc' if st.session_state['sort_order'] == 'asc' else 'asc'
                        else:
                            st.session_state['sort_by'] = 'size'
                            st.session_state['sort_order'] = 'desc'
                        st.rerun()
                with col_date_h:
                    if st.button(f"Modified{date_arrow}", key="sort_date", use_container_width=True):
                        if st.session_state['sort_by'] == 'date':
                            st.session_state['sort_order'] = 'desc' if st.session_state['sort_order'] == 'asc' else 'asc'
                        else:
                            st.session_state['sort_by'] = 'date'
                            st.session_state['sort_order'] = 'desc'
                        st.rerun()

                # File rows
                for i, file_info in enumerate(file_info_list):
                    video_file = file_info['path']
                    file_name = file_info['name']
                    file_size_mb = file_info['size'] / (1024 * 1024)
                    file_date = datetime.fromtimestamp(file_info['date']).strftime('%Y-%m-%d %H:%M')

                    col_check, col_name, col_size, col_date = st.columns([0.5, 3, 1, 1.5])
                    with col_check:
                        current_state = st.session_state['file_selection_state'].get(video_file, True)
                        toggle_counter = st.session_state.get('vc_toggle_counter', 0)
                        file_key = f"file_check_{hash(video_file)}_{toggle_counter}"
                        is_selected = st.checkbox("✓", value=current_state, key=file_key, label_visibility="collapsed")
                        st.session_state['file_selection_state'][video_file] = is_selected
                    with col_name:
                        st.markdown(f'<p>📄 {file_name}</p>', unsafe_allow_html=True)
                    with col_size:
                        st.markdown(f'<p style="text-align:center;"><strong>{file_size_mb:.1f} MB</strong></p>', unsafe_allow_html=True)
                    with col_date:
                        st.markdown(f'<p style="text-align:center;">{file_date}</p>', unsafe_allow_html=True)
                    if is_selected:
                        selected_files.append(video_file)
            else:
                st.info("📂 Select a folder or files to begin")

    # --- Conversion logic (full width, after both columns) ---
    if stop_conversion:
        st.session_state['converter'].stop_conversion()
        st.session_state['conversion_running'] = False
        st.warning("Conversion has been stopped.")
        st.rerun()

    if start_conversion and not st.session_state['conversion_running'] and selected_files:
        with st.spinner("⚙️ Preparing conversion..."):
            st.session_state['conversion_running'] = True
        st.rerun()

    if st.session_state.get('conversion_running', False) and selected_files:
        convert_videos_realtime(selected_files, selected_codec, selected_resolution, selected_quality, selected_fps, selected_scan, custom_video_br, custom_audio_br)

    # --- Status bar ---
    _codec_display = selected_codec.upper()
    _res_display = selected_resolution.upper() if selected_resolution != "original" else "ORIGINAL"
    _quality_display = selected_quality.upper()
    st.markdown(f'''
    <div class="vt-status-bar">
        <span>CODEC: <strong>{_codec_display}</strong></span>
        <span style="color: var(--border-default);">|</span>
        <span>RES: <strong>{_res_display}</strong></span>
        <span style="color: var(--border-default);">|</span>
        <span>QUALITY: <strong>{_quality_display}</strong></span>
    </div>
    ''', unsafe_allow_html=True)


# 탭 2: 유튜브 다운로더
with tab2:
    # Stats cards
    _yt_queue = st.session_state['yt_queue']
    _yt_sel = sum(1 for item in _yt_queue if st.session_state['yt_queue_selection'].get(item['url'], True)) if _yt_queue else 0
    st.markdown(f'''
    <div class="vt-stats-row">
        <div class="vt-stats-card"><div class="vt-stats-card__label">QUEUE TOTAL</div><div class="vt-stats-card__value">{len(_yt_queue)}</div></div>
        <div class="vt-stats-card"><div class="vt-stats-card__label">SELECTED</div><div class="vt-stats-card__value vt-stats-card__value--accent">{_yt_sel}</div></div>
    </div>
    ''', unsafe_allow_html=True)

    # Pre-init variables
    download_now_btn = False
    add_to_queue_btn = False
    youtube_url = ""
    yt_custom_video_br = None
    yt_custom_audio_br = None
    start_batch = False
    stop_batch = False

    # 2-column layout (matches Tab 1)
    yt_left_col, yt_right_col = st.columns([2, 3])

    with yt_left_col:
        st.markdown('<p class="vt-section-label">DOWNLOAD SETTINGS</p>', unsafe_allow_html=True)

        with st.container(key="yt_config_panel"):
            # yt-dlp version info
            current_ver = st.session_state['yt_downloader'].get_yt_dlp_version()
            ver_display = current_ver or 'Not found'
            st.markdown(f'<p style="font-size:0.8rem;color:var(--text-secondary);margin-bottom:0.5rem;">yt-dlp <strong>v{ver_display}</strong></p>', unsafe_allow_html=True)
            if st.button("Update yt-dlp", key="ytdlp_update_btn", use_container_width=True):
                with st.spinner("Updating yt-dlp..."):
                    success, output, new_ver = st.session_state['yt_downloader'].update_yt_dlp()
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
            else:
                yt_selected_codec = "h264"
                yt_selected_resolution = "original"
                yt_selected_quality = "balanced"
                yt_selected_fps = "original"
                yt_selected_scan = "progressive"

        # Default values if expander not opened
        if 'yt_selected_fps' not in dir():
            yt_selected_fps = "original"
        if 'yt_selected_scan' not in dir():
            yt_selected_scan = "progressive"

        st.markdown('<p class="vt-section-label">SAVE LOCATION</p>', unsafe_allow_html=True)
        with st.container(key="yt_save_panel"):
            st.text_input("Location:", value=st.session_state['yt_save_folder_path'], disabled=True, label_visibility="collapsed", key="yt_save_path_display")
            if st.button("Select Folder", key="yt_folder_select", use_container_width=True):
                selected = open_folder_dialog()
                if selected:
                    st.session_state['yt_save_folder_path'] = selected
                    st.rerun()

        # Start/Stop batch buttons
        if st.session_state['yt_queue']:
            _sel_count = sum(1 for item in st.session_state['yt_queue']
                           if st.session_state['yt_queue_selection'].get(item['url'], True))
            if _sel_count > 0:
                col_start, col_stop = st.columns([3, 1])
                with col_start:
                    start_batch = st.button(
                        f"▶ Batch Download ({_sel_count})",
                        type="primary", use_container_width=True,
                        disabled=st.session_state.get('yt_download_running', False)
                    )
                with col_stop:
                    stop_batch = st.button(
                        "■ Stop", use_container_width=True,
                        disabled=not st.session_state.get('yt_download_running', False),
                        key="yt_stop_batch"
                    )

    with yt_right_col:
        st.markdown('<p class="vt-section-label">DOWNLOAD QUEUE</p>', unsafe_allow_html=True)

        with st.container(key="yt_queue_panel"):
            # URL input
            youtube_url = st.text_input(
                "YouTube URL:",
                value=st.session_state.get('yt_url_input', ''),
                placeholder="https://www.youtube.com/watch?v=...",
                key="yt_url_input_field",
                label_visibility="collapsed"
            )
            if youtube_url != st.session_state.get('yt_url_input', ''):
                st.session_state['yt_url_input'] = youtube_url

            col_dl, col_q = st.columns(2)
            with col_dl:
                download_now_btn = st.button(
                    "Download Now", type="primary", use_container_width=True,
                    disabled=not youtube_url or st.session_state.get('yt_download_running', False),
                    key="yt_download_now"
                )
            with col_q:
                add_to_queue_btn = st.button(
                    "Add to Queue", use_container_width=True,
                    disabled=not youtube_url or st.session_state.get('yt_download_running', False),
                    key="yt_add_to_queue"
                )

            # Queue items
            if st.session_state['yt_queue']:
                col_sel, col_clr = st.columns(2)
                with col_sel:
                    all_selected = all(st.session_state['yt_queue_selection'].get(item['url'], True) for item in st.session_state['yt_queue'])
                    sel_label = "☐ Deselect All" if all_selected else "☑ Select All"
                    if st.button(sel_label, use_container_width=True, key="yt_select_all"):
                        new_state = not all_selected
                        for item in st.session_state['yt_queue']:
                            st.session_state['yt_queue_selection'][item['url']] = new_state
                        st.session_state['yt_toggle_counter'] += 1
                        st.rerun()
                with col_clr:
                    if st.button("Clear Queue", use_container_width=True, key="yt_clear_queue"):
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
                        toggle_counter = st.session_state.get('yt_toggle_counter', 0)
                        is_selected = st.checkbox(
                            "sel", value=st.session_state['yt_queue_selection'].get(url, True),
                            key=f"yt_queue_check_{idx}_{toggle_counter}", label_visibility="collapsed"
                        )
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
                            st.markdown('<p style="font-size:0.75rem;color:var(--text-muted);margin:0;">Download Only</p>', unsafe_allow_html=True)
                        else:
                            codec_name = codec_options[settings['codec']].split(' - ')[0]
                            res_name = resolution_options[settings['resolution']]
                            st.markdown(f'<p style="font-size:0.75rem;color:var(--text-muted);margin:0;">Convert: {codec_name}, {res_name}</p>', unsafe_allow_html=True)
                    with col_remove:
                        if st.button("✕", key=f"yt_remove_{idx}", help="Remove"):
                            items_to_remove.append(idx)

                if items_to_remove:
                    for idx in sorted(items_to_remove, reverse=True):
                        removed_url = st.session_state['yt_queue'][idx]['url']
                        st.session_state['yt_queue'].pop(idx)
                        if removed_url in st.session_state['yt_queue_selection']:
                            del st.session_state['yt_queue_selection'][removed_url]
                    st.rerun()
            else:
                st.info("Enter a YouTube URL above to begin")

    # --- Download logic (full width, after both columns) ---
    if stop_batch:
        st.session_state['yt_downloader'].stop_download()
        st.session_state['converter'].stop_conversion()
        st.session_state['yt_download_running'] = False
        st.warning("Batch operation has been stopped.")
        st.rerun()

    if start_batch and not st.session_state.get('yt_download_running', False):
        with st.spinner("Preparing batch download/conversion..."):
            st.session_state['yt_download_running'] = True
        st.rerun()

    if download_now_btn and youtube_url:
        st.session_state['yt_download_running'] = True
        download_and_convert_youtube(
            youtube_url, yt_selected_codec, yt_selected_resolution, yt_selected_quality,
            yt_selected_fps, yt_selected_scan, yt_custom_video_br, yt_custom_audio_br, download_only
        )
        st.session_state['yt_download_running'] = False
        st.session_state['yt_url_input'] = ""

    if add_to_queue_btn and youtube_url:
        already_in_queue = youtube_url in [item['url'] for item in st.session_state['yt_queue']]
        if not already_in_queue:
            with st.spinner("Fetching video title..."):
                video_info = st.session_state['yt_downloader'].get_video_title_fast(youtube_url)
            if video_info:
                queue_item = {
                    'url': youtube_url,
                    'info': video_info,
                    'settings': {
                        'codec': yt_selected_codec, 'resolution': yt_selected_resolution,
                        'quality': yt_selected_quality, 'fps': yt_selected_fps,
                        'scan': yt_selected_scan, 'custom_video_br': yt_custom_video_br,
                        'custom_audio_br': yt_custom_audio_br, 'download_only': download_only
                    }
                }
                st.session_state['yt_queue'].append(queue_item)
                st.session_state['yt_queue_selection'][youtube_url] = True
                st.success(f"Added: {video_info['title']}")
                st.session_state['yt_url_input'] = ""
                st.rerun()
            else:
                st.error("Cannot fetch video title. Check URL.")
        else:
            st.warning("Already in queue!")

    if st.session_state.get('yt_download_running', False) and st.session_state['yt_queue']:
        selected_items = [item for item in st.session_state['yt_queue']
                         if st.session_state['yt_queue_selection'].get(item['url'], True)]
        if selected_items:
            batch_download_and_convert(selected_items)


# Tab 3: Mute Video (무음 비디오 생성)
with tab3:
    with st.container(key="mute_card"):
        st.markdown('''
        <h3 style="font-size: 1.17em; margin-bottom: 0.5rem;">
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display: inline-block; vertical-align: middle; margin-right: 8px;">
                <path d="M11 5L6 9H2v6h4l5 4V5z"></path>
                <line x1="23" y1="9" x2="17" y2="15"></line>
                <line x1="17" y1="9" x2="23" y2="15"></line>
            </svg>
            Mute Video Generator
        </h3>
        ''', unsafe_allow_html=True)

        st.markdown('''
        <div class="feature-box" style="margin-bottom: 1.5rem;">
            <div style="display: flex; align-items: flex-start; gap: 0.75rem;">
                <span style="font-size: 1.5rem; line-height: 1;">🔇</span>
                <div>
                    <h4 style="margin: 0 0 0.25rem 0; font-size: 0.95rem;">Remove Audio Track from Video</h4>
                    <ul style="font-size: 0.85rem; margin: 0; padding-left: 1.25rem;">
                        <li>Paste a direct video URL (S3, CDN, archive) or local file path</li>
                        <li>Video stream is copied without re-encoding — <b>extremely fast</b></li>
                        <li>Output saved to Downloads folder</li>
                    </ul>
                </div>
            </div>
        </div>
        ''', unsafe_allow_html=True)

        mute_input = st.text_input(
            "Video URL or local file path:",
            placeholder="https://example.com/video.mp4",
            key="mute_input_source"
        )

        default_output_name = "output_muted.mp4"
        if mute_input:
            from urllib.parse import urlparse, unquote
            if mute_input.startswith(('http://', 'https://')):
                parsed_path = urlparse(mute_input).path
                src_name = os.path.splitext(os.path.basename(unquote(parsed_path)))[0]
            else:
                src_name = os.path.splitext(os.path.basename(mute_input))[0]
            if src_name:
                default_output_name = f"{src_name}_muted.mp4"

        mute_output_name = st.text_input("Output filename:", value=default_output_name, key="mute_output_name")

        default_save_path = os.path.join(os.path.expanduser("~"), "Downloads")
        st.markdown(f'''
        <div style="display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1rem; padding: 0.5rem 0.75rem; background-color: var(--bg-tertiary); border-radius: 0.375rem;">
            <span style="font-size: 0.85rem;">📁</span>
            <span style="font-size: 0.8rem; color: var(--text-secondary);">Save to: <b>{default_save_path}</b></span>
        </div>
        ''', unsafe_allow_html=True)

        if st.button("🔇 Generate Muted Video", type="primary", key="mute_generate_btn",
                     disabled=not mute_input, use_container_width=True):
            output_file = os.path.join(default_save_path, mute_output_name)
            is_url = mute_input.startswith(('http://', 'https://'))

            if is_url:
                with st.spinner("Generating muted video from URL..."):
                    success, msg = st.session_state['converter'].strip_audio(mute_input, output_file, None)
            else:
                progress_bar = st.progress(0)
                status_text = st.empty()
                status_text.markdown("**🔇 Generating muted video...**")
                def mute_progress(p):
                    progress_bar.progress(min(p, 1.0))
                success, msg = st.session_state['converter'].strip_audio(mute_input, output_file, mute_progress)
                status_text.empty()
                if success:
                    progress_bar.progress(1.0)
                else:
                    progress_bar.empty()

            if success:
                file_size_mb = os.path.getsize(output_file) / (1024 * 1024)
                st.success(f"Saved: {mute_output_name} ({file_size_mb:.1f} MB)")
            else:
                st.error(f"Failed: {msg}")

# 푸터
st.markdown("---")
st.markdown("""
<div style="text-align: center; color: var(--text-secondary); padding: 2rem;">
    <p>🎥 Video Tool v6.0 - Made by Channy</p>
    <p>Video conversion tool with batch YouTube download support.</p>
</div>
""", unsafe_allow_html=True)


