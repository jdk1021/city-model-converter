"""작업 하나(업로드 묶음)를 CityGML로 변환한다. 별도 프로세스에서 실행된다."""

from __future__ import annotations

import base64
import json
import os
import shutil
import time
import traceback
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from . import geometry
from .citygml import VERSIONS, Writer
from .loader import MODEL_SUFFIXES, load_scene

PREVIEW_MAX_TRIANGLES = 400_000


@dataclass
class Options:
    version: str = "2.0"  # CityGML 버전: 2.0 | 3.0
    epsg: int = 5186
    src_epsg: int | None = None  # 지정하면 offset 적용 뒤 epsg로 재투영
    up_axis: str = "y"  # 모델의 위쪽 축
    scale: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    offset_z: float = 0.0
    lod: int = 2
    split: str = "object"  # object | component | single
    semantics: bool = True
    wall_angle: float = 15.0  # 법선이 수평에서 이 각도 안이면 벽
    merge_coplanar: bool = True
    fix_normals: bool = True
    textures: bool = True
    materials: bool = True
    output: str = "merged"  # merged | per_file
    precision: int = 3

    def validate(self) -> None:
        if self.version not in VERSIONS:
            raise ValueError("version은 2.0 또는 3.0이어야 합니다")
        if self.up_axis not in ("y", "z"):
            raise ValueError("up_axis는 y 또는 z여야 합니다")
        if self.lod not in (2, 3):
            raise ValueError("lod는 2 또는 3이어야 합니다")
        if self.split not in ("object", "component", "single"):
            raise ValueError("split 값이 올바르지 않습니다")
        if self.output not in ("merged", "per_file"):
            raise ValueError("output 값이 올바르지 않습니다")
        if not (0 < self.wall_angle < 90):
            raise ValueError("wall_angle은 0~90 사이여야 합니다")
        if not (0 <= self.precision <= 9):
            raise ValueError("precision은 0~9 사이여야 합니다")
        if self.scale <= 0:
            raise ValueError("scale은 0보다 커야 합니다")


def write_status(job_dir: Path, **fields) -> None:
    path = job_dir / "status.json"
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text())
        except json.JSONDecodeError:
            current = {}
    current.update(fields, updated=time.time())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False))
    os.replace(tmp, path)


def read_status(job_dir: Path) -> dict | None:
    path = job_dir / "status.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


class _Textures:
    """원본 텍스처를 출력 폴더로 복사하고 CityGML에 적을 상대 경로를 돌려준다."""

    def __init__(self, out_dir: Path, folder: str):
        self.dir = out_dir / folder
        self.folder = folder
        self._by_source: dict[Path, str | None] = {}
        self._names: set[str] = set()
        self.warnings: list[str] = []

    def uri(self, source: Path) -> str | None:
        if source in self._by_source:
            return self._by_source[source]
        self.dir.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix.lower()
        keep = suffix in (".jpg", ".jpeg", ".png")
        stem, n = source.stem, 1
        name = f"{stem}{suffix if keep else '.png'}"
        while name.lower() in self._names:
            n += 1
            name = f"{stem}_{n}{suffix if keep else '.png'}"
        try:
            if keep:
                shutil.copyfile(source, self.dir / name)
            else:
                Image.open(source).save(self.dir / name)
        except Exception as exc:
            self.warnings.append(f"텍스처를 옮기지 못했습니다: {source.name} ({exc})")
            self._by_source[source] = None
            return None
        self._names.add(name.lower())
        self._by_source[source] = f"{self.folder}/{name}"
        return self._by_source[source]


def _units(scene, stem: str, split: str, decimals: int):
    """장면을 건물 단위 (이름, tris, uvs, mats)로 나눈다."""
    if split == "object":
        for n, part in enumerate(scene.parts, start=1):
            name = part.name or (stem if len(scene.parts) == 1 else f"{stem}_{n}")
            yield name, part.tris, part.uvs, part.mats
        return
    tris = np.concatenate([p.tris for p in scene.parts])
    uvs = np.concatenate([p.uvs for p in scene.parts])
    mats = np.concatenate([p.mats for p in scene.parts])
    if split == "single":
        yield stem, tris, uvs, mats
        return
    labels = geometry.split_components(tris, decimals)
    for n, label in enumerate(np.unique(labels), start=1):
        sel = labels == label
        yield f"{stem}_{n}", tris[sel], uvs[sel], mats[sel]


def _convert_group(job_dir, out_dir, out_name, models, opts: Options, progress, report, preview):
    in_dir = job_dir / "input"
    offset = (opts.offset_x, opts.offset_y, opts.offset_z)
    reproject = None
    if opts.src_epsg and opts.src_epsg != opts.epsg:
        from pyproj import Transformer

        reproject = Transformer.from_crs(opts.src_epsg, opts.epsg, always_xy=True)

    textures = _Textures(out_dir, f"{out_name}_appearance")
    buildings: list[tuple[geometry.Building, list]] = []
    for model in models:
        rel = str(model.relative_to(in_dir))
        progress(f"읽는 중: {rel}")
        scene = load_scene(model, in_dir)
        report["warnings"] += [f"{rel}: {w}" for w in scene.warnings]
        textured = (
            {i for i, m in enumerate(scene.materials) if m.texture is not None} if opts.textures else set()
        )
        for name, tris, uvs, mats in _units(scene, model.stem, opts.split, opts.precision):
            world = geometry.to_world(tris, opts.up_axis, opts.scale, offset)
            if reproject is not None:
                x, y = reproject.transform(world[..., 0], world[..., 1])
                world = np.stack([x, y, world[..., 2]], axis=-1)
            b = geometry.build(
                name,
                rel,
                world,
                uvs,
                mats,
                textured,
                semantics=opts.semantics,
                wall_angle=opts.wall_angle,
                merge_coplanar=opts.merge_coplanar,
                fix_normals=opts.fix_normals,
                decimals=opts.precision,
            )
            if b is None:
                report["warnings"].append(f"{rel}: '{name}'에 유효한 면이 없어 제외했습니다")
                continue
            buildings.append((b, scene.materials))
            report["buildings"].append(b.stats)

    if not buildings:
        return None
    envelope = np.array(
        [
            np.min([b.bbox[0] for b, _ in buildings], axis=0),
            np.max([b.bbox[1] for b, _ in buildings], axis=0),
        ]
    )
    gml_path = out_dir / f"{out_name}.gml"
    writer = Writer(gml_path, opts.version, opts.epsg, opts.lod, opts.precision, envelope)
    for n, (b, materials) in enumerate(buildings, start=1):
        if n % 20 == 1:
            progress(f"쓰는 중: {out_name}.gml ({n}/{len(buildings)})")

        def appearance(poly, materials=materials):
            m = materials[poly.mat] if 0 <= poly.mat < len(materials) else None
            if m is None:
                return None, None
            tex = None
            if opts.textures and m.texture is not None and poly.uv is not None:
                tex = textures.uri(m.texture)
            color = (*m.color, m.opacity) if (opts.materials and m.color is not None) else None
            return tex, color

        writer.add_building(b, appearance)
        preview.add(b, materials, opts, textures)
    writer.close()
    report["warnings"] += textures.warnings
    return gml_path


class _Preview:
    """미리보기용 삼각형과 겉모습(텍스처·재질 색)을 모은다.

    looks는 겉모습 목록이고 삼각형마다 그 색인(look)을 둔다. -1은 겉모습이 없어 분류 색으로 그린다.
    텍스처 경로는 출력 폴더 기준 상대 경로로, CityGML에 적힌 imageURI와 같다.
    """

    def __init__(self):
        self.parts: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
        self.looks: list[dict] = []
        self._keys: dict[tuple, int] = {}

    def _look(self, key: tuple, value: dict) -> int:
        if key not in self._keys:
            self._keys[key] = len(self.looks)
            self.looks.append(value)
        return self._keys[key]

    def add(self, b: geometry.Building, materials, opts: Options, textures: _Textures) -> None:
        n = len(b.tris)
        uv = b.tri_uv if b.tri_uv is not None else np.full((n, 3, 2), np.nan)
        has_uv = ~np.isnan(uv).any(axis=(1, 2))
        look = np.full(n, -1, dtype=np.int32)
        mats = b.tri_mat if b.tri_mat is not None else np.full(n, -1)
        for mi in np.unique(mats):
            m = materials[mi] if 0 <= mi < len(materials) else None
            if m is None:
                continue
            sel = mats == mi
            color = m.color if (opts.materials and m.color is not None) else None
            if opts.textures and m.texture is not None and (sel & has_uv).any():
                uri = textures.uri(m.texture)
                if uri is not None:
                    look[sel & has_uv] = self._look(("tex", uri), {"texture": uri})
                    sel = sel & ~has_uv
            if color is not None and sel.any():
                c = [round(float(v), 4) for v in color]
                look[sel] = self._look(("color", *c), {"color": c})
        self.parts.append((b.tris, b.tri_cls, np.nan_to_num(uv), look))


def _write_preview(job_dir: Path, preview: _Preview) -> None:
    if not preview.parts:
        return
    tris = np.concatenate([p[0] for p in preview.parts])
    cls = np.concatenate([p[1] for p in preview.parts])
    uvs = np.concatenate([p[2] for p in preview.parts])
    look = np.concatenate([p[3] for p in preview.parts])
    total = len(tris)
    if total > PREVIEW_MAX_TRIANGLES:
        pick = np.random.default_rng(0).choice(total, PREVIEW_MAX_TRIANGLES, replace=False)
        tris, cls, uvs, look = tris[pick], cls[pick], uvs[pick], look[pick]
    lo, hi = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
    center = (lo + hi) / 2
    data = {
        "total_triangles": total,
        "shown_triangles": len(tris),
        "center": center.tolist(),
        "size": (hi - lo).tolist(),
        "positions": base64.b64encode((tris - center).astype("<f4").tobytes()).decode(),
        "classes": base64.b64encode(cls.astype(np.uint8).tobytes()).decode(),
        "uvs": base64.b64encode(uvs.astype("<f4").tobytes()).decode(),
        "look": base64.b64encode(look.astype("<i4").tobytes()).decode(),
        "looks": preview.looks,
    }
    (job_dir / "preview.json").write_text(json.dumps(data))


def run(job_dir_str: str, options: dict) -> None:
    job_dir = Path(job_dir_str)
    started = time.time()
    try:
        opts = Options(**options)
        opts.validate()
        in_dir, out_dir = job_dir / "input", job_dir / "output"
        out_dir.mkdir(exist_ok=True)
        models = sorted(p for p in in_dir.rglob("*") if p.is_file() and p.suffix.lower() in MODEL_SUFFIXES)
        if not models:
            raise ValueError("업로드한 파일에 .obj 또는 .3ds가 없습니다")

        if opts.output == "merged":
            name = models[0].stem if len(models) == 1 else "citymodel"
            groups = [(name, models)]
        else:
            groups, seen = [], set()
            for m in models:
                name, n = m.stem, 1
                while name.lower() in seen:
                    n += 1
                    name = f"{m.stem}_{n}"
                seen.add(name.lower())
                groups.append((name, [m]))

        report = {"options": asdict(opts), "files": [], "buildings": [], "warnings": []}
        preview = _Preview()
        for i, (name, group) in enumerate(groups):
            def progress(message, i=i):
                write_status(job_dir, state="running", progress=i / len(groups), message=message)

            gml = _convert_group(job_dir, out_dir, name, group, opts, progress, report, preview)
            if gml is not None:
                report["files"].append({"name": gml.name, "bytes": gml.stat().st_size})
        if not report["files"]:
            raise ValueError("변환할 수 있는 형상이 없습니다")

        write_status(job_dir, state="running", progress=0.95, message="압축하는 중")
        (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        zip_path = job_dir / "result.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in sorted(out_dir.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(out_dir))
        _write_preview(job_dir, preview)
        report["elapsed_seconds"] = round(time.time() - started, 2)
        report["zip_bytes"] = zip_path.stat().st_size
        write_status(job_dir, state="done", progress=1.0, message="완료", report=report)
    except Exception as exc:
        (job_dir / "error.log").write_text(traceback.format_exc())
        message = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
        write_status(job_dir, state="error", message=message)
