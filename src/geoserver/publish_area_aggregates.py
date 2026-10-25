"""Publish immutable candidate layers, verify delivery, then allow promotion."""

import argparse
from datetime import datetime, timezone
import json
import math
import struct
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlencode

import requests
from sqlalchemy import text

from src.area_contract import LEVEL_WIDTH, STABLE_AREA_VIEWS
from src.area_tiles import TILE_FIELDS, tile_projection, tile_values
from src.db.session import SessionLocal
from src.etl.area_inputs import json_text
from src.etl.area_releases import release_id
from src.geoserver.publish_density_grids import DensityLayerConfig, configure_gwc_layer, post_json
from src.geoserver.update_energy_certificate_class import MVT_FORMAT, geoserver_config, get_json, get_xml, put_json, put_xml, request, truncate_gwc_layer


# Minimal protobuf reader for MVT properties. Geometry commands are deliberately
# ignored: PostGIS/GeoServer owns geometry encoding, and verification compares data.
def fields(data):
    offset = 0

    def varint():
        nonlocal offset
        result = shift = 0
        while offset < len(data) and shift < 70:
            byte = data[offset]
            offset += 1
            result |= (byte & 127) << shift
            if byte < 128:
                return result
            shift += 7
        raise ValueError("Truncated or invalid protobuf varint")

    while offset < len(data):
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value = varint()
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value = data[offset:offset + size]
            offset += size
            if len(value) != size:
                raise ValueError("Truncated protobuf fixed value")
        elif wire == 2:
            size = varint()
            value = data[offset:offset + size]
            offset += size
            if len(value) != size:
                raise ValueError("Truncated protobuf field")
        else:
            raise ValueError("Unsupported protobuf wire type")
        yield number, wire, value


def packed_varints(data):
    result, value, shift = [], 0, 0
    for byte in data:
        value |= (byte & 127) << shift
        if byte < 128:
            result.append(value)
            value = shift = 0
        else:
            shift += 7
            if shift >= 70:
                raise ValueError("Invalid packed protobuf value")
    if shift:
        raise ValueError("Truncated packed protobuf value")
    return result


def decode_mvt(data):
    layers = {}
    for number, _, layer in fields(data):
        if number != 3:
            continue
        name, keys, values, features = None, [], [], []
        for number, _, value in fields(layer):
            if number == 1:
                name = value.decode()
            elif number == 2:
                features.append(value)
            elif number == 3:
                keys.append(value.decode())
            elif number == 4:
                parts = list(fields(value))
                if len(parts) != 1:
                    raise ValueError("Invalid MVT value")
                kind, _, item = parts[0]
                values.append(item.decode() if kind == 1 else
                              struct.unpack('<f' if kind == 2 else '<d', item)[0] if kind in (2, 3) else
                              (item >> 1) ^ -(item & 1) if kind == 6 else bool(item) if kind == 7 else
                              item - 2**64 if kind == 4 and item >= 2**63 else item)
        if name is None or name in layers:
            raise ValueError("Missing or duplicate MVT source layer")
        output = []
        for feature in features:
            tags = []
            for number, _, value in fields(feature):
                if number == 2:
                    tags.extend(packed_varints(value))
            if len(tags) % 2:
                raise ValueError("Invalid MVT property tags")
            output.append({keys[tags[i]]: values[tags[i + 1]] for i in range(0, len(tags), 2)})
        layers[name] = output
    return layers


def view_names(value):
    suffix = release_id(value).replace('-', '')
    return {level: f"energymap_{level}_areas_{suffix}" for level in LEVEL_WIDTH}


def create_views(session, identifier):
    identifier = release_id(identifier)
    for level, name in view_names(identifier).items():
        # Identifier names and release UUID have been strictly constructed above.
        exists = session.execute(text("SELECT to_regclass(:name)"), {"name": name}).scalar()
        if exists:
            # Existing immutable views are reused, never overwritten.
            definition = session.execute(text("SELECT pg_get_viewdef(to_regclass(:name))"), {"name": name}).scalar()
            if identifier not in definition or f"'{level}'" not in definition:
                raise RuntimeError("Existing area view does not match this release")
        else:
            session.execute(text(f"""
                CREATE VIEW {name} AS
                SELECT a.area_code::bigint AS fid,s.release_id,a.level,a.area_code,
                  a.level||':'||a.area_code AS area_id,a.name_fi,a.name_sv,a.name_en,
                  s.building_count,s.valid_official_class_count,s.certificate_coverage,
                  a.display_geom AS geom,{tile_projection()}
                FROM energymap_area_stats s JOIN energymap_area_boundaries a USING(boundary_set_id,level,area_code)
                WHERE s.release_id='{identifier}' AND a.level='{level}'
            """))
        if session.execute(text("SELECT 1 FROM pg_roles WHERE rolname='sandbox_energymap_readonly'")).first():
            session.execute(text(f"GRANT SELECT ON {name} TO sandbox_energymap_readonly"))


def create_stable_views(session):
    """Views resolve the active pointer on each read; promotion swaps all levels."""
    for level, name in STABLE_AREA_VIEWS.items():
        session.execute(text(f"""
            CREATE OR REPLACE VIEW {name} AS
            SELECT a.area_code::bigint AS fid,a.level,a.area_code,
              a.level||':'||a.area_code AS area_id,a.name_fi,a.name_sv,a.name_en,
              s.building_count,s.valid_official_class_count,s.certificate_coverage,
              a.display_geom AS geom,{tile_projection()}
            FROM energymap_area_stats s
            JOIN energymap_area_releases r USING(release_id)
            JOIN energymap_area_boundaries a ON a.boundary_set_id=s.boundary_set_id
              AND a.level=s.level AND a.area_code=s.area_code
            WHERE r.active AND r.validated AND a.level='{level}'
        """))
        if session.execute(text("SELECT 1 FROM pg_roles WHERE rolname='sandbox_energymap_readonly'")).first():
            session.execute(text(f"GRANT SELECT ON {name} TO sandbox_energymap_readonly"))


def publish_catalog(config, identifier, *, stable=False):
    for level, name in (STABLE_AREA_VIEWS if stable else view_names(identifier)).items():
        base = f"/rest/workspaces/{quote(config['workspace'], safe='')}/datastores/{quote(config['datastore'], safe='')}/featuretypes"
        try:
            existing = get_json(config, f"{base}/{name}.json")["featureType"]
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
            payload = {"featureType": {"name": name, "nativeName": name,
                       "title": f"Energy Map {level} areas" + ("" if stable else f" — {identifier}"), "srs": "EPSG:3857",
                       "projectionPolicy": "FORCE_DECLARED", "enabled": True, "advertised": stable}}
            post_json(config, base + '?recalculate=nativebbox,latlonbbox', payload)
        else:
            if existing.get("nativeName") != name or existing.get("srs") != "EPSG:3857":
                raise RuntimeError("Existing GeoServer feature type does not match the immutable view")
            if existing.get("advertised", stable) != stable:
                put_json(config, f"{base}/{name}.json", {"featureType": {"advertised": stable}})
            if stable:
                attributes = existing.get('attributes', {}).get('attribute', [])
                names = {a['name'] for a in attributes}
                missing = [field for field in TILE_FIELDS if field not in names]
                if missing:
                    # Reload this native schema before configuring attributes.
                    # Sending new names against a cached old schema is rejected
                    # by GeoServer's CQL validation even after the SQL view grew.
                    put_json(config, f"{base}/{name}.json?recalculate=attributes", {'featureType': {'enabled': True}})
                    refreshed = get_json(config, f"{base}/{name}.json")["featureType"]
                    names = {a['name'] for a in refreshed.get('attributes', {}).get('attribute', [])}
                    if set(TILE_FIELDS) - names:
                        raise RuntimeError("GeoServer did not reload the thematic native schema")
        configure_gwc_layer(config, name, DensityLayerConfig(name, f"Energy Map {level}", 0, 14, 0, 14, 1, 1))
        cache = get_xml(config, f"/gwc/rest/layers/{quote(config['workspace']+':'+name,safe='')}.xml")
        subset = cache.find("gridSubsets/gridSubset[gridSetName='EPSG:900913']")
        if subset is None or any(subset.findtext(k) != v for k,v in
                                 {'zoomStart':'0','zoomStop':'14','minCachedLevel':'0','maxCachedLevel':'14'}.items()) or MVT_FORMAT not in [node.text for node in cache.findall('mimeFormats/string')]:
            raise RuntimeError("Published area cache configuration verification failed")
        actual = get_json(config, f"{base}/{name}.json")["featureType"]
        if not actual.get("enabled") or actual.get("nativeName") != name or actual.get("advertised", stable) != stable:
            raise RuntimeError("Published GeoServer feature type verification failed")


def publish_latest(session, config, *, api_url=None):
    identifier = session.execute(text("SELECT release_id FROM energymap_area_releases WHERE active AND validated")).scalar()
    if not identifier:
        raise RuntimeError("A verified active release is required for stable publication")
    create_stable_views(session)
    session.commit()
    publish_catalog(config, identifier, stable=True)
    for name in STABLE_AREA_VIEWS.values():
        path = '/gwc/rest/layers/' + quote(config['workspace'] + ':' + name, safe='') + '.xml'
        cache = get_xml(config, path)
        expiry = cache.find('expireClients')
        if expiry is None:
            expiry = ET.SubElement(cache, 'expireClients')
        expiry.text = '0'
        put_xml(config, path, cache)
        truncate_gwc_layer(config, config['workspace'] + ':' + name)
    result = {"latest": True, "layers": STABLE_AREA_VIEWS, "cache_truncated": True}
    if api_url:
        verified = verify_delivery(session, identifier, api_url, config=config, stable=True)
        session.execute(text("""
            UPDATE energymap_area_releases
            SET publication=jsonb_set(publication,'{latest_layers}',CAST(:layers AS jsonb))
            WHERE release_id=:id AND active AND validated
        """), {'id': identifier, 'layers': json_text(verified['layers'])})
        session.commit()
        result.update(verified)
    return result


def samples(session, identifier, level, limit=3):
    return session.execute(text("""
        SELECT a.area_code,ST_X(ST_PointOnSurface(a.display_geom)) AS x,
          ST_Y(ST_PointOnSurface(a.display_geom)) AS y
        FROM energymap_area_stats s JOIN energymap_area_boundaries a USING(boundary_set_id,level,area_code)
        WHERE s.release_id=:id AND s.level=:level ORDER BY ST_Area(a.display_geom) DESC,a.area_code LIMIT :n
    """), {"id": identifier, "level": level, "n": limit}).mappings().all()


def check_properties(session, identifier, level, tile, required_code, *, stable=False):
    decoded = decode_mvt(tile)
    source_layers = [name for name, rows in decoded.items() if rows]
    if len(source_layers) != 1:
        raise RuntimeError("Expected one nonempty MVT source layer")
    rows = decoded[source_layers[0]]
    expected = {row['area_code']: row for row in session.execute(text("""
        SELECT area_code,building_count,valid_official_class_count,certificate_coverage,metrics
        FROM energymap_area_stats WHERE release_id=:id AND level=:level
    """), {"id": identifier, "level": level}).mappings()}
    codes = set()
    for row in rows:
        code = row.get('area_code')
        release_matches = 'release_id' not in row if stable else row.get('release_id') == identifier
        if code in codes or code not in expected or not release_matches or row.get('area_id') != f'{level}:{code}':
            raise RuntimeError("MVT identifiers do not match the release")
        codes.add(code)
        for field in ('building_count', 'valid_official_class_count', 'certificate_coverage'):
            actual, wanted = row.get(field), expected[code][field]
            if actual != wanted and not (isinstance(actual, (int, float)) and isinstance(wanted, (int, float)) and math.isclose(actual, wanted, rel_tol=1e-6)):
                raise RuntimeError(f"MVT {field} does not match the summary")
        for field, wanted in tile_values(expected[code]['metrics'], expected[code]['building_count']).items():
            if row.get(field) != wanted:
                raise RuntimeError(f"MVT {field} does not match the thematic summary")
    if required_code not in codes:
        raise RuntimeError("Representative area is missing from its MVT tile")
    return source_layers[0], codes


def verify_delivery(session, identifier, api_url, *, config=None, get=None, stable=False):
    """Check all levels using release-pinned detail and real decoded tile properties."""
    identifier = release_id(identifier)
    api_url = api_url.rstrip('/')
    get = get or requests.get
    response = get(f'{api_url}/energy-data/areas/metadata', params={} if stable else {'release_id': identifier}, timeout=60)
    response.raise_for_status()
    if (stable and (not response.json().get('latest') or 'release_id' in response.json())) or (not stable and response.json().get('release_id') != identifier):
        raise RuntimeError("API metadata release mismatch")
    layers, checks = {}, []
    world = 20037508.342789244
    for level in LEVEL_WIDTH:
        source_layer = None
        for area in samples(session, identifier, level):
            code = area['area_code']
            response = get(f'{api_url}/energy-data/areas/{level}/{code}', params={} if stable else {'release_id': identifier}, timeout=60)
            response.raise_for_status()
            detail = response.json()
            expected = session.execute(text("SELECT building_count,valid_official_class_count,certificate_coverage,metrics FROM energymap_area_stats WHERE release_id=:id AND level=:level AND area_code=:code"), {'id': identifier, 'level': level, 'code': code}).mappings().one()
            release_matches = 'release_id' not in detail and detail.get('latest') if stable else detail.get('release_id') == identifier
            if not release_matches or detail.get('area_code') != code or any(detail.get(k) != v for k, v in expected.items()):
                raise RuntimeError("Area detail does not match the database summary")
            z = 12
            x = min(2**z-1, max(0, int((area['x']+world)/(world*2)*2**z)))
            y = min(2**z-1, max(0, int((world-area['y'])/(world*2)*2**z)))
            if config:
                name = (STABLE_AREA_VIEWS if stable else view_names(identifier))[level]
                width = world*2/2**z
                bbox = [x*width-world,world-(y+1)*width,(x+1)*width-world,world-y*width]
                params = {'service': 'WMS', 'version': '1.1.1', 'request': 'GetMap', 'layers': config['workspace']+':'+name,
                          'srs': 'EPSG:3857', 'bbox': ','.join(map(str,bbox)), 'width': 256, 'height': 256, 'format': MVT_FORMAT}
                _, direct = request(config, '/wms?'+urlencode(params), accept=MVT_FORMAT)
                qualified = quote(config['workspace']+':'+name, safe='')
                tile_url = f"/gwc/service/tms/1.0.0/{qualified}@EPSG%3A900913@pbf/{z}/{x}/{2**z-1-y}.pbf"
                _, cached = request(config, tile_url, accept=MVT_FORMAT)
                direct_layer, direct_codes = check_properties(session, identifier, level, direct, code, stable=stable)
                current_layer, cached_codes = check_properties(session, identifier, level, cached, code, stable=stable)
                if direct_codes != cached_codes or direct_layer != current_layer:
                    raise RuntimeError("Direct and cached area tiles disagree")
            else:
                path = f'{level}/tiles' if stable else f'{identifier}/{level}/tiles'
                response = get(f'{api_url}/energy-data/areas/{path}/{z}/{x}/{y}.pbf', timeout=60)
                response.raise_for_status()
                if not stable and (response.headers.get('X-Energy-Map-Release') != identifier or 'immutable' not in response.headers.get('Cache-Control','')):
                    raise RuntimeError("Native tile release/cache headers are missing")
                current_layer, _ = check_properties(session, identifier, level, response.content, code, stable=stable)
            if source_layer not in (None, current_layer):
                raise RuntimeError("MVT source layer changed between samples")
            source_layer = current_layer
            checks.append({'level': level, 'area_code': code, 'z': z, 'x': x, 'y': y})
        if not source_layer:
            raise RuntimeError("No representative boundaries for a required level")
        if config:
            name = (STABLE_AREA_VIEWS if stable else view_names(identifier))[level]
            url = f"{config['base_url']}/gwc/service/tms/1.0.0/{quote(config['workspace']+':'+name, safe='')}@EPSG%3A900913@pbf/{{z}}/{{x}}/{{y}}.pbf"
            layers[level] = {'url': url, 'scheme': 'tms', 'source_layer': source_layer, 'minzoom': 0, 'maxzoom': 14}
        else:
            path = level if stable else f'{identifier}/{level}'
            layers[level] = {'url': f'{api_url}/energy-data/areas/{path}/tiles/{{z}}/{{x}}/{{y}}.pbf', 'scheme': 'xyz', 'source_layer': source_layer, 'minzoom': 0, 'maxzoom': 14}
    return {'verified': True, 'release_id': identifier, 'delivery': 'geoserver' if config else 'native',
            'verified_at': datetime.now(timezone.utc).isoformat(), 'layers': layers, 'checks': checks}


def record_publication(session, identifier, publication):
    identifier = release_id(identifier)
    if publication.get('release_id') != identifier or not publication.get('verified') or set(publication.get('layers', {})) != set(LEVEL_WIDTH):
        raise ValueError("Incomplete publication verification")
    changed = session.execute(text("""
        UPDATE energymap_area_releases SET publication=CAST(:p AS jsonb)
        WHERE release_id=:id AND validated AND NOT active
    """), {'id': identifier, 'p': json_text(publication)}).rowcount
    if changed != 1:
        raise RuntimeError("Only a validated, inactive candidate may have its delivery recorded")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-id')
    parser.add_argument('--api-url')
    parser.add_argument('--latest', action='store_true', help='Publish stable names for the active release and invalidate their caches')
    parser.add_argument('--delivery', choices=('native','geoserver'), default='native')
    parser.add_argument('--apply', action='store_true', help='Publish/verify and record delivery; default only describes the candidate')
    args = parser.parse_args()
    if args.latest:
        if not args.apply:
            print(json_text({'latest': True, 'views': STABLE_AREA_VIEWS, 'apply': False}))
        else:
            if not args.api_url:
                parser.error('--api-url is required to verify stable publication')
            with SessionLocal() as session:
                print(json_text(publish_latest(session, geoserver_config(), api_url=args.api_url)))
        return
    if not args.release_id or not args.api_url:
        parser.error('--release-id and --api-url are required for candidate verification')
    identifier = release_id(args.release_id)
    with SessionLocal() as session:
        row = session.execute(text("SELECT validated,active FROM energymap_area_releases WHERE release_id=:id"), {'id': identifier}).mappings().first()
        if row is None or not row['validated'] or row['active']:
            raise RuntimeError("Expected a validated, inactive candidate")
        if not args.apply:
            print(json_text({'release_id': identifier, 'delivery': args.delivery, 'views': view_names(identifier), 'apply': False}))
            return
        config = geoserver_config() if args.delivery == 'geoserver' else None
        if config:
            create_views(session, identifier)
            session.commit()  # GeoServer uses another connection; views must be visible first.
            publish_catalog(config, identifier)
        publication = verify_delivery(session, identifier, args.api_url, config=config)
        record_publication(session, identifier, publication)
        session.commit()
    print(json_text(publication))


if __name__ == '__main__':
    main()
