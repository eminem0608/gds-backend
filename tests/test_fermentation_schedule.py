import importlib.util
from pathlib import Path
from datetime import datetime
import unittest

spec = importlib.util.spec_from_file_location('fermentation_tasks', Path(__file__).parents[1] / 'addons/craft_brew_api/services/fermentation_tasks.py')
schedule = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schedule)

class FermentationScheduleTests(unittest.TestCase):
    def design(self, rules): return {'target_days':3, 'steps':rules}
    def test_daily_uses_shanghai_calendar(self):
        data = schedule.build_schedule(self.design([{'condition':'time','schedule_type':'daily','action':'measure_temperature'}]), datetime(2026,10,8,18))
        self.assertEqual([e['date'] for e in data['entries']], ['2026-10-09','2026-10-10','2026-10-11'])
        schedule.evaluate(data, datetime(2026,10,9,18))
        self.assertEqual([e['status'] for e in data['entries']], ['overdue','pending','future'])
    def test_day_and_interval(self):
        rules=[{'condition':'time','schedule_type':'day','day_value':3,'action':'seal'}, {'condition':'time','schedule_type':'interval','interval_days':2,'action':'measure_gravity'}]
        self.assertEqual([e['day'] for e in schedule.build_schedule(self.design(rules),datetime(2026,10,9))['entries']], [3,2])
    def test_trigger_keeps_evidence(self):
        data=schedule.build_schedule(self.design([{'condition':'gravity','operator':'<=','condition_value':4,'action':'seal'}]), datetime(2026,10,9))
        data['records']=[{'id':'a','metrics':{'gravity':3},'measured_at':'2026-10-09T01:00:00','submitted_at':'2026-10-09T01:00:00'}]
        schedule.evaluate(data,datetime(2026,10,9,2))
        self.assertEqual(data['entries'][0]['trigger']['record_id'],'a')
        data['records'].append({'id':'b','metrics':{'gravity':6},'measured_at':'2026-10-09T02:00:00','submitted_at':'2026-10-09T02:00:00'})
        schedule.evaluate(data,datetime(2026,10,9,3))
        self.assertEqual(data['entries'][0]['trigger']['record_id'],'a')
        self.assertFalse(data['can_complete'])
    def test_backfill_does_not_replace_newer_measurement(self):
        data=schedule.build_schedule(self.design([{'condition':'gravity','operator':'<=','condition_value':4,'action':'seal'}]),datetime(2026,10,9))
        data['records']=[{'id':'new','metrics':{'gravity':6},'measured_at':'2026-10-09T02:00:00','submitted_at':'2026-10-09T02:00:00'}, {'id':'old','metrics':{'gravity':3},'measured_at':'2026-10-09T01:00:00','submitted_at':'2026-10-09T03:00:00'}]
        self.assertEqual(schedule.evaluate(data,datetime(2026,10,9,4))['entries'][0]['status'],'waiting')
    def test_same_metric_trigger_reuses_measurement(self):
        rules=[{'condition':'gravity','operator':'>=','condition_value':10,'action':'measure_gravity'}, {'condition':'gravity','operator':'>=','condition_value':10,'action':'seal'}, {'condition':'gravity','operator':'>=','condition_value':10,'action':'measure_temperature'}]
        data=schedule.build_schedule(self.design(rules),datetime(2026,10,9))
        data['records']=[{'id':'r','metrics':{'gravity':10},'measured_at':'2026-10-09T01:00:00','submitted_at':'2026-10-09T01:00:00'}]
        schedule.evaluate(data,datetime(2026,10,9,2))
        self.assertEqual([e['status'] for e in data['entries']],['done','pending','pending'])
        self.assertEqual(data['entries'][0]['record'],'r')
        # Also repair a previously triggered, still-pending same-metric task.
        data['entries'][0].update(state='pending',record=None)
        schedule.evaluate(data,datetime(2026,10,9,2))
        self.assertEqual(data['entries'][0]['status'],'done')

    def test_invalid_measurements(self):
        for value in ['nan','inf',-1,101,True]:
            with self.assertRaises(ValueError): schedule.validate_metrics({'attenuation':value})
        self.assertEqual(schedule.validate_metrics({'temperature':0}), {'temperature':0})
    def test_offset_conversion(self):
        self.assertEqual(schedule.parse_time('2026-10-09T10:00:00+08:00'),datetime(2026,10,9,2))

if __name__ == '__main__': unittest.main()
