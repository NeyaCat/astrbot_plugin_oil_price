"""Shared image shell and persistent, validated header uploads.

Assemble local partials before sending a standalone template to AstrBot's renderer;
the remote renderer never needs access to this plugin's filesystem.
"""
from __future__ import annotations

import base64
import binascii
from io import BytesIO
from pathlib import Path
import tempfile

TEMPLATES = Path(__file__).resolve().parent / "templates"
DEFAULT_HEADER = Path(__file__).resolve().parent / "assets" / "bg_main.jpg"
HEADER_FILENAME = "header_background.jpg"
MAX_UPLOAD_BYTES = 512 * 1024


def compose_template(kind: str) -> str:
    if kind not in {"report", "province", "calc"}:
        raise ValueError("Unknown image component")
    def read(name: str) -> str:
        return (TEMPLATES / name).read_text(encoding="utf-8")
    return (read("base.html.j2")
            .replace("/* COMMON_CSS */", read("common.css"))
            .replace("/* CONTENT_CSS */", read(f"{kind}.css"))
            .replace("<!-- CONTENT -->", read(f"{kind}.html.j2"))
            .replace("__KIND__", kind))


def header_data_uri(data_dir: Path | None = None) -> str:
    candidates = ([Path(data_dir) / HEADER_FILENAME] if data_dir else []) + [DEFAULT_HEADER]
    for path in candidates:
        try:
            return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            continue
    return ""


def save_header(data_dir: Path, data_uri: str) -> None:
    """Accept raster data only, decode and re-encode it, then atomically replace the header."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not isinstance(data_uri, str) or len(data_uri) > MAX_UPLOAD_BYTES * 4 // 3 + 128:
        raise ValueError("上传图片不能超过 512 KB，请缩小图片后重试")
    try:
        prefix, encoded = data_uri.split(",", 1)
        if prefix not in {"data:image/jpeg;base64", "data:image/png;base64", "data:image/webp;base64"}:
            raise ValueError("仅支持 JPEG、PNG、WebP 图片")
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise ValueError("上传图片不能超过 512 KB")
        with Image.open(BytesIO(raw)) as original:
            if original.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("仅支持 JPEG、PNG、WebP 图片")
            if original.width * original.height > 16_000_000:
                raise ValueError("图片尺寸过大，请缩小后重试")
            original.load()
            image = ImageOps.exif_transpose(original).convert("RGBA")
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            background = Image.new("RGB", image.size, "#2a3138")
            background.paste(image, mask=image.getchannel("A"))
            output = BytesIO()
            background.save(output, "JPEG", quality=88)
    except (binascii.Error, UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("图片无效或无法解码，请重新选择图片") from exc
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    # Never use an uploaded filename or a caller-supplied filesystem path.
    with tempfile.NamedTemporaryFile(dir=data_dir, prefix="header_", suffix=".tmp", delete=False) as f:
        temporary = Path(f.name)
        f.write(output.getvalue())
    try:
        temporary.replace(data_dir / HEADER_FILENAME)
    finally:
        temporary.unlink(missing_ok=True)


def reset_header(data_dir: Path) -> None:
    (Path(data_dir) / HEADER_FILENAME).unlink(missing_ok=True)
