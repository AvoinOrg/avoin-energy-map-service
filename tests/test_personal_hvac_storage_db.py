"""Three guarded F004.3 pairs: v1/v2/v3 archive -> NEW v3 target, real PostgreSQL."""
import json
import os
import unittest
from unittest.mock import patch

from src.api.auth import AuthenticatedPrincipal
from src.api.building_examples import public_example
from src.building_contract import CanonicalBuildingV1, PersonalBuildingPutV1
from tests.personal_storage_fixture import archive_document
from src.db.state import state_session, state_ready
from src import user_buildings as owner, building_shares as shares
from src.state_storage import operations, recovery
from src.state_storage.catalog import HISTORICAL_VERSION, PROPERTY_VERSION, HVAC_VERSION as VERSION, VERSION as CURRENT_VERSION, audit_schema, audit_acl, rows
from src.state_storage.config import load_target
from src.state_storage.operations import connected, apply, load_bundle, upgrade_personal_v3
from tests.test_state_storage_db import guarded, runtime_environment
from tests.test_personal_hvac import full_systems, systems

PAIRS='ENERGY_MAP_HVAC_STORAGE_PAIRS'


@unittest.skipUnless(os.getenv(PAIRS),'requires three fresh doctor-owned F004.3 descriptor pairs')
class HvacStorageDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pairs=json.loads(os.environ[PAIRS])
        if len(pairs)!=3: raise RuntimeError('Three exact F004.3 pairs required')
        cls.pairs=[]
        for pair in pairs:
            if len(pair)!=2: raise RuntimeError('Invalid pair')
            with patch.dict(os.environ,{'ENERGY_MAP_STATE_TEST_CONFIG':pair[0], 'ENERGY_MAP_STATE_RESTORE_CONFIG':pair[1]}):
                target,restore=guarded()
            if any('f0043_hvac' not in t['database'] for t in (target,restore)):
                raise RuntimeError('Fresh F004.3 fixtures required')
            cls.pairs.append((target,restore))
        if len({t['database'] for pair in cls.pairs for t in pair})!=6: raise RuntimeError('Distinct targets required')

    def exercise(self,index,profile):
        target,restore=self.pairs[index]
        with connected(target,database=target['source_database'],read_only=True) as source:
            source_before=rows(source,'SELECT to_jsonb(t) FROM public.source_probe t ORDER BY probe_id')
        upgrade=operations.command.upgrade
        with patch('src.state_storage.operations.command.upgrade',side_effect=lambda c,r:upgrade(c,profile)):
            self.assertEqual(apply(target)['version'],profile)
        bundle=load_bundle(target)
        base=CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
        principal=AuthenticatedPrincipal('https://synthetic.invalid','hvac-owner')
        token=None
        with runtime_environment(target,bundle):
            self.assertEqual(state_ready(3),profile==VERSION)
            models=[PersonalBuildingPutV1()]
            if profile!=HISTORICAL_VERSION:
                models.append(archive_document({'schema_version':2,
                    'scenario_inputs':{'apartment_studio_count':0,'apartment_two_room_count':None}}))
            if profile==VERSION:
                models.append(archive_document({'schema_version':3,
                    'overrides':{'ventilation_type_id':'0','total_area':None},
                    'scenario_inputs':{'apartment_studio_count':0,'systems':full_systems()}}))
            with state_session() as session:
                for i,value in enumerate(models):
                    key='hvac-synthetic-'+str(i)
                    row=owner.create_owned(session,principal,key,value,base.model_copy(update={'building_key':key}))
                    token=shares.rotate(session,row)
                session.commit()
        # Full row/share fingerprints must survive the additive schema upgrade.
        with connected(target,database=target['database'],read_only=True) as connection:
            before=recovery.data_fingerprint(connection);audit_schema(connection,profile);audit_acl(connection,target)
        self.assertEqual(recovery.backup(target)['version'],profile)
        if profile!=VERSION:
            self.assertEqual(upgrade_personal_v3(target)['version'],VERSION)
            with connected(target,database=target['database'],read_only=True) as connection:
                after=recovery.data_fingerprint(connection)
                self.assertEqual({k:v for k,v in before.items() if k!='alembic_version'},
                                 {k:v for k,v in after.items() if k!='alembic_version'})
        # A committed metadata-upgrade interruption cannot return ready early,
        # replay the archive or lose grants on retry.
        actual=recovery.migrate_personal_v5
        def interrupted(t,b):
            actual(t,b)
            raise RuntimeError('synthetic_post_upgrade_commit')
        if profile!=VERSION:
            with patch('src.state_storage.recovery.migrate_personal_v5',side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError,'synthetic_post_upgrade_commit'): recovery.restore(restore)
            with connected(restore,database=restore['database'],read_only=True) as connection:
                audit_schema(connection,CURRENT_VERSION)
                self.assertFalse(recovery.scalar(connection,'SELECT rolcanlogin FROM pg_roles WHERE rolname=%s',(restore['writer'],)))
        self.assertEqual(recovery.restore(restore)['status'],'restore_verified')
        with connected(restore,database=restore['database'],read_only=True) as connection:
            audit_schema(connection,CURRENT_VERSION);audit_acl(connection,restore)
            actual_rows=recovery.data_fingerprint(connection)
            self.assertEqual({k:v for k,v in actual_rows.items() if k!='alembic_version'},
                             {k:v for k,v in before.items() if k!='alembic_version'})
        with runtime_environment(restore,load_bundle(restore)):
            self.assertTrue(state_ready(3))
            with state_session(read_only=True) as session:
                row=shares.lookup(session,token)
                self.assertEqual(row.schema_version,len(models))
                if profile==VERSION:
                    self.assertEqual(row.scenario_inputs['systems'], full_systems())
                    self.assertEqual(row.scenario_inputs['apartment_studio_count'],0)
            with state_session() as session:
                row=shares.lookup(session,token);rotated=shares.rotate(session,row);session.commit()
            with state_session() as session:
                with self.assertRaises(shares.ShareNotFound): shares.lookup(session,token)
                row=shares.lookup(session,rotated);shares.revoke(session,row);session.commit()
            with state_session() as session:
                row=owner.load_owned(session,principal,'hvac-synthetic-'+str(len(models)-1),lock=True)
                rotated=shares.rotate(session,row);session.commit()
            with state_session() as session:
                row=shares.lookup(session,rotated);session.delete(row);session.commit()
            with state_session(read_only=True) as session:
                with self.assertRaises(shares.ShareNotFound): shares.lookup(session,rotated)
        with connected(target,database=target['source_database'],read_only=True) as source:
            self.assertEqual(source_before,rows(source,'SELECT to_jsonb(t) FROM public.source_probe t ORDER BY probe_id'))

    def test_00_v1_archive_and_post_upgrade_retry(self): self.exercise(0,HISTORICAL_VERSION)
    def test_01_v2_archive_and_post_upgrade_retry(self): self.exercise(1,PROPERTY_VERSION)
    def test_02_mixed_v3_archive_and_capability_lifecycle(self): self.exercise(2,VERSION)
