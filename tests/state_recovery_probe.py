"""Second-interpreter recovery acceptance; no private value leaves this process."""
import sys
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from src.api.auth import AuthenticatedPrincipal
from src import building_shares, user_buildings
from src.db.state import check_state
from src.db.user_building_models import UserBuildingShare
from src.state_storage.config import load_target
from src.state_storage.operations import load_bundle
from src.state_storage.private import read_json


def main():
    try:
        target=load_target(sys.argv[1]);probe=read_json(sys.argv[2])
        if target['mode']!='disposable':return 1
        bundle=load_bundle(target)
        engine=create_engine('postgresql+psycopg2://',hide_parameters=True,connect_args=target.settings(target['writer'],bundle['writer_password'],target['database']).kwargs())
        try:
            with Session(engine) as session:
                check_state(session)
                principal=AuthenticatedPrincipal(probe['issuer'],probe['subject'])
                row=user_buildings.load_owned(session,principal,probe['key'])
                if row is None or user_buildings.etag(row)!=probe['tag']:return 1
                share=building_shares.lookup(session,probe['token'])
                if str(share.saved_id)!=probe['saved_id']:return 1
                # Real revoke/rotate/delete services in a rollback-only rehearsal.
                building_shares.revoke(session,row)
                try:building_shares.lookup(session,probe['token']);return 1
                except building_shares.ShareNotFound:pass
                replacement=building_shares.rotate(session,row)
                if building_shares.lookup(session,replacement).saved_id!=row.saved_id:return 1
                session.delete(row);session.flush()
                try:building_shares.lookup(session,replacement);return 1
                except building_shares.ShareNotFound:pass
                session.rollback()
        finally:engine.dispose()
        print('recovery_operations=true');return 0
    except Exception:
        print('recovery_operations=false');return 1


if __name__=='__main__':sys.exit(main())
