"""
Video clip extraction and processing using yt-dlp and FFmpeg
Supports YouTube, Bilibili, and 1000+ other sites
"""
import os
import tempfile
import asyncio
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass
import re

@dataclass
class ClipSpec:
    """Specification for a single clip"""
    url: str
    start: float  # seconds
    end: float    # seconds
    resolution: Optional[str] = None  # e.g., "1080p", "720p", "480p", "360p"
    fps: Optional[int] = None
    bitrate: Optional[str] = None  # e.g., "2500k", "1500k"
    output_format: Optional[str] = None  # e.g., "mp4", "gif", "mp3"
    estimated_size_mb: float = 0.0
    video_title: str = ""
    source_site: str = ""

@dataclass
class QualityOption:
    """A suggested quality setting"""
    resolution: str
    bitrate: str
    fps: int
    estimated_size_mb: float
    label: str  # e.g., "A", "B", "C"

# Per-channel clip job storage
pending_clips: Dict[str, List[ClipSpec]] = {}

def parse_time(time_str: str) -> float:
    """
    Parse time string to seconds
    Supports: "65", "1:05", "1:05.5"
    """
    if ':' in time_str:
        parts = time_str.split(':')
        if len(parts) == 2:  # MM:SS
            return float(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 3:  # HH:MM:SS
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    return float(time_str)

def format_time(seconds: float) -> str:
    """Convert seconds to MM:SS format"""
    mins = int(seconds // 60)
    secs = seconds % 60
    if secs == int(secs):
        return f"{mins}:{int(secs):02d}"
    return f"{mins}:{secs:05.2f}"

def estimate_clip_size(duration: float, resolution: str, bitrate: str, fps: int, audio: bool = True) -> float:
    """
    Estimate clip size in MB
    
    Args:
        duration: Duration in seconds
        resolution: e.g., "1080p", "720p"
        bitrate: Video bitrate e.g., "2500k"
        fps: Frames per second
        audio: Include audio bitrate
    
    Returns:
        Estimated size in MB
    """
    # Parse video bitrate (e.g., "2500k" -> 2500)
    video_kbps = float(bitrate.replace('k', '').replace('K', ''))
    
    # Audio bitrate (typical: 128kbps for most formats)
    audio_kbps = 128 if audio else 0
    
    # Total bitrate
    total_kbps = video_kbps + audio_kbps
    
    # Size in MB = (bitrate_kbps * duration_seconds) / 8 / 1024
    size_mb = (total_kbps * duration) / 8 / 1024
    
    return round(size_mb, 2)

def get_quality_options(duration: float, max_size_mb: float, output_format: str = "mp4") -> List[QualityOption]:
    """
    Generate quality options that fit within size limit
    
    Returns 3-4 options ranging from highest to lowest quality
    """
    # Check if audio-only
    audio_formats = ['mp3', 'm4a', 'wav', 'aac', 'ogg', 'flac']
    is_audio = output_format.lower() in audio_formats
    
    if is_audio:
        # Audio-only options
        options = [
            QualityOption("audio", "320k", 0, estimate_clip_size(duration, "audio", "320k", 0, True), "A"),
            QualityOption("audio", "192k", 0, estimate_clip_size(duration, "audio", "192k", 0, True), "B"),
            QualityOption("audio", "128k", 0, estimate_clip_size(duration, "audio", "128k", 0, True), "C"),
        ]
        return [opt for opt in options if opt.estimated_size_mb <= max_size_mb] or [options[-1]]
    
    # Video quality presets
    presets = [
        ("1080p", "2500k", 30),
        ("720p", "1500k", 30),
        ("480p", "1000k", 30),
        ("360p", "600k", 30),
    ]
    
    # GIF has different handling
    if output_format.lower() == 'gif':
        presets = [
            ("720p", "1000k", 15),
            ("480p", "600k", 15),
            ("360p", "400k", 10),
        ]
    
    options = []
    labels = ['A', 'B', 'C', 'D', 'E']
    
    for i, (res, br, fps) in enumerate(presets):
        size = estimate_clip_size(duration, res, br, fps, output_format != 'gif')
        options.append(QualityOption(res, br, fps, size, labels[i]))
    
    return options

async def get_video_info(url: str) -> Tuple[str, str, float]:
    """
    Get video information without downloading
    
    Returns:
        (title, site_name, duration)
    """
    try:
        import yt_dlp
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Unknown')
            site = info.get('extractor_key', 'Unknown')
            duration = info.get('duration', 0)
            
            return title, site, duration
    except Exception as e:
        print(f"Error getting video info: {e}")
        return "Unknown", "Unknown", 0

async def create_clip(clip: ClipSpec, output_path: str) -> bool:
    """
    Download and process clip using yt-dlp and FFmpeg
    
    Args:
        clip: ClipSpec with all settings
        output_path: Where to save the output file
    
    Returns:
        True if successful, False otherwise
    """
    try:
        import yt_dlp
        
        # Determine if audio-only
        audio_formats = ['mp3', 'm4a', 'wav', 'aac', 'ogg', 'flac']
        is_audio = clip.output_format and clip.output_format.lower() in audio_formats
        
        # Build yt-dlp options
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'format': 'bestaudio' if is_audio else 'bestvideo+bestaudio',
            'outtmpl': output_path.replace(f'.{clip.output_format}', '.temp.%(ext)s'),
        }
        
        # Download video
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clip.url, download=True)
            temp_file = ydl.prepare_filename(info).replace('.%(ext)s', f'.temp.{info["ext"]}')
        
        # Build FFmpeg command for clipping and processing
        duration = clip.end - clip.start
        
        ffmpeg_cmd = [
            'ffmpeg',
            '-ss', str(clip.start),
            '-i', temp_file,
            '-t', str(duration),
        ]
        
        # Format-specific settings
        if clip.output_format == 'gif':
            # GIF conversion
            fps_val = clip.fps or 15
            ffmpeg_cmd.extend([
                '-vf', f'fps={fps_val},scale=-1:360:flags=lanczos',
                '-loop', '0',
            ])
        elif is_audio:
            # Audio extraction
            bitrate_val = clip.bitrate or '192k'
            ffmpeg_cmd.extend([
                '-vn',  # No video
                '-b:a', bitrate_val,
            ])
        else:
            # Video clipping
            filters = []
            
            # Resolution scaling
            if clip.resolution:
                height = clip.resolution.replace('p', '')
                filters.append(f'scale=-2:{height}')
            
            # FPS
            if clip.fps:
                filters.append(f'fps={clip.fps}')
            
            if filters:
                ffmpeg_cmd.extend(['-vf', ','.join(filters)])
            
            # Bitrate
            if clip.bitrate:
                ffmpeg_cmd.extend(['-b:v', clip.bitrate])
        
        # Output file
        ffmpeg_cmd.extend([
            '-y',  # Overwrite
            output_path
        ])
        
        # Run FFmpeg
        process = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        await process.communicate()
        
        # Clean up temp file
        try:
            os.remove(temp_file)
        except:
            pass
        
        return process.returncode == 0
        
    except Exception as e:
        print(f"Error creating clip: {e}")
        return False

def get_discord_size_limit(premium_tier: int) -> float:
    """
    Get Discord file size limit based on server boost level
    
    Args:
        premium_tier: 0 (no boost), 1 (Level 1), 2 (Level 2), 3 (Level 3)
    
    Returns:
        Size limit in MB
    """
    limits = {
        0: 10,   # No boost
        1: 10,   # Level 1 (still 10MB)
        2: 50,   # Level 2
        3: 100,  # Level 3
    }
    return limits.get(premium_tier, 10)

def store_pending_clips(channel_id: str, clips: List[ClipSpec]):
    """Store pending clips for a channel"""
    pending_clips[channel_id] = clips

def get_pending_clips(channel_id: str) -> Optional[List[ClipSpec]]:
    """Get pending clips for a channel"""
    return pending_clips.get(channel_id)

def clear_pending_clips(channel_id: str):
    """Clear pending clips for a channel"""
    if channel_id in pending_clips:
        del pending_clips[channel_id]

def update_clip_setting(channel_id: str, clip_index: int, **kwargs):
    """
    Update specific clip settings
    
    Args:
        channel_id: Discord channel ID
        clip_index: Which clip to update (0-based)
        **kwargs: Settings to update (resolution, fps, bitrate, etc.)
    """
    clips = pending_clips.get(channel_id)
    if not clips or clip_index >= len(clips):
        return False
    
    clip = clips[clip_index]
    
    for key, value in kwargs.items():
        if hasattr(clip, key):
            setattr(clip, key, value)
    
    # Recalculate estimated size
    if clip.resolution and clip.bitrate and clip.fps is not None:
        duration = clip.end - clip.start
        is_audio = clip.output_format and clip.output_format.lower() in ['mp3', 'm4a', 'wav', 'aac', 'ogg', 'flac']
        clip.estimated_size_mb = estimate_clip_size(
            duration, 
            clip.resolution or "720p", 
            clip.bitrate or "1500k", 
            clip.fps or 30,
            not (clip.output_format == 'gif' or is_audio)
        )
    
    return True
