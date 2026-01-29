from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy import text

from src.db.session import SessionLocal, SessionSandbox

app = FastAPI()

# CORS settings
origins = [
    "http://127.0.0.1:8080", # local
    "null" # local html debug file
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/energy-data/tiles/{z}/{x}/{y}.pbf")
def tiles(z: int, x: int, y: int, env: str = "local"):
    # prevent loading too much data for low zoom levels
    if (z < 11):
        return Response(content=b"", media_type="application/vnd.mapbox-vector-tile")

    if (env == "sandbox"):
        session = SessionSandbox()
    else:
        session = SessionLocal()

    with session as db:
        query = text("""
            WITH bounds AS (
                SELECT ST_TileEnvelope(:z, :x, :y) AS geom
            ),
            mvtgeom AS (
                SELECT
                    a.address_key,
                    a.address_fin,
                    a.postal_code,
                    a.postal_office_fin,

                    b.heating_method,
                    b.heating_energy_source,
                    b.completion_date,
                    b.floor_area,
                    b.gross_floor_area,
                    b.total_area,
                    b.volume,

                    cm_heating_method.label_en AS heating_method_label_en,
                    cm_heating_energy_source.label_en AS heating_energy_source_label_en,

                    ST_AsMVTGeom(
                        ST_Transform(b.point, 3857),
                        bounds.geom
                    ) AS geom
                FROM addresses a
                JOIN buildings b ON a.building_key = b.building_key
                JOIN code_maps cm_heating_method
                    ON b.heating_method = cm_heating_method.code AND cm_heating_method.type = 'heating_method'
                JOIN code_maps cm_heating_energy_source
                    ON b.heating_energy_source = cm_heating_energy_source.code AND cm_heating_energy_source.type = 'heating_energy_source'
                JOIN bounds ON ST_Intersects(ST_Transform(b.point, 3857), bounds.geom)
            )

            SELECT ST_AsMVT(mvtgeom.*, 'default_layer', 4096, 'geom') AS mvt_blob
            FROM mvtgeom;
        """)

        mvt_bytes = db.execute(query, {"z": z, "x": x, "y": y}).scalar()
        return Response(
            content=mvt_bytes,
            media_type="application/vnd.mapbox-vector-tile"
        )