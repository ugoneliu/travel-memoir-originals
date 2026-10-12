#!/usr/bin/env python3
"""Compose original photos and protected decoration layers from version-3 JSON.

Only Pillow and Python's standard library are required. This module does not
detect faces, create layouts or judge whether a supplied mask omitted a person.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import random
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps


class EngineError(ValueError):
    pass


def fields(value: Any, allowed: set[str], required: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise EngineError(f"{label} must be an object")
    unknown, missing = set(value) - allowed, required - set(value)
    if unknown or missing:
        raise EngineError(f"{label}: unknown={sorted(unknown)}, missing={sorted(missing)}")
    return value


def integer(value: Any, label: str, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise EngineError(f"{label} must be an integer" + (f" >= {minimum}" if minimum is not None else ""))
    return value


def named(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EngineError(f"{label} must be a nonempty string")
    return value


def resolve(value: Any, base: Path, label: str) -> Path:
    path = Path(named(value, label)).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def read_spec(path: Path) -> tuple[Path, dict]:
    path = path.expanduser().resolve()
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EngineError(f"Cannot read layout: {error}") from error
    fields(spec, {"version", "background", "output", "report", "photos", "foregrounds"},
           {"version", "background", "output", "photos"}, "layout")
    if integer(spec["version"], "version") != 3:
        raise EngineError("Only version: 3 is supported")
    if not isinstance(spec["photos"], list) or not spec["photos"]:
        raise EngineError("photos must be a nonempty array")
    if not isinstance(spec.get("foregrounds", []), list):
        raise EngineError("foregrounds must be an array")
    return path, spec


def same_file(a: Path, b: Path) -> bool:
    return a == b or (a.exists() and b.exists() and a.samefile(b))


def safe_destination(path: Path, inputs: list[Path], label: str) -> None:
    for source in inputs:
        if same_file(path, source):
            raise EngineError(f"{label} must not overwrite input: {source}")
    if path.exists() and not path.is_file():
        raise EngineError(f"{label} must be a regular file: {path}")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def open_image(path: Path) -> tuple[Image.Image, dict]:
    try:
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            meta = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
                    "encoded_size": list(source.size), "format": source.format,
                    "exif_orientation": source.getexif().get(274)}
            image = ImageOps.exif_transpose(source).copy()
    except (OSError, ValueError) as error:
        raise EngineError(f"Cannot decode {path}: {error}") from error
    meta["oriented_size"] = list(image.size)
    return image, meta


def png_layer(path: Path, label: str, rgba_required: bool = False) -> tuple[Image.Image, dict]:
    image, meta = open_image(path)
    if meta["format"] != "PNG" or meta["exif_orientation"] not in (None, 1):
        raise EngineError(f"{label} must be a PNG already in canvas orientation")
    if rgba_required and image.mode != "RGBA":
        raise EngineError(f"{label} must use PNG RGBA, with explicit transparency")
    return image.convert("RGBA"), meta


def load_mask(value: Any, size: tuple[int, int], base: Path, label: str,
              binary: bool = True) -> tuple[Image.Image, list[Path]]:
    value = fields(value, {"path", "rectangles", "polygons"}, set(), label)
    if "path" in value:
        if len(value) != 1:
            raise EngineError(f"{label}: path cannot be combined with shapes")
        path = resolve(value["path"], base, label + ".path")
        image, meta = open_image(path)
        if (meta["format"] != "PNG" or image.mode != "L" or image.size != size
                or meta["exif_orientation"] not in (None, 1)):
            raise EngineError(f"{label}: require oriented grayscale L PNG of size {size}")
        inputs = [path]
    else:
        if not binary:
            raise EngineError(f"{label}: custom alpha requires {{\"path\": ...}}")
        if not value:
            raise EngineError(f"{label}: provide path, rectangles or polygons")
        image = Image.new("L", size)
        draw = ImageDraw.Draw(image)
        for key in ("rectangles", "polygons"):
            if not isinstance(value.get(key, []), list):
                raise EngineError(f"{label}.{key} must be an array")
        for index, box in enumerate(value.get("rectangles", [])):
            if not isinstance(box, list) or len(box) != 4:
                raise EngineError(f"{label}.rectangles[{index}] must be XYXY")
            x1, y1, x2, y2 = [integer(v, label + ".rectangle", 0) for v in box]
            if not (x1 < x2 <= size[0] and y1 < y2 <= size[1]):
                raise EngineError(f"{label}: rectangle outside source or empty")
            draw.rectangle((x1, y1, x2 - 1, y2 - 1), fill=255)
        for index, polygon in enumerate(value.get("polygons", [])):
            if not isinstance(polygon, list) or len(polygon) < 3:
                raise EngineError(f"{label}.polygons[{index}] needs >=3 points")
            points = []
            for point in polygon:
                if not isinstance(point, list) or len(point) != 2:
                    raise EngineError(f"{label}: polygon points must be [x,y]")
                x, y = [integer(v, label + ".point", 0) for v in point]
                if x >= size[0] or y >= size[1]:
                    raise EngineError(f"{label}: polygon point outside source")
                points.append((x, y))
            if len(set(points)) < 3 or not any(
                    (points[i][0] - points[0][0]) * (points[j][1] - points[0][1]) !=
                    (points[j][0] - points[0][0]) * (points[i][1] - points[0][1])
                    for i in range(1, len(points)) for j in range(i + 1, len(points))):
                raise EngineError(f"{label}: polygon must have nonzero area")
            draw.polygon(points, fill=255)
        inputs = []
    if binary and sum(image.histogram()[1:255]):
        raise EngineError(f"{label} must contain only 0 and 255")
    if not image.getbbox():
        raise EngineError(f"{label} must not be empty")
    return image, inputs


def cover_resize(source: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Conservative coverage: retain every source mask pixel, including hair tips."""
    width, height = size
    source_w, source_h = source.size
    result = Image.new("L", size)
    draw = ImageDraw.Draw(result)
    data = source.tobytes()
    for sy in range(source_h):
        row = data[sy * source_w:(sy + 1) * source_w]
        first = row.find(b"\xff")
        while first != -1:
            end = row.find(b"\x00", first)
            if end == -1:
                end = source_w
            draw.rectangle((first * width // source_w, sy * height // source_h,
                            (end * width + source_w - 1) // source_w - 1,
                            ((sy + 1) * height + source_h - 1) // source_h - 1), fill=255)
            first = row.find(b"\xff", end)
    return result


def geometry(image: Image.Image, width: int) -> tuple[int, int]:
    # Exact half-up rounding; no second photo resize to fit a slot.
    return width, max(1, (2 * image.height * width + image.width) // (2 * image.width))


def photo_fields(value: Any, label: str) -> dict:
    value = fields(value, {"id", "path", "x", "y", "width", "protect", "audit", "shape", "border"},
                   {"id", "path", "x", "y", "width", "protect", "audit", "shape"}, label)
    named(value["id"], label + ".id")
    integer(value["x"], label + ".x")
    integer(value["y"], label + ".y")
    integer(value["width"], label + ".width", 1)
    if not isinstance(value["audit"], list) or not value["audit"]:
        raise EngineError(f"{label}.audit must be a nonempty array")
    names = set()
    for audit in value["audit"]:
        fields(audit, {"name", "mask"}, {"name", "mask"}, label + ".audit")
        name = named(audit["name"], label + ".audit.name")
        if name in names:
            raise EngineError(f"{label}: duplicate audit name {name}")
        names.add(name)
    return value


def ensure_inside(mask: Image.Image, x: int, y: int, canvas_size: tuple[int, int], label: str) -> None:
    left, top, right, bottom = mask.getbbox()
    if left + x < 0 or top + y < 0 or right + x > canvas_size[0] or bottom + y > canvas_size[1]:
        raise EngineError(f"{label}: checked/protected pixels would be clipped by canvas")


def torn_alpha(size: tuple[int, int], depth: int, step: int, seed: int) -> Image.Image:
    width, height = size
    if depth * 2 >= min(size):
        raise EngineError("torn depth must be less than half the shorter rendered dimension")
    rng = random.Random(seed)
    points = [(rng.randint(0, depth), rng.randint(0, depth))]
    points += [(x, rng.randint(0, depth)) for x in range(step, width - 1, step)]
    points += [(width - 1 - rng.randint(0, depth), rng.randint(0, depth))]
    points += [(width - 1 - rng.randint(0, depth), y) for y in range(step, height - 1, step)]
    points += [(width - 1 - rng.randint(0, depth), height - 1 - rng.randint(0, depth))]
    points += [(x, height - 1 - rng.randint(0, depth)) for x in range(width - 1 - step, 0, -step)]
    points += [(rng.randint(0, depth), height - 1 - rng.randint(0, depth))]
    points += [(rng.randint(0, depth), y) for y in range(height - 1 - step, 0, -step)]
    result = Image.new("L", size)
    ImageDraw.Draw(result).polygon(points, fill=255)
    return result


@dataclass
class PhotoLayer:
    id: str
    x: int
    y: int
    protect: Image.Image
    layer: Image.Image
    origin: tuple[int, int]
    inputs: list[Path]
    metadata: dict


def prepare_photo(value: Any, base: Path, label: str) -> PhotoLayer:
    value = photo_fields(value, label)
    path = resolve(value["path"], base, label + ".path")
    original, metadata = open_image(path)
    rgb = original.convert("RGB")
    source_alpha = original.convert("RGBA").getchannel("A")
    source_protect, inputs = load_mask(value["protect"], rgb.size, base, label + ".protect")
    if ImageChops.subtract(source_protect, source_alpha).getbbox():
        raise EngineError(f"{label}: source transparency fades protected pixels")
    size = geometry(rgb, value["width"])
    protect = cover_resize(source_protect, size)
    inputs = [path, *inputs]
    for audit in value["audit"]:
        _, audit_inputs = load_mask(audit["mask"], rgb.size, base, label + ".audit." + audit["name"])
        inputs.extend(audit_inputs)
    shape = fields(value["shape"], {"type", "depth", "step", "seed", "alpha"}, {"type"}, label + ".shape")
    if shape["type"] == "rect":
        fields(shape, {"type"}, {"type"}, label + ".shape")
        alpha = Image.new("L", size, 255)
    elif shape["type"] == "torn":
        fields(shape, {"type", "depth", "step", "seed"}, {"type"}, label + ".shape")
        depth = integer(shape.get("depth", max(1, min(size) // 30)), label + ".depth", 1)
        step = integer(shape.get("step", max(3, depth)), label + ".step", 1)
        seed = integer(shape.get("seed", 0), label + ".seed", 0)
        alpha = torn_alpha(size, depth, step, seed)
    elif shape["type"] == "custom":
        fields(shape, {"type", "alpha"}, {"type", "alpha"}, label + ".shape")
        source_shape, alpha_inputs = load_mask(shape["alpha"], rgb.size, base, label + ".alpha", binary=False)
        inputs.extend(alpha_inputs)
        if ImageChops.subtract(source_protect, source_shape).getbbox():
            raise EngineError(f"{label}: custom alpha fades source protected pixels")
        alpha = source_shape.resize(size, Image.Resampling.NEAREST)
    else:
        raise EngineError(f"{label}: shape.type must be rect, torn or custom")
    alpha = ImageChops.multiply(alpha, source_alpha.resize(size, Image.Resampling.NEAREST))
    if ImageChops.subtract(protect, alpha).getbbox():
        raise EngineError(f"{label}: rendered alpha fades protected pixels; provide margin or adjust shape")
    border = fields(value.get("border", {}), {"type", "size"}, set(), label + ".border")
    kind = border.get("type", "none")
    if kind not in ("none", "white"):
        raise EngineError(f"{label}: border.type must be none or white")
    b = integer(border.get("size", 0 if kind == "none" else 8), label + ".border.size", 0)
    if (kind == "none" and b != 0) or (kind == "white" and b == 0):
        raise EngineError(f"{label}: none requires size 0; white requires positive size")
    # Only this statement resamples original RGB. Masks are independent.
    rendered = rgb.resize(size, Image.Resampling.LANCZOS).convert("RGBA")
    rendered.putalpha(alpha)
    padded = Image.new("L", (size[0] + 2 * b, size[1] + 2 * b))
    padded.paste(alpha, (b, b))
    if b:
        outline = padded
        for _ in range(b):
            outline = outline.filter(ImageFilter.MaxFilter(3))
        layer = Image.new("RGBA", padded.size, "white")
        layer.putalpha(outline)
        layer.alpha_composite(rendered, (b, b))
    else:
        layer = rendered
    metadata.update({"id": value["id"], "source_spec_path": value["path"],
                     "rendered_size": list(size), "translation": [value["x"], value["y"]],
                     "actual_resize_factors": [size[0] / rgb.width, size[1] / rgb.height],
                     "height_rounding_error_pixels": size[1] - rgb.height * size[0] / rgb.width,
                     "resize_filter": "LANCZOS", "original_rgb_resize_passes": 1,
                     "protect_pixels": protect.histogram()[255], "shape": shape, "border": {"type": kind, "size": b}})
    return PhotoLayer(value["id"], value["x"], value["y"], protect, layer,
                      (value["x"] - b, value["y"] - b), inputs, metadata)


def visible_touches_protect(layer: Image.Image, origin: tuple[int, int], photo: PhotoLayer) -> bool:
    x, y = origin
    left, top = max(x, photo.x), max(y, photo.y)
    right = min(x + layer.width, photo.x + photo.protect.width)
    bottom = min(y + layer.height, photo.y + photo.protect.height)
    if right <= left or bottom <= top:
        return False
    visible = layer.getchannel("A").crop((left - x, top - y, right - x, bottom - y)).point([0] + [255] * 255)
    protected = photo.protect.crop((left - photo.x, top - photo.y, right - photo.x, bottom - photo.y))
    return ImageChops.multiply(visible, protected).getbbox() is not None


def clipped_composite(canvas: Image.Image, layer: Image.Image, origin: tuple[int, int]) -> None:
    x, y = origin
    left, top = max(0, x), max(0, y)
    right, bottom = min(canvas.width, x + layer.width), min(canvas.height, y + layer.height)
    if right > left and bottom > top:
        canvas.alpha_composite(layer.crop((left - x, top - y, right - x, bottom - y)), (left, top))


def atomic_json(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix=".memoir-report-", suffix=".json", dir=path.parent, delete=False)
    staged = Path(handle.name)
    try:
        with handle:
            handle.write((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def compose(spec_path: Path) -> dict:
    spec_path, spec = read_spec(spec_path)
    base = spec_path.parent
    background = resolve(spec["background"], base, "background")
    output = resolve(spec["output"], base, "output")
    report_path = resolve(spec.get("report", str(output.with_suffix(".report.json"))), base, "report")
    if output.suffix.lower() != ".png" or report_path.suffix.lower() != ".json":
        raise EngineError("output must end .png and report must end .json")
    if same_file(output, report_path):
        raise EngineError("output and report must be distinct files")
    canvas, background_meta = png_layer(background, "background")
    photos = [prepare_photo(value, base, f"photos[{i}]") for i, value in enumerate(spec["photos"])]
    if len({photo.id for photo in photos}) != len(photos):
        raise EngineError("photo ids must be unique")
    inputs = [spec_path, background, *[path for photo in photos for path in photo.inputs]]
    foregrounds = []
    for i, value in enumerate(spec.get("foregrounds", [])):
        value = fields(value, {"path", "x", "y"}, {"path", "x", "y"}, f"foregrounds[{i}]")
        path = resolve(value["path"], base, f"foregrounds[{i}].path")
        layer, meta = png_layer(path, f"foregrounds[{i}]", rgba_required=True)
        origin = (integer(value["x"], "foreground.x"), integer(value["y"], "foreground.y"))
        foregrounds.append((layer, origin, meta))
        inputs.append(path)
    safe_destination(output, inputs, "output")
    safe_destination(report_path, inputs, "report")
    for i, photo in enumerate(photos):
        ensure_inside(photo.protect, photo.x, photo.y, canvas.size, photo.id + ".protect")
        for earlier in photos[:i]:
            if visible_touches_protect(photo.layer, photo.origin, earlier):
                raise EngineError(f"photo {photo.id} would cover protected pixels of {earlier.id}")
        clipped_composite(canvas, photo.layer, photo.origin)
    for i, (layer, origin, _) in enumerate(foregrounds):
        for photo in photos:
            if visible_touches_protect(layer, origin, photo):
                raise EngineError(f"foreground {i} would cover protected pixels of {photo.id}")
        clipped_composite(canvas, layer, origin)
    output.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix=".memoir-image-", suffix=".png", dir=output.parent, delete=False)
    staged = Path(handle.name)
    handle.close()
    try:
        canvas.save(staged, format="PNG")
        # Import only after this module's definitions are available. verify reads
        # the saved PNG and original files afresh, not in-memory photo layers.
        from verify import verify
        audit = verify(spec_path, staged)
        if not audit["passed"]:
            failed = [f"{c['photo_id']}/{c['name']}:{c['changed_pixels']}" for c in audit["checks"] if c["changed_pixels"]]
            raise EngineError("Saved PNG audit failed: " + ", ".join(failed))
        report = {"version": 3, "output": str(output), "canvas_size": list(canvas.size),
                  "layout_sha256": digest(spec_path), "background": background_meta,
                  "photos": [photo.metadata for photo in photos],
                  "foregrounds": [{**meta, "translation": list(origin)} for _, origin, meta in foregrounds],
                  "input_files": [{"path": str(path), "sha256": digest(path)} for path in dict.fromkeys(inputs)],
                  "verification": audit,
                  "publication": "Saved PNG audited before atomic publication; published bytes hash confirmed."}
        os.replace(staged, output)
        if digest(output) != audit["final_sha256"]:
            raise EngineError("Published PNG hash differs from verified saved PNG")
        report["verification"]["image"] = str(output)
        for item in report["verification"]["input_files"]:
            if item["path"] == str(staged):
                item["path"] = str(output)
        atomic_json(report_path, report)
    finally:
        staged.unlink(missing_ok=True)
    return {"output": str(output), "report": str(report_path), "photo_count": len(photos),
            "foreground_count": len(foregrounds), "passed": True, "final_sha256": audit["final_sha256"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layout", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(compose(args.layout), ensure_ascii=False))
        return 0
    except (EngineError, OSError, OverflowError, MemoryError) as error:
        print(f"Composition error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # verify imports these helpers; preserve this exception class in CLI mode.
    sys.modules.setdefault("compose", sys.modules[__name__])
    raise SystemExit(main())
