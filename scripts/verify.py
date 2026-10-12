#!/usr/bin/env python3
"""Independently reopen original photos and saved PNG; check complete declared ROIs.

This checks supplied audit/protection masks, not whether they include every face.
No production alpha, unchanged-pixel test or previous composition report filters
the selected pixels. Only Pillow and Python's standard library are required.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops

from compose import (EngineError, atomic_json, cover_resize, digest, ensure_inside,
                     fields, geometry, integer, load_mask, open_image, photo_fields,
                     png_layer, read_spec, resolve, safe_destination)


def check_region(saved: Image.Image, reference: Image.Image, selected: Image.Image,
                 x: int, y: int, photo_id: str, name: str, kind: str) -> dict:
    bbox = selected.getbbox()
    if not bbox:
        raise EngineError(f"{photo_id}/{name}: empty audit region")
    left, top, right, bottom = bbox
    canvas_box = (left + x, top + y, right + x, bottom + y)
    mask = selected.crop(bbox)
    actual = saved.crop(canvas_box)
    expected = reference.crop(bbox)
    difference = ImageChops.difference(actual.convert("RGB"), expected)
    channels = difference.split()
    rgb_nonzero = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2]).point([0] + [255] * 255)
    rgb_changed = ImageChops.multiply(rgb_nonzero, mask)
    alpha_difference = ImageChops.subtract(Image.new("L", mask.size, 255), actual.getchannel("A"))
    alpha_changed = ImageChops.multiply(alpha_difference.point([0] + [255] * 255), mask)
    any_changed = ImageChops.lighter(rgb_changed, alpha_changed)
    changed_bbox = any_changed.getbbox()
    maximum = max(ImageChops.multiply(channel, mask).getextrema()[1] for channel in channels)
    count = mask.histogram()[255]
    return {"photo_id": photo_id, "name": name, "kind": kind,
            "final_bbox": list(canvas_box), "checked_pixels": count,
            "changed_pixels": any_changed.histogram()[255],
            "changed_rgb_pixels": rgb_changed.histogram()[255],
            "nonopaque_final_pixels": alpha_changed.histogram()[255],
            "max_rgb_difference": maximum,
            "max_alpha_difference": ImageChops.multiply(alpha_difference, mask).getextrema()[1],
            "changed_final_bbox": None if changed_bbox is None else
            [changed_bbox[0] + canvas_box[0], changed_bbox[1] + canvas_box[1],
             changed_bbox[2] + canvas_box[0], changed_bbox[3] + canvas_box[1]]}


def verify(spec_path: Path, image_path: Path | None = None) -> dict:
    spec_path, spec = read_spec(spec_path)
    base = spec_path.parent
    image_path = (image_path.expanduser().resolve() if image_path is not None else
                  resolve(spec["output"], base, "output"))
    # Reopen encoded saved pixels. No in-memory composite or production report.
    saved, saved_meta = png_layer(image_path, "saved image")
    background_path = resolve(spec["background"], base, "background")
    background, background_meta = png_layer(background_path, "background")
    if saved.size != background.size:
        raise EngineError(f"Saved PNG size {saved.size} differs from canvas {background.size}")
    inputs = [spec_path, image_path, background_path]
    checks, photos, ids = [], [], set()
    for index, value in enumerate(spec["photos"]):
        label = f"photos[{index}]"
        value = photo_fields(value, label)
        if value["id"] in ids:
            raise EngineError("photo ids must be unique")
        ids.add(value["id"])
        path = resolve(value["path"], base, label + ".path")
        original, source_meta = open_image(path)
        rgb = original.convert("RGB")
        size = geometry(rgb, value["width"])
        # A fresh, single resize from original RGB independently reconstructs
        # reference pixels. No colors from compose layers are reused.
        reference = rgb.resize(size, Image.Resampling.LANCZOS)
        x, y = value["x"], value["y"]
        source_protect, protect_inputs = load_mask(value["protect"], rgb.size, base, label + ".protect")
        protect = cover_resize(source_protect, size)
        ensure_inside(protect, x, y, saved.size, label + ".protect")
        checks.append(check_region(saved, reference, protect, x, y, value["id"], "protect", "production_protection"))
        inputs.extend([path, *protect_inputs])
        for audit in value["audit"]:
            source_audit, audit_inputs = load_mask(audit["mask"], rgb.size, base, label + ".audit." + audit["name"])
            selected = cover_resize(source_audit, size)
            ensure_inside(selected, x, y, saved.size, label + ".audit." + audit["name"])
            checks.append(check_region(saved, reference, selected, x, y, value["id"], audit["name"], "independent_declared_audit"))
            inputs.extend(audit_inputs)
        shape = fields(value["shape"], {"type", "depth", "step", "seed", "alpha"}, {"type"}, label + ".shape")
        if shape["type"] == "custom":
            fields(shape, {"type", "alpha"}, {"type", "alpha"}, label + ".shape")
            _, shape_inputs = load_mask(shape["alpha"], rgb.size, base, label + ".alpha", binary=False)
            inputs.extend(shape_inputs)
        elif shape["type"] == "rect":
            fields(shape, {"type"}, {"type"}, label + ".shape")
        elif shape["type"] == "torn":
            fields(shape, {"type", "depth", "step", "seed"}, {"type"}, label + ".shape")
            depth = integer(shape.get("depth", max(1, min(size) // 30)), label + ".depth", 1)
            integer(shape.get("step", max(3, depth)), label + ".step", 1)
            integer(shape.get("seed", 0), label + ".seed", 0)
            if depth * 2 >= min(size):
                raise EngineError(f"{label}: torn depth must be less than half shorter dimension")
        else:
            raise EngineError(f"{label}: unsupported shape.type")
        photos.append({**source_meta, "id": value["id"], "source_spec_path": value["path"],
                       "rendered_size": list(size), "translation": [x, y],
                       "actual_resize_factors": [size[0] / rgb.width, size[1] / rgb.height],
                       "height_rounding_error_pixels": size[1] - rgb.height * size[0] / rgb.width,
                       "reference_original_rgb_resize_passes": 1, "resize_filter": "LANCZOS"})
    for index, value in enumerate(spec.get("foregrounds", [])):
        value = fields(value, {"path", "x", "y"}, {"path", "x", "y"}, f"foregrounds[{index}]")
        integer(value["x"], f"foregrounds[{index}].x")
        integer(value["y"], f"foregrounds[{index}].y")
        path = resolve(value["path"], base, f"foregrounds[{index}].path")
        png_layer(path, f"foregrounds[{index}]", rgba_required=True)
        inputs.append(path)
    return {"version": 3, "layout": str(spec_path), "layout_sha256": digest(spec_path),
            "image": str(image_path), "final_sha256": saved_meta["sha256"],
            "canvas_size": list(saved.size), "background": background_meta, "photos": photos,
            "input_files": [{"path": str(path), "sha256": digest(path)} for path in dict.fromkeys(inputs)],
            "method": "Fresh EXIF-oriented original RGB, one declared LANCZOS resize, translation; saved PNG reopened. Complete conservative geometric ROI coverage. No selection by production alpha, prior reports, changed-pixel status or image content.",
            "coverage_limit": "Only supplied masks are checked; this tool does not detect omitted faces, hair or important content.",
            "checks": checks, "passed": all(check["changed_pixels"] == 0 for check in checks),
            "total_checked_pixels_including_overlap": sum(check["checked_pixels"] for check in checks)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layout", type=Path)
    parser.add_argument("--image", type=Path, help="Saved PNG override; never modified")
    parser.add_argument("--report", type=Path, help="Write independent JSON; default is image.audit.json")
    args = parser.parse_args()
    try:
        report = verify(args.layout, args.image)
        path = (args.report.expanduser().resolve() if args.report else
                Path(report["image"]).with_suffix(".audit.json"))
        if path.suffix.lower() != ".json":
            raise EngineError("audit report must end .json")
        safe_destination(path, [Path(item["path"]) for item in report["input_files"]], "audit report")
        # Also protect the layout's nominal output/report when --image differs.
        spec_path, spec = read_spec(args.layout)
        base = spec_path.parent
        output = resolve(spec["output"], base, "output")
        composition_report = resolve(spec.get("report", str(output.with_suffix(".report.json"))), base, "report")
        safe_destination(path, [output, composition_report], "audit report")
        atomic_json(path, report)
        print(json.dumps({"passed": report["passed"], "report": str(path),
                          "final_sha256": report["final_sha256"], "region_count": len(report["checks"]),
                          "changed_pixels_including_overlap": sum(c["changed_pixels"] for c in report["checks"])}, ensure_ascii=False))
        return 0 if report["passed"] else 2
    except (EngineError, OSError, OverflowError, MemoryError) as error:
        print(f"Verification error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
