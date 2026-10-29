import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from src.building_lookup_contract import BuildingLookupRequestV1


def request(mode='permanent_building_identifier', value='0000000001', **kwargs):
    field = 'address_fin' if mode == 'address' else mode
    return BuildingLookupRequestV1.model_validate({'schema_version': 1,
        'query': {'mode': mode, field: value}, **kwargs})


class LookupContractTests(unittest.TestCase):
    def test_exact_unicode_and_string_identifiers(self):
        for value in ('Mäkelänkatu 1', 'Ma\u0308kela\u0308nkatu 1', '  A 1 ', "A %_';-- 1"):
            self.assertEqual(request('address', value).query.address_fin, value)
        self.assertEqual(request('property_identifier', '00012345678901').query.property_identifier, '00012345678901')
        for value in ('0000000001', ' Opaque-ID ', 'ää', 'a' * 256):
            self.assertEqual(request(value=value).query.permanent_building_identifier, value)
        self.assertEqual(request().limit, 20)
        for value in (1, 50):
            self.assertEqual(request(limit=value).limit, value)

    def test_strict_envelopes_types_and_bounds(self):
        for kwargs in ({'schema_version': True}, {'schema_version': '1'}, {'schema_version': 2},
                       {'unknown': 'PRIVATE'}, {'limit': True}, {'limit': '1'}, {'limit': 0},
                       {'limit': 51}, {'cursor': 'a' * 2049}, {'cursor': ''}, {'cursor': '++=='},
                       {'query': {'mode': 'unknown'}}, {'query': {'mode': 'address', 'address_fin': 'A', 'unknown': 1}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                BuildingLookupRequestV1.model_validate({'schema_version': 1,
                    'query': {'mode': 'permanent_building_identifier', 'permanent_building_identifier': '01'}, **kwargs})
        with self.assertRaises(ValidationError):
            BuildingLookupRequestV1(query={'mode': 'address', 'address_fin': 'A'})

    def test_invalid_property_has_no_human_form_conversion(self):
        for value in (12345678901, True, '00000000000000', '1-2-3-4', '0001234567890',
                      ' 00012345678901', '０００１２３４５６７８９０１', '0001234567890\n'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                request('property_identifier', value)

    def test_text_and_optional_address_components(self):
        for mode in ('address', 'permanent_building_identifier'):
            for value in ('', ' ', 'A\nB', '\ud800', 1, None, 'ä' * 257):
                with self.subTest(mode=mode, value=repr(value)), self.assertRaises(ValidationError):
                    request(mode, value)
        for name, valid, invalid in (('postal_code', '00550', ('', None, '0055', '００５５０', 550)),
                                     ('municipality_number', '091', ('', None, '91', 91))):
            query = {'mode': 'address', 'address_fin': 'A 1', name: valid}
            self.assertEqual(getattr(BuildingLookupRequestV1(schema_version=1, query=query).query, name), valid)
            for value in invalid:
                with self.assertRaises(ValidationError):
                    BuildingLookupRequestV1(schema_version=1, query={**query, name: value})

    def test_export_parity_and_complete_typed_examples(self):
        from src.api.export_building_lookup_contract import bundle, examples
        root = Path(__file__).resolve().parents[1] / 'src/api'
        self.assertEqual((root / 'building-lookup-v1.schema.json').read_text(), bundle())
        self.assertEqual((root / 'building-lookup-v1.examples.json').read_text(), examples())
        schema = json.loads(bundle())
        for definition in schema['$defs'].values():
            if definition.get('type') == 'object':
                self.assertIs(definition.get('additionalProperties'), False)
