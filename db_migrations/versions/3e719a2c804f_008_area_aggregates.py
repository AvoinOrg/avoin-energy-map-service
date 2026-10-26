"""Versioned postal, municipality and region analytics.

Revision ID: 3e719a2c804f
Revises: c8f4d2a91b7e
"""
from alembic import op

revision = "3e719a2c804f"
down_revision = "c8f4d2a91b7e"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE energymap_area_boundary_sets (
	boundary_set_id TEXT NOT NULL,
	vintage INTEGER NOT NULL,
	manifest JSONB DEFAULT '{}'::jsonb NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (boundary_set_id)
)""")
    op.execute("""CREATE TABLE energymap_area_boundaries (
	boundary_set_id TEXT NOT NULL,
	level TEXT NOT NULL,
	area_code TEXT NOT NULL,
	name_fi TEXT NOT NULL,
	name_sv TEXT,
	name_en TEXT,
	region_code TEXT,
	geom geometry(MULTIPOLYGON,3067) NOT NULL,
	display_geom geometry(MULTIPOLYGON,3857) NOT NULL,
	PRIMARY KEY (boundary_set_id, level, area_code),
	CONSTRAINT area_code_format CHECK ((level='postal_code' AND area_code ~ '^[0-9]{5}$') OR (level='municipality' AND area_code ~ '^[0-9]{3}$') OR (level='region' AND area_code ~ '^[0-9]{2}$')),
	CONSTRAINT area_region_format CHECK (region_code IS NULL OR region_code ~ '^[0-9]{2}$'),
	CONSTRAINT area_municipality_region CHECK ((level='municipality') = (region_code IS NOT NULL)),
	FOREIGN KEY(boundary_set_id) REFERENCES energymap_area_boundary_sets (boundary_set_id)
)""")
    op.execute("""CREATE INDEX idx_area_boundary_display_geom ON energymap_area_boundaries USING gist (display_geom)""")
    op.execute("""CREATE INDEX idx_area_boundary_geom ON energymap_area_boundaries USING gist (geom)""")
    op.execute("""CREATE TABLE energymap_area_geography_snapshots (
	snapshot_id TEXT NOT NULL,
	manifest JSONB DEFAULT '{}'::jsonb NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (snapshot_id)
)""")
    op.execute("""CREATE TABLE energymap_area_geography (
	snapshot_id TEXT NOT NULL,
	building_key TEXT NOT NULL,
	municipality_code TEXT,
	point geometry(POINT,3067),
	provenance JSONB DEFAULT '{}'::jsonb NOT NULL,
	PRIMARY KEY (snapshot_id, building_key),
	CONSTRAINT geography_municipality_format CHECK (municipality_code IS NULL OR municipality_code ~ '^[0-9]{3}$'),
	FOREIGN KEY(snapshot_id) REFERENCES energymap_area_geography_snapshots (snapshot_id)
)""")
    op.execute("""CREATE TABLE energymap_area_releases (
	release_id TEXT NOT NULL,
	boundary_set_id TEXT NOT NULL,
	geography_snapshot_id TEXT NOT NULL,
	as_of DATE NOT NULL,
	metric_version TEXT NOT NULL,
	manifest JSONB DEFAULT '{}'::jsonb NOT NULL,
	validation JSONB DEFAULT '{}'::jsonb NOT NULL,
	publication JSONB DEFAULT '{}'::jsonb NOT NULL,
	validated BOOLEAN DEFAULT false NOT NULL,
	active BOOLEAN DEFAULT false NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (release_id),
	CONSTRAINT release_boundary_set_unique UNIQUE (release_id, boundary_set_id),
	CONSTRAINT active_area_release_validated CHECK (NOT active OR validated),
	FOREIGN KEY(boundary_set_id) REFERENCES energymap_area_boundary_sets (boundary_set_id),
	FOREIGN KEY(geography_snapshot_id) REFERENCES energymap_area_geography_snapshots (snapshot_id)
)""")
    op.execute("""CREATE UNIQUE INDEX one_active_area_release ON energymap_area_releases (active) WHERE active""")
    op.execute("""CREATE TABLE energymap_area_buildings (
	release_id TEXT NOT NULL,
	building_key TEXT NOT NULL,
	attributes JSONB DEFAULT '{}'::jsonb NOT NULL,
	point geometry(POINT,3067),
	location_method TEXT NOT NULL,
	registry_municipality_code TEXT,
	PRIMARY KEY (release_id, building_key),
	FOREIGN KEY(release_id) REFERENCES energymap_area_releases (release_id)
)""")
    op.execute("""CREATE INDEX idx_area_building_point ON energymap_area_buildings USING gist (point)""")
    op.execute("""CREATE TABLE energymap_building_area_membership (
	release_id TEXT NOT NULL,
	building_key TEXT NOT NULL,
	boundary_set_id TEXT NOT NULL,
	postal_code TEXT,
	municipality_code TEXT,
	region_code TEXT,
	postal_method TEXT NOT NULL,
	municipality_method TEXT NOT NULL,
	PRIMARY KEY (release_id, building_key),
	FOREIGN KEY(release_id, building_key) REFERENCES energymap_area_buildings (release_id, building_key),
	FOREIGN KEY(release_id, boundary_set_id) REFERENCES energymap_area_releases (release_id, boundary_set_id),
	CONSTRAINT membership_postal_format CHECK (postal_code IS NULL OR postal_code ~ '^[0-9]{5}$'),
	CONSTRAINT membership_municipality_format CHECK (municipality_code IS NULL OR municipality_code ~ '^[0-9]{3}$'),
	CONSTRAINT membership_region_format CHECK (region_code IS NULL OR region_code ~ '^[0-9]{2}$'),
	CONSTRAINT membership_region_requires_municipality CHECK ((municipality_code IS NULL) = (region_code IS NULL))
)""")
    op.execute("""CREATE INDEX idx_area_membership_municipality_code ON energymap_building_area_membership (release_id, municipality_code)""")
    op.execute("""CREATE INDEX idx_area_membership_postal_code ON energymap_building_area_membership (release_id, postal_code)""")
    op.execute("""CREATE INDEX idx_area_membership_region_code ON energymap_building_area_membership (release_id, region_code)""")
    op.execute("""CREATE TABLE energymap_area_stats (
	release_id TEXT NOT NULL,
	boundary_set_id TEXT NOT NULL,
	level TEXT NOT NULL,
	area_code TEXT NOT NULL,
	building_count BIGINT NOT NULL,
	valid_official_class_count BIGINT NOT NULL,
	certificate_coverage FLOAT,
	metrics JSONB DEFAULT '{}'::jsonb NOT NULL,
	PRIMARY KEY (release_id, level, area_code),
	FOREIGN KEY(boundary_set_id, level, area_code) REFERENCES energymap_area_boundaries (boundary_set_id, level, area_code),
	FOREIGN KEY(release_id, boundary_set_id) REFERENCES energymap_area_releases (release_id, boundary_set_id),
	CONSTRAINT area_stat_counts CHECK (building_count >= 0 AND valid_official_class_count BETWEEN 0 AND building_count),
	CONSTRAINT area_stat_coverage CHECK (certificate_coverage IS NULL OR certificate_coverage BETWEEN 0 AND 1),
	FOREIGN KEY(release_id) REFERENCES energymap_area_releases (release_id)
)""")
    op.execute("""CREATE TABLE energymap_postal_municipality_stats (
	release_id TEXT NOT NULL,
	postal_code TEXT NOT NULL,
	municipality_code TEXT NOT NULL,
	building_count BIGINT NOT NULL,
	valid_official_class_count BIGINT NOT NULL,
	certificate_coverage FLOAT,
	metrics JSONB DEFAULT '{}'::jsonb NOT NULL,
	PRIMARY KEY (release_id, postal_code, municipality_code),
	CONSTRAINT postal_municipality_counts CHECK (building_count > 0 AND valid_official_class_count BETWEEN 0 AND building_count),
	CONSTRAINT postal_municipality_code_format CHECK (postal_code ~ '^[0-9]{5}$' AND municipality_code ~ '^[0-9]{3}$'),
	CONSTRAINT postal_municipality_coverage CHECK (certificate_coverage IS NULL OR certificate_coverage BETWEEN 0 AND 1),
	FOREIGN KEY(release_id) REFERENCES energymap_area_releases (release_id)
)""")
    op.execute("""CREATE INDEX idx_postal_municipality_reverse ON energymap_postal_municipality_stats (release_id, municipality_code, postal_code)""")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='sandbox_energymap_readonly') THEN
            GRANT SELECT ON energymap_area_releases,energymap_area_boundaries,
                energymap_area_stats,energymap_postal_municipality_stats TO sandbox_energymap_readonly;
        END IF;
    END $$""")


def downgrade():
    op.execute("""DO $$ DECLARE item record; BEGIN
        FOR item IN SELECT viewname FROM pg_views WHERE schemaname='public'
          AND viewname ~ '^energymap_(postal_code|municipality|region)_areas_[0-9a-f]{32}$'
        LOOP EXECUTE format('DROP VIEW public.%I',item.viewname); END LOOP;
    END $$""")
    op.execute("DROP TABLE energymap_postal_municipality_stats")
    op.execute("DROP TABLE energymap_area_stats")
    op.execute("DROP TABLE energymap_building_area_membership")
    op.execute("DROP TABLE energymap_area_buildings")
    op.execute("DROP TABLE energymap_area_releases")
    op.execute("DROP TABLE energymap_area_geography")
    op.execute("DROP TABLE energymap_area_geography_snapshots")
    op.execute("DROP TABLE energymap_area_boundaries")
    op.execute("DROP TABLE energymap_area_boundary_sets")
