import unittest
from unittest.mock import patch
from uuid import UUID
import xml.etree.ElementTree as ET

from src.geoserver.publish_area_aggregates import publish_catalog, record_publication, view_names, publish_latest
from src.geoserver.update_energy_certificate_class import MVT_FORMAT


class AreaCatalogTests(unittest.TestCase):
    identifier=str(UUID(int=123))
    config={'workspace':'energy','datastore':'areas','base_url':'https://example.test/geoserver'}

    def cache(self):
        return ET.fromstring(f'<GeoServerLayer><mimeFormats><string>{MVT_FORMAT}</string></mimeFormats><gridSubsets><gridSubset><gridSetName>EPSG:900913</gridSetName><zoomStart>0</zoomStart><zoomStop>14</zoomStop><minCachedLevel>0</minCachedLevel><maxCachedLevel>14</maxCachedLevel></gridSubset></gridSubsets></GeoServerLayer>')

    def test_only_candidate_names_created_with_verified_cache_config(self):
        catalog={}
        def get(config,path):
            name=path.rsplit('/',1)[1].removesuffix('.json')
            if name not in catalog: raise RuntimeError('HTTP 404')
            return {'featureType':catalog[name]}
        def post(config,path,payload):
            row=payload['featureType'];catalog[row['name']]=row;return 201
        with patch('src.geoserver.publish_area_aggregates.get_json',side_effect=get),patch('src.geoserver.publish_area_aggregates.post_json',side_effect=post),patch('src.geoserver.publish_area_aggregates.configure_gwc_layer') as configure,patch('src.geoserver.publish_area_aggregates.get_xml',side_effect=lambda *args:self.cache()):
            publish_catalog(self.config,self.identifier)
            publish_catalog(self.config,self.identifier)
        self.assertEqual(set(catalog),set(view_names(self.identifier).values()))
        self.assertEqual(configure.call_count,6)
        for row in catalog.values():
            self.assertEqual(row['nativeName'],row['name'])
            self.assertEqual(row['srs'],'EPSG:3857')
            self.assertFalse(row['advertised'])

    def test_existing_unrelated_catalog_and_unverified_cache_are_rejected(self):
        with patch('src.geoserver.publish_area_aggregates.get_json',return_value={'featureType':{'nativeName':'unrelated','srs':'EPSG:3857'}}):
            with self.assertRaisesRegex(RuntimeError,'immutable view'): publish_catalog(self.config,self.identifier)
        def get(config,path):
            name=path.rsplit('/',1)[1].removesuffix('.json')
            return {'featureType':{'nativeName':name,'srs':'EPSG:3857','enabled':True}}
        wrong=self.cache();wrong.find('gridSubsets/gridSubset/zoomStop').text='17'
        with patch('src.geoserver.publish_area_aggregates.get_json',side_effect=get),patch('src.geoserver.publish_area_aggregates.configure_gwc_layer'),patch('src.geoserver.publish_area_aggregates.get_xml',return_value=wrong):
            with self.assertRaisesRegex(RuntimeError,'cache configuration'): publish_catalog(self.config,self.identifier)

    def test_invalid_release_and_incomplete_seal_rejected_before_sql(self):
        with self.assertRaises(ValueError): view_names('evil;DROP TABLE main')
        with self.assertRaisesRegex(ValueError,'Incomplete'):
            record_publication(None,self.identifier,{'verified':True,'release_id':self.identifier,'layers':{'postal_code':{}}})

    def test_stable_publication_disables_client_cache_and_truncates_all_layers(self):
        from unittest.mock import MagicMock
        from src.area_contract import STABLE_AREA_VIEWS, latest_layers
        session=MagicMock();session.execute.return_value.scalar.return_value=self.identifier
        with patch('src.geoserver.publish_area_aggregates.create_stable_views'),patch('src.geoserver.publish_area_aggregates.publish_catalog') as catalog,patch('src.geoserver.publish_area_aggregates.get_xml',side_effect=lambda *a:self.cache()),patch('src.geoserver.publish_area_aggregates.put_xml') as put,patch('src.geoserver.publish_area_aggregates.truncate_gwc_layer') as truncate:
            result=publish_latest(session,self.config)
        self.assertEqual(result['layers'],STABLE_AREA_VIEWS)
        catalog.assert_called_once_with(self.config,self.identifier,stable=True)
        self.assertEqual(truncate.call_count,3)
        for call in put.call_args_list:
            self.assertEqual(call.args[2].findtext('expireClients'),'0')
        with patch.dict('os.environ',{'ENERGY_MAP_GEOSERVER_URL':'https://example.test/geoserver','ENERGY_MAP_AREA_WORKSPACE':'energy'}):
            for level,layer in latest_layers().items():
                self.assertIn(STABLE_AREA_VIEWS[level],layer['url'])
                self.assertNotIn(self.identifier,layer['url'])

    def test_stable_catalog_reloads_changed_native_schema_before_delivery(self):
        from src.area_contract import STABLE_AREA_VIEWS
        from src.area_tiles import TILE_FIELDS
        catalog={name:{'nativeName':name,'srs':'EPSG:3857','enabled':True,'advertised':True,
                       'attributes':{'attribute':[{'name':'geom'}]}} for name in STABLE_AREA_VIEWS.values()}
        def get(config,path):
            return {'featureType':catalog[path.rsplit('/',1)[1].removesuffix('.json')]}
        def put(config,path,payload):
            self.assertIn('?recalculate=attributes',path)
            name=path.rsplit('/',1)[1].split('.json')[0]
            catalog[name]['attributes']['attribute'] += [{'name':field} for field in TILE_FIELDS]
        with patch('src.geoserver.publish_area_aggregates.get_json',side_effect=get),patch('src.geoserver.publish_area_aggregates.put_json',side_effect=put) as update,patch('src.geoserver.publish_area_aggregates.configure_gwc_layer'),patch('src.geoserver.publish_area_aggregates.get_xml',side_effect=lambda *a:self.cache()):
            publish_catalog(self.config,self.identifier,stable=True)
        self.assertEqual(update.call_count,3)
