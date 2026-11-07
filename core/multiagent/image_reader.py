"""Bounded image validation and descriptor-based reads inside an explicit root."""

import base64
import hashlib
import json
import os
import stat
import struct
import sys
import zlib
from pathlib import PurePosixPath


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_CONTEXT_IMAGE_BYTES = 10 * 1024 * 1024


def image_info(data):
    """Check PNG chunks or JPEG frame headers; no pixel decoder is required."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        index, dimensions, pixels = 8, None, False
        while index + 12 <= len(data):
            length = int.from_bytes(data[index:index + 4], "big")
            end = index + 12 + length
            if end > len(data):
                raise ValueError("truncated PNG chunk")
            kind = data[index + 4:index + 8]
            payload = data[index + 8:end - 4]
            checksum = int.from_bytes(data[end - 4:end], "big")
            if zlib.crc32(data[index + 4:end - 4]) & 0xffffffff != checksum:
                raise ValueError("invalid PNG checksum")
            if index == 8:
                if kind != b"IHDR" or length != 13:
                    raise ValueError("invalid PNG header")
                dimensions = struct.unpack(">II", payload[:8])
            if kind == b"IDAT":
                pixels = True
            if kind == b"IEND":
                if length or not pixels or end != len(data):
                    raise ValueError("invalid PNG ending")
                return "image/png", *dimensions
            index = end
        raise ValueError("incomplete PNG")
    if data.startswith(b"\xff\xd8"):
        index = 2
        frames = {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}
        while index < len(data):
            if data[index] != 0xff:
                break
            while index < len(data) and data[index] == 0xff:
                index += 1
            if index >= len(data):
                break
            marker = data[index]
            index += 1
            if marker in {0xd9, 0xda}:
                break
            if marker == 1 or 0xd0 <= marker <= 0xd7:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index:index + 2], "big")
            if length < 2 or index + length > len(data):
                break
            if marker in frames and length >= 8 and b"\xff\xd9" in data[index + length:]:
                height, width = struct.unpack(">HH", data[index + 3:index + 7])
                return "image/jpeg", width, height
            index += length
        raise ValueError("invalid or incomplete JPEG")
    raise ValueError("unsupported image format; use PNG or JPEG, or convert the image with Python first")


def image_attachment(data, path):
    """Validate bytes and describe an immutable image for a model or frontend."""
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image exceeds 5 MiB; resize or compress it first")
    mime, width, height = image_info(data)
    if min(width, height) <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError("image dimensions must be positive and at most 25 million pixels")
    return {"path": path, "mime_type": mime, "width": width, "height": height,
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "data_url": "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")}


def read_image(path, *, root="/workspace", label_root="/workspace"):
    if not isinstance(path, str) or not path or len(path) > 4096 or "\0" in path:
        raise ValueError("path must be a nonempty string of at most 4096 characters")
    relative = PurePosixPath(path)
    if relative.is_absolute():
        try:
            relative = relative.relative_to(root) if relative.is_relative_to(root) else relative.relative_to("/workspace")
        except ValueError:
            raise ValueError("image path must be inside /workspace") from None
    if not relative.parts or ".." in relative.parts:
        raise ValueError("use a file path inside /workspace without '..'")
    # Old immutable history may still label uploads with the former root path.
    if relative.parts[0] == ".agent-inputs":
        relative = PurePosixPath(".recoding", "inputs", *relative.parts[1:])
    elif relative.parts[0] == ".recoding...":
        relative = PurePosixPath(".recoding", *relative.parts[1:])
    # Open each component relative to a held directory descriptor. Symlinks are
    # deliberately rejected, including parent components; no resolve/open race.
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(relative.parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("image must be a regular file")
        if info.st_size > MAX_IMAGE_BYTES:
            raise ValueError("image exceeds 5 MiB; resize or compress it with Python first")
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image exceeds 5 MiB; resize or compress it with Python first")
    attachment = image_attachment(data, str(PurePosixPath(label_root)/relative))
    data_url = attachment.pop("data_url")
    return {"image": attachment, "_image_data": data_url}


def main():
    try:
        request = json.loads(sys.stdin.read(32768))
        result = read_image(request["path"])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result = {"error": str(exc)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
