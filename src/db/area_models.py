"""Versioned analytical area data. The map's main/buildings tables are inputs."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from geoalchemy2 import Geometry

from .models import Base


def json_column(name):
    return sa.Column(name, JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))


def geometry(name, kind, srid, nullable=True):
    return sa.Column(name, Geometry(kind, srid=srid, spatial_index=False), nullable=nullable)


boundary_sets = sa.Table(
    "energymap_area_boundary_sets", Base.metadata,
    sa.Column("boundary_set_id", sa.Text, primary_key=True),
    sa.Column("vintage", sa.Integer, nullable=False),
    json_column("manifest"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)
boundaries = sa.Table(
    "energymap_area_boundaries", Base.metadata,
    sa.Column("boundary_set_id", sa.Text, sa.ForeignKey(boundary_sets.c.boundary_set_id), primary_key=True),
    sa.Column("level", sa.Text, primary_key=True),
    sa.Column("area_code", sa.Text, primary_key=True),
    sa.Column("name_fi", sa.Text, nullable=False),
    sa.Column("name_sv", sa.Text),
    sa.Column("name_en", sa.Text),
    sa.Column("region_code", sa.Text),
    geometry("geom", "MULTIPOLYGON", 3067, nullable=False),
    geometry("display_geom", "MULTIPOLYGON", 3857, nullable=False),
    sa.CheckConstraint(
        "(level='postal_code' AND area_code ~ '^[0-9]{5}$') OR "
        "(level='municipality' AND area_code ~ '^[0-9]{3}$') OR "
        "(level='region' AND area_code ~ '^[0-9]{2}$')", name="area_code_format"),
    sa.CheckConstraint("region_code IS NULL OR region_code ~ '^[0-9]{2}$'", name="area_region_format"),
    sa.CheckConstraint("(level='municipality') = (region_code IS NOT NULL)", name="area_municipality_region"),
)
sa.Index("idx_area_boundary_geom", boundaries.c.geom, postgresql_using="gist")
sa.Index("idx_area_boundary_display_geom", boundaries.c.display_geom, postgresql_using="gist")

geography_snapshots = sa.Table(
    "energymap_area_geography_snapshots", Base.metadata,
    sa.Column("snapshot_id", sa.Text, primary_key=True),
    json_column("manifest"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)
geography = sa.Table(
    "energymap_area_geography", Base.metadata,
    sa.Column("snapshot_id", sa.Text, sa.ForeignKey(geography_snapshots.c.snapshot_id), primary_key=True),
    sa.Column("building_key", sa.Text, primary_key=True),
    sa.Column("municipality_code", sa.Text),
    geometry("point", "POINT", 3067),
    json_column("provenance"),
    sa.CheckConstraint("municipality_code IS NULL OR municipality_code ~ '^[0-9]{3}$'", name="geography_municipality_format"),
)

releases = sa.Table(
    "energymap_area_releases", Base.metadata,
    sa.Column("release_id", sa.Text, primary_key=True),
    sa.Column("boundary_set_id", sa.Text, sa.ForeignKey(boundary_sets.c.boundary_set_id), nullable=False),
    sa.Column("geography_snapshot_id", sa.Text, sa.ForeignKey(geography_snapshots.c.snapshot_id), nullable=False),
    sa.Column("as_of", sa.Date, nullable=False),
    sa.Column("metric_version", sa.Text, nullable=False),
    json_column("manifest"),
    json_column("validation"),
    json_column("publication"),
    sa.Column("validated", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("active", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.UniqueConstraint("release_id", "boundary_set_id", name="release_boundary_set_unique"),
    sa.CheckConstraint("NOT active OR validated", name="active_area_release_validated"),
)
sa.Index("one_active_area_release", releases.c.active, unique=True, postgresql_where=releases.c.active)

buildings = sa.Table(
    "energymap_area_buildings", Base.metadata,
    sa.Column("release_id", sa.Text, sa.ForeignKey(releases.c.release_id), primary_key=True),
    sa.Column("building_key", sa.Text, primary_key=True),
    json_column("attributes"),
    geometry("point", "POINT", 3067),
    sa.Column("location_method", sa.Text, nullable=False),
    sa.Column("registry_municipality_code", sa.Text),
)
sa.Index("idx_area_building_point", buildings.c.point, postgresql_using="gist")

membership = sa.Table(
    "energymap_building_area_membership", Base.metadata,
    sa.Column("release_id", sa.Text, primary_key=True),
    sa.Column("building_key", sa.Text, primary_key=True),
    sa.Column("boundary_set_id", sa.Text, nullable=False),
    sa.Column("postal_code", sa.Text),
    sa.Column("municipality_code", sa.Text),
    sa.Column("region_code", sa.Text),
    sa.Column("postal_method", sa.Text, nullable=False),
    sa.Column("municipality_method", sa.Text, nullable=False),
    sa.ForeignKeyConstraint(["release_id", "building_key"], [buildings.c.release_id, buildings.c.building_key]),
    sa.ForeignKeyConstraint(["release_id", "boundary_set_id"], [releases.c.release_id, releases.c.boundary_set_id]),
    sa.CheckConstraint("postal_code IS NULL OR postal_code ~ '^[0-9]{5}$'", name="membership_postal_format"),
    sa.CheckConstraint("municipality_code IS NULL OR municipality_code ~ '^[0-9]{3}$'", name="membership_municipality_format"),
    sa.CheckConstraint("region_code IS NULL OR region_code ~ '^[0-9]{2}$'", name="membership_region_format"),
    sa.CheckConstraint("(municipality_code IS NULL) = (region_code IS NULL)", name="membership_region_requires_municipality"),
)
for field in ("postal_code", "municipality_code", "region_code"):
    sa.Index("idx_area_membership_" + field, membership.c.release_id, membership.c[field])

stats = sa.Table(
    "energymap_area_stats", Base.metadata,
    sa.Column("release_id", sa.Text, sa.ForeignKey(releases.c.release_id), primary_key=True),
    sa.Column("boundary_set_id", sa.Text, nullable=False),
    sa.Column("level", sa.Text, primary_key=True),
    sa.Column("area_code", sa.Text, primary_key=True),
    sa.Column("building_count", sa.BigInteger, nullable=False),
    sa.Column("valid_official_class_count", sa.BigInteger, nullable=False),
    sa.Column("certificate_coverage", sa.Float),
    json_column("metrics"),
    sa.ForeignKeyConstraint(["boundary_set_id", "level", "area_code"],
                            [boundaries.c.boundary_set_id, boundaries.c.level, boundaries.c.area_code]),
    sa.ForeignKeyConstraint(["release_id", "boundary_set_id"], [releases.c.release_id, releases.c.boundary_set_id]),
    sa.CheckConstraint("building_count >= 0 AND valid_official_class_count BETWEEN 0 AND building_count", name="area_stat_counts"),
    sa.CheckConstraint("certificate_coverage IS NULL OR certificate_coverage BETWEEN 0 AND 1", name="area_stat_coverage"),
)
crosswalk = sa.Table(
    "energymap_postal_municipality_stats", Base.metadata,
    sa.Column("release_id", sa.Text, sa.ForeignKey(releases.c.release_id), primary_key=True),
    sa.Column("postal_code", sa.Text, primary_key=True),
    sa.Column("municipality_code", sa.Text, primary_key=True),
    sa.Column("building_count", sa.BigInteger, nullable=False),
    sa.Column("valid_official_class_count", sa.BigInteger, nullable=False),
    sa.Column("certificate_coverage", sa.Float),
    json_column("metrics"),
    sa.CheckConstraint("building_count > 0 AND valid_official_class_count BETWEEN 0 AND building_count", name="postal_municipality_counts"),
    sa.CheckConstraint("postal_code ~ '^[0-9]{5}$' AND municipality_code ~ '^[0-9]{3}$'", name="postal_municipality_code_format"),
    sa.CheckConstraint("certificate_coverage IS NULL OR certificate_coverage BETWEEN 0 AND 1", name="postal_municipality_coverage"),
)
sa.Index("idx_postal_municipality_reverse", crosswalk.c.release_id, crosswalk.c.municipality_code, crosswalk.c.postal_code)

AREA_TABLES = (boundary_sets, boundaries, geography_snapshots, geography, releases, buildings, membership, stats, crosswalk)
