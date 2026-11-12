import importlib.util
from pathlib import Path
from datetime import datetime
import unittest

root = Path(__file__).parents[1]
def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value
scale = module('batch_execution', root / 'addons/craft_brew_workflow/models/execution_design.py')
schedule = module('fermentation_schedule', root / 'addons/craft_brew_api/services/fermentation_tasks.py')

class WorkbenchScheduleTests(unittest.TestCase):
    def test_execution_uses_batch_amounts_and_keeps_source_untouched(self):
        source={'design':{'brew_volume_l':2000,'fermentables':[{'quantity':30}], 'hops':[{'quantity':20},{'quantity':30}], 'mash':{'water_calculation':'original','water_grain_ratio':20,'evaporation_rate':10,'sparge_count':3,'sparge_water_liters':[533.33,533.33,533.34]}}}
        result=scale.batch_design(source,200)
        self.assertEqual([r['quantity'] for r in result['hops']],[2,3])
        self.assertEqual(scale.node_values('prepare_mash_water', result)['quantity'],60)
        self.assertAlmostEqual(sum(result['mash']['sparge_water_liters']),160)
        self.assertEqual(source['design']['hops'][0]['quantity'],20)
        self.assertEqual(result['mash']['sparge_water_liters'],[53.33,53.33,53.34])
    def test_parallel_water_does_not_block_rest_but_blocks_physical_sparging(self):
        nodes=[{'code':'mash_in','state':'pending'}, {'code':'prepare_sparge_water','state':'pending','parallel_after':'mash_in'}, {'code':'rest_0_start','state':'pending'}, {'code':'rest_0_end','state':'pending'}, {'code':'sparge_1','state':'pending'}]
        self.assertEqual([n['code'] for n in scale.ready_mash_nodes(nodes)],['mash_in'])
        nodes[0]['state']='done'
        self.assertEqual([n['code'] for n in scale.ready_mash_nodes(nodes)],['rest_0_start','prepare_sparge_water'])
        nodes[2]['state']='done'
        self.assertEqual([n['code'] for n in scale.ready_mash_nodes(nodes)],['rest_0_end','prepare_sparge_water'])
        nodes[3]['state']='done'
        self.assertEqual([n['code'] for n in scale.ready_mash_nodes(nodes)],['prepare_sparge_water'])
        nodes[1]['state']='done'
        self.assertEqual([n['code'] for n in scale.ready_mash_nodes(nodes)],['sparge_1'])
        self.assertIsNone(nodes[0].get('completed_at'))

    def test_sparge_requirements_are_structured_without_changing_the_action(self):
        design={'mash':{'sparge_water_liters':[53.33,53.33,53.34],'workflow':{'sparge_temperature_c':78}}}
        node={'code':'prepare_sparge_water','input_mode':'confirm'}
        self.assertEqual(scale.operation_highlights(node,design),[
            {'label':'洗糟用水','value':160.0,'unit':'L'},
            {'label':'目标水温','value':78,'unit':'℃'}])
        self.assertEqual(node,{'code':'prepare_sparge_water','input_mode':'confirm'})

    def test_monitoring_continues_after_target_day_without_duplicate_entries(self):
        data=schedule.build_schedule({'target_days':2,'steps':[{'action':'measure_gravity','condition':'time','schedule_type':'daily'}]},datetime(2026,10,1))
        schedule.extend_daily_schedule(data,datetime(2026,10,4))
        schedule.extend_daily_schedule(data,datetime(2026,10,4))
        self.assertEqual([e['day'] for e in data['entries']],[1,2,3,4,5])
        schedule.evaluate(data,datetime(2026,10,4))
        self.assertEqual(data['entries'][-1]['status'],'future')
    def test_target_temperature_can_decrease(self):
        design={'mash':{'steps':[{'temperature':30},{'temperature':20}]}}
        self.assertIn('降温',scale.node_values('rest_1_start',design)['title'])

if __name__=='__main__':unittest.main()
