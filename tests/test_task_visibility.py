import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


spec = importlib.util.spec_from_file_location(
    'task_visibility', Path(__file__).parents[1] / 'addons/craft_brew_api/services/task_visibility.py'
)
visibility = importlib.util.module_from_spec(spec)
spec.loader.exec_module(visibility)


class TaskVisibilityTests(unittest.TestCase):
    def task(self, id, **values):
        return SimpleNamespace(**{
            'id': id, 'task_type': 'recipe_review', 'company_id': SimpleNamespace(id=1),
            'target_model': 'brew.recipe.version', 'target_id': 42,
            'name': '配方审核 · 测试配方 · 第 1 轮', 'state': 'pending',
            'assignee_id': SimpleNamespace(id=False), **values,
        })

    def test_two_role_copies_show_once(self):
        tasks = [self.task(1), self.task(2)]
        self.assertEqual(visibility.unique_review_tasks(tasks, 7), [tasks[0]])

    def test_single_role_task_remains_visible(self):
        tasks = [self.task(2)]
        self.assertEqual(visibility.unique_review_tasks(tasks, 7), tasks)

    def test_rounds_versions_and_companies_remain_separate(self):
        tasks = [self.task(1), self.task(2, name='配方审核 · 测试配方 · 第 2 轮'),
                 self.task(3, target_id=43), self.task(4, company_id=SimpleNamespace(id=2))]
        self.assertEqual(visibility.unique_review_tasks(tasks, 7), tasks)

    def test_prefer_current_users_claimed_copy(self):
        tasks = [self.task(1), self.task(2, state='claimed', assignee_id=SimpleNamespace(id=7))]
        self.assertEqual(visibility.unique_review_tasks(tasks, 7), [tasks[1]])

    def test_prefer_open_task_over_finished_copy(self):
        tasks = [self.task(1, state='done', assignee_id=SimpleNamespace(id=7)), self.task(2)]
        self.assertEqual(visibility.unique_review_tasks(tasks, 7), [tasks[1]])

    def test_other_work_is_not_collapsed(self):
        tasks = [self.task(1, task_type='mashing'), self.task(2, task_type='fermentation'),
                 self.task(3, target_id=False), self.task(4, target_id=False)]
        self.assertEqual(visibility.unique_review_tasks(tasks, 7), tasks)

    def test_previous_completed_rounds_remain_visible(self):
        tasks = [self.task(1, state='done'), self.task(2, state='done'),
                 self.task(3, name='配方审核 · 测试配方 · 第 2 轮')]
        self.assertEqual(visibility.unique_review_tasks(tasks, 7), [tasks[0], tasks[2]])


if __name__ == '__main__':
    unittest.main()
