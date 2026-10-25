"""Area release promotion is one transaction; candidates never replace active data."""

import argparse
from uuid import UUID

from sqlalchemy import text

from src.db.session import SessionLocal
from src.etl.area_inputs import json_text
from src.area_contract import LEVEL_WIDTH


def release_id(value):
    return str(UUID(str(value)))


def promote_release(session, value):
    value = release_id(value)
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('energymap_area_promotion'))"))
    row = session.execute(text("SELECT validated,publication FROM energymap_area_releases WHERE release_id=:id FOR UPDATE"), {"id": value}).mappings().first()
    if (row is None or not row["validated"] or not row["publication"].get("verified")
            or row["publication"].get("release_id") != value
            or set(row["publication"].get("layers", {})) != set(LEVEL_WIDTH)):
        raise RuntimeError("Only a validated release with verified delivery can be promoted")
    from src.geoserver.publish_area_aggregates import create_stable_views
    create_stable_views(session)
    previous = session.execute(text("SELECT release_id FROM energymap_area_releases WHERE active")).scalar()
    session.execute(text("UPDATE energymap_area_releases SET active=false WHERE active"))
    session.execute(text("UPDATE energymap_area_releases SET active=true WHERE release_id=:id"), {"id": value})
    return {"release_id": value, "previous_release_id": previous}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args()
    with SessionLocal() as session:
        report = promote_release(session, args.release_id)
        session.commit()
    print(json_text(report))


if __name__ == "__main__":
    main()
