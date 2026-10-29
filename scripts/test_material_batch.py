"""Offline shared-budget batch acquisition contracts; no model calls."""
import unittest
from materials import MaterialStore

class MaterialBatchTests(unittest.TestCase):
    def store(self, **limits):
        return MaterialStore({'files': [
            {'path': 'one.py', 'content': 'alpha\nbeta\n'},
            {'path': 'two.py', 'content': 'gamma\ndelta\n'}]}, **limits)

    def test_independent_ranges_keep_leaf_cursors(self):
        store=self.store()
        requests=[{'tool':'read_material','arguments':{'path':p,'line_count':1}} for p in ['one.py','two.py']]
        result=store.execute('batch_read',{'requests':requests})
        self.assertEqual([r['result'] for r in result['results']],
                         [store.execute(r['tool'],r['arguments']) for r in requests])
        self.assertFalse(result['has_more'])
        self.assertTrue(all(r['result']['has_more'] for r in result['results']))

    def test_character_budget_is_shared_and_partial_line_is_resumable(self):
        store=MaterialStore({'files':[{'path':p,'content':v} for p,v in [('a','12345678'),('b','abcdefgh'),('c','last')]]},max_chars=10)
        result=store.execute('batch_read',{'requests':[{'tool':'read_material','arguments':{'path':p}} for p in ['a','b','c']]})
        self.assertEqual(sum(len(r['result']['text']) for r in result['results']),10)
        second=result['results'][1]['result']
        self.assertEqual(second['text'],'ab')
        tail=store.execute('read_material',{'path':'b','start_line':second['next_start_line'],'char_offset':second['next_char_offset']})
        self.assertEqual(second['text']+tail['text'],'abcdefgh')
        self.assertEqual(result['next_request_index'],2)

    def test_line_budget_is_shared(self):
        store=self.store(max_lines=2)
        result=store.execute('batch_read',{'requests':[{'tool':'read_material','arguments':{'path':p,'line_count':1}} for p in ['one.py','two.py','one.py']]})
        self.assertEqual(len(result['results']),2)
        self.assertEqual(sum(len(r['result']['line_numbers']) for r in result['results']),2)
        self.assertEqual(result['next_request_index'],2)

    def test_search_match_budget_is_shared(self):
        store=MaterialStore({'files':[{'path':'a','content':'needle\n'*30}]})
        result=store.execute('batch_search',{'requests':[
            {'tool':'search_material','arguments':{'query':'needle','limit':8}},
            {'tool':'search_material','arguments':{'query':'needle'}},
            {'tool':'search_material','arguments':{'query':'needle'}}]})
        self.assertEqual([len(r['result']['matches']) for r in result['results']],[8,12])
        self.assertTrue(result['results'][1]['result']['has_more'])
        self.assertEqual(result['next_request_index'],2)

    def test_invalid_leaf_does_not_stop_other_items(self):
        result=self.store().execute('batch_read',{'requests':[
            {'tool':'read_material','arguments':{'path':'missing'}},
            {'tool':'read_material','arguments':{'path':'one.py','line_count':1}}]})
        self.assertIn('error',result['results'][0]['result'])
        self.assertEqual(result['results'][1]['result']['text'],'alpha\n')

    def test_packet_cannot_invoke_repository_or_nested_tools(self):
        store=self.store()
        for name in ['read_file','batch_read',{'invalid':True}]:
            result=store.execute('batch_read',{'requests':[{'tool':name,'arguments':{'path':'one.py'}}]})
            self.assertIn('error',result['results'][0]['result'])
        schema=next(t for t in store.tools() if t['function']['name']=='batch_read')['function']['parameters']['properties']['requests']['items']
        self.assertEqual(schema['properties']['tool']['enum'],['read_material'])

    def test_packet_batch_schema_keeps_exact_material_leaf_arguments(self):
        store = self.store()
        tools = {item['function']['name']: item['function']['parameters'] for item in store.tools()}
        for batch, leaf in [('batch_read', 'read_material'), ('batch_search', 'search_material')]:
            item = tools[batch]['properties']['requests']['items']
            self.assertNotIn('oneOf', item)  # Official oneOf needs at least two alternatives.
            self.assertEqual(item['properties']['tool']['enum'], [leaf])
            self.assertEqual(item['properties']['arguments'], tools[leaf])
            self.assertNotIn('root', item['properties']['arguments']['properties'])
            self.assertNotIn('offset', item['properties']['arguments']['properties'])
        valid = store.execute('batch_search', {'requests': [
            {'tool': 'search_material', 'arguments': {'query': 'alpha', 'path': 'one.py', 'limit': 1}}]})
        self.assertEqual(valid['results'][0]['result']['matches'][0]['path'], 'one.py')
        invalid = store.execute('batch_search', {'requests': [
            {'tool': 'search_material', 'arguments': {'query': 'alpha', 'offset': 0}}]})
        self.assertEqual(invalid['results'][0]['result']['error'], 'unknown argument')
        invalid_read = store.execute('batch_read', {'requests': [
            {'tool': 'read_material', 'arguments': {'path': 'one.py', 'root': 'repo'}}]})
        self.assertEqual(invalid_read['results'][0]['result']['error'], 'unknown argument')

    def test_invalid_containers_and_leaf_arguments_are_rejected(self):
        store=self.store()
        for args in [None,{'requests':[]},{'requests':'a'},{'requests':[{}]*21},{'requests':[{}],'extra':True}]:
            self.assertIn('error',store.execute('batch_read',args))
        invalid=store.execute('batch_search',{'requests':[{'tool':'search_material','arguments':{'query':'alpha','command':'never'}}]})
        self.assertIn('error',invalid['results'][0]['result'])

if __name__=='__main__':unittest.main()
