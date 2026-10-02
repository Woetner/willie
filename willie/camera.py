"""Camera orientation. The Camera Module 3 sits sideways in the pod, and rpicam can only rotate 0 or 180 degrees,
so the Pi turns each JPEG itself (Pillow, imported only when a rotation is needed). `camera.rotation` is the number
of degrees to turn the picture clockwise."""

from __future__ import annotations

import io
from pathlib import Path

_STEPS = {90: "ROTATE_270", 180: "ROTATE_180", 270: "ROTATE_90"}      # PIL turns counter-clockwise


def rotation() -> int:
    """The configured rotation: 0, 90, 180 or 270."""
    try:
        from willie.config import Config
        return int(Config().get("camera.rotation")) % 360
    except Exception:
        return 0


def rotate_jpeg(data: bytes, degrees: int | None = None, quality: int = 85) -> bytes:
    """`data` turned clockwise by `degrees` (default: the setting). Returns the input when 0 or when it fails."""
    degrees = rotation() if degrees is None else degrees % 360
    if not degrees or degrees not in _STEPS:
        return data
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            out = io.BytesIO()
            im.transpose(getattr(Image.Transpose, _STEPS[degrees])).save(out, "JPEG", quality=quality)
            return out.getvalue()
    except Exception:
        return data


def rotate_file(path: Path, degrees: int | None = None, quality: int = 90) -> None:
    """The same for a JPEG file, in place."""
    path = Path(path)
    try:
        data = path.read_bytes()
        turned = rotate_jpeg(data, degrees, quality)
        if turned is not data:
            path.write_bytes(turned)
    except OSError:
        pass


def mode_args() -> list[str]:
    """rpicam arguments that pick the sensor mode. At 640x480 or 1024x768 libcamera takes the Camera Module 3's
    1536x864 mode, which is a crop of the middle 2/3 of the sensor. 2304x1296 uses the whole sensor (binned), so the
    view is 1.5x wider at the same sharpness. Setting camera.wide_fov turns it on (default) or off."""
    try:
        from willie.config import Config
        wide = bool(Config().get("camera.wide_fov"))
    except Exception:
        wide = True
    return ["--mode", "2304:1296"] if wide else []
