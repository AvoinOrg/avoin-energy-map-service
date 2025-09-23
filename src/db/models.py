from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, TIMESTAMP, text
from geoalchemy2 import Geometry

class Base(DeclarativeBase):
    pass

class CodeMap(Base):
    __tablename__ = "code_maps"
    scheme: Mapped[str] = mapped_column(String, primary_key=True)  # url / path of reference e.g. 'rytj/lammitystapa'
    code:   Mapped[str] = mapped_column(String, primary_key=True)  # e.g. '07'
    label_fi: Mapped[str | None] = mapped_column(String)
    label_sv: Mapped[str | None] = mapped_column(String)
    label_en: Mapped[str | None] = mapped_column(String)

class Building(Base):
    __tablename__ = "buildings"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    building_key: Mapped[str | None] = mapped_column(String, unique=True)  # from GeoJSON properties.building_key
    permanent_building_identifier: Mapped[str | None] = mapped_column(String, unique=True, index=True)  # PRT
    heating_method: Mapped[str | None] = mapped_column(String)  # store code only, e.g. '07'
    point: Mapped[str | None] = mapped_column(Geometry("POINT", srid=4326, spatial_index=True))
    created_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
