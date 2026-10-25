from src.api.log_redaction import install_log_redaction
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
import os

from sqlalchemy import text

from src.db.session import SessionLocal, SessionSandbox
from src.api.building_shares import router as share_router
from src.api.areas import router as area_router
from src.api.buildings import router as building_router
from src.api.user_buildings import router as user_building_router, PrivateBoundary, readiness as state_readiness

install_log_redaction()

app = FastAPI(title="Energy Map API", description="Canonical building details, building tiles and latest published area statistics.")
app.include_router(area_router)
app.include_router(building_router)
app.include_router(user_building_router)
app.include_router(share_router)
app.add_api_route('/readyz/state', state_readiness, methods=['GET'], tags=['Readiness'])


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.get("/readyz")
def ready(response: Response):
    from sqlalchemy.exc import SQLAlchemyError
    try:
        with SessionLocal() as session:
            available = session.execute(text(
                "SELECT EXISTS(SELECT 1 FROM energymap_area_releases WHERE active AND validated)"
            )).scalar_one()
    except SQLAlchemyError:
        available = False
    if not available:
        response.status_code = 503
    return {"status": "ready" if available else "area_data_unavailable"}

# CORS settings
origins = [value.strip() for value in os.getenv(
    "ENERGY_MAP_API_ORIGINS", "http://127.0.0.1:8080,null"
).split(",") if value.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["ETag"],
)

# Wrap CORS too, so its preflight/denial responses also carry no-store.
app.add_middleware(PrivateBoundary)


@app.get("/energy-data/tiles/{z}/{x}/{y}.pbf")
def tiles(z: int, x: int, y: int, env: str = "local"):
    # prevent loading too much data for low zoom levels
    if z < 11:
        return Response(content=b"", media_type="application/vnd.mapbox-vector-tile")

    if env == "sandbox":
        session = SessionSandbox()
    else:
        session = SessionLocal()

    with session as db:
        query = text("""
            WITH bounds AS (
                SELECT ST_TileEnvelope(:z, :x, :y) AS geom
            ),
            -- point layer - only display point for buildings without polygon
            mvt_points AS (
                SELECT ST_AsMVT(q, 'layer_points', 4096, 'geom') AS mvt
                FROM (
                    SELECT
                        main.*,

                        ST_AsMVTGeom(point, bounds.geom) AS geom
                    FROM main, bounds
                    WHERE ST_Intersects(point, bounds.geom)
                        AND polygon IS NULL
                ) q
            ),
            -- polygon layer
            mvt_polygons AS (
                SELECT ST_AsMVT(q, 'layer_polygons', 4096, 'geom') AS mvt
                FROM (
                    SELECT
                        main.*,

                        cm_heating_method.label_en AS heating_method_label_en,
                        cm_heating_energy_source.label_en AS heating_energy_source_label_en,
                        cm_main_purpose.label_en AS main_purpose_label_en,

                        ST_AsMVTGeom(polygon, bounds.geom) AS geom
                    FROM main
                    JOIN code_maps cm_heating_method
                        ON main.heating_method = cm_heating_method.code AND cm_heating_method.type = 'heating_method'
                    JOIN code_maps cm_heating_energy_source
                        ON main.heating_energy_source = cm_heating_energy_source.code AND cm_heating_energy_source.type = 'heating_energy_source'
                    JOIN code_maps cm_main_purpose
                        ON main.main_purpose = cm_main_purpose.code AND cm_main_purpose.type = 'main_purpose'
                    JOIN bounds ON ST_Intersects(main.polygon, bounds.geom)
                    WHERE polygon IS NOT NULL
                ) q
            )
            -- merge both layers into one binary tile
            SELECT mvt_points.mvt || mvt_polygons.mvt AS full_mvt
            FROM mvt_points, mvt_polygons;
        """)

        mvt_bytes = db.execute(query, {"z": z, "x": x, "y": y}).scalar()
        return Response(
            content=mvt_bytes, media_type="application/vnd.mapbox-vector-tile"
        )
