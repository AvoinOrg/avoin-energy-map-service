from datetime import date
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, TIMESTAMP, text, Date
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
    id: Mapped[str] = mapped_column(String, primary_key=True)
    building_key: Mapped[str | None] = mapped_column(String, unique=True)
    permanent_building_identifier: Mapped[str | None] = mapped_column(String, unique=True)
    property_identifier: Mapped[str | None] = mapped_column(String, unique=True)
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
