"""Real digest capability/transaction acceptance on the exact owner fixture."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import secrets
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from src.api.auth import AuthenticatedPrincipal, get_introspector
from src.api.main import app
from src import building_shares as shares, user_buildings as owner
from src.building_contract import personal_document
from src.db.state import state_engine, state_ready, state_session, StateUnavailable
from src.db.user_building_models import UserBuildingShare as Share
from tests.test_user_buildings_db import OwnerDatabaseTests as Fixture, KEY, PATH, TABLE, guarded_urls
from tests.test_auth import claims, settings, ISSUER
from src.api.auth import ZitadelIntrospector

SHARES = 'energymap_state.energymap_user_building_shares'


@unittest.skipUnless(os.getenv('RUN_BUILDING_SHARES_DB_TESTS') == '1', 'requires exact disposable owner/share PostGIS fixture')
class ShareDatabaseTests(unittest.TestCase):
    # Reuse fixture guard/source/auth seams without inheriting all owner tests.
    setUpClass = classmethod(Fixture.setUpClass.__func__)
    setUp = Fixture.setUp
    source_dependency = Fixture.source_dependency
    provider = Fixture.provider
    merge = Fixture.merge
    auth = Fixture.auth
    create = Fixture.create
    save = Fixture.save
    read = Fixture.read
    count = Fixture.count
    checksum = Fixture.checksum
    stored = Fixture.stored

    def share(self, tag, subject='A', path=PATH):
        return self.client.post(path+'/share', headers=self.auth(subject, **{'If-Match':tag}))

    def enabled(self):
        with self.Admin() as session:
            return [{**dict(r), 'token_digest': bytes(r.token_digest)} for r in session.execute(text(f'SELECT * FROM {SHARES} ORDER BY saved_id')).mappings()]

    def resolve(self, created):
        return self.client.get(created.json()['resolver_path'])

    def assert_status(self, result, status):
        self.assertEqual(result.status_code,status,result.text)
        self.assertEqual(result.headers['cache-control'],'no-store')

    def test_lifecycle_latest_values_flags_null_reset_and_204_validators(self):
        saved=self.save({'display_name':'Visible name','overrides':{'address_fin':'Personal street','floor_area':0,'total_area':None},'scenario_inputs':{'resident_count':2}})
        self.assertFalse(saved.json()['share_enabled'])
        created=self.client.post(PATH+'/share',headers=self.auth(**{'If-Match':saved.headers['etag'],
            'Host':'untrusted.invalid','Forwarded':'host=evil.invalid;proto=https'}))
        self.assert_status(created,201)
        body=created.json();token=body['share_token']
        self.assertEqual(len(token),43)
        self.assertEqual(body['resolver_path'],shares.RESOLVER_PREFIX+token)
        self.assertEqual(body['revision'],2)
        self.assertEqual(self.enabled()[0]['token_digest'],shares.digest(token))
        self.assertEqual(set(self.enabled()[0]),{'saved_id','token_digest'})
        current=self.read();self.assertEqual(created.headers['etag'],current.headers['etag'])
        self.assertTrue(current.json()['share_enabled'])
        listing=self.client.get('/energy-data/user/buildings',headers=self.auth()).json()
        self.assertTrue(listing['items'][0]['share_enabled'])
        self.assertNotIn(token,json.dumps(listing))
        public=self.resolve(created);self.assert_status(public,200)
        self.assertNotIn('etag',public.headers)
        conditional=self.client.get(body['resolver_path'],headers={'If-None-Match':'*'})
        self.assert_status(conditional,200);self.assertNotIn('etag',conditional.headers)
        self.assertEqual(public.json()['effective']['details']['floor_area'],0)
        self.assertEqual(public.json()['effective']['details']['address_fin'],'Personal street')
        self.assertEqual(public.json()['field_provenance']['details.total_area']['status'],'user_unknown')
        for private in ('saved_id','owner_issuer','owner_subject','address_key','share_enabled','token_digest',token,str(saved.json()['saved_id'])):
            self.assertNotIn(private,public.text)
        updated=self.client.patch(PATH,json={'set':{'overrides':{'floor_area':175}},'unset':['overrides.total_area','scenario_inputs.resident_count']},headers=self.auth(**{'If-Match':created.headers['etag']}))
        self.assert_status(updated,200);self.assertTrue(updated.json()['share_enabled'])
        public=self.resolve(created).json()
        self.assertEqual(public['effective']['details']['floor_area'],175)
        self.assertEqual(public['field_provenance']['details.total_area']['origin'],'ryhti')
        reset=self.client.put(PATH,json={},headers=self.auth(**{'If-Match':updated.headers['etag']}))
        self.assert_status(reset,200);self.assertTrue(reset.json()['share_enabled'])
        self.assertEqual(self.resolve(created).json()['overrides'],{})
        revoked=self.client.delete(PATH+'/share',headers=self.auth(**{'If-Match':reset.headers['etag'],'Origin':'http://127.0.0.1:8080'}))
        self.assert_status(revoked,204);self.assertEqual(revoked.content,b'')
        self.assertEqual(revoked.headers['access-control-expose-headers'],'ETag')
        self.assertEqual(revoked.headers['access-control-allow-origin'],'http://127.0.0.1:8080')
        self.assertEqual(revoked.headers['etag'],self.read().headers['etag'])
        self.assertFalse(self.read().json()['share_enabled'])
        self.assert_status(self.resolve(created),404)
        again=self.client.delete(PATH+'/share',headers=self.auth(**{'If-Match':revoked.headers['etag']}))
        self.assert_status(again,204)
        self.assertEqual(self.read().json()['revision'],reset.json()['revision']+2)
        stale=self.client.delete(PATH+'/share',headers=self.auth(**{'If-Match':revoked.headers['etag']}))
        self.assert_status(stale,412)
        edited=self.client.patch(PATH,json={'display_name':'Next'},headers=self.auth(**{'If-Match':again.headers['etag']}))
        self.assert_status(edited,200)
        self.assertEqual(self.count(),1)

    def test_rotation_uniform_unknown_revoke_delete_and_recreate(self):
        saved=self.save();first=self.share(saved.headers['etag'])
        second=self.share(first.headers['etag']);self.assert_status(second,201)
        self.assertNotEqual(first.json()['share_token'],second.json()['share_token'])
        missing=self.client.get(shares.RESOLVER_PREFIX+secrets.token_urlsafe(32))
        expected=(missing.status_code,missing.content,missing.headers['content-type'],missing.headers['cache-control'])
        def uniform(r):self.assertEqual((r.status_code,r.content,r.headers['content-type'],r.headers['cache-control']),expected)
        uniform(self.resolve(first))
        for spelling in ('bad','x'*2000,'bad/path','%FF','%2F',''):
            uniform(self.client.get(shares.RESOLVER_PREFIX+spelling))
        self.assert_status(self.resolve(second),200)
        deleted=self.client.delete(PATH,headers=self.auth(**{'If-Match':second.headers['etag']}))
        self.assert_status(deleted,204)
        self.assertTrue(deleted.headers['etag'].endswith(':4"'))
        self.assertEqual(self.enabled(),[]);uniform(self.resolve(second))
        new=self.save();self.assertNotEqual(new.json()['saved_id'],saved.json()['saved_id'])
        self.assert_status(self.share(deleted.headers['etag']),412)
        uniform(self.resolve(second))

    def test_ownership_preconditions_forgery_and_provider_outage(self):
        saved=self.save();created=self.share(saved.headers['etag']);snapshot=(self.stored(),self.enabled())
        for method in ('post','delete'):
            url=PATH+'/share'
            for subject in ('B',):
                r=self.client.request(method,url,headers=self.auth(subject));self.assert_status(r,404)
            self.issuer='https://other-issuer.example'
            self.assert_status(self.client.request(method,url,headers=self.auth()),404)
            self.issuer=ISSUER
            self.assert_status(self.client.request(method,url,headers=self.auth()),428)
            self.assert_status(self.client.request(method,url,headers=self.auth(**{'If-Match':saved.headers['etag']})),412)
            self.assert_status(self.client.request(method,url,headers=self.auth(**{'If-None-Match':'*'})),412)
            self.assert_status(self.client.request(method,url,headers=self.auth(**{'If-Match':'*'})),400)
            self.assert_status(self.client.request(method,url),401)
        self.assert_status(self.client.post(PATH+'/share',json={'owner_subject':'forged'},headers=self.auth(**{'If-Match':created.headers['etag']})),422)
        self.assert_status(self.client.post(PATH+'/share?owner=A',headers=self.auth(**{'If-Match':created.headers['etag']})),422)
        # Token is a capability, never an identity. Real dependency sees inactive.
        self.provider_reply=lambda request:httpx.Response(200,json={'active':False})
        self.assert_status(self.client.patch(PATH,json={},headers={'Authorization':'Bearer '+created.json()['share_token'],'If-Match':created.headers['etag']}),401)
        self.provider_reply=lambda request:httpx.Response(503)
        self.assert_status(self.share(created.headers['etag']),503)
        for body in ({**claims(),'active':False},{**claims(),'exp':1000},{**claims(),'aud':['wrong']}):
            self.provider_reply=lambda request,b=body:httpx.Response(200,json=b)
            self.assert_status(self.share(created.headers['etag']),401)
        original_dependency=app.dependency_overrides[get_introspector]
        app.dependency_overrides[get_introspector]=lambda:ZitadelIntrospector(
            settings(required_scopes=frozenset({'fixture-required-permission'})),
            transport=httpx.MockTransport(lambda r:httpx.Response(200,json=claims())),clock=lambda:1000)
        self.assert_status(self.share(created.headers['etag']),403)
        app.dependency_overrides[get_introspector]=original_dependency
        self.assertEqual((self.stored(),self.enabled()),snapshot)
        # Valid shares ignore even invalid supplied credentials and missing config.
        def provider_must_not_run():raise AssertionError('identity provider used by public read')
        app.dependency_overrides[get_introspector]=provider_must_not_run
        for headers in ({},{'Authorization':'Bearer invalid'}):
            self.assert_status(self.client.get(created.json()['resolver_path'],headers=headers),200)
        self.assertEqual(self.client.get('/energy-data/buildings/'+KEY).status_code,200)
        self.assertEqual(self.client.get('/healthz').status_code,200)
        self.assertEqual((self.stored(),self.enabled()),snapshot)

    def test_encoded_terminal_suffix_with_both_records_present(self):
        # Source keys are not aliases. Both competing records actually exist.
        with self.Admin.begin() as session:
            for key in ('a','a/share'):
                session.execute(text('INSERT INTO buildings(building_key) VALUES (:key)'),{'key':key})
        root='/energy-data/user/buildings/'
        a=self.create(key='a');b=self.create(key='a%2Fshare')
        self.assert_status(a,201);self.assert_status(b,201)
        sa=self.share(a.headers['etag'],path=root+'a');sb=self.share(b.headers['etag'],path=root+'a%2Fshare')
        self.assert_status(sa,201);self.assert_status(sb,201)
        r=self.client.delete(root+'a/share',headers=self.auth(**{'If-Match':sa.headers['etag']}))
        self.assert_status(r,204);self.assert_status(self.resolve(sa),404);self.assert_status(self.resolve(sb),200)
        self.assert_status(self.client.delete(root+'a%2Fshare',headers=self.auth(**{'If-Match':sb.headers['etag']})),204)
        self.assert_status(self.resolve(sb),404)
        self.assertEqual(self.client.get(root+'a',headers=self.auth()).status_code,200)

    def test_transaction_failure_generation_constraint_flush_and_commit_preserve_old_link(self):
        saved=self.save();created=self.share(saved.headers['etag']);old=(self.stored(),self.enabled())
        def broken_commit(session):raise OperationalError('synthetic',{},Exception('private'))
        failures=[patch('src.building_shares.secrets.token_urlsafe',side_effect=RuntimeError('generation')),
                  patch('src.api.user_buildings.ShareCreatedV1',side_effect=RuntimeError('projection')),
                  patch.object(Session,'commit',broken_commit)]
        for failure in failures:
            with failure:r=self.share(created.headers['etag'])
            self.assertIn(r.status_code,(500,503));self.assertEqual(r.headers['cache-control'],'no-store')
            self.assertEqual((self.stored(),self.enabled()),old)
            self.assert_status(self.resolve(created),200)
            self.assertNotIn(created.json()['share_token'],r.text)
        # Real digest uniqueness failure after old digest deletion and revision bump.
        other=self.create(subject='B');collision=self.share(other.headers['etag'],subject='B')
        with patch('src.building_shares.secrets.token_urlsafe',return_value=collision.json()['share_token']):
            self.assert_status(self.share(created.headers['etag']),503)
        self.assertEqual(self.stored()[0]['revision']+self.stored()[1]['revision'],4)
        self.assert_status(self.resolve(created),200);self.assert_status(self.resolve(collision),200)
        # A failure after revoke DML rolls back the delete too.
        original=shares.revoke
        def fail_after(*args):original(*args);raise RuntimeError('after delete')
        with patch('src.api.user_buildings.shares.revoke',fail_after):
            self.assert_status(self.client.delete(PATH+'/share',headers=self.auth(**{'If-Match':created.headers['etag']})),500)
        self.assert_status(self.resolve(created),200)
        with patch.object(Session,'commit',broken_commit):
            self.assert_status(self.client.delete(PATH,headers=self.auth(**{'If-Match':created.headers['etag']})),503)
        self.assert_status(self.resolve(created),200)

    def test_concurrent_owner_edit_rotate_revoke_and_delete(self):
        saved=self.save();current=self.share(saved.headers['etag'])
        def request(kind,tag):
            if kind=='post':return self.share(tag)
            if kind=='patch':return self.client.patch(PATH,json={'display_name':'race'},headers=self.auth(**{'If-Match':tag}))
            return self.client.delete(PATH+('/share' if kind=='revoke' else ''),headers=self.auth(**{'If-Match':tag}))
        for left,right in (('post','post'),('patch','post'),('patch','revoke'),('post','revoke'),('delete','post')):
            current_read=self.read();tag=current_read.headers['etag'];barrier=threading.Barrier(2)
            original=owner.locked_owned
            def synchronized(*args,**kwargs):barrier.wait(timeout=10);return original(*args,**kwargs)
            with patch('src.api.user_buildings.service.locked_owned',synchronized),ThreadPoolExecutor(2) as pool:
                results=list(pool.map(lambda kind:request(kind,tag),(left,right)))
            successes=[r for r in results if r.status_code in (200,201,204)]
            self.assertEqual(len(successes),1,[r.text for r in results])
            self.assertIn(next(r for r in results if r not in successes).status_code,(404,412))
            read=self.read()
            if read.status_code==200:
                self.assertEqual(read.json()['revision'],current_read.json()['revision']+1)
                self.assertEqual(read.json()['share_enabled'],bool(self.enabled()))
            else:self.assertEqual(self.enabled(),[])

    def test_waiter_rechecks_lock_timeout_and_slow_read_rechecks_capability_and_latest_document(self):
        saved=self.save();created=self.share(saved.headers['etag'])
        with state_session() as blocker,ThreadPoolExecutor(1) as pool:
            row=owner.load_owned(blocker,AuthenticatedPrincipal(ISSUER,'A'),KEY,lock=True)
            future=pool.submit(self.share,created.headers['etag'])
            r=future.result(timeout=10)
            self.assert_status(r,503);blocker.rollback()
        self.assert_status(self.resolve(created),200)
        from src.api.user_buildings import observe
        reached=threading.Event();release=threading.Event()
        def slow(key):
            result=observe(key);reached.set();release.wait(timeout=10);return result
        # Source work overlaps owner change: final state read uses latest document.
        with patch('src.api.building_shares.observe',slow),ThreadPoolExecutor(1) as pool:
            future=pool.submit(self.resolve,created);self.assertTrue(reached.wait(10))
            edited=self.client.patch(PATH,json={'set':{'overrides':{'floor_area':222}}},headers=self.auth(**{'If-Match':created.headers['etag']}))
            self.assert_status(edited,200);release.set();r=future.result(timeout=10)
        self.assertEqual(r.json()['effective']['details']['floor_area'],222)
        reached.clear();release.clear()
        with patch('src.api.building_shares.observe',slow),ThreadPoolExecutor(1) as pool:
            future=pool.submit(self.resolve,created);self.assertTrue(reached.wait(10))
            revoked=self.client.delete(PATH+'/share',headers=self.auth(**{'If-Match':edited.headers['etag']}))
            self.assert_status(revoked,204);release.set();self.assert_status(future.result(timeout=10),404)

    def test_real_source_rebuild_loss_ambiguity_outage_and_public_isolation(self):
        saved=self.save({'overrides':{'floor_area':175}});created=self.share(saved.headers['etag'])
        source=self.checksum();state=(self.stored(),self.enabled())
        before=self.client.get('/energy-data/buildings/'+KEY).json();before.pop('resolved_at')
        tile='/energy-data/tiles/15/18568/9515.pbf'
        tile_before=self.client.get(tile).content
        for _ in range(2):self.assert_status(self.resolve(created),200)
        self.assertEqual(self.checksum(),source);self.assertEqual((self.stored(),self.enabled()),state)
        public=self.client.get('/energy-data/buildings/'+KEY).json();public.pop('resolved_at')
        self.assertEqual(public,before);self.assertEqual(self.client.get(tile).content,tile_before)
        with self.Admin.begin() as session:session.execute(text('UPDATE buildings SET total_area=456 WHERE building_key=:key'),{'key':KEY})
        self.merge()
        result=self.resolve(created).json()
        self.assertEqual(result['effective']['details']['floor_area'],175)
        self.assertEqual(result['effective']['details']['total_area'],456)
        self.assertEqual(self.enabled(),state[1])
        with self.Admin.begin() as session:
            session.execute(text('DELETE FROM main WHERE building_key=:key'),{'key':KEY})
            session.execute(text('DELETE FROM buildings WHERE building_key=:key'),{'key':KEY})
        result=self.resolve(created).json()
        self.assertEqual(result['base_unavailable_reason'],'missing');self.assertIsNone(result['base'])
        self.assertEqual(result['last_known_summary']['address_fin'],'A street 1')
        self.assertEqual(result['effective']['details']['floor_area'],175)
        with patch('src.api.building_shares.observe',return_value=(None,'ambiguous',None)):
            self.assertEqual(self.resolve(created).json()['base_unavailable_reason'],'ambiguous')
        with patch('src.api.user_buildings.SessionLocal',side_effect=__import__('src.building_resolver',fromlist=['BuildingSourceUnavailable']).BuildingSourceUnavailable):
            self.assertEqual(self.resolve(created).json()['base_unavailable_reason'],'source_unavailable')
        # Reappearance of exactly the same canonical key restores current facts.
        with self.Admin.begin() as session:
            session.execute(text('INSERT INTO buildings(building_key,volume) VALUES (:key,888)'),{'key':KEY})
        result=self.resolve(created).json()
        self.assertEqual(result['base_status'],'available')
        self.assertEqual(result['effective']['details']['volume'],888)
        self.assertEqual(self.enabled(),state[1])
        # Lifecycle needs no source at all, even after loss.
        with patch('src.api.user_buildings.observe',side_effect=AssertionError('source used by lifecycle')):
            rotated=self.share(created.headers['etag']);self.assert_status(rotated,201)
            self.assert_status(self.client.delete(PATH+'/share',headers=self.auth(**{'If-Match':rotated.headers['etag']})),204)
        self.assert_status(self.resolve(created),404)

    def test_database_constraints_grants_and_missing_share_readiness(self):
        self.assertTrue(state_ready());saved=self.save();created=self.share(saved.headers['etag'])
        identity=saved.json()['saved_id'];digest=self.enabled()[0]['token_digest']
        attempts=[({'saved_id':identity,'token_digest':secrets.token_bytes(32)},'23505'),
                  ({'saved_id':str(uuid4()),'token_digest':secrets.token_bytes(32)},'23503')]
        for values,code in attempts:
            with state_session() as session:
                with self.assertRaises(IntegrityError) as failure:session.execute(Share.__table__.insert().values(**values))
                self.assertEqual(failure.exception.orig.pgcode,code);session.rollback()
        # Delete current row within a rolled-back transaction to isolate length.
        with state_session() as session:
            session.execute(text(f'DELETE FROM {SHARES}'))
            with self.assertRaises(IntegrityError) as failure:
                session.execute(Share.__table__.insert().values(saved_id=identity,token_digest=b'short'))
            self.assertEqual(failure.exception.orig.pgcode,'23514');session.rollback()
        with state_session() as session:
            from sqlalchemy.exc import DBAPIError
            with self.assertRaises(DBAPIError) as failure:session.execute(text(f'UPDATE {SHARES} SET token_digest=token_digest'))
            self.assertEqual(failure.exception.orig.pgcode,'42501');session.rollback()
        with self.Reader() as session:
            from sqlalchemy.exc import DBAPIError
            with self.assertRaises(DBAPIError):session.execute(text(f'SELECT * FROM {SHARES}'))
        with self.Admin.begin() as session:session.execute(text(f'ALTER TABLE {SHARES} DROP CONSTRAINT building_shares_digest_length'))
        try:
            self.assertFalse(state_ready());self.assert_status(self.resolve(created),503)
            self.assertEqual(self.client.get('/energy-data/buildings/'+KEY).status_code,200)
        finally:
            with self.Admin.begin() as session:session.execute(text(f'ALTER TABLE {SHARES} ADD CONSTRAINT building_shares_digest_length CHECK (octet_length(token_digest)=32)'))
        self.assertTrue(state_ready());self.assert_status(self.resolve(created),200)

# unittest must not collect the imported fixture class a second time.
del Fixture
