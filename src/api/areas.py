"""Read precomputed release summaries; never spatially join national buildings."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from src.db.session import SessionLocal
from src.area_contract import LEVEL_WIDTH, METRIC_DEFINITIONS, empty_metrics, latest_layers
from src.area_tiles import tile_projection

Level = Literal["postal_code", "municipality", "region"]
router = APIRouter(prefix="/energy-data/areas", tags=["Building area summaries"])


def area_session():
    with SessionLocal() as session:
        yield session


def selected_release(session, value=None):
    try:
        row = session.execute(text("""
            SELECT r.*,b.vintage FROM energymap_area_releases r
            JOIN energymap_area_boundary_sets b USING(boundary_set_id)
            WHERE r.validated AND """ + ("r.release_id=:id" if value else "r.active")),
            {"id": str(value)} if value else {}).mappings().first()
    except ProgrammingError:
        raise HTTPException(503, "Area data has not been initialized") from None
    if row is None:
        raise HTTPException(404 if value else 503, "Area release not found" if value else "No active area release")
    return row


def validate_code(value, level):
    if len(value) != LEVEL_WIDTH[level] or not value.isascii() or not value.isdigit():
        raise HTTPException(422, f"Expected a {LEVEL_WIDTH[level]}-digit {level} code")
    return value


def summary(row):
    return {k: row[k] for k in ("building_count", "valid_official_class_count", "certificate_coverage", "metrics")}


@router.get("/metadata")
def metadata(response: Response,
             release_id: UUID | None = Query(default=None, include_in_schema=False),
             session=Depends(area_session)):
    response.headers["Cache-Control"] = "no-cache"
    release = selected_release(session, release_id)
    identifier = release["release_id"]
    publication = release["publication"]
    layers = publication.get("layers") or {
        level: {"url": f"/energy-data/areas/{identifier}/{level}/tiles/{{z}}/{{x}}/{{y}}.pbf",
                "source_layer": "areas", "minzoom": 0, "maxzoom": 14}
        for level in LEVEL_WIDTH
    }
    output = {"boundary_vintage": release["vintage"],
            "as_of": release["as_of"], "metric_version": release["metric_version"],
            "metric_definitions": release["manifest"].get("metric_definitions", METRIC_DEFINITIONS),
            "validation": release["validation"], "sources": release["manifest"],
            "layers": layers, "building_filters_supported": False,
            "postal_areas_nested_in_municipalities": False}
    if release_id is not None:
        # Retained solely for existing operator candidate-verification tooling.
        output["release_id"] = identifier
    else:
        output["layers"] = publication.get("latest_layers") or latest_layers()
        output["latest"] = True
    return output


@router.get("/{release_id}/{level}/tiles/{z}/{x}/{y}.pbf", include_in_schema=False)
def tile(release_id: UUID, level: Level, z: int, x: int, y: int, session=Depends(area_session)):
    return render_tile(session, str(release_id), level, z, x, y, pinned=True)


@router.get("/{level}/tiles/{z}/{x}/{y}.pbf")
def latest_tile(level: Level, z: int, x: int, y: int, session=Depends(area_session)):
    release = selected_release(session)
    return render_tile(session, str(release["release_id"]), level, z, x, y, pinned=False)


def render_tile(session, identifier, level, z, x, y, *, pinned):
    if not (0 <= z <= 14 and 0 <= x < 2**z and 0 <= y < 2**z):
        raise HTTPException(422, "Invalid area tile coordinates")
    selected_release(session, identifier)
    projection = "s.release_id," if pinned else ""
    data = session.execute(text(f"""
        WITH bounds AS (SELECT ST_TileEnvelope(:z,:x,:y) AS geom),
        features AS (
          SELECT a.area_code::bigint AS fid,{projection}a.level,a.area_code,
            a.level||':'||a.area_code AS area_id,a.name_fi,a.name_sv,a.name_en,
            s.building_count,s.valid_official_class_count,s.certificate_coverage,
            {tile_projection()},
            ST_AsMVTGeom(a.display_geom,b.geom,4096,64,true) AS geom
          FROM energymap_area_stats s JOIN energymap_area_boundaries a USING(boundary_set_id,level,area_code)
          CROSS JOIN bounds b
          WHERE s.release_id=:id AND a.level=:level
            AND a.display_geom && ST_Expand(b.geom,(ST_XMax(b.geom)-ST_XMin(b.geom))*64.0/4096)
        )
        SELECT ST_AsMVT(features,'areas',4096,'geom','fid') FROM features
    """), {"id": identifier, "level": level, "z": z, "x": x, "y": y}).scalar_one()
    return Response(bytes(data or b""), media_type="application/vnd.mapbox-vector-tile",
                    headers={"Cache-Control": "public, max-age=31536000, immutable",
                             "X-Energy-Map-Release": identifier} if pinned else
                            {"Cache-Control": "no-cache"})


@router.get("/{level}/{code}")
def detail(level: Level, code: str, response: Response,
           release_id: UUID | None = Query(default=None, include_in_schema=False),
           municipality_code: str | None = Query(default=None), session=Depends(area_session)):
    validate_code(code, level)
    if municipality_code is not None:
        validate_code(municipality_code, "municipality")
        if level != "postal_code":
            raise HTTPException(422, "Municipality scope is only supported for postal areas")
    release = selected_release(session, release_id)
    identifier = release["release_id"]
    params = {"id": identifier, "level": level, "code": code, "municipality": municipality_code,
              "bid": release["boundary_set_id"]}
    row = session.execute(text("""
        SELECT a.name_fi,a.name_sv,a.name_en,a.region_code,s.building_count,
          s.valid_official_class_count,s.certificate_coverage,s.metrics
        FROM energymap_area_stats s JOIN energymap_area_boundaries a USING(boundary_set_id,level,area_code)
        WHERE s.release_id=:id AND s.level=:level AND s.area_code=:code
    """), params).mappings().first()
    if row is None:
        raise HTTPException(404, "Area not found")
    output = {"release_id": identifier, "level": level, "area_code": code, "area_id": f"{level}:{code}",
              "names": {lang: row["name_" + lang] for lang in ("fi", "sv", "en")},
              "boundary_vintage": release["vintage"], "as_of": release["as_of"],
              "metric_version": release["metric_version"], **summary(row)}
    if not output["metrics"]:
        output["metrics"] = empty_metrics()
    if municipality_code is not None:
        if not session.execute(text("SELECT 1 FROM energymap_area_boundaries WHERE boundary_set_id=:bid AND level='municipality' AND area_code=:municipality"), params).first():
            raise HTTPException(404, "Municipality not found")
        intersection = session.execute(text("""
            SELECT building_count,valid_official_class_count,certificate_coverage,metrics
            FROM energymap_postal_municipality_stats
            WHERE release_id=:id AND postal_code=:code AND municipality_code=:municipality
        """), params).mappings().first()
        output.update(summary(intersection) if intersection else
                      {"building_count": 0, "valid_official_class_count": 0,
                       "certificate_coverage": None, "metrics": empty_metrics()})
        output["scope"] = {"municipality_code": municipality_code, "geometry": "full_postal_area"}
    else:
        output["scope"] = {"geometry": "full_area"}
    if level in ("postal_code", "municipality"):
        field = "municipality_code" if level == "postal_code" else "postal_code"
        where = "postal_code" if level == "postal_code" else "municipality_code"
        output["related_areas"] = [dict(r) for r in session.execute(text(f"""
            SELECT {field} AS area_code,building_count FROM energymap_postal_municipality_stats
            WHERE release_id=:id AND {where}=:code ORDER BY {field}
        """), params).mappings()]
        output["related_area_level"] = "municipality" if level == "postal_code" else "postal_code"
        if level == "municipality":
            output["region_code"] = row["region_code"]
    else:
        output["related_areas"] = [dict(r) for r in session.execute(text("""
            SELECT s.area_code,s.building_count FROM energymap_area_stats s
            JOIN energymap_area_boundaries a USING(boundary_set_id,level,area_code)
            WHERE s.release_id=:id AND s.level='municipality' AND a.region_code=:code ORDER BY s.area_code
        """), params).mappings()]
        output["related_area_level"] = "municipality"
    response.headers["Cache-Control"] = "no-cache"
    if release_id is None:
        output.pop("release_id")
        output["latest"] = True
    return output
