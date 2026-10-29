"""Reproducible, source-free lookup schema and complete synthetic examples."""
import json
from pathlib import Path

from pydantic.json_schema import models_json_schema

from src.building_lookup import MESSAGES, build_page
from src.building_lookup_contract import (
    BuildingLookupErrorV1, BuildingLookupPageV1, BuildingLookupRequestV1,
)

MODELS = [(BuildingLookupRequestV1, 'validation'), (BuildingLookupPageV1, 'serialization'),
          (BuildingLookupErrorV1, 'serialization')]


def bundle():
    _, schema = models_json_schema(MODELS, title='EnergyMapBuildingLookupV1')
    return json.dumps(schema, indent=2, ensure_ascii=False) + '\n'


def typed_examples():
    result = {}
    for mode, field, value in [('address','address_fin','Mäkelänkatu 1'),
                             ('property_identifier','property_identifier','00012345678901'),
                             ('permanent_building_identifier','permanent_building_identifier','0000000001')]:
        body = {'schema_version':1,'query':{'mode':mode,field:value},'limit':20}
        if mode == 'address': body['query'].update(postal_code='00550',municipality_number='091')
        result['request_' + mode] = (BuildingLookupRequestV1,body)
    request = BuildingLookupRequestV1.model_validate(result['request_permanent_building_identifier'][1])
    rows = [{'building_key':key, 'permanent_building_identifier':'0000000001',
        'property_identifier':'00012345678901','address_fin':'Mäkelänkatu 1','postal_code':'00550',
        'postal_office_fin':'Helsinki','geometry_present':geometry,'oversized':False}
        for key,geometry in [('000-synthetic-key',False),('synthetic/key',True)]]
    stamp = 1793275200
    for name, selected in [('ambiguity',rows),('single_no_geometry',rows[:1]),('no_match_in_scope',[])]:
        result[name] = (BuildingLookupPageV1,build_page(request,selected,'a'*64,now=stamp).model_dump(mode='json'))
    first_request = request.model_copy(update={'limit':1})
    first = build_page(first_request,rows,'a'*64,now=stamp)
    second_request = first_request.model_copy(update={'cursor':first.page.next_cursor})
    second = build_page(second_request,rows,'a'*64,now=stamp+10)
    result['first_page'] = (BuildingLookupPageV1,first.model_dump(mode='json'))
    result['next_page_request'] = (BuildingLookupRequestV1,second_request.model_dump(exclude_none=True))
    result['last_page'] = (BuildingLookupPageV1,second.model_dump(mode='json'))
    for code, (_status,message) in MESSAGES.items():
        result[code] = (BuildingLookupErrorV1, {'detail':{'code':code,'message':message,'mode':None,'component':None}})
    return result


def examples():
    values = {}
    for name,(model,value) in typed_examples().items():
        model.model_validate_json(json.dumps(value))
        values[name] = value
    return json.dumps(values, indent=2, ensure_ascii=False) + '\n'


def export(directory=None):
    directory = Path(directory) if directory else Path(__file__).parent
    (directory / 'building-lookup-v1.schema.json').write_text(bundle())
    (directory / 'building-lookup-v1.examples.json').write_text(examples())
    print('Closed lookup v1 schema and complete synthetic examples validated and exported.')


if __name__ == '__main__':
    export()
