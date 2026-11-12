import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('history_policy',Path(__file__).parents[1]/'addons/craft_brew_api/services/history_policy.py')
policy=importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
class HistoryPolicyTests(unittest.TestCase):
    def test_administrative_operations_are_excluded(self):
        for event in ['purchase_requisition','purchase_order','purchase_receipt','quality_inspection','material_requisition','inventory_count']:
            for action in ['create','update','submit','create_from_requisitions','start','update_lines','update_results','recommend-lots']:
                self.assertFalse(policy.is_task_result(event,action),(event,action))
    def test_completed_responsibilities_are_included(self):
        for event,action in [('approval','approve'),('approval','return'),('purchase_order','place'),('quality_inspection','complete'),('material_requisition','settle'),('inventory_count','confirm')]:
            self.assertTrue(policy.is_task_result(event,action))
    def test_unknown_actions_do_not_leak_into_history(self):
        self.assertFalse(policy.is_task_result('unrelated','confirm'))
