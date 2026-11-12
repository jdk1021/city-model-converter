"""좌표 변환, 건물 단위 분리, 면 의미 분류, 동일 평면 병합."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import shapely
import trimesh
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree
from shapely.geometry.polygon import orient

ROOF, WALL, GROUND, CEILING, UNCLASSIFIED = 0, 1, 2, 3, 4
CLASS_ELEMENT = {
    ROOF: "RoofSurface",
    WALL: "WallSurface",
    GROUND: "GroundSurface",
    CEILING: "OuterCeilingSurface",
}


@dataclass
class Polygon:
    cls: int
    group: int  # 같은 값이면 같은 경계면(thematic surface)
    mat: int
    exterior: np.ndarray  # (n, 3) 닫지 않은 링
    holes: list[np.ndarray] = field(default_factory=list)
    uv: np.ndarray | None = None  # (n, 2) exterior와 같은 순서


@dataclass
class Building:
    name: str
    source: str
    polygons: list[Polygon]
    bbox: np.ndarray  # (2, 3)
    height: float
    solid: bool  # 닫힌 단일 껍질이면 True
    tris: np.ndarray  # 미리보기용 (k, 3, 3)
    tri_cls: np.ndarray  # 미리보기용 (k,)
    stats: dict


def to_world(
    points: np.ndarray,
    up_axis: str,
    scale: float,
    offset: tuple[float, float, float],
) -> np.ndarray:
    """모델 좌표 → Z-up 실좌표. points는 (..., 3)."""
    p = np.asarray(points, dtype=np.float64)
    if up_axis == "y":
        p = np.stack([p[..., 0], -p[..., 2], p[..., 1]], axis=-1)
    return p * scale + np.asarray(offset, dtype=np.float64)


def split_components(tris: np.ndarray, decimals: int) -> np.ndarray:
    """정점을 공유하는 면끼리 묶어 면별 덩어리 번호를 돌려준다."""
    flat = np.round(tris.reshape(-1, 3), decimals)
    _, inverse = np.unique(flat, axis=0, return_inverse=True)
    faces = inverse.reshape(-1, 3)
    n = int(faces.max()) + 1
    rows = np.concatenate([faces[:, 0], faces[:, 1]])
    cols = np.concatenate([faces[:, 1], faces[:, 2]])
    graph = coo_matrix((np.ones(len(rows), dtype=np.int8), (rows, cols)), shape=(n, n))
    _, labels = connected_components(graph, directed=False)
    return labels[faces[:, 0]]


def _weld(tris: np.ndarray, decimals: int):
    flat = tris.reshape(-1, 3)
    _, first, inverse = np.unique(
        np.round(flat, decimals), axis=0, return_index=True, return_inverse=True
    )
    vertices = flat[first]
    faces = inverse.reshape(-1, 3)
    return vertices, faces


def _classify(normals, centroids_z, zmin, height, wall_angle_deg):
    limit = np.sin(np.radians(wall_angle_deg))
    nz = normals[:, 2]
    cls = np.full(len(nz), WALL, dtype=np.uint8)
    cls[nz > limit] = ROOF
    down = nz < -limit
    ground_band = zmin + max(0.2, 0.02 * height)
    cls[down & (centroids_z <= ground_band)] = GROUND
    cls[down & (centroids_z > ground_band)] = CEILING
    return cls


def _face_groups(mesh: trimesh.Trimesh, cls: np.ndarray) -> np.ndarray:
    """같은 분류의 인접 면을 하나의 경계면으로 묶는다."""
    nf = len(mesh.faces)
    adj = mesh.face_adjacency
    if len(adj) == 0:
        return np.arange(nf)
    same = cls[adj[:, 0]] == cls[adj[:, 1]]
    a = adj[same]
    graph = coo_matrix((np.ones(len(a), dtype=np.int8), (a[:, 0], a[:, 1])), shape=(nf, nf))
    _, labels = connected_components(graph, directed=False)
    return labels


def _merge_facet(vertices, faces, normal):
    """동일 평면 삼각형 묶음을 (외곽 링, 구멍 링들) 목록으로 합친다. 실패하면 None."""
    vid = np.unique(faces)
    origin = vertices[vid[0]]
    helper = np.array([1.0, 0.0, 0.0]) if abs(normal[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = np.cross(normal, helper)
    u /= np.linalg.norm(u)
    v = np.cross(normal, u)
    local = vertices[vid] - origin
    pts2d = np.stack([local @ u, local @ v], axis=1)
    lookup = np.full(int(vid.max()) + 1, -1, dtype=np.int64)
    lookup[vid] = np.arange(len(vid))
    tri2d = pts2d[lookup[faces]]
    polys = shapely.polygons(tri2d)
    merged = shapely.union_all(polys)
    if merged.is_empty or not merged.is_valid:
        return None
    if abs(merged.area - float(shapely.area(polys).sum())) > 1e-6 * max(merged.area, 1e-12) + 1e-9:
        return None  # 겹친 면이 있어 합치면 형상이 달라진다
    parts = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
    if any(p.geom_type != "Polygon" for p in parts):
        return None
    tree = cKDTree(pts2d)
    scale = float(np.abs(pts2d).max()) or 1.0

    def ring3d(coords):
        xy = np.asarray(coords)[:-1]
        dist, idx = tree.query(xy)
        out = vertices[vid[idx]].copy()
        far = dist > 1e-7 * scale
        if far.any():
            out[far] = origin + np.outer(xy[far, 0], u) + np.outer(xy[far, 1], v)
        return out

    result = []
    for p in parts:
        p = orient(p, 1.0)
        result.append((ring3d(p.exterior.coords), [ring3d(r.coords) for r in p.interiors]))
    return result


def build(
    name: str,
    source: str,
    tris: np.ndarray,
    uvs: np.ndarray,
    mats: np.ndarray,
    textured_mats: set[int],
    *,
    semantics: bool,
    wall_angle: float,
    merge_coplanar: bool,
    fix_normals: bool,
    decimals: int,
) -> Building | None:
    warnings: list[str] = []

    vertices, faces = _weld(tris, decimals)
    ok = (
        (faces[:, 0] != faces[:, 1])
        & (faces[:, 1] != faces[:, 2])
        & (faces[:, 0] != faces[:, 2])
    )
    tv = vertices[faces]
    area = 0.5 * np.linalg.norm(np.cross(tv[:, 1] - tv[:, 0], tv[:, 2] - tv[:, 0]), axis=1)
    ok &= area > 1e-10
    dropped = int((~ok).sum())
    faces, uvs, mats = faces[ok], uvs[ok], mats[ok]
    if len(faces) == 0:
        return None

    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    if fix_normals:
        before = mesh.faces.copy()
        try:
            trimesh.repair.fix_normals(mesh, multibody=True)
            after = np.asarray(mesh.faces)
            flipped = after[:, 0] != before[:, 0]
            if flipped.any():
                uvs = uvs.copy()
                uvs[flipped] = uvs[flipped][:, ::-1]
                mesh = trimesh.Trimesh(vertices=vertices, faces=after, process=False)
        except Exception as exc:
            mesh = trimesh.Trimesh(vertices=vertices, faces=before, process=False)
            warnings.append(f"면 방향 보정을 건너뛰었습니다 ({exc})")

    faces = np.asarray(mesh.faces)
    normals = np.asarray(mesh.face_normals)
    tv = vertices[faces]
    bbox = np.array([vertices[np.unique(faces)].min(axis=0), vertices[np.unique(faces)].max(axis=0)])
    height = float(bbox[1, 2] - bbox[0, 2])

    watertight = bool(mesh.is_watertight and mesh.is_winding_consistent)
    bodies = int(mesh.body_count) if watertight else 0
    solid = watertight and bodies == 1 and float(mesh.volume) > 0
    if watertight and bodies > 1:
        warnings.append(f"닫힌 덩어리가 {bodies}개라 Solid 대신 MultiSurface로 기록했습니다")

    if semantics:
        cls = _classify(normals, tv[:, :, 2].mean(axis=1), bbox[0, 2], height, wall_angle)
        groups = _face_groups(mesh, cls)
    else:
        cls = np.full(len(faces), UNCLASSIFIED, dtype=np.uint8)
        groups = np.zeros(len(faces), dtype=np.int64)

    has_tex = np.isin(mats, list(textured_mats)) & ~np.isnan(uvs).any(axis=(1, 2))
    polygons: list[Polygon] = []
    used = np.zeros(len(faces), dtype=bool)
    merged_count = 0
    if merge_coplanar:
        for facet in mesh.facets:
            facet = np.asarray(facet)
            if has_tex[facet].any() or len(np.unique(mats[facet])) > 1:
                continue  # 텍스처 좌표·재질을 보존하려면 삼각형 그대로 둔다
            if len(np.unique(cls[facet])) > 1:
                continue
            rings = _merge_facet(vertices, faces[facet], normals[facet].mean(axis=0))
            if rings is None:
                continue
            f0 = int(facet[0])
            for exterior, holes in rings:
                polygons.append(Polygon(int(cls[f0]), int(groups[f0]), int(mats[f0]), exterior, holes))
            used[facet] = True
            merged_count += len(facet)
    for i in np.flatnonzero(~used):
        polygons.append(
            Polygon(
                int(cls[i]),
                int(groups[i]),
                int(mats[i]),
                tv[i],
                uv=uvs[i] if has_tex[i] else None,
            )
        )

    counts = {CLASS_ELEMENT[c]: int((cls == c).sum()) for c in CLASS_ELEMENT if (cls == c).any()}
    stats = {
        "name": name,
        "source": source,
        "triangles": int(len(faces)),
        "polygons": len(polygons),
        "merged_triangles": merged_count,
        "dropped_degenerate": dropped,
        "watertight": watertight,
        "solid": solid,
        "height": round(height, 3),
        "surfaces": int(len(np.unique(groups))) if semantics else 0,
        "class_triangles": counts,
        "warnings": warnings,
    }
    return Building(name, source, polygons, bbox, height, solid, tv, cls, stats)
