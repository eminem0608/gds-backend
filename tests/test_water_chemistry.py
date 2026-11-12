import copy
import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('water',Path(__file__).parents[1]/'addons/craft_brew_workflow/models/water_chemistry.py')
water=importlib.util.module_from_spec(spec);spec.loader.exec_module(water)

def fixture():
 source={'ions':{'ca':30,'mg':5,'na':10,'cl':20,'so4':30,'hco3':80},'ph':7.5,'alkalinity':65.56}
 return {'enabled':True,'source':source,'target':{'name':'测试目标','ions':{'ca':{'min':50,'max':100}}},'stages':{'mash':{'additions':{'gypsum':{'amount':100,'purity':100},'calcium_chloride':{'amount':100,'purity':100}},'acid':{'amount_ml':0}},'sparge':{'additions':{}}}}
class WaterChemistryTests(unittest.TestCase):
 def test_real_source_and_hydrated_salts_mass_balance(self):
  w=fixture();r=water.calculate_stage(w,w['stages']['mash'],1000)
  self.assertAlmostEqual(r['ions']['ca'],80.538)
  self.assertAlmostEqual(r['ions']['cl'],68.23)
  self.assertAlmostEqual(r['ions']['so4'],85.79)
  self.assertEqual(r['ions']['mg'],5)
  self.assertEqual(r['comparison']['ca']['status'],'ok')
 def test_missing_ions_and_invalid_values_not_silently_zero(self):
  for bad in ['',None,float('nan'),float('inf'),-1,True]:
   w=fixture();w['source']['ions']['ca']=bad
   with self.assertRaises(ValueError):water.calculate_stage(w,w['stages']['mash'],1000)
 def test_dilution_and_purity(self):
  w=fixture();w['dilution_percent']=50;w['dilution_ions']={k:0 for k in water.IONS};w['stages']['mash']['additions']['gypsum']['purity']=50
  r=water.calculate_stage(w,w['stages']['mash'],1000)
  self.assertAlmostEqual(r['ions']['ca'],15+11.639+27.26)
  self.assertEqual(r['source_volume_l'],500)
 def test_acid_cannot_claim_exact_hco3_or_ph(self):
  w=fixture();w['stages']['mash']['acid']={'amount_ml':100,'concentration':88,'density':1.21}
  r=water.calculate_stage(w,w['stages']['mash'],1000)
  self.assertIsNone(r['ions']['hco3']);self.assertNotIn('ph',r)
  self.assertGreater(r['acid_neutralization'],0)
  self.assertGreaterEqual(r['alkalinity_estimate'],0)
 def test_no_alkalinity_measurement_stays_unknown(self):
  w=fixture();w['source']['alkalinity']=None
  self.assertIsNone(water.calculate_stage(w,w['stages']['mash'],1000)['alkalinity_estimate'])
 def test_stage_water_and_scaling_never_use_preboil_volume(self):
  w=fixture();design={'fermentables':[{'quantity':100}],'mash':{'water_grain_ratio':3,'sparge_water_liters':[200,200]},'water_adjustment':w}
  self.assertEqual(water.water_volumes(design),{'mash':300,'sparge':400})
  snapshot=water.stage_snapshot(design,'mash');self.assertEqual(snapshot['recipe_stage']['volume_l'],300)
  old=copy.deepcopy(design);design['fermentables'][0]['quantity']=50;design['mash']['sparge_water_liters']=[100,100]
  water.scale_additions(design,water.water_volumes(old))
  self.assertEqual(w['stages']['mash']['additions']['gypsum']['amount'],50)
  self.assertEqual(old['water_adjustment']['stages']['mash']['additions']['gypsum']['amount'],100)
 def test_high_source_cannot_be_fixed_by_negative_salt(self):
  w=fixture();w['target']['ions']['ca']={'min':0,'max':10}
  self.assertTrue(any('加盐不能降低' in s for s in water.calculate_stage(w,w['stages']['mash'],1000)['warnings']))
  w['stages']['mash']['additions']['gypsum']['amount']=-1
  with self.assertRaises(ValueError):water.calculate_stage(w,w['stages']['mash'],1000)
 def test_ratio_with_no_chloride_is_unknown(self):
  w=fixture();w['source']['ions']['cl']=0;w['stages']['mash']['additions'].pop('calcium_chloride')
  self.assertIsNone(water.calculate_stage(w,w['stages']['mash'],1000)['ratio'])
