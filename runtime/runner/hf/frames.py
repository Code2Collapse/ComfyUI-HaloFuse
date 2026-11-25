"""Preview frame re-encoding for the live relay (C-3 section 11, PREVIEW_DESIGN section 3).

Every frame that travels inline in a C-3 `preview` line is re-encoded to a hard byte cap: JPEG, metadata stripped,
quality and size stepped down until it fits. The budget is a byte cap, not a quality target.

Pillow, OpenCV and NumPy are optional here: they are imported inside the functions and every failure gives None
(the caller then sends a path-only event). Nothing in this module raises.
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_BYTES = 48128  # owner 2026-11-24: previews too poor; the largest a log line carries
MIN_MAX_BYTES = 4096
MAX_MAX_BYTES = 48128
_PASS_THROUGH_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")
_VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".gif")
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")


@dataclass(frozen=True)
class Fitted:
    data: bytes
    mime: str
    w: int
    h: int


def max_bytes_from_env(env=None) -> int:
    """HF_PREVIEW_MAX_BYTES, default 24576, clamped to 4096..48128 (<= 64 KiB line as base64 with room to spare)."""
    env = os.environ if env is None else env
    try:
        value = int(str(env.get("HF_PREVIEW_MAX_BYTES") or "").strip())
    except ValueError:
        return DEFAULT_MAX_BYTES
    return max(MIN_MAX_BYTES, min(MAX_MAX_BYTES, value))


def is_video(path) -> bool:
    return Path(path).suffix.lower() in _VIDEO_EXTS


def is_image(path) -> bool:
    return Path(path).suffix.lower() in _IMAGE_EXTS


def _ladder(long_edge: int):
    edge = max(16, int(long_edge))
    steps = [(70, edge), (55, edge), (40, edge)]
    for smaller, quality in ((256, 40), (192, 30), (128, 25), (96, 20)):
        if smaller < edge:
            steps.append((quality, smaller))
    return steps


def _encode_to_cap(img, max_bytes: int, long_edge: int, mask: bool):
    from PIL import Image  # noqa: WPS433 - optional dependency

    img = img.convert("L" if mask else "RGB")
    for quality, edge in _ladder(long_edge):
        candidate = img
        if max(candidate.size) > edge:
            scale = edge / max(candidate.size)
            size = (max(1, round(candidate.size[0] * scale)), max(1, round(candidate.size[1] * scale)))
            candidate = candidate.resize(size, Image.BILINEAR)
        buf = io.BytesIO()
        candidate.save(buf, "JPEG", quality=quality, optimize=True)  # no exif/icc passed: metadata stripped
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return Fitted(data, "image/jpeg", candidate.size[0], candidate.size[1])
    return None


def _grey_image(img) -> bool:
    if img.mode in ("L", "LA", "I", "I;16", "1", "F"):
        return True
    try:
        small = img.convert("RGB")
        small.thumbnail((64, 64))
        r, g, b = small.split()
        return r.tobytes() == g.tobytes() == b.tobytes()
    except Exception:  # noqa: BLE001
        return False


def fit(image_bytes: bytes, mime: str, max_bytes: int, long_edge: int = 384, *, mask: bool = False):
    """Re-encode one frame to at most `max_bytes`. Returns Fitted or None (never raises).

    Without Pillow the original bytes pass through only when they already fit and are JPEG/PNG (w = h = 0)."""
    if not image_bytes:
        return None
    try:
        from PIL import Image  # noqa: WPS433
    except Exception:  # noqa: BLE001 - optional: path-only events then
        if len(image_bytes) <= max_bytes and image_bytes.startswith(_PASS_THROUGH_MAGIC):
            return Fitted(bytes(image_bytes), mime, 0, 0)
        return None
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img.load()
            return _encode_to_cap(img, max_bytes, long_edge, mask)
    except Exception:  # noqa: BLE001 - undecodable or truncated frame
        return None


def is_grey(image_bytes: bytes) -> bool:
    try:
        from PIL import Image  # noqa: WPS433
        with Image.open(io.BytesIO(image_bytes)) as img:
            img.load()
            return _grey_image(img)
    except Exception:  # noqa: BLE001
        return False


def image_thumb(path, max_bytes: int, long_edge: int = 384, *, mask: bool = False):
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return fit(data, "image/png", max_bytes, long_edge, mask=mask)


def video_sheet(path, max_bytes: int, long_edge: int = 512):
    """A 2x2 sheet of 4 evenly spaced frames (first, 1/3, 2/3, last) of a video file, or None."""
    try:
        import cv2  # noqa: WPS433 - optional
        import numpy as np  # noqa: WPS433
        from PIL import Image  # noqa: WPS433
    except Exception:  # noqa: BLE001
        return None
    cap = None
    try:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return None
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        picks = [0, count // 3, (2 * count) // 3, max(count - 1, 0)] if count > 0 else [0, 0, 0, 0]
        shots = []
        for index in picks:
            cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = cap.read()
            if not ok or frame is None:
                if shots:
                    shots.append(shots[-1])
                    continue
                return None
            shots.append(frame)
        h, w = shots[0].shape[:2]
        shots = [s if s.shape[:2] == (h, w) else cv2.resize(s, (w, h)) for s in shots]
        top = np.concatenate(shots[:2], axis=1)
        bottom = np.concatenate(shots[2:], axis=1)
        sheet = np.concatenate([top, bottom], axis=0)
        img = Image.fromarray(cv2.cvtColor(sheet, cv2.COLOR_BGR2RGB))
        return _encode_to_cap(img, max_bytes, long_edge, mask=False)
    except Exception:  # noqa: BLE001 - codec not readable here: no sheet
        return None
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass


def animated_webp(data: bytes, mime: str, max_bytes: int, long_edge: int = 384, max_frames: int = 16,
                  fps: float | None = None):
    """An animated sampling preview (KJNodes ModelPreviewOverride sends video/mp4 or animated image/webp) as a small
    animated WebP that fits one live line (owner 2026-11-25: the Live tab showed no sampling animation). Up to
    `max_frames` evenly spaced frames; size and quality step down until it fits. Returns Fitted or None (never raises).
    A browser plays an animated WebP inside a plain <img>, so the Live stage and the node card need no video player."""
    try:
        from PIL import Image, ImageSequence  # noqa: WPS433
    except Exception:  # noqa: BLE001
        return None
    frames = []
    try:
        if mime == "video/mp4":
            import av  # noqa: WPS433 - ComfyUI core depends on PyAV
            with av.open(io.BytesIO(data)) as container:
                stream = container.streams.video[0]
                if fps is None and stream.average_rate:
                    fps = float(stream.average_rate)
                frames = [f.to_image() for f in container.decode(stream)]
        else:
            with Image.open(io.BytesIO(data)) as img:
                if fps is None and img.info.get("duration"):
                    fps = 1000.0 / float(img.info["duration"])
                frames = [f.convert("RGB").copy() for f in ImageSequence.Iterator(img)]
    except Exception:  # noqa: BLE001 - undecodable clip
        return None
    if not frames:
        return None
    if len(frames) > max_frames:
        step = len(frames) / max_frames
        frames = [frames[int(i * step)] for i in range(max_frames)]
    duration = int(1000.0 / (fps or 12.0)) or 83
    for edge in (long_edge, 320, 256, 192, 160):
        scaled = []
        for f in frames:
            f = f.convert("RGB")
            if max(f.size) > edge:
                f = f.copy()
                f.thumbnail((edge, edge), Image.LANCZOS)
            scaled.append(f)
        for quality in (70, 55, 40, 28):
            buf = io.BytesIO()
            try:
                scaled[0].save(buf, format="WEBP", save_all=True, append_images=scaled[1:], duration=duration,
                               loop=0, quality=quality, method=4)
            except Exception:  # noqa: BLE001 - Pillow without WebP
                return None
            if buf.tell() <= max_bytes:
                return Fitted(buf.getvalue(), "image/webp", scaled[0].width, scaled[0].height)
    return None
