from datetime import date, datetime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, TIMESTAMP, text, Date, Integer, Float, Text, Index, BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from geoalchemy2 import Geometry

class Base(DeclarativeBase):
    pass

class CodeMap(Base):
    __tablename__ = "code_maps"
    type: Mapped[str] = mapped_column(String, primary_key=True)  # type of code, e.g. 'heating_method'
    code:   Mapped[str] = mapped_column(String, primary_key=True)  # e.g. '07'
    label_fi: Mapped[str | None] = mapped_column(String)
    label_sv: Mapped[str | None] = mapped_column(String)
    label_en: Mapped[str | None] = mapped_column(String)
    src_last_modified: Mapped[str | None] = mapped_column(String)

class Building(Base):
    __tablename__ = "buildings"
    building_key: Mapped[str] = mapped_column(String, primary_key=True, unique=True)
    permanent_building_identifier: Mapped[str | None] = mapped_column(String)
    property_identifier: Mapped[str | None] = mapped_column(String)
    completion_date: Mapped[date | None] = mapped_column(Date)
    main_purpose: Mapped[str | None] = mapped_column(String)  # store code only
    municipality_number: Mapped[str | None] = mapped_column(String)
    usage_status: Mapped[str | None] = mapped_column(String)  # store code only
    demolition_date: Mapped[date | None] = mapped_column(Date)
    facade_material: Mapped[str | None] = mapped_column(String)  # store code only
    heating_method: Mapped[str | None] = mapped_column(String)  # store code only
    heating_energy_source: Mapped[str | None] = mapped_column(String)  # store code only
    material_of_load_bearing_structures: Mapped[str | None] = mapped_column(String)  # store code only
    construction_method: Mapped[str | None] = mapped_column(String)  # store code only
    volume: Mapped[float | None] = mapped_column()  # in m3
    number_of_storeys: Mapped[int | None] = mapped_column()
    gross_floor_area: Mapped[float | None] = mapped_column()  # in m2
    total_area: Mapped[float | None] = mapped_column()  # in m2
    floor_area: Mapped[float | None] = mapped_column()  # in m2
    point_location_srid: Mapped[int | None] = mapped_column()  # original SRID of the point location
    modified_timestamp_utc: Mapped[str | None] = mapped_column(String)  # source system's last modified timestamp in UTC
    voting_district_number: Mapped[str | None] = mapped_column(String)
    apartment_count: Mapped[int | None] = mapped_column()
    point: Mapped[str | None] = mapped_column(Geometry("POINT", srid=4326, spatial_index=True)) #  SRID 4326 / WGS84 coordinates
    created_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

class EnergyCertificate(Base):
    __tablename__ = "energy_certificates"
    __table_args__ = (
        Index("idx_energy_certificates_building_identifier", "building_identifier"),
        Index("idx_energy_certificates_signed_at", "signed_at"),
    )

    certificate_id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int | None] = mapped_column(Integer)
    signed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=False))
    valid_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=False))
    language: Mapped[str | None] = mapped_column(String)
    drafting_stage: Mapped[str | None] = mapped_column(String)
    inspection_date: Mapped[date | None] = mapped_column(Date)
    name_fi: Mapped[str | None] = mapped_column(String)
    name_sv: Mapped[str | None] = mapped_column(String)
    completion_year: Mapped[int | None] = mapped_column(Integer)
    street_address_fi: Mapped[str | None] = mapped_column(String)
    street_address_sv: Mapped[str | None] = mapped_column(String)
    postal_code: Mapped[str | None] = mapped_column(String)
    building_identifier: Mapped[str | None] = mapped_column(String)
    use_purpose: Mapped[str | None] = mapped_column(String)
    sub_use_purpose_fi: Mapped[str | None] = mapped_column(String)
    e_value: Mapped[float | None] = mapped_column(Float)
    e_class: Mapped[str | None] = mapped_column(String)
    e_class_limit_2018: Mapped[float | None] = mapped_column(Float)
    e_class_purpose_label_fi: Mapped[str | None] = mapped_column(String)
    recommendations_fi: Mapped[str | None] = mapped_column(Text)
    recommendations_sv: Mapped[str | None] = mapped_column(Text)
    heated_net_area: Mapped[float | None] = mapped_column(Float)
    ventilation_type_id: Mapped[str | None] = mapped_column(String)
    ventilation_description_fi: Mapped[str | None] = mapped_column(Text)
    ventilation_description_sv: Mapped[str | None] = mapped_column(Text)
    heating_form_1_id: Mapped[str | None] = mapped_column(String)
    heating_form_2_id: Mapped[str | None] = mapped_column(String)
    heating_form_1_description_fi: Mapped[str | None] = mapped_column(Text)
    heating_form_1_description_sv: Mapped[str | None] = mapped_column(Text)
    heating_form_2_description_fi: Mapped[str | None] = mapped_column(Text)
    heating_form_2_description_sv: Mapped[str | None] = mapped_column(Text)
    heat_distribution_id: Mapped[str | None] = mapped_column(String)
    heat_distribution_description_fi: Mapped[str | None] = mapped_column(Text)
    heat_distribution_description_sv: Mapped[str | None] = mapped_column(Text)
    source_row: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

class EnergymapDensityGridZ5_8(Base):
    __tablename__ = "energymap_density_grid_z5_8"
    __table_args__ = (
        Index("idx_energymap_density_grid_z5_8_geom", "geom", postgresql_using="gist"),
    )

    cell_id: Mapped[str] = mapped_column(String, primary_key=True)
    cell_size_m: Mapped[int] = mapped_column(Integer)
    min_zoom: Mapped[int] = mapped_column(Integer)
    max_zoom: Mapped[int] = mapped_column(Integer)
    cell_x: Mapped[int] = mapped_column(BigInteger)
    cell_y: Mapped[int] = mapped_column(BigInteger)
    building_count: Mapped[int] = mapped_column(Integer)
    certified_building_count: Mapped[int] = mapped_column(Integer)
    certificate_coverage: Mapped[float | None] = mapped_column(Float)
    class_a_count: Mapped[int] = mapped_column(Integer)
    class_b_count: Mapped[int] = mapped_column(Integer)
    class_c_count: Mapped[int] = mapped_column(Integer)
    class_d_count: Mapped[int] = mapped_column(Integer)
    class_e_count: Mapped[int] = mapped_column(Integer)
    class_f_count: Mapped[int] = mapped_column(Integer)
    class_g_count: Mapped[int] = mapped_column(Integer)
    class_unknown_count: Mapped[int] = mapped_column(Integer)
    dominant_energy_certificate_class: Mapped[str | None] = mapped_column(String)
    dominant_energy_certificate_class_count: Mapped[int] = mapped_column(Integer)
    geom: Mapped[str | None] = mapped_column(Geometry("POLYGON", srid=3857, spatial_index=True))

class EnergymapDensityGridZ9_12(Base):
    __tablename__ = "energymap_density_grid_z9_12"
    __table_args__ = (
        Index("idx_energymap_density_grid_z9_12_geom", "geom", postgresql_using="gist"),
    )

    cell_id: Mapped[str] = mapped_column(String, primary_key=True)
    cell_size_m: Mapped[int] = mapped_column(Integer)
    min_zoom: Mapped[int] = mapped_column(Integer)
    max_zoom: Mapped[int] = mapped_column(Integer)
    cell_x: Mapped[int] = mapped_column(BigInteger)
    cell_y: Mapped[int] = mapped_column(BigInteger)
    building_count: Mapped[int] = mapped_column(Integer)
    certified_building_count: Mapped[int] = mapped_column(Integer)
    certificate_coverage: Mapped[float | None] = mapped_column(Float)
    class_a_count: Mapped[int] = mapped_column(Integer)
    class_b_count: Mapped[int] = mapped_column(Integer)
    class_c_count: Mapped[int] = mapped_column(Integer)
    class_d_count: Mapped[int] = mapped_column(Integer)
    class_e_count: Mapped[int] = mapped_column(Integer)
    class_f_count: Mapped[int] = mapped_column(Integer)
    class_g_count: Mapped[int] = mapped_column(Integer)
    class_unknown_count: Mapped[int] = mapped_column(Integer)
    dominant_energy_certificate_class: Mapped[str | None] = mapped_column(String)
    dominant_energy_certificate_class_count: Mapped[int] = mapped_column(Integer)
    geom: Mapped[str | None] = mapped_column(Geometry("POLYGON", srid=3857, spatial_index=True))

class Address(Base):
    __tablename__ = "addresses"
    address_key: Mapped[str | None] = mapped_column(String, primary_key=True) # primary address key
    building_key: Mapped[str | None] = mapped_column(String) # building key, used to map with buildings table
    address_fin: Mapped[str | None] = mapped_column(String) # street name and number
    postal_office_fin: Mapped[str | None] = mapped_column(String)
    municipality_number: Mapped[str | None] = mapped_column(String)
    postal_code: Mapped[str | None] = mapped_column(String)
    modified_timestamp_utc: Mapped[str | None] = mapped_column(String)  # source system's last modified timestamp in UTC
    point: Mapped[str | None] = mapped_column(Geometry("POINT", srid=4326, spatial_index=True)) #  SRID 4326 / WGS84 coordinates
    created_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

class Polygon(Base):
    __tablename__ = "polygons"
    polygon_key: Mapped[str | None] = mapped_column(String, primary_key=True) # primary polygon key
    building_key: Mapped[str | None] = mapped_column(String) # building key, used to map with buildings table
    modified_timestamp_utc: Mapped[str | None] = mapped_column(String)  # source system's last modified timestamp in UTC - corresponding to beginLifespanVersion column
    polygon: Mapped[str | None] = mapped_column(Geometry("MULTIPOLYGON", srid=4326, spatial_index=True)) #  SRID 4326 / WGS84 coordinates
    created_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

# Register analytical tables in the same Alembic metadata.
from . import area_models  # noqa: E402,F401
from . import user_building_models  # noqa: E402,F401
