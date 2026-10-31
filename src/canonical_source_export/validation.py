"""Closed pinned JSON Schema with mandatory x-text and strict byte parsing."""
import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path
from referencing import Registry, Resource

from jsonschema import Draft202012Validator, ValidationError, validators


class ExportError(Exception):
    """Only constant codes/allowlisted fields may cross the diagnostic boundary."""
    def __init__(self, code, field=None):
        super().__init__(code)
        self.code, self.field = code, field


def text_ok(value, maximum=256, nonblank=False):
    return (isinstance(value, str) and not any(unicodedata.category(c) in ('Cc', 'Cs') for c in value)
            and 1 <= len(value.encode('utf-8')) <= maximum and (not nonblank or bool(value.strip())))


def x_text(validator, rule, value, schema):
    if isinstance(value, str) and not text_ok(value, rule['maxBytes'], rule.get('nonblank', False)):
        yield ValidationError('text')


Validator = validators.extend(Draft202012Validator, {'x-text': x_text})
SCHEMAS = [json.loads((Path(__file__).parent / 'schemas' / name).read_bytes()) for name in
           ('canonical-search-input-v1.schema.json', 'canonical-search-v1.schema.json')]
REGISTRY = Registry().with_resources((s['$id'], Resource.from_contents(s)) for s in SCHEMAS)
CHECKERS = {name: Validator({'$ref': SCHEMAS[0]['$id'] + '#/$defs/' + name}, registry=REGISTRY)
            for name in ('Manifest', 'IdentityRecord', 'AddressRecord')}


def validate(name, value):
    if next(CHECKERS[name].iter_errors(value), None) is not None:
        raise ExportError('schema_invalid', name)
    return value


def encode(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8') + b'\n'
    except (ValueError, UnicodeError, TypeError):
        raise ExportError('json_invalid') from None


def strict_json(raw, compact=False):
    def pairs(items):
        d = {}
        for key, val in items:
            if key in d:
                raise ExportError('duplicate_member')
            d[key] = val
        return d
    def bad(_):
        raise ExportError('json_invalid')
    try:
        if raw.startswith(b'\xef\xbb\xbf'):
            raise ExportError('json_invalid')
        s = raw.decode('utf-8', 'strict')
        # JSON whitespace outside string tokens is forbidden in NDJSON records.
        if compact and re.search(r'\s', re.sub(r'"(?:[^"\\]|\\.)*"', '""', s)):
            raise ExportError('json_invalid')
        result = json.loads(s, object_pairs_hook=pairs, parse_constant=bad)
        def check(v, depth=0):
            if depth > 32:
                raise ExportError('json_invalid')
            if isinstance(v, str) and any(unicodedata.category(c) == 'Cs' for c in v):
                raise ExportError('json_invalid')
            if isinstance(v, float) and not math.isfinite(v):
                raise ExportError('json_invalid')
            if isinstance(v, dict):
                for k, val in v.items():
                    check(k, depth + 1); check(val, depth + 1)
            if isinstance(v, list):
                for val in v:
                    check(val, depth + 1)
        check(result)
        return result
    except (UnicodeError, ValueError, RecursionError):
        raise ExportError('json_invalid') from None


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def bundle_digest(streams):
    return digest(''.join(s['file'] + '\0' + s['sha256'] + '\n' for s in streams).encode())
