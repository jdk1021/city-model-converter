"""3ds·obj 파일을 읽어 삼각형 집합으로 돌려준다 (Assimp 사용)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import assimp_py
import numpy as np

MODEL_SUFFIXES = {".obj", ".3ds"}

_FLAGS = (
    assimp_py.Process_Triangulate
    | assimp_py.Process_SortByPType  # 선·점 프리미티브를 별도 메시로 분리
)


@dataclass
class Material:
    name: str
    color: tuple[float, float, float] | None = None
    opacity: float = 1.0
    texture: Path | None = None  # 업로드 폴더 안에서 찾은 실제 파일
    texture_ref: str | None = None  # 모델 파일에 적힌 원래 경로


@dataclass
class Part:
    """모델 파일 안의 객체 하나(같은 이름의 메시를 합친 것)."""

    name: str
    tris: np.ndarray  # (k, 3, 3) float64
    uvs: np.ndarray  # (k, 3, 2) float64, 없으면 NaN
    mats: np.ndarray  # (k,) int, Scene.materials 색인


@dataclass
class Scene:
    source: Path
    parts: list[Part] = field(default_factory=list)
    materials: list[Material] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _find_texture(ref: str, model_dir: Path, index: dict[str, Path]) -> Path | None:
    ref_norm = ref.replace("\\", "/").strip()
    if not ref_norm:
        return None
    direct = model_dir / ref_norm
    if direct.is_file():
        return direct
    # 3ds는 8.3 대문자 이름, 절대 경로 등이 흔하므로 파일명만으로 대소문자 무시 검색
    return index.get(Path(ref_norm).name.lower())


def load_scene(path: Path, search_root: Path) -> Scene:
    scene = Scene(source=path)
    try:
        ai = assimp_py.import_file(str(path), _FLAGS)
    except Exception as exc:  # assimp가 던지는 예외 형이 일정하지 않다
        raise ValueError(f"{path.name}: 모델을 읽지 못했습니다 ({exc})") from exc

    tex_index: dict[str, Path] = {}
    for p in search_root.rglob("*"):
        if p.is_file():
            tex_index.setdefault(p.name.lower(), p)

    missing: set[str] = set()
    for m in ai.materials:
        name = str(m.get("NAME", ""))
        color = m.get("COLOR_DIFFUSE")
        mat = Material(
            name=name,
            color=tuple(float(c) for c in color[:3]) if color else None,
            opacity=float(m.get("OPACITY", 1.0)),
        )
        if name == "DefaultMaterial":
            mat.color = None
        tex_list = (m.get("TEXTURES") or {}).get(assimp_py.TextureType_DIFFUSE) or []
        if tex_list:
            mat.texture_ref = str(tex_list[0])
            mat.texture = _find_texture(mat.texture_ref, path.parent, tex_index)
            if mat.texture is None:
                missing.add(mat.texture_ref)
        scene.materials.append(mat)
    for ref in sorted(missing):
        scene.warnings.append(f"텍스처 파일을 찾지 못했습니다: {ref}")

    # 메시 이름은 포맷마다 의미가 달라(obj는 재질명이 들어가기도 한다) 노드 단위로 객체를 나눈다.
    # 노드 변환은 직접 누적해 적용한다. 3ds는 루트 노드에 Z-up → Y-up 회전이 들어 있다.
    meshes: list[tuple[np.ndarray, np.ndarray, np.ndarray] | None] = []
    skipped = 0
    for mesh in ai.meshes:
        if mesh.num_faces == 0 or mesh.num_indices != mesh.num_faces * 3:
            meshes.append(None)
            skipped += 1
            continue
        verts = np.asarray(mesh.vertices, dtype=np.float64).reshape(-1, 3)
        idx = np.asarray(mesh.indices, dtype=np.int64).reshape(-1, 3)
        uvs = np.full((len(idx), 3, 2), np.nan)
        if mesh.texcoords and mesh.texcoords[0] is not None and mesh.num_uv_components:
            comp = int(mesh.num_uv_components[0])
            uv = np.asarray(mesh.texcoords[0], dtype=np.float64).reshape(-1, comp)[:, :2]
            uvs = uv[idx]
        mats = np.full(len(idx), int(mesh.material_index), dtype=np.int64)
        meshes.append((verts[idx], uvs, mats))
    if skipped:
        scene.warnings.append(f"면이 아닌 메시(선·점) {skipped}개를 제외했습니다")

    stack = [(ai.root_node, np.eye(4))]
    while stack:
        node, parent = stack.pop()
        world = parent @ np.asarray(node.transformation, dtype=np.float64).reshape(4, 4)
        chunks = [meshes[i] for i in node.mesh_indices if meshes[i] is not None]
        if chunks:
            tris = np.concatenate([c[0] for c in chunks]) @ world[:3, :3].T + world[:3, 3]
            uvs = np.concatenate([c[1] for c in chunks])
            if np.linalg.det(world[:3, :3]) < 0:  # 거울 변환이면 면 방향이 뒤집힌다
                tris, uvs = tris[:, ::-1], uvs[:, ::-1]
            scene.parts.append(
                Part(name=node.name or "", tris=tris, uvs=uvs, mats=np.concatenate([c[2] for c in chunks]))
            )
        stack.extend((child, world) for child in reversed(node.children))
    if not scene.parts:
        raise ValueError(f"{path.name}: 변환할 면이 없습니다")
    return scene
