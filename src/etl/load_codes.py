# etl/load_codes.py
import requests
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from db.session import SessionLocal
from db.models import CodeMap

TYPES = {
    "main_purpose": "https://koodistot.suomi.fi/codelist-api/api/v1/coderegistries/rytj/codeschemes/avoin_rakennusluokitus/",
    "usage_status": "https://koodistot.suomi.fi/codelist-api/api/v1/coderegistries/vtj/codeschemes/rake_kaytossaolotilanne/",
    "heating_method": "https://koodistot.suomi.fi/codelist-api/api/v1/coderegistries/rytj/codeschemes/lammitystapa/",
    "heating_energy_source": "https://koodistot.suomi.fi/codelist-api/api/v1/coderegistries/rytj/codeschemes/lammitysenergianlahde/",
    "material_of_load_bearing_structures": "https://koodistot.suomi.fi/codelist-api/api/v1/coderegistries/rytj/codeschemes/kantavanrakenteenrakennusaine/",
}

# params pass to the code list endpoint
DEFAULT_PARAMS = {
    "format": "json",
    "embedCodes": "true",
    "expand": "code" # include code details
}

def _extract_items(payload):
    items = []
    if isinstance(payload, dict):
        for code_obj in payload.get("codes", []) or []:
            code = code_obj.get("codeValue")
            labels = code_obj.get("prefLabel") or {}
            modified = code_obj.get("modified")
            items.append((str(code), labels.get("fi"), labels.get("sv"), labels.get("en"), modified))        
    return items

def load_all_code_types():
    total_count = 0
    with SessionLocal() as session:
        for code_type, base_url in TYPES.items():
            response = requests.get(base_url, params=DEFAULT_PARAMS, timeout=30)
            response.raise_for_status()
            codes = _extract_items(response.json())

            for code_value, label_fi, label_sv, label_en, modified in codes:
                stmt = insert(CodeMap).values(
                    type=code_type,
                    code=code_value,
                    label_fi=label_fi,
                    label_sv=label_sv,
                    label_en=label_en,
                    src_last_modified=modified,
                ).on_conflict_do_update(
                    index_elements=[CodeMap.type, CodeMap.code],
                    set_={
                        "label_fi": label_fi,
                        "label_sv": label_sv,
                        "label_en": label_en,
                        "src_last_modified": modified,
                    },
                )
                session.execute(stmt)

            session.commit()
            total_count += len(codes)
            print(f"Upserted {len(codes)} codes for type={code_type}")

    print(f"Total upserted codes: {total_count}")

if __name__ == "__main__":
    load_all_code_types()
