"""No dotenv, application engine, source values or exception dumps."""
import argparse
import json
import sys
import uuid
from .config import load_config, DEFAULTS
from .bundle import export, verify, preflight, cleanup
from .validation import ExportError


def main(argv=None):
    p=argparse.ArgumentParser(description='Private canonical-input-v1 analytical producer')
    sub=p.add_subparsers(dest='command',required=True)
    for name in ('preflight','export','cleanup'):
        q=sub.add_parser(name);q.add_argument('--config-file',required=True)
        if name in ('export','cleanup'):
            q.add_argument('--generation-id',required=name=='cleanup')
    q=sub.add_parser('verify');q.add_argument('--bundle-dir',required=True);q.add_argument('--config-file')
    args=p.parse_args(argv)
    try:
        if args.command=='verify':
            result=verify(args.bundle_dir,load_config(args.config_file).limits if args.config_file else DEFAULTS)
        else:
            cfg=load_config(args.config_file)
            if args.command=='preflight':
                result=preflight(cfg)
            elif args.command=='export':
                result=export(cfg,args.generation_id or ('export-'+uuid.uuid4().hex))
            else:
                result=cleanup(cfg.root,args.generation_id)
        print(json.dumps(result,separators=(',',':')))
        return 0
    except ExportError as e:
        # Field/code values originate solely in this package's fixed vocabulary.
        print(json.dumps({'ok':False,'code':e.code,'field':e.field}),file=sys.stderr)
    except BaseException:
        print('{"ok":false,"code":"operation_failed","field":null}',file=sys.stderr)
    return 1


if __name__=='__main__':
    sys.exit(main())
