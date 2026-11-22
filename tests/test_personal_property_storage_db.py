"""Isolated old-schema upgrade and mixed archive recovery on guarded NEW targets."""
from datetime import datetime, timezone
import json
import os
from unittest.mock import patch
import unittest

from sqlalchemy import text

from src.api.auth import AuthenticatedPrincipal
from src.api.building_examples import public_example
from src.building_contract import CanonicalBuildingV1, PersonalBuildingPutV1
from tests.personal_storage_fixture import archive_document
from src.db.state import state_ready, state_session, StateSchemaUpgradeRequired, require_personal_schema
from src import user_buildings as owner, building_shares as shares
from src.state_storage import operations, recovery
from src.state_storage.catalog import PROPERTY_VERSION as VERSION, VERSION as CURRENT_VERSION, HISTORICAL_VERSION, audit_schema, audit_acl, rows, scalar
from src.state_storage.operations import apply, connected, load_bundle, verify, upgrade_personal_v2
from src.state_storage.private import StorageError
from tests.test_state_storage_db import guarded, runtime_environment


@unittest.skipUnless(os.getenv('ENERGY_MAP_STATE_TEST_CONFIG'), 'requires guarded disposable state descriptors')
class PropertyStorageDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.target,cls.restore=guarded()
        # The doctor gave this child a fresh fixed pair, never a shared target.
        if 'f0042_property' not in cls.target['database']:
            raise RuntimeError('F004.2 fresh fixture required')

    def test_00_preexisting_v1_share_upgrade_exact_profiles_and_grants(self):
        t=self.target
        actual_upgrade=operations.command.upgrade
        def historical(config, revision):return actual_upgrade(config,HISTORICAL_VERSION)
        with patch('src.state_storage.operations.command.upgrade', side_effect=historical):
            result=apply(t)
        self.assertEqual(result['version'],HISTORICAL_VERSION)
        bundle=load_bundle(t)
        base=CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
        principal=AuthenticatedPrincipal('https://fixture.example.invalid','property-owner')
        with runtime_environment(t,bundle):
            self.assertFalse(state_ready());self.assertFalse(state_ready(2))
            with state_session() as session:
                with self.assertRaises(StateSchemaUpgradeRequired):require_personal_schema(session,2)
                row=owner.create_owned(session,principal,base.building_key,PersonalBuildingPutV1.model_validate({
                    'display_name':'Old row','overrides':{'main_purpose':'historical','total_area':None},
                    'scenario_inputs':{'resident_count':2}}),base)
                token=shares.rotate(session,row)
                type(self).active_token=token
                session.commit()
            # A real separate writer session reloads the saved/share row.
            with state_session(read_only=True) as session:
                row=shares.lookup(session,token)
                self.assertEqual(row.schema_version,1)
                self.assertEqual(row.scenario_inputs['resident_count'],2)
        with connected(t,database=t['database'],read_only=True) as connection:
            before=recovery.data_fingerprint(connection);audit_schema(connection,HISTORICAL_VERSION)
        self.assertEqual(upgrade_personal_v2(t)['version'],VERSION)
        with connected(t,database=t['database'],read_only=True) as connection:
            after=recovery.data_fingerprint(connection);audit_schema(connection,VERSION);audit_acl(connection,t)
        self.assertEqual({k:v for k,v in before.items() if k!='alembic_version'},
                         {k:v for k,v in after.items() if k!='alembic_version'})
        with runtime_environment(t,bundle):
            self.assertTrue(state_ready(2))
            with state_session() as session:
                row=shares.lookup(session,token)
                saved_id, created_at, revision = row.saved_id, row.created_at, row.revision
                value = archive_document({'schema_version': 2, 'display_name': row.display_name,
                    'overrides': row.overrides, 'scenario_inputs': {**row.scenario_inputs,
                        'apartment_studio_count': 0, 'apartment_two_room_count': None,
                        'apartment_three_room_count': 1, 'apartment_four_plus_room_count': 2,
                        'personal_energy_class': {'grade': 'B', 'scale': 2018},
                        'personal_energy_class_date': {'precision': 'year', 'year': 2020},
                        'annual_maintenance_charge_eur_cents': 123456}})
                owner.bump_revision(row);owner.store_document(row,value,None);session.commit()
            with state_session(read_only=True) as session:
                row=shares.lookup(session,token)
                self.assertEqual((row.saved_id,row.created_at,row.revision),(saved_id,created_at,revision+1))
                self.assertEqual(row.scenario_inputs['apartment_studio_count'],0)
                self.assertEqual(row.scenario_inputs['resident_count'],2)
            # A mixed-format collection/backup includes a separate untouched v1 row.
            with state_session() as session:
                second=base.model_copy(update={'building_key':'property-mixed-v1'})
                owner.create_owned(session,principal,second.building_key,PersonalBuildingPutV1(),second)
                third=base.model_copy(update={'building_key':'property-count-only-v2'})
                owner.create_owned(session,principal,third.building_key,archive_document({
                    'schema_version':2,'scenario_inputs':{'apartment_three_room_count':0}}),third)
                session.commit()
        self.assertEqual(verify(t)['version'],VERSION)

    def test_01_latest_mixed_archive_new_target_restore_and_capability_lifecycle(self):
        t=self.target;r=self.restore
        self.assertEqual(recovery.backup(t)['version'],VERSION)
        self.assertEqual(recovery.restore(r)['status'],'restore_verified')
        with connected(t,database=t['database'],read_only=True) as source,connected(r,database=r['database'],read_only=True) as recovered:
            fingerprint=recovery.data_fingerprint(source)
            restored=recovery.data_fingerprint(recovered)
            self.assertEqual({k:v for k,v in fingerprint.items() if k!='alembic_version'},
                             {k:v for k,v in restored.items() if k!='alembic_version'})
            self.assertEqual(rows(recovered,'SELECT schema_version FROM energymap_state.energymap_user_buildings ORDER BY schema_version'),[(1,),(2,),(2,)])
            audit_schema(recovered,CURRENT_VERSION);audit_acl(recovered,r)
        bundle=load_bundle(r)
        with runtime_environment(r,bundle):
            self.assertTrue(state_ready(2))
            with state_session(read_only=True) as session:
                restored=shares.lookup(session,type(self).active_token)
                values = restored.scenario_inputs
                self.assertEqual(values['apartment_studio_count'], 0)
                self.assertEqual(values['personal_energy_class'], {'grade': 'B', 'scale': 2018})
                self.assertEqual(values['personal_energy_class_date']['year'], 2020)
                self.assertEqual(values['annual_maintenance_charge_eur_cents'], 123456)
            with state_session() as session:
                principal=AuthenticatedPrincipal('https://fixture.example.invalid','property-owner')
                row=owner.load_owned(session,principal,public_example()['building_key'],lock=True)
                token=shares.rotate(session,row);session.commit()
            with state_session() as session:
                row=shares.lookup(session,token)
                self.assertEqual(row.scenario_inputs['apartment_studio_count'],0)
                shares.revoke(session,row);session.commit()
            with state_session() as session:
                with self.assertRaises(shares.ShareNotFound):shares.lookup(session,token)
                row=owner.load_owned(session,principal,public_example()['building_key'],lock=True)
                token=shares.rotate(session,row);session.commit()
            with state_session() as session:
                row=shares.lookup(session,token);session.delete(row);session.commit()
            with state_session(read_only=True) as session:
                with self.assertRaises(shares.ShareNotFound):shares.lookup(session,token)
        with connected(r,database=r['database']) as connection:
            connection.autocommit=False
            rows(connection,'ALTER TABLE energymap_state.energymap_user_buildings DROP CONSTRAINT user_buildings_version')
            rows(connection,'ALTER TABLE energymap_state.energymap_user_buildings ADD CONSTRAINT user_buildings_version CHECK (schema_version IN (1,2,3))')
            with self.assertRaises(StorageError):audit_schema(connection)
            connection.rollback()
