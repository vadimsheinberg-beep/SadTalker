"""Minimal PNG read/crop, so slide rasterising needs no imaging library.

Only what the Chromium fallback in :mod:`pipeline.render.deck` needs: decode a
truecolour PNG, sample it, and crop from the top-left. Anything richer belongs
in a real imaging dependency, not here.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class PngError(RuntimeError):
    pass


@dataclass
class Png:
    width: int
    height: int
    channels: int
    bit_depth: int
    colour_type: int
    rows: list[bytes]

    def pixel(self, x: int, y: int) -> tuple[int, ...]:
        offset = x * self.channels
        return tuple(self.rows[y][offset : offset + self.channels])


def read_png(path: Path) -> Png:
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise PngError(f"{path} is not a PNG")

    position = 8
    compressed = b""
    width = height = bit_depth = colour_type = 0
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        chunk_type = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        if chunk_type == b"IHDR":
            width, height, bit_depth, colour_type = struct.unpack(">IIBB", payload[:10])
        elif chunk_type == b"IDAT":
            compressed += payload
        elif chunk_type == b"IEND":
            break
        position += 12 + length

    if bit_depth != 8 or colour_type not in (2, 6):
        raise PngError(
            f"unsupported PNG: bit depth {bit_depth}, colour type {colour_type} "
            "(only 8-bit RGB/RGBA is handled)"
        )

    channels = CHANNELS[colour_type]
    stride = width * channels
    raw = zlib.decompress(compressed)
    rows: list[bytes] = []
    previous = bytearray(stride)
    index = 0
    for _ in range(height):
        filter_type = raw[index]
        index += 1
        line = bytearray(raw[index : index + stride])
        index += stride
        _unfilter(line, previous, filter_type, channels)
        rows.append(bytes(line))
        previous = line

    return Png(width, height, channels, bit_depth, colour_type, rows)


def _unfilter(line: bytearray, previous: bytearray, filter_type: int, channels: int) -> None:
    if filter_type == 0:
        return
    for x in range(len(line)):
        left = line[x - channels] if x >= channels else 0
        up = previous[x]
        up_left = previous[x - channels] if x >= channels else 0
        if filter_type == 1:
            line[x] = (line[x] + left) & 0xFF
        elif filter_type == 2:
            line[x] = (line[x] + up) & 0xFF
        elif filter_type == 3:
            line[x] = (line[x] + (left + up) // 2) & 0xFF
        elif filter_type == 4:
            estimate = left + up - up_left
            da, db, dc = (
                abs(estimate - left),
                abs(estimate - up),
                abs(estimate - up_left),
            )
            nearest = left if (da <= db and da <= dc) else (up if db <= dc else up_left)
            line[x] = (line[x] + nearest) & 0xFF
        else:
            raise PngError(f"unknown PNG filter type {filter_type}")


def write_png(path: Path, image: Png) -> Path:
    """Re-encode with no per-row filtering. Larger files, simpler code."""
    body = b"".join(b"\x00" + row for row in image.rows)
    chunks = [
        _chunk(
            b"IHDR",
            struct.pack(
                ">IIBBBBB", image.width, image.height, 8, image.colour_type, 0, 0, 0
            ),
        ),
        _chunk(b"IDAT", zlib.compress(body, 6)),
        _chunk(b"IEND", b""),
    ]
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"".join(chunks))
    return Path(path)


def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def crop_top_left(path: Path, width: int, height: int) -> Path:
    """Crop a PNG in place to ``width`` x ``height`` anchored at the top-left."""
    image = read_png(path)
    if image.width == width and image.height == height:
        return Path(path)
    if image.width < width or image.height < height:
        raise PngError(
            f"cannot crop {image.width}x{image.height} up to {width}x{height}"
        )
    stride = width * image.channels
    image.rows = [row[:stride] for row in image.rows[:height]]
    image.width, image.height = width, height
    return write_png(path, image)
