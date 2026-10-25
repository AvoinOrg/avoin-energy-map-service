import csv
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import math
from urllib.parse import parse_qs, urlparse
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from shapely.geometry import MultiPolygon, box, mapping
from shapely.ops import unary_union
from sqlalchemy import text

from src.api.main import app
from src.area_contract import ATTRIBUTES, CATEGORIES, empty_metrics
from src.db.session import SessionLocal, ENGINE_LOCAL
from src.etl.area_inputs import area_code, boundary_rows, input_manifest, download_wfs, geography_record, import_boundaries, import_geography
from src.etl.build_area_aggregates import build_release, validate_release
from src.etl.backfill_area_energy_classes import backfill
from src.area_tiles import tile_values
from src.etl.area_releases import promote_release
from src.geoserver.publish_area_aggregates import create_views, decode_mvt, record_publication, verify_delivery, view_names


def key(n):
    return str(UUID(int=n))


class AreaInputTests(unittest.TestCase):
    def test_codes_preserve_zero_and_reject_bad_input(self):
        self.assertEqual(area_code('5','municipality'), '005')
        self.assertEqual(area_code('00100','postal_code'), '00100')
        for value in ('1.0','-5','1234',None,True,'５'):
            with self.assertRaises(ValueError):
                area_code(value,'municipality')

    def test_registry_export_coordinates_and_missing_values(self):
        row = {'building_key':key(1),'municipality_number':'5','X':'300000','Y':'6800000','point_location_srid':'4258'}
        result = geography_record(row,3067)
        self.assertEqual(result[:3],(key(1),'005','POINT(300000.0 6800000.0)'))
        self.assertEqual(json.loads(result[3])['point_location_srid'],'4258')
        self.assertIsNone(geography_record({**row,'X':'','Y':''},3067)[2])
        for changed in ({'Y':''},{'X':'NaN'},{'building_key':'bad'},{'municipality_number':'abc'}):
            with self.assertRaises(ValueError):
                geography_record({**row,**changed},3067)
        with self.assertRaises(ValueError):
            geography_record(row,4258)

    def test_wfs_rejects_duplicate_and_short_download_preserves_file(self):
        class Reply:
            text='test:areas'
            def raise_for_status(self): pass
            def __init__(self,data=None): self.data=data
            def json(self): return self.data
        class Http:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def get(self,url,params,timeout):
                if params['request'] != 'GetFeature': return Reply()
                start=params['startIndex']
                return Reply({'numberMatched':2,'features':[{'id':'same'}] if start<2 else []})
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'areas.geojson'
            path.write_text('existing')
            with patch('src.etl.area_inputs.requests.Session',return_value=Http()):
                with self.assertRaisesRegex(RuntimeError,'Duplicate'):
                    download_wfs(path,'https://example.test/wfs','test:areas',2,page_size=1)
            self.assertEqual(path.read_text(),'existing')

    def test_source_manifest_rejects_credentials_before_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'source.csv';path.write_text('fixture')
            for url in ('https://user:password@example.test/file','https://example.test/file?api_key=secret'):
                with self.assertRaisesRegex(ValueError,'without credentials'): input_manifest(path,url)
            original=input_manifest(path,'https://example.test/file')
            self.assertEqual(original['file'],'source.csv')
            self.assertIsNone(original['retrieved_at'])
            path.with_suffix('.csv.source.json').write_text(json.dumps({'sha256':original['sha256'],'retrieved_at':'2026-09-15','api_key':'secret','source_url':'https://example.test/file?token=secret'}))
            self.assertEqual(input_manifest(path,'https://example.test/file')['retrieved_at'],'2026-09-15')
            self.assertNotIn('secret',json.dumps(input_manifest(path,'https://example.test/file')))

    def test_mvt_invalid_and_truncated_data_rejected(self):
        self.assertEqual(decode_mvt(b''),{})
        for payload in (b'\x1a\x05a',b'\x80',b'\x07'):
            with self.assertRaises(ValueError): decode_mvt(payload)


@unittest.skipUnless(os.getenv('RUN_AREA_DB_TESTS')=='1','requires disposable PostGIS')
class AreaDatabaseTests(unittest.TestCase):
    def setUp(self):
        if os.getenv('PGDATABASE') != 'energymap_areas_test':
            raise RuntimeError('Refusing fixtures outside the named disposable test database')
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)
        with SessionLocal() as session:
            # Only the disposable database may be changed. Immutable views from previous fixtures are dropped first.
            names=session.execute(text("SELECT viewname FROM pg_views WHERE schemaname='public' AND viewname ~ '^energymap_(postal_code|municipality|region)_areas_[0-9a-f]{32}$'")).scalars().all()
            for name in names: session.execute(text(f'DROP VIEW {name}'))
            session.execute(text('TRUNCATE energymap_area_boundary_sets,energymap_area_geography_snapshots CASCADE'))
            session.execute(text('DROP TABLE IF EXISTS main CASCADE'))
            attrs=','.join(f'{name} text' for name in ATTRIBUTES)
            session.execute(text(f'CREATE TABLE main (building_key text,address_key text,point geometry(POINT,3857),polygon geometry(MULTIPOLYGON,3857),{attrs})'))
            session.commit()
        self.make_inputs()
        with SessionLocal() as session:
            boundary=import_boundaries(session,**self.boundary_args())
            geography=import_geography(session,self.path/'geography.csv',min_records=1)
            self.bid=boundary['boundary_set_id'];self.gid=geography['snapshot_id']
            session.commit()
        self.make_main()
        app.dependency_overrides.clear()
        self.client=TestClient(app)
        self.addCleanup(self.client.close)

    def geojson(self,name,level,rows):
        codefield={'postal_code':'posti_alue','municipality':'kunta','region':'maakunta'}[level]
        features=[{'type':'Feature','properties':{codefield:code,'vuosi':2026,'nimi':code,'namn':code,'name':code},'geometry':mapping(geom)} for code,geom in rows]
        (self.path/name).write_text(json.dumps({'type':'FeatureCollection','crs':{'type':'name','properties':{'name':'EPSG:3067'}},'features':features}))

    def make_inputs(self):
        m1=box(300000,6800000,301000,6801000).difference(box(300400,6800400,300600,6800600))
        island=box(302500,6800000,302700,6800200)
        m2=box(301000,6800000,302000,6801000);m3=box(303000,6800000,304000,6801000)
        self.geojson('municipalities.geojson','municipality',[('005',m1),('005',island),('091',m2),('009',m3)])
        self.geojson('postals.geojson','postal_code',[('00100',box(300000,6800000,301500,6801000)),('00200',box(301500,6800000,303000,6801000)),('00300',m3)])
        self.geojson('regions.geojson','region',[('01',unary_union([m1,island,m2])),('02',m3)])
        with (self.path/'classification.csv').open('w',newline='') as target:
            writer=csv.DictWriter(target,fieldnames=['municipality_code','region_code','name_fi','name_sv','name_en'])
            writer.writeheader()
            writer.writerows([{'municipality_code':c,'region_code':r,'name_fi':c,'name_sv':c,'name_en':c} for c,r in [('005','01'),('091','01'),('009','02')]])
        records=[(1,'5',300200,6800200),(2,'091',301200,6800200),(3,'091',301500,6800200),(4,'005','',''),(8,'091',300800,6800200),(9,'',300500,6800500),(11,'005',300200,6800200),(1,'5',300200,6800200)]
        with (self.path/'geography.csv').open('w',newline='') as target:
            writer=csv.writer(target);writer.writerow(['building_key','municipality_number','X','Y'])
            writer.writerows((key(n),m,x,y) for n,m,x,y in records)

    def boundary_args(self):
        return dict(postal_path=self.path/'postals.geojson',municipality_path=self.path/'municipalities.geojson',region_path=self.path/'regions.geojson',classification_path=self.path/'classification.csv',municipality_source_url='https://example.test/detailed-2026',municipality_source_version='fixture-2026',expected_counts={'postal_code':3,'municipality':3,'region':2},simplify_m=0)

    def make_main(self):
        rows=[]
        for n in range(1,10):
            data=dict(building_key=key(n),address_key=f'address-{n}',heating_method='01',heating_energy_source='02',main_purpose='0110',completion_date='1990-01-01',floor_area='100',energy_certificate_class='C',energy_certificate_class_year='2018',energy_certificate_valid_until='2030-01-01')
            if n==2: data.update(energy_certificate_class='B',energy_certificate_valid_until='2020-01-01')
            if n==3: data.update(floor_area='Infinity',energy_certificate_class='A',energy_certificate_class_year='2013')
            if n==4: data.update(energy_certificate_class=None,energy_certificate_valid_until=None)
            if n==5: data.update(energy_certificate_class_year=None,modeled_energy_class='D',modeled_energy_class_reason='model_ok')
            if n==6: data.update(energy_certificate_valid_until=None)
            if n==7: data.update(floor_area='garbage',heating_method=None,completion_date='2099-01-01')
            official=data.get('energy_certificate_class')
            data['energy_class']=official or data.get('modeled_energy_class')
            data['is_energy_class_modeled']='false' if official else 'true' if data['energy_class'] else None
            rows.append(data)
        rows.append({**rows[0],'address_key':'duplicate-address'})
        rows.append({**rows[4],'address_key':'other-footprint'})
        with SessionLocal() as session:
            for row in rows:
                columns=list(row)
                session.execute(text('INSERT INTO main ('+','.join(columns)+') VALUES ('+','.join(':'+c for c in columns)+')'),row)
            session.execute(text("UPDATE main SET polygon=ST_Multi(ST_Transform(ST_MakeEnvelope(302510,6800010,302550,6800050,3067),3857)) WHERE building_key=:key"),{'key':key(5)})
            session.execute(text("UPDATE main SET polygon=ST_Multi(ST_Transform(ST_MakeEnvelope(302580,6800080,302650,6800150,3067),3857)) WHERE address_key='other-footprint'"))
            session.execute(text("UPDATE main SET point=ST_Transform(ST_SetSRID(ST_MakePoint(301800,6800200),3067),3857) WHERE building_key=:key"),{'key':key(6)})
            session.commit()

    def build(self):
        with SessionLocal() as session:
            result=build_release(session,boundary_set_id=self.bid,geography_snapshot_id=self.gid,as_of=date(2026,10,1))
            session.commit()
        return result

    def fetch(self,url,**kwargs):
        kwargs.pop('timeout',None)
        return self.client.get(url,**kwargs)

    def publish(self,identifier):
        with SessionLocal() as session:
            publication=verify_delivery(session,identifier,'http://testserver',get=self.fetch)
            record_publication(session,identifier,publication)
            session.commit()
        return publication

    def promote(self,identifier):
        with SessionLocal() as session:
            report=promote_release(session,identifier);session.commit()
        return report

    def test_boundary_holes_islands_lookup_and_idempotent_inputs(self):
        with SessionLocal() as session:
            boundary=import_boundaries(session,**self.boundary_args())
            geography=import_geography(session,self.path/'geography.csv',min_records=1)
            self.assertTrue(boundary['reused']);self.assertTrue(geography['reused'])
            self.assertEqual(geography['identical_duplicate_rows'],1)
            self.assertEqual(session.execute(text("SELECT ST_NumGeometries(geom) FROM energymap_area_boundaries WHERE level='municipality' AND area_code='005'")).scalar_one(),2)
            self.assertFalse(session.execute(text("SELECT ST_Covers(geom,ST_SetSRID(ST_MakePoint(300500,6800500),3067)) FROM energymap_area_boundaries WHERE level='municipality' AND area_code='005'")).scalar_one())
            with self.assertRaisesRegex(RuntimeError,'minimum'):
                import_geography(session,self.path/'geography.csv',min_records=1_000_000)

    def test_unique_population_all_levels_conflicts_and_crosswalk(self):
        result=self.build();identifier=result['release_id']
        self.assertEqual(result['manifest']['building_count'],9)
        self.assertEqual(result['manifest']['duplicate_map_rows'],2)
        self.assertEqual(result['manifest']['geography_keys_outside_map'],1)
        self.assertEqual(result['validation']['levels'],{'postal_code':{'assigned':7,'unassigned':2},'municipality':{'assigned':6,'unassigned':3},'region':{'assigned':6,'unassigned':3}})
        with SessionLocal() as session:
            assignments={r['building_key']:r for r in session.execute(text('SELECT * FROM energymap_building_area_membership WHERE release_id=:id'),{'id':identifier}).mappings()}
            self.assertEqual(assignments[key(3)]['postal_method'],'shared_edge_code_tiebreak')
            self.assertEqual(assignments[key(4)]['municipality_method'],'registry_only')
            self.assertEqual(assignments[key(5)]['municipality_code'],'005')
            self.assertEqual(assignments[key(8)]['municipality_method'],'registry_spatial_conflict')
            self.assertIsNone(assignments[key(9)]['municipality_code'])
            totals={(r.level,r.area_code):r.building_count for r in session.execute(text('SELECT * FROM energymap_area_stats WHERE release_id=:id'),{'id':identifier})}
            self.assertEqual(totals,{('postal_code','00100'):5,('postal_code','00200'):2,('postal_code','00300'):0,('municipality','005'):3,('municipality','091'):3,('municipality','009'):0,('region','01'):6,('region','02'):0})
            row=session.execute(text("SELECT * FROM energymap_area_stats WHERE release_id=:id AND level='municipality' AND area_code='091'"),{'id':identifier}).mappings().one()
            self.assertEqual(row['valid_official_class_count'],1)
            self.assertAlmostEqual(row['certificate_coverage'],1/3)
            self.assertEqual(row['metrics']['official_class']['counts'],{'2018:expired:B':1,'2013:valid:A':1,'2018:unknown_validity:C':1})
            self.assertEqual(row['metrics']['official_class']['valid_counts_by_scale'],{'2013':{'A':1}})
            self.assertEqual(row['metrics']['floor_area']['sum'],200)
            self.assertEqual(row['metrics']['floor_area']['unknown_building_count'],1)
            cross={(r.postal_code,r.municipality_code):r.building_count for r in session.execute(text('SELECT * FROM energymap_postal_municipality_stats'))}
            self.assertEqual(cross,{('00100','005'):1,('00100','091'):2,('00200','005'):1,('00200','091'):1})
            self.assertEqual(session.execute(text('SELECT count(*) FROM main')).scalar_one(),11)

    def test_effective_classes_include_models_once_and_preserve_official_precedence(self):
        with SessionLocal() as session:
            session.execute(text("UPDATE main SET modeled_energy_class='E',energy_class='E',is_energy_class_modeled='true' WHERE building_key=:key"),{'key':key(4)})
            session.commit()
        identifier=self.build()['release_id']
        with SessionLocal() as session:
            metrics=session.execute(text("SELECT metrics FROM energymap_area_stats WHERE release_id=:id AND level='municipality' AND area_code='005'"),{'id':identifier}).scalar_one()
            self.assertEqual(metrics['energy_class']['counts'],{'C':2,'E':1})
            self.assertEqual(metrics['energy_class_source']['counts'],{'official':2,'modeled':1})
            # Building 5 contains both source values but its published class is C.
            self.assertEqual(metrics['modeled_class']['counts'],{'D':1,'E':1,'unknown':1})
            fields=tile_values(metrics,3)
            self.assertEqual(fields['energy_class_c_count'],2)
            self.assertEqual(fields['energy_class_e_count'],1)
            self.assertEqual(fields['energy_class_modeled_count'],1)
            self.assertEqual(fields['heating_other_count'],3)

    def test_legacy_extension_is_atomic_idempotent_and_preserves_previous_metrics(self):
        with SessionLocal() as session:
            session.execute(text("UPDATE main SET modeled_energy_class='E',energy_class='E',is_energy_class_modeled='true' WHERE building_key=:key"),{'key':key(4)})
            session.commit()
        identifier=self.build()['release_id']
        with SessionLocal() as session:
            session.execute(text("UPDATE energymap_area_buildings SET attributes=attributes-'energy_class'-'is_energy_class_modeled'-'energy_class_year' WHERE release_id=:id"),{'id':identifier})
            for table in ('energymap_area_stats','energymap_postal_municipality_stats'):
                session.execute(text(f"UPDATE {table} SET metrics=metrics-'energy_class'-'energy_class_source' WHERE release_id=:id"),{'id':identifier})
            before=session.execute(text("SELECT metrics FROM energymap_area_stats WHERE release_id=:id AND level='municipality' AND area_code='005'"),{'id':identifier}).scalar_one()
            session.commit()
        with SessionLocal() as session:
            result=backfill(session,identifier);session.commit()
            self.assertTrue(result['validated'])
            self.assertEqual(result['building_projection_mismatches'],0)
        with SessionLocal() as session:
            result=backfill(session,identifier);session.commit()
            self.assertFalse(any(result['updated_rows'].values()))
            after=session.execute(text("SELECT metrics FROM energymap_area_stats WHERE release_id=:id AND level='municipality' AND area_code='005'"),{'id':identifier}).scalar_one()
            self.assertEqual(before,{k:v for k,v in after.items() if k not in ('energy_class','energy_class_source')})
            self.assertEqual(after['energy_class_source']['counts'],{'official':2,'modeled':1})
            session.execute(text("UPDATE main SET energy_class='G' WHERE building_key=:key"),{'key':key(4)});session.commit()
        with SessionLocal() as session:
            with self.assertRaisesRegex(RuntimeError,'disagree'):
                backfill(session,identifier)
            session.rollback()

    def test_native_delivery_api_leading_zero_scope_and_zero_area(self):
        identifier=self.build()['release_id']
        self.assertEqual(self.client.get('/energy-data/areas/metadata').status_code,503)
        publication=self.publish(identifier)
        self.assertEqual(set(publication['layers']),{'postal_code','municipality','region'})
        self.assertGreaterEqual(len(publication['checks']),6)
        self.promote(identifier)
        metadata=self.client.get('/energy-data/areas/metadata')
        self.assertNotIn('release_id',metadata.json())
        self.assertTrue(metadata.json()['latest'])
        self.assertEqual(metadata.headers['cache-control'],'no-cache')
        full=self.client.get('/energy-data/areas/postal_code/00100').json()
        scoped=self.client.get('/energy-data/areas/postal_code/00100?municipality_code=091').json()
        self.assertEqual(full['building_count'],5);self.assertEqual(scoped['building_count'],2)
        self.assertEqual(scoped['scope'],{'municipality_code':'091','geometry':'full_postal_area'})
        empty=self.client.get('/energy-data/areas/municipality/009').json()
        self.assertEqual(empty['building_count'],0);self.assertIsNone(empty['certificate_coverage']);self.assertEqual(empty['metrics'],empty_metrics())
        self.assertEqual(self.client.get('/energy-data/areas/postal_code/00100?municipality_code=009').json()['building_count'],0)
        for url in ('/municipality/5','/city/005','/municipality/005?municipality_code=091',f'/{identifier}/region/tiles/15/0/0.pbf'):
            self.assertEqual(self.client.get('/energy-data/areas'+url).status_code,422)
        self.assertEqual(self.client.get('/energy-data/areas/region/99').status_code,404)
        self.assertEqual(self.client.get('/energy-data/areas/metadata?release_id='+str(uuid4())).status_code,404)
        # Production reads must continue with the national map table unavailable.
        with SessionLocal() as session:
            session.execute(text('ALTER TABLE main RENAME TO fixture_map_hidden'));session.commit()
        self.assertEqual(self.client.get('/energy-data/areas/municipality/005').status_code,200)
        self.publish_requires_inactive(identifier)
        with SessionLocal() as session:
            session.execute(text('ALTER TABLE fixture_map_hidden RENAME TO main'));session.commit()

    def publish_requires_inactive(self,identifier):
        with SessionLocal() as session:
            with self.assertRaisesRegex(RuntimeError,'inactive'):
                record_publication(session,identifier,{'verified':True,'release_id':identifier,'layers':dict.fromkeys(('postal_code','municipality','region'),{})})

    def test_geoserver_direct_and_cached_verification_uses_tms_y_and_release(self):
        identifier=self.build()['release_id']
        config={'workspace':'energy','base_url':'https://example.test/geoserver'}
        calls=[]
        names={name:level for level,name in view_names(identifier).items()}
        def request(config,path,**kwargs):
            calls.append(path)
            if path.startswith('/wms?'):
                params=parse_qs(urlparse(path).query)
                level=names[params['layers'][0].split(':')[1]]
                bbox=list(map(float,params['bbox'][0].split(',')))
                world=20037508.342789244;width=world*2/2**12
                x=round((bbox[0]+world)/width);y=round((world-bbox[3])/width)
            else:
                parts=path.split('/')
                level=names[parts[-4].split('@')[0].split('%3A')[1]]
                x=int(parts[-2]);y=2**12-1-int(parts[-1].removesuffix('.pbf'))
            response=self.client.get(f'/energy-data/areas/{identifier}/{level}/tiles/12/{x}/{y}.pbf')
            self.assertEqual(response.status_code,200)
            return 200,response.content
        with SessionLocal() as session,patch('src.geoserver.publish_area_aggregates.request',side_effect=request):
            result=verify_delivery(session,identifier,'http://testserver',config=config,get=self.fetch)
        self.assertEqual(result['delivery'],'geoserver')
        self.assertTrue(all(layer['scheme']=='tms' for layer in result['layers'].values()))
        self.assertTrue(any('/gwc/service/tms/' in path for path in calls))

    def test_promotion_failed_build_and_rollback_preserve_active(self):
        first=self.build()['release_id']
        with SessionLocal() as session:
            with self.assertRaisesRegex(RuntimeError,'verified delivery'): promote_release(session,first)
        self.publish(first);self.promote(first)
        second=self.build()['release_id']
        self.publish(second)
        self.assertEqual(self.promote(second)['previous_release_id'],first)
        self.assertEqual(self.promote(first)['previous_release_id'],second)
        with SessionLocal() as session:
            session.execute(text("UPDATE main SET heating_method='99' WHERE address_key='duplicate-address'"));session.commit()
        with SessionLocal() as session:
            with self.assertRaisesRegex(RuntimeError,'Conflicting analytical'):
                build_release(session,boundary_set_id=self.bid,geography_snapshot_id=self.gid)
        self.assertTrue(self.client.get('/energy-data/areas/metadata').json()['latest'])
        with SessionLocal() as session:
            self.assertEqual(session.execute(text('SELECT release_id FROM energymap_area_releases WHERE active')).scalar_one(),first)
            self.assertEqual(session.execute(text('SELECT count(*) FROM energymap_area_releases')).scalar_one(),2)

    def test_latest_views_details_and_tiles_follow_promotion_without_public_versions(self):
        from src.area_contract import STABLE_AREA_VIEWS
        first=self.build()['release_id']; self.publish(first); self.promote(first)
        self.assertEqual(self.client.get('/healthz').status_code,200)
        self.assertEqual(self.client.get('/readyz').status_code,200)
        public=self.client.get('/energy-data/areas/postal_code/00100')
        self.assertNotIn('release_id',public.json()); self.assertTrue(public.json()['latest'])
        self.assertEqual(public.headers['cache-control'],'no-cache')
        with SessionLocal() as session:
            for level,name in STABLE_AREA_VIEWS.items():
                self.assertNotIn('release_id',session.execute(text(f'SELECT * FROM {name} LIMIT 1')).mappings().one())
            session.execute(text("UPDATE main SET floor_area='200' WHERE building_key=:key"),{'key':key(1)})
            session.commit()
        second=self.build()['release_id']; self.publish(second); self.promote(second)
        latest=self.client.get('/energy-data/areas/postal_code/00100').json()
        retained=self.client.get('/energy-data/areas/postal_code/00100?release_id='+first).json()
        self.assertEqual(latest['metrics']['floor_area']['sum'],retained['metrics']['floor_area']['sum']+100)
        self.promote(first)
        self.assertEqual(self.client.get('/energy-data/areas/postal_code/00100').json()['metrics'],retained['metrics'])
        with SessionLocal() as session:
            xy=session.execute(text("SELECT floor((ST_X(ST_PointOnSurface(display_geom))+20037508.342789244)/40075016.685578488*4096)::int AS x,floor((20037508.342789244-ST_Y(ST_PointOnSurface(display_geom)))/40075016.685578488*4096)::int AS y FROM energymap_area_boundaries WHERE level='postal_code' AND area_code='00100'")).mappings().one()
        tile=self.client.get(f"/energy-data/areas/postal_code/tiles/12/{xy['x']}/{xy['y']}.pbf")
        self.assertEqual(tile.status_code,200);self.assertEqual(tile.headers['cache-control'],'no-cache')
        self.assertNotIn('x-energy-map-release',tile.headers)
        self.assertTrue(decode_mvt(tile.content)['areas'])
        self.assertTrue(all('release_id' not in f for f in decode_mvt(tile.content)['areas']))

    def test_reruns_reproduce_metrics_and_versioned_views(self):
        first=self.build();second=self.build()
        self.assertEqual(first['manifest'],second['manifest'])
        with SessionLocal() as session:
            create_views(session,first['release_id']);create_views(session,second['release_id']);create_views(session,first['release_id'])
            for name in view_names(first['release_id']).values():
                self.assertLessEqual(len(name),63)
                self.assertEqual(session.execute(text(f'SELECT DISTINCT release_id FROM {name}')).scalar_one(),first['release_id'])
            diff=session.execute(text('''SELECT count(*) FROM (
                (SELECT level,area_code,building_count,metrics FROM energymap_area_stats WHERE release_id=:a
                 EXCEPT SELECT level,area_code,building_count,metrics FROM energymap_area_stats WHERE release_id=:b)
            ) d'''),{'a':first['release_id'],'b':second['release_id']}).scalar_one()
            self.assertEqual(diff,0)

    def test_validation_detects_corrupted_histogram(self):
        identifier=self.build()['release_id']
        with SessionLocal() as session:
            session.execute(text("UPDATE energymap_area_stats SET metrics=jsonb_set(metrics,'{heating_method,counts,01}','99') WHERE release_id=:id AND level='municipality' AND area_code='005'"),{'id':identifier})
            with self.assertRaisesRegex(RuntimeError,'histogram'):
                validate_release(session,identifier,9)

    def test_bad_geography_and_overlapping_boundaries_fail_without_partial_import(self):
        source=self.path/'bad.csv'
        source.write_text((self.path/'geography.csv').read_text()+f'{key(1)},005,300900,6800900\n')
        with SessionLocal() as session:
            with self.assertRaisesRegex(RuntimeError,'Conflicting geography'): import_geography(session,source,min_records=1)
        self.geojson('postals.geojson','postal_code',[('00100',box(300000,6800000,302000,6801000)),('00200',box(301000,6800000,303000,6801000)),('00300',box(303000,6800000,304000,6801000))])
        with SessionLocal() as session:
            with self.assertRaisesRegex(RuntimeError,'Interior overlap'): import_boundaries(session,**self.boundary_args())
        with SessionLocal() as session:
            self.assertEqual(session.execute(text('SELECT count(*) FROM energymap_area_boundary_sets')).scalar_one(),1)
            self.assertEqual(session.execute(text('SELECT count(*) FROM energymap_area_geography_snapshots')).scalar_one(),1)


if __name__=='__main__':
    unittest.main()
