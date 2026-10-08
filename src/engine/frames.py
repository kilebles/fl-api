import shutil
import subprocess
from pathlib import Path

_WINDOWS_FFMPEG = (
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
)


def find_ffmpeg() -> str:
    """Locate ffmpeg on PATH or in the usual Windows install folders."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    for path in _WINDOWS_FFMPEG:
        if Path(path).exists():
            return path
    raise FileNotFoundError("ffmpeg not found. Install it and add to PATH or place at C:\\ffmpeg\\bin\\ffmpeg.exe")


def extract_last_frame(video_path: Path, output_path: Path) -> None:
    """Save the last frame of a video as JPEG."""
    subprocess.run(
        [find_ffmpeg(), "-y", "-sseof", "-0.5", "-i", str(video_path), "-frames:v", "1", "-q:v", "2", str(output_path)],
        check=True,
        capture_output=True,
    )
