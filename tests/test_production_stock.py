import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('production_stock', Path(__file__).parents[1] / 'addons/craft_brew_api/services/production_stock.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class ProductionStockTests(unittest.TestCase):
    def test_missing_actual_is_explicit_estimate(self):
        result = module.liquid_balance(None, 200)
        self.assertEqual((result['volume_l'], result['volume_basis']), (200, 'planned'))

    def test_actual_overrides_plan_and_packaging_consumes_liquid(self):
        result = module.liquid_balance(180, 200, packaged=100, packaging_loss=5)
        self.assertEqual((result['volume_l'], result['volume_basis']), (75, 'actual'))

    def test_recorded_zero_does_not_fall_back_to_plan(self):
        self.assertEqual(module.liquid_balance(0, 200)['volume_l'], 0)

    def test_emptied_or_fully_packaged_has_no_remaining_liquid(self):
        self.assertEqual(module.liquid_balance(180, 200, emptied=True)['volume_l'], 0)
        self.assertEqual(module.liquid_balance(180, 200, 175, 5)['volume_l'], 0)
        self.assertEqual(module.liquid_balance(180, 200, 190, 5)['volume_l'], 0)
