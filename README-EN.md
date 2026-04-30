# Video Tool v6.0

Streamlit-based video conversion tool with automatic installation

## Features

- Smart Codec Conversion: H.264, H.265, VP9, AV1
- Resolution Conversion: 4K, HD, SD
- Frame Rate Conversion: 23.976, 24, 25, 29.97, 30, 50, 59.94, 60 fps
- Deinterlacing: Progressive / Interlaced
- Custom Bitrate: Set video/audio bitrate manually
- YouTube Batch Download: Queue multiple videos for batch download/conversion
- Real-time Monitoring: Progress, ETA, stop function
- App Auto Update: Detect latest GitHub Release on launch and update with one confirmation

## Auto Update (Mac)

- Running `run.command`/`실행하기.command` or `VideoTool.app` checks the latest release automatically.
- If a newer version exists, the launcher prompts: `Update now? [Y/n]`.
- On confirmation, it downloads, applies the update, and restarts automatically.
- Update source: `https://github.com/ElieveKoh/video_tool/releases`

## How to Use

### Windows Users
1. Double-click `start.bat`
2. Wait for automatic installation (3-5 minutes on first run)
3. App opens automatically in browser (localhost:8601)
4. **Note**: Requires Python 3.10+ (check "Add Python to PATH" during installation)

### Mac Users
1. Double-click `VideoTool.app` or `run.command`
2. If security warning appears: System Settings > Privacy & Security > "Open Anyway"
3. Wait for automatic installation (3-5 minutes on first run)
4. App opens automatically in browser (localhost:8601)

## Auto-Installation

- Python 3.11 (if not installed)
- FFmpeg (video processing engine)
- yt-dlp (YouTube downloader)
- Streamlit (web interface)

All dependencies are downloaded to local `bin/` folder without touching system files.

## Usage Guide

### Tab 1: Local File Conversion
1. Click "Select Folder" or "Select Files"
2. Check files to convert
3. Select codec, resolution, quality, frame rate, scan type
4. If Custom Bitrate selected, enter video/audio bitrate
5. Click "Start Conversion"
6. Converted files saved to `converted_[codec]` folder

### Tab 2: YouTube Download + Conversion
1. Check/uncheck "Download Only"
   - Checked: Download only (no conversion)
   - Unchecked: Download + Convert
2. Select conversion settings (codec, resolution, quality, frame rate, etc.)
3. Paste YouTube URL and press Enter
4. Verify video info and automatically add to Queue
5. Add more videos with different settings
6. Select/deselect videos in Download Queue
7. Click "Start Batch Download/Convert"
8. Files saved to `youtube_downloads` folder

## Codec Comparison

| Codec | Features | Recommended Use |
|------|------|-----------|
| H.264 | Best compatibility | General use, all devices |
| H.265 | 50% smaller files | Save storage, modern devices |
| VP9 | Web optimized | YouTube, web streaming |
| AV1 | Next-gen efficiency | Future-proof, latest browsers |

## Frame Rate Guide

- 23.976 fps: Film (Cinema)
- 24 fps: Digital cinema
- 25 fps: PAL (Europe/Asia)
- 29.97 fps: NTSC (North America/Japan TV)
- 30 fps: Digital video
- 50/59.94/60 fps: High frame rate (sports, gaming)

## Quality Presets

- Fast: Quick conversion, normal quality
- Balanced: Balanced speed/quality (recommended)
- High: Slow conversion, best quality
- Custom Bitrate: Manually set video/audio bitrate

## System Requirements

- Mac: macOS 10.14+
- Windows: Windows 10+ with Python 3.10+
- Free Space: 1.5x original file size
- Internet: Required for initial setup and YouTube downloads
- Memory: Minimum 4GB RAM recommended

## Troubleshooting

**Security Warning (Mac):**
1. Double-click app shows "unidentified developer" warning
2. System Settings > Privacy & Security
3. Click "Open Anyway"

**Installation Failed:**
- Check internet connection
- Verify sufficient disk space (minimum 500MB)

**Conversion Failed:**
- Check sufficient disk space
- Verify file permissions
- Check if file is in use by another program

**YouTube Download Failed:**
- Verify URL is correct
- Check if video is private/restricted
- Verify yt-dlp is up to date (auto-downloaded)

## Folder Structure

```
VideoTool/
├── start.bat                # Windows launcher
├── video_converter_app.py   # Main app
├── requirements.txt         # Python packages
├── README.md                # Documentation
├── bin/                     # Auto-downloaded binaries
├── venv/                    # Python virtual environment
├── converted_[codec]/       # Converted local files
└── youtube_downloads/       # YouTube downloads
```

## Privacy

- All processing is done locally
- Files are not sent to external servers
- Internet connection only used for initial setup and YouTube downloads

## License

Free for personal and commercial use.

---

Made with love by Channy

## Version History

### v6.0.1 - 2026.04.30
- Fixed AppTranslocation read-only path issue when launching `VideoTool.app`
- Moved runtime venv/binaries to user-writable path (`~/Library/Application Support/VideoTool`)

### v6.0 - 2026.04.30
- Added GitHub Releases-based one-confirmation auto updater
- Enabled updater for both launcher script and `VideoTool.app` launch path
- Preserved local runtime folders during updates (`venv`, `bin`, output folders)

### v5.0 - 2025.11.26
- Added SVG tab icons
- Improved dark mode
- Simplified scan type text
- Download Only left-aligned
- Improved checkbox synchronization
- Optimized theme toggle button position
- Changed default port to 8601
- Improved distribution packages

### v4.5 - 2025.11.20
- Applied v5.0 UI design
- Improved dark mode support
- v5.0 style tabs, buttons, progress bars
- Clean layout and spacing optimization
- Primary Red & Blue color scheme
- Enhanced hover effects and transitions

### v4.0 - 2025.11.13
- Added YouTube batch download Queue system
- Added frame rate conversion
- Added deinterlacing options
- Added custom bitrate settings
- Added Download Only mode
- Fixed conversion freeze bug
- Major UI/UX improvements
