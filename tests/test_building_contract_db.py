"""Opt-in, exact-name/endpoint-guarded disposable PostGIS evidence.

Runtime doctor owns provisioning/migrations. This suite only owns fixtures in
its dedicated disposable database; it never reads private environment files.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import event, text

from src.api.main import app
from src.api.buildings import building_session
from src.building_contract import *
from src.building_resolver import *
from src.building_resolver import MAIN_INVARIANTS, REGISTRY, ADDRESS
from src.db.session import SessionLocal, ENGINE_LOCAL
from src.etl.certificate_consumption import SOURCE_PREFIX, CARRIERS
from src.geoserver.publish_area_aggregates import decode_mvt

KEY='000-building-not-uuid'
PID='0000000001'
POLYGON='MULTIPOLYGON(((24 60,24.001 60,24.001 60.001,24 60.001,24 60)))'
POLYGON2='MULTIPOLYGON(((24.002 60,24.003 60,24.003 60.001,24.002 60.001,24.002 60)))'


@unittest.skipUnless(os.getenv('RUN_BUILDING_CONTRACT_DB_TESTS')=='1','requires dedicated disposable PostGIS')
class BuildingContractDatabaseTests(unittest.TestCase):
    def setUp(self):
        if (os.getenv('PGDATABASE') != 'energymap_f001_building_contract_test' or
            os.getenv('PGHOST') != '127.0.0.1' or os.getenv('PGPORT') != '55432' or
            os.getenv('DATABASE_URL') or os.getenv('PGOPTIONS')):
            raise RuntimeError('Refusing fixtures outside doctor-provisioned disposable endpoint')
        with SessionLocal() as session:
            self.assertEqual(session.execute(text('SELECT current_database()')).scalar_one(),'energymap_f001_building_contract_test')
            session.execute(text('DROP TABLE IF EXISTS main CASCADE'))
            session.execute(text('TRUNCATE buildings,addresses,polygons,energy_certificates,code_maps'))
            attrs=','.join(f'"{name}" text' for name in dict.fromkeys((*REGISTRY,*ADDRESS,*(c for c in MAIN_INVARIANTS if c not in SCENARIO_COLUMNS))))
            session.execute(text(f'CREATE TABLE main (building_key text,{attrs},point geometry(POINT,3857),polygon geometry(MULTIPOLYGON,3857),unexpected_owner_secret text)'))
            session.execute(text('CREATE TABLE IF NOT EXISTS fixture_private_sentinel (private_value text)'))
            session.execute(text('TRUNCATE fixture_private_sentinel'))
            session.execute(text("INSERT INTO fixture_private_sentinel VALUES ('PRIVATE_SENTINEL')"))
            for kind, codes in {'main_purpose':('05','06','99'),'heating_method':('01','03'),'heating_energy_source':('01','04','09')}.items():
                for code in codes:
                    session.execute(text('INSERT INTO code_maps(type,code) VALUES(:t,:c)'),{'t':kind,'c':code})
            session.execute(text('''INSERT INTO buildings(building_key,permanent_building_identifier,property_identifier,
                completion_date,main_purpose,floor_area,apartment_count,modified_timestamp_utc,point)
                VALUES (:key,:pid,'00012345678901','1998-05-06','05',500,0,'2026-10-01T10:00:00Z',ST_SetSRID(ST_MakePoint(24,60),4326))'''),{'key':KEY,'pid':PID})
            for address_key, street in [('a','A street 1'),('b','B street 2')]:
                session.execute(text('''INSERT INTO addresses(address_key,building_key,address_fin,postal_code,postal_office_fin,modified_timestamp_utc)
                    VALUES(:a,:key,:street,'00100','City','2026-10-02T10:00:00Z')'''),{'a':address_key,'key':KEY,'street':street})
            session.execute(text('''INSERT INTO polygons(polygon_key,building_key,polygon,modified_timestamp_utc)
                VALUES('poly',:key,ST_GeomFromText(:poly,4326),'2026-10-03T10:00:00Z')'''),{'key':KEY,'poly':POLYGON})
            session.commit()
        self.rows=[self.main_row(address_key='b',address_fin='B street 2',polygon=POLYGON2),
                   self.main_row(address_key='a',address_fin='A street 1',polygon=POLYGON),
                   self.main_row(address_key='a',address_fin='A street 1',polygon=POLYGON)]
        self.insert_main(self.rows)
        app.dependency_overrides[building_session]=self.read_session
        self.addCleanup(app.dependency_overrides.clear)
        self.client=TestClient(app)
        self.addCleanup(self.client.close)

    def read_session(self):
        with SessionLocal() as session:
            yield session

    def main_row(self, **change):
        return {'building_key':KEY,'permanent_building_identifier':PID,'property_identifier':'00012345678901',
                'address_key':'a','address_fin':'A street 1','postal_code':'00100','postal_office_fin':'City',
                'completion_date':'2000-02-29','main_purpose':'05','heating_method':'01','heating_energy_source':'04',
                'floor_area':'100','number_of_storeys':'0','energy_class':'E','is_energy_class_modeled':'true',
                'modeled_energy_class':'E','energy_class_year':'2018','polygon':POLYGON,'unexpected_owner_secret':'SECRET_OWNER_TOKEN',**change}

    def insert_main(self, rows, *, replace=False):
        with SessionLocal() as session:
            if replace:session.execute(text('TRUNCATE main'))
            for row in rows:
                attrs=[k for k in row if k not in ('point','polygon')]
                values=[f':{k}' for k in attrs]
                for geometry in ('polygon','point'):
                    if row.get(geometry):
                        attrs.append(geometry)
                        values.append(f'ST_Transform(ST_GeomFromText(:{geometry},4326),3857)')
                session.execute(text(f"INSERT INTO main({','.join(attrs)}) VALUES ({','.join(values)})"),row)
            session.commit()

    def resolve(self,key=KEY):
        with SessionLocal() as session:return resolve_building(session,key)

    def certificate(self,identifier,**change):
        row={'certificate_id':identifier,'version':2018,'building_identifier':PID,
             'signed_at':'2026-01-02','valid_until':'2025-01-01','e_class':'B','heated_net_area':80,
             'ventilation_type_id':'0','ventilation_description_fi':'Original FI',
             'recommendations_fi':'Public recommendation',
             'source_row':json.dumps({**{SOURCE_PREFIX+suffix:0 for suffix in CARRIERS.values()},'owner_token':'PRIVATE_CERTIFICATE_SECRET'}),**change}
        attrs=list(row)
        with SessionLocal() as session:
            session.execute(text(f"INSERT INTO energy_certificates({','.join(attrs)}) VALUES ({','.join('CAST(:source_row AS jsonb)' if k=='source_row' else ':'+k for k in attrs)})"),row)
            session.commit()

    @contextmanager
    def read_only_audit(self):
        queries=[]
        def before(connection,cursor,statement,parameters,context,executemany):
            self.assertTrue(statement.lstrip().upper().startswith(('SELECT','WITH','SET','SHOW')),statement[:80])
            self.assertNotIn('fixture_private_sentinel',statement)
            queries.append((statement,parameters))
        event.listen(ENGINE_LOCAL,'before_cursor_execute',before)
        try:yield queries
        finally:event.remove(ENGINE_LOCAL,'before_cursor_execute',before)

    def checksum(self):
        with SessionLocal() as session:
            return {table:session.execute(text(f"SELECT md5(COALESCE(string_agg(value,',' ORDER BY value),'')) FROM (SELECT to_jsonb(t)::text AS value FROM {table} t) s")).scalar_one()
                    for table in ('main','buildings','addresses','polygons','energy_certificates','code_maps','fixture_private_sentinel')}

    def test_deterministic_duplicates_summary_geometry_and_zero_writes(self):
        before=self.checksum()
        with self.read_only_audit():
            result=self.resolve().value
            self.assertEqual(result.details.address_fin,'A street 1')
            self.assertEqual(result.details.floor_area,100)
            self.assertEqual(result.details.apartment_count,0)
            self.assertEqual(result.details.number_of_storeys,0)
            self.assertEqual(result.selection.address_variants,2)
            self.assertEqual(result.selection.geometry_variants,2)
            self.assertEqual(result.geometry.srid,3857)
            self.assertEqual(result.geometry.source,'main.polygon')
            with SessionLocal() as session:
                summary=resolve_building_summaries(session,[KEY,KEY])[KEY].value
            self.assertEqual(summary.location,result.location)
            self.assertEqual(summary.address_fin,result.details.address_fin)
            response=self.client.get('/energy-data/buildings/'+KEY)
            self.assertEqual(response.status_code,200)
            self.assertNotIn('SECRET',response.text)
            self.assertNotIn('source_row',response.text)
        self.assertEqual(before,self.checksum())
        saved=result.model_dump(mode='json');saved.pop('resolved_at')
        self.insert_main(list(reversed(self.rows)),replace=True)
        again=self.resolve().value.model_dump(mode='json');again.pop('resolved_at')
        self.assertEqual(saved,again)

    def test_address_ranking_missing_keys_and_invalid_postal_summary_agree(self):
        self.insert_main([self.main_row(address_key=None,address_fin='Recognizable street',postal_code='bad'),
                          self.main_row(address_key='0',address_fin=None),
                          self.main_row(address_key=None,address_fin=None,postal_code=None,postal_office_fin=None)],replace=True)
        base=self.resolve().value
        self.assertEqual(base.details.address_fin,'Recognizable street')
        self.assertIsNone(base.identity.address_key)
        self.assertIsNone(base.details.postal_code)
        with SessionLocal() as session:summary=resolve_building_summaries(session,[KEY])[KEY].value
        self.assertIsNone(summary.postal_code)
        self.assertEqual(summary.source_availability['postal_code'].status,'invalid')
        self.assertEqual(summary.address_fin,base.details.address_fin)

    def test_read_transaction_rejects_actual_writes(self):
        with SessionLocal() as session:
            resolve_building(session,KEY)
            self.assertEqual(session.execute(text('SHOW transaction_read_only')).scalar_one(),'on')
            self.assertEqual(session.execute(text('SHOW transaction_isolation')).scalar_one(),'repeatable read')
            from sqlalchemy.exc import DBAPIError
            with self.assertRaises(DBAPIError):
                session.execute(text("UPDATE buildings SET floor_area=1 WHERE building_key=:key"),{'key':KEY})

    def test_missing_orphan_exact_opaque_identity_and_errors(self):
        with self.read_only_audit():
            self.assertEqual(self.resolve('missing').status,'missing')
            self.assertEqual(self.resolve(' '+KEY).status,'missing')
            self.assertEqual(self.resolve(PID).status,'missing')
        with SessionLocal() as session:
            session.execute(text("INSERT INTO addresses(address_key,building_key,address_fin) VALUES ('orphan','orphan','Orphan address')"));session.commit()
        self.assertEqual(self.resolve('orphan').status,'missing')
        self.assertEqual(self.client.get('/energy-data/buildings/orphan').status_code,404)

    def test_identity_conflicts_and_null_invariants_are_ambiguous(self):
        for change in ({'permanent_building_identifier':'different'}, {'floor_area':'101'}, {'floor_area':None}, {'energy_class':'F'}):
            self.insert_main([self.rows[0],{**self.rows[0],**change}],replace=True)
            self.assertEqual(self.resolve().status,'ambiguous')
            self.assertEqual(self.client.get('/energy-data/buildings/'+KEY).status_code,409)
        self.insert_main([self.main_row(permanent_building_identifier='different')],replace=True)
        self.assertEqual(self.resolve().status,'ambiguous')

    def test_certificate_join_identifier_shared_by_other_canonical_key_is_ambiguous(self):
        self.certificate('1')
        for table in ('buildings','main'):
            with SessionLocal() as session:
                session.execute(text(f"INSERT INTO {table}(building_key,permanent_building_identifier) VALUES('second',:pid)"),{'pid':PID});session.commit()
            self.assertEqual(self.resolve().status,'ambiguous')
            with SessionLocal() as session:
                session.execute(text(f"DELETE FROM {table} WHERE building_key='second'"));session.commit()

    def test_building_only_main_only_and_source_precedence(self):
        self.insert_main([],replace=True)
        result=self.resolve().value
        self.assertEqual(result.details.floor_area,500)
        self.assertEqual(result.details.apartment_count,0)
        self.assertEqual(result.details.address_fin,'A street 1')
        self.assertEqual(result.geometry.source,'polygons.polygon')
        self.assertEqual(result.geometry.srid,4326)
        self.assertEqual(result.sources.main.status,'missing')
        self.assertEqual(result.sources.ryhti_polygon.timestamp_status,'known')
        self.insert_main([self.main_row(floor_area=None,main_purpose=None)],replace=True)
        result=self.resolve().value
        self.assertIsNone(result.details.floor_area)
        self.assertIsNone(result.details.main_purpose)
        self.assertEqual(result.details.apartment_count,0)
        self.insert_main([self.main_row(building_key='only-main',permanent_building_identifier='other-pid')])
        result=self.resolve('only-main').value
        self.assertEqual(result.sources.ryhti_building.status,'missing')
        self.assertEqual(result.details.floor_area,100)

    def test_certificate_order_is_lexical_expired_newest_blank_and_history_not_filled(self):
        self.certificate('10',signed_at='2026-01-02',e_class='A')
        self.certificate('8',signed_at='2026-01-02',e_class=None)
        self.certificate('9',signed_at='2026-01-02',e_class=None,heated_net_area=None,ventilation_type_id=' 0 ',
                         ventilation_description_fi=None,ventilation_description_sv='Swedish field')
        self.certificate('zz',signed_at=None,e_class='G')
        result=self.resolve().value
        cert=result.certificate
        self.assertEqual(cert.certificate_id,'9')
        self.assertIsNone(cert.energy_class)
        self.assertIsNone(cert.previous_class)
        self.assertEqual(cert.history_count,4)
        self.assertEqual(str(cert.valid_until),'2025-01-01')
        self.assertEqual(str(cert.signed_at),'2026-01-02')
        self.assertIsNone(cert.heated_net_area)
        self.assertIsNotNone(cert.imported_at)
        self.assertEqual(cert.consumption.total_kwh_year,0)
        self.assertEqual(cert.consumption.certificate_id,'9')
        self.assertEqual(result.details.ventilation_description,'Swedish field')
        self.assertEqual(result.field_provenance['details.ventilation_description'].source_language,'sv')
        self.assertEqual(result.details.ventilation_category,'gravity')
        self.assertNotIn('PRIVATE',result.model_dump_json())

    def test_certificate_publication_mismatch_and_user_never_relabels_official_facts(self):
        self.certificate('1')
        self.insert_main([self.main_row(energy_certificate_class='A',energy_certificate_heated_net_area='90',
                                        energy_certificate_consumption='{"owner":"SECRET"}')],replace=True)
        base=self.resolve().value
        self.assertEqual(base.certificate.energy_class,'B')
        self.assertEqual(base.published_certificate.energy_class,'A')
        self.assertEqual(base.certificate.publication_comparison,'differs')
        self.assertEqual(base.details.heated_net_area,80)
        view=project_building(base,BuildingOverridesV1(heated_net_area=2,ventilation_type_id='1',floor_area=0),ScenarioInputsV1(resident_count=None))
        self.assertEqual(view.effective.certificate.consumption.heated_net_area_m2,80)
        self.assertEqual(view.effective.certificate.ventilation_type_id,'0')
        self.assertEqual(view.effective.published_certificate,base.published_certificate)
        self.assertEqual(view.effective.details.ventilation_category,'mechanical')
        self.assertEqual(view.field_provenance['details.heated_net_area'].origin,'user')
        self.assertNotIn('SECRET',view.model_dump_json())
        self.assertIn('floor_area',view.effective.model_input_divergence)

    def test_timestamps_are_truthful_and_invalid_source_dates_remain_gaps(self):
        self.certificate('1')
        with SessionLocal() as session:
            session.execute(text("UPDATE code_maps SET label_fi='Erilliset pientalot' WHERE type='main_purpose' AND code='05'"));session.commit()
        result=self.resolve().value
        self.assertEqual(result.code_labels.main_purpose.label_fi,'Erilliset pientalot')
        self.assertIsNone(result.source_observed_at)
        self.assertEqual(result.timestamp_status,'unknown')
        self.assertEqual(result.sources.ryhti_building.timestamp_status,'known')
        self.assertEqual(result.sources.ryhti_address.timestamp_status,'known')
        self.assertIsNotNone(result.sources.certificate.imported_at)
        self.assertIsNotNone(result.resolved_at.tzinfo)
        self.insert_main([self.main_row(completion_date='2026-02-30',property_identifier='00000000000000')],replace=True)
        with SessionLocal() as session:
            session.execute(text("UPDATE buildings SET modified_timestamp_utc='not-a-date'"));session.commit()
        result=self.resolve().value
        self.assertIsNone(result.details.construction_year)
        self.assertIsNone(result.details.completion_date)
        self.assertEqual(result.field_provenance['details.completion_date'].status,'invalid')
        self.assertIsNone(result.identity.property_identifier)
        self.assertEqual(result.sources.ryhti_building.timestamp_status,'invalid')
        self.assertIsNone(result.source_observed_at)

    def test_geometry_point_missing_invalid_empty_and_oversized(self):
        cases=[({'polygon':None,'point':'POINT(24 60)'},'available','main.point'),
               ({'polygon':None,'point':None},'missing',None),
               ({'polygon':'MULTIPOLYGON EMPTY','point':None},'invalid','main.polygon'),
               ({'polygon':'MULTIPOLYGON(((24 60,25 61,24 61,25 60,24 60)))','point':None},'invalid','main.polygon')]
        for geometry,status,source in cases:
            self.insert_main([self.main_row(**geometry)],replace=True)
            base=self.resolve().value
            self.assertEqual(base.selection.geometry_status.status,status)
            self.assertEqual(base.selection.geometry_source,source)
            if status!='available':self.assertIsNone(base.geometry)
        self.insert_main(self.rows,replace=True)
        # Same production branch with a small bound: exercise availability
        # without generating a multi-megabyte geometry just for the assertion.
        with patch('src.building_resolver.GEOMETRY_BYTES',10):
            base=self.resolve().value
        self.assertIsNone(base.geometry)
        self.assertEqual(base.selection.geometry_status.reason,'source_value_too_large')

    def test_real_one_mebibyte_geometry_limit(self):
        with SessionLocal() as session:
            session.execute(text("UPDATE main SET polygon=ST_Multi(ST_Transform(ST_Buffer(ST_SetSRID(ST_MakePoint(24,60),4326),0.01,10000),3857))"))
            size=session.execute(text('SELECT max(octet_length(ST_AsGeoJSON(polygon,17,0))) FROM main')).scalar_one()
            session.commit()
        self.assertGreater(size,GEOMETRY_BYTES)
        base=self.resolve().value
        self.assertIsNone(base.geometry)
        self.assertEqual(base.selection.geometry_status.reason,'source_value_too_large')
        with SessionLocal() as session:
            summary=resolve_building_summaries(session,[KEY])[KEY].value
        self.assertIsNone(summary.location)

    def test_invalid_geometry_does_not_hide_valid_point_and_source_text_bound(self):
        self.insert_main([self.main_row(polygon='MULTIPOLYGON EMPTY',point='POINT(24 60)',address_fin='X'*16385)],replace=True)
        base=self.resolve().value
        self.assertEqual(base.geometry.source,'main.point')
        self.assertIsNone(base.details.address_fin)
        self.assertEqual(base.field_provenance['details.address_fin'].reason,'source_value_too_large')
        self.certificate('1',recommendations_fi='X'*16385,ventilation_type_id='unknown')
        base=self.resolve().value
        self.assertIsNone(base.certificate.recommendations_fi)
        self.assertEqual(base.certificate.field_availability['recommendations_fi'].reason,'source_value_too_large')
        self.assertEqual(base.certificate.ventilation_type_id,'unknown')
        self.assertEqual(base.certificate.field_availability['ventilation_type_id'].status,'unsupported')

    def test_optional_columns_and_sources_degrade_without_mass_assignment(self):
        with SessionLocal() as session:
            for column in SCENARIO_COLUMNS:
                session.execute(text(f'ALTER TABLE main ADD COLUMN "{column}" double precision'))
            session.execute(text('UPDATE main SET distr_default_heat=0,awhp_aahp_heat=0'))
            session.commit()
        base=self.resolve().value
        self.assertEqual(base.model.consumption_scenarios['distr']['default']['heat'].value,0)
        self.assertEqual(base.model.consumption_scenarios['distr']['default']['heat'].status,'available')
        self.assertEqual(base.model.consumption_scenarios['awhp']['aahp']['heat'].status,'unsupported')
        with SessionLocal() as session:
            session.execute(text('ALTER TABLE energy_certificates RENAME TO fixture_hidden_certificates'))
            session.execute(text('TRUNCATE code_maps'))
            session.execute(text("UPDATE main SET energy_certificate_class='A', energy_certificate_valid_until='2020-01-01'"));session.commit()
        try:
            base=self.resolve().value
            self.assertEqual(base.sources.certificate.status,'source_unavailable')
            self.assertEqual(base.sources.code_catalog.status,'source_unavailable')
            self.assertEqual(base.details.address_fin,'A street 1')
            self.assertEqual(base.published_certificate.energy_class,'A')
            self.assertEqual(str(base.published_certificate.valid_until),'2020-01-01')
            self.assertIsNone(base.certificate)
        finally:
            with SessionLocal() as session:
                session.execute(text('ALTER TABLE fixture_hidden_certificates RENAME TO energy_certificates'));session.commit()

    def test_missing_scenario_columns_not_projected_and_nonfinite_source_values(self):
        base=self.resolve().value
        self.assertEqual(base.model.consumption_scenarios['distr']['default']['heat'].status,'not_projected')
        for value in ('NaN','Infinity','-1'):
            self.insert_main([self.main_row(floor_area=value)],replace=True)
            base=self.resolve().value
            self.assertIsNone(base.details.floor_area)
            self.assertEqual(base.field_provenance['details.floor_area'].status,'invalid')

    def test_summary_mixed_batch_100_keys_constant_query_budget_and_explain(self):
        self.insert_main([self.main_row(building_key='conflict',permanent_building_identifier='c1'),
                          self.main_row(building_key='conflict',permanent_building_identifier='c2')])
        keys=[KEY,'conflict']+['missing'+str(i) for i in range(98)]
        with self.read_only_audit() as queries:
            with SessionLocal() as session:outcomes=resolve_building_summaries(session,keys+[KEY])
        self.assertEqual(len(outcomes),100)
        self.assertEqual(outcomes[KEY].status,'available')
        self.assertEqual(outcomes['conflict'].status,'ambiguous')
        self.assertEqual(outcomes['missing0'].status,'missing')
        selects=[q for q in queries if q[0].lstrip().upper().startswith(('SELECT','WITH'))]
        self.assertLessEqual(len(selects),7)
        self.assertFalse(any('energy_certificates WHERE' in sql or 'source_row' in sql for sql,_ in selects))
        plans=[]
        with ENGINE_LOCAL.connect() as connection:
            for sql,params in selects:
                if 'FROM main WHERE building_key' in sql or 'WITH filtered AS MATERIALIZED' in sql and 'ST_AsEWKB' in sql:
                    plans.append('\n'.join(row[0] for row in connection.exec_driver_sql('EXPLAIN '+sql,params)))
            indexes=connection.execute(text("SELECT indexdef FROM pg_indexes WHERE tablename='main'")).scalars().all()
        self.assertFalse(any('building_key' in item for item in indexes))
        self.assertTrue(any('Seq Scan on main' in plan for plan in plans))
        Path('.codex-orch/features/F001.1-building-contract/query-evidence.json').write_text(json.dumps({'summary_source_queries':len(selects),'batch_distinct_keys':100,'fixture_only':True,'plans':plans,'main_key_index_present':False},indent=2))

    def test_required_relation_failure_is_503_never_missing(self):
        with SessionLocal() as session:
            session.execute(text('ALTER TABLE main RENAME TO fixture_hidden_main'));session.commit()
        try:
            with self.assertRaises(BuildingSourceUnavailable):self.resolve()
            response=self.client.get('/energy-data/buildings/'+KEY)
            self.assertEqual(response.status_code,503)
            self.assertEqual(response.json()['detail']['code'],'building_source_unavailable')
            self.assertNotIn('fixture_hidden',response.text)
        finally:
            with SessionLocal() as session:
                session.execute(text('ALTER TABLE fixture_hidden_main RENAME TO main'));session.commit()

    def test_concurrent_source_rebuild_lock_is_sanitized_503(self):
        with SessionLocal() as blocker:
            blocker.execute(text('LOCK TABLE main IN ACCESS EXCLUSIVE MODE'))
            # A fixture source-rebuild lock must not hang browsing or become a
            # false per-building 404. The resolver's local lock timeout applies.
            response=self.client.get('/energy-data/buildings/'+KEY)
            self.assertEqual(response.status_code,503)
            self.assertEqual(response.json()['detail']['code'],'building_source_unavailable')
        self.assertEqual(self.client.get('/energy-data/buildings/'+KEY).status_code,200)

    def test_controlled_refresh_is_one_coherent_snapshot(self):
        with SessionLocal() as reader:
            old=resolve_building(reader,KEY).value
            with SessionLocal() as writer:
                writer.execute(text('DELETE FROM main WHERE building_key=:key'),{'key':KEY})
                row=self.main_row(floor_area='200')
                attrs=[k for k in row if k not in ('point','polygon')]
                writer.execute(text(f"INSERT INTO main({','.join(attrs)}) VALUES ({','.join(':'+k for k in attrs)})"),row)
                writer.execute(text('UPDATE buildings SET apartment_count=10 WHERE building_key=:key'),{'key':KEY})
                writer.commit()
            same=resolve_building(reader,KEY).value
            self.assertEqual((same.details.floor_area,same.details.apartment_count),(old.details.floor_area,old.details.apartment_count))
        latest=self.resolve().value
        self.assertEqual((latest.details.floor_area,latest.details.apartment_count),(200,10))

    def test_native_building_tiles_normal_zoom_postgis_and_low_zoom(self):
        self.assertEqual(self.client.get('/energy-data/tiles/10/0/0.pbf').content,b'')
        with SessionLocal() as session:
            xy=session.execute(text('''SELECT floor((ST_X(ST_Transform(ST_SetSRID(ST_MakePoint(24,60),4326),3857))+20037508.342789244)/40075016.685578488*4096)::int AS x,
                floor((20037508.342789244-ST_Y(ST_Transform(ST_SetSRID(ST_MakePoint(24,60),4326),3857)))/40075016.685578488*4096)::int AS y''')).mappings().one()
        with self.read_only_audit():
            response=self.client.get(f"/energy-data/tiles/12/{xy['x']}/{xy['y']}.pbf")
        self.assertEqual(response.status_code,200)
        self.assertTrue(decode_mvt(response.content)['layer_polygons'])
