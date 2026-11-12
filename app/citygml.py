"""CityGML 2.0·3.0 작성기. 건물 단위로 흘려 쓰고 Appearance는 끝에 모아 쓴다.

두 버전은 네임스페이스와 요소 배치가 달라 버전별 차이를 메서드 안에서 갈라 쓴다.
  - 2.0: GML 3.1.1, 경계면은 bldg:boundedBy/bldg:RoofSurface, 형상 속성은 bldg 네임스페이스
  - 3.0: GML 3.2, 경계면은 core:boundary/con:RoofSurface, 형상 속성은 core 네임스페이스
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import numpy as np
from lxml import etree

from .geometry import CLASS_ELEMENT, Building

XLINK = "http://www.w3.org/1999/xlink"
XSI = "http://www.w3.org/2001/XMLSchema-instance"

NAMESPACES = {
    "2.0": {
        "core": "http://www.opengis.net/citygml/2.0",
        "bldg": "http://www.opengis.net/citygml/building/2.0",
        "app": "http://www.opengis.net/citygml/appearance/2.0",
        "gen": "http://www.opengis.net/citygml/generics/2.0",
        "gml": "http://www.opengis.net/gml",
        "xlink": XLINK,
        "xsi": XSI,
    },
    "3.0": {
        "core": "http://www.opengis.net/citygml/3.0",
        "bldg": "http://www.opengis.net/citygml/building/3.0",
        "con": "http://www.opengis.net/citygml/construction/3.0",
        "app": "http://www.opengis.net/citygml/appearance/3.0",
        "gen": "http://www.opengis.net/citygml/generics/3.0",
        "gml": "http://www.opengis.net/gml/3.2",
        "xlink": XLINK,
        "xsi": XSI,
    },
}
SCHEMA_LOCATION = {
    "2.0": " ".join(
        [
            "http://www.opengis.net/citygml/2.0 http://schemas.opengis.net/citygml/2.0/cityGMLBase.xsd",
            "http://www.opengis.net/citygml/building/2.0 http://schemas.opengis.net/citygml/building/2.0/building.xsd",
            "http://www.opengis.net/citygml/appearance/2.0 http://schemas.opengis.net/citygml/appearance/2.0/appearance.xsd",
            "http://www.opengis.net/citygml/generics/2.0 http://schemas.opengis.net/citygml/generics/2.0/generics.xsd",
        ]
    ),
    "3.0": "http://www.opengis.net/citygml/profiles/base/3.0 "
    "http://schemas.opengis.net/citygml/profiles/base/3.0/CityGML.xsd",
}
VERSIONS = tuple(NAMESPACES)
_XMLNS_DECL = re.compile(rb'\sxmlns:\w+="[^"]*"')


class Writer:
    def __init__(
        self, path: Path, version: str, epsg: int, lod: int, precision: int, envelope: np.ndarray
    ):
        if version not in NAMESPACES:
            raise ValueError(f"지원하지 않는 CityGML 버전입니다: {version}")
        self.v3 = version == "3.0"
        self.ns = NAMESPACES[version]
        self.srs = f"urn:ogc:def:crs:EPSG::{epsg}"
        self.lod = lod
        self._fmt = f"%.{precision}f"
        self._gml_id = self.q("gml", "id")
        self._href = f"{{{XLINK}}}href"
        # 형상 속성(lodNSolid, lodNMultiSurface)이 속한 네임스페이스
        self._geom_ns = "core" if self.v3 else "bldg"

        self._file = open(path, "wb")
        decls = "".join(f' xmlns:{k}="{v}"' for k, v in self.ns.items())
        self._file.write(
            f'<?xml version="1.0" encoding="UTF-8"?>\n<core:CityModel{decls} '
            f'xsi:schemaLocation="{SCHEMA_LOCATION[version]}">\n'.encode()
        )
        self._emit(self._envelope(envelope))
        # 텍스처 URI → [(폴리곤 id, 링 id, uv)], 색상 → [폴리곤 id]
        self.textures: dict[str, list[tuple[str, str, np.ndarray]]] = {}
        self.colors: dict[tuple, list[str]] = {}

    def _emit(self, element) -> None:
        """루트 바로 아래 요소를 쓴다. 루트에서 이미 선언한 xmlns는 첫 태그에서 뺀다."""
        data = etree.tostring(element, pretty_print=True, encoding="UTF-8")
        head, rest = data.split(b">", 1)
        self._file.write(_XMLNS_DECL.sub(b"", head) + b">" + rest)

    def q(self, prefix: str, tag: str) -> str:
        return f"{{{self.ns[prefix]}}}{tag}"

    def _sub(self, parent, prefix: str, tag: str, text: str | None = None, **attrib):
        el = etree.SubElement(parent, self.q(prefix, tag), attrib)
        if text is not None:
            el.text = text
        return el

    def _coords(self, pts: np.ndarray) -> str:
        closed = np.vstack([pts, pts[:1]])
        return " ".join(self._fmt % v for v in closed.ravel())

    def _envelope(self, box: np.ndarray):
        bounded = etree.Element(self.q("gml", "boundedBy"), nsmap=self.ns)
        env = self._sub(bounded, "gml", "Envelope", srsName=self.srs, srsDimension="3")
        self._sub(env, "gml", "lowerCorner", " ".join(self._fmt % v for v in box[0]))
        self._sub(env, "gml", "upperCorner", " ".join(self._fmt % v for v in box[1]))
        return bounded

    def _polygon(self, parent, pid: str, poly) -> None:
        member = self._sub(parent, "gml", "surfaceMember")
        el = etree.SubElement(member, self.q("gml", "Polygon"), {self._gml_id: pid})
        rings = [("exterior", poly.exterior)] + [("interior", h) for h in poly.holes]
        for n, (kind, pts) in enumerate(rings):
            ring = etree.SubElement(
                self._sub(el, "gml", kind), self.q("gml", "LinearRing"), {self._gml_id: f"{pid}_r{n}"}
            )
            self._sub(ring, "gml", "posList", self._coords(pts), srsDimension="3")

    def _shell(self, solid_prop):
        """Solid 외곽 껍질 요소. GML 3.1.1은 CompositeSurface, 3.2는 Shell."""
        solid = self._sub(solid_prop, "gml", "Solid")
        exterior = self._sub(solid, "gml", "exterior")
        return self._sub(exterior, "gml", "Shell" if self.v3 else "CompositeSurface")

    def add_building(self, b: Building, poly_appearance) -> None:
        """poly_appearance(poly) → (텍스처 URI | None, 색상 | None)"""
        bid = "BLDG_" + uuid.uuid4().hex
        lod = self.lod
        member = etree.Element(self.q("core", "cityObjectMember"), nsmap=self.ns)
        bldg = etree.SubElement(member, self.q("bldg", "Building"), {self._gml_id: bid})
        self._sub(bldg, "gml", "name", b.name)
        bldg.append(self._envelope(b.bbox))
        if self.v3:
            attr = self._sub(self._sub(bldg, "core", "genericAttribute"), "gen", "StringAttribute")
            self._sub(attr, "gen", "name", "source_file")
            self._sub(attr, "gen", "value", b.source)
        else:
            attr = self._sub(bldg, "gen", "stringAttribute", name="source_file")
            self._sub(attr, "gen", "value", b.source)
            self._sub(bldg, "bldg", "measuredHeight", self._fmt % b.height, uom="m")

        ids = [f"{bid}_p{i}" for i in range(len(b.polygons))]
        for pid, poly in zip(ids, b.polygons):
            tex, color = poly_appearance(poly)
            if tex is not None and poly.uv is not None:
                self.textures.setdefault(tex, []).append((pid, f"{pid}_r0", poly.uv))
            elif color is not None:
                self.colors.setdefault(color, []).append(pid)

        semantic = any(p.cls in CLASS_ELEMENT for p in b.polygons)
        solid_tag = self.q(self._geom_ns, f"lod{lod}Solid")
        multi_tag = self.q(self._geom_ns, f"lod{lod}MultiSurface")

        def solid_by_reference():
            prop = etree.Element(solid_tag)
            shell = self._shell(prop)
            for pid in ids:
                etree.SubElement(shell, self.q("gml", "surfaceMember"), {self._href: f"#{pid}"})
            return prop

        def thematic_surfaces():
            out = []
            by_group: dict[tuple[int, int], list[int]] = {}
            for i, poly in enumerate(b.polygons):
                by_group.setdefault((poly.cls, poly.group), []).append(i)
            for n, ((cls, _), members) in enumerate(sorted(by_group.items())):
                if self.v3:
                    bounded = etree.Element(self.q("core", "boundary"))
                    surf_tag = self.q("con", CLASS_ELEMENT[cls])
                else:
                    bounded = etree.Element(self.q("bldg", "boundedBy"))
                    surf_tag = self.q("bldg", CLASS_ELEMENT[cls])
                surf = etree.SubElement(bounded, surf_tag, {self._gml_id: f"{bid}_s{n}"})
                ms = self._sub(etree.SubElement(surf, multi_tag), "gml", "MultiSurface")
                for i in members:
                    self._polygon(ms, ids[i], b.polygons[i])
                out.append(bounded)
            return out

        if semantic:
            # 스키마 순서
            #   2.0 LoD2: lod2Solid → boundedBy,  2.0 LoD3: boundedBy → lod3Solid
            #   3.0: boundary → lodNSolid
            surfaces = thematic_surfaces()
            if not self.v3 and lod == 2:
                if b.solid:
                    bldg.append(solid_by_reference())
                bldg.extend(surfaces)
            else:
                bldg.extend(surfaces)
                if b.solid:
                    bldg.append(solid_by_reference())
        elif b.solid:
            shell = self._shell(etree.SubElement(bldg, solid_tag))
            for pid, poly in zip(ids, b.polygons):
                self._polygon(shell, pid, poly)
        else:
            ms = self._sub(etree.SubElement(bldg, multi_tag), "gml", "MultiSurface")
            for pid, poly in zip(ids, b.polygons):
                self._polygon(ms, pid, poly)

        if self.v3:
            height = self._sub(self._sub(bldg, "con", "height"), "con", "Height")
            self._sub(height, "con", "highReference", "highestRoofEdge")
            self._sub(height, "con", "lowReference", "lowestGroundPoint")
            self._sub(height, "con", "status", "measured")
            self._sub(height, "con", "value", self._fmt % b.height, uom="m")

        self._emit(member)

    def _appearance(self):
        member = etree.Element(self.q("core" if self.v3 else "app", "appearanceMember"), nsmap=self.ns)
        app = self._sub(member, "app", "Appearance")
        self._sub(app, "app", "theme", "visual")
        data_tag = "surfaceData" if self.v3 else "surfaceDataMember"
        for uri, targets in self.textures.items():
            tex = self._sub(self._sub(app, "app", data_tag), "app", "ParameterizedTexture")
            self._sub(tex, "app", "imageURI", uri)
            self._sub(tex, "app", "mimeType", "image/png" if uri.lower().endswith(".png") else "image/jpeg")
            self._sub(tex, "app", "wrapMode", "wrap")
            for pid, rid, uv in targets:
                closed = " ".join("%.6f" % v for v in np.vstack([uv, uv[:1]]).ravel())
                if self.v3:
                    assoc = self._sub(
                        self._sub(tex, "app", "textureParameterization"), "app", "TextureAssociation"
                    )
                    self._sub(assoc, "app", "target", f"#{pid}")
                    tcl = self._sub(self._sub(assoc, "app", "textureParameterization"), "app", "TexCoordList")
                    self._sub(tcl, "app", "textureCoordinates", closed)
                    self._sub(tcl, "app", "ring", f"#{rid}")
                else:
                    target = self._sub(tex, "app", "target", uri=f"#{pid}")
                    tcl = self._sub(target, "app", "TexCoordList")
                    self._sub(tcl, "app", "textureCoordinates", closed, ring=f"#{rid}")
        for color, pids in self.colors.items():
            mat = self._sub(self._sub(app, "app", data_tag), "app", "X3DMaterial")
            self._sub(mat, "app", "diffuseColor", "%.4f %.4f %.4f" % color[:3])
            if color[3] < 1.0:
                self._sub(mat, "app", "transparency", "%.4f" % (1.0 - color[3]))
            for pid in pids:
                self._sub(mat, "app", "target", f"#{pid}")
        return member

    def close(self) -> None:
        if self.textures or self.colors:
            self._emit(self._appearance())
        self._file.write(b"</core:CityModel>\n")
        self._file.close()
