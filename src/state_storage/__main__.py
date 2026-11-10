"""Sanitized CLI: operations and private config paths only."""
import argparse
import json
import sys

from .config import load_target
from .operations import apply, inspect, verify, upgrade_personal_v2, upgrade_personal_v3, upgrade_personal_v4, upgrade_personal_v5
from .private import StorageError
from .recovery import backup, restore
from .source_entry import prepare, reload_policy


def main(argv=None):
    parser = argparse.ArgumentParser(description='Guarded dedicated Energy state operations')
    parser.add_argument('operation',choices=('inspect','apply','verify','backup','restore','entry-prepare','entry-reload','upgrade-personal-v2','upgrade-personal-v3','upgrade-personal-v4','upgrade-personal-v5'))
    parser.add_argument('--config-file',required=True)
    args = parser.parse_args(argv)
    try:
        target = load_target(args.config_file)
        result = {'inspect':inspect,'apply':apply,'verify':verify,'backup':backup,'restore':restore,'entry-prepare':prepare,'entry-reload':reload_policy,'upgrade-personal-v2':upgrade_personal_v2,'upgrade-personal-v3':upgrade_personal_v3,'upgrade-personal-v4':upgrade_personal_v4,'upgrade-personal-v5':upgrade_personal_v5}[args.operation](target)
    except StorageError as exc:
        print(json.dumps({'status':'failed','category':exc.category}))
        return 1
    except Exception:
        # Never reflect driver/client/path/JSON exceptions or their chained traceback.
        print(json.dumps({'status':'failed','category':'operation_failed'}))
        return 1
    print(json.dumps(result,sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
