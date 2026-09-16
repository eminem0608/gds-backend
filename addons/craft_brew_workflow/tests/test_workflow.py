import json

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestWorkflow(TransactionCase):
    def test_approval_uses_rule_snapshot(self):
        rule = self.env["brew.configuration.rule"].create(
            {"name": "计划审批", "code": "production_plan", "rule_kind": "approval", "company_id": self.env.company.id, "state": "active", "config_json": json.dumps({"conditions": {"quantity": 1000}, "roles": ["production_director"]})}
        )
        target = self.env["brew.import.batch"].create(
            {"name": "Target", "resource": "factories", "company_id": self.env.company.id, "file_name": "empty.csv", "file_data": "bmFtZQo="}
        )
        instance = self.env["brew.approval.instance"].create_for_target("production_plan", target, self.env.user, metrics={"quantity": 1200})
        rule.config_json = json.dumps({"conditions": {"quantity": 9999}, "roles": ["general_manager"]})
        snapshot = json.loads(instance.rule_snapshot_json)
        self.assertEqual(snapshot["config"]["roles"], ["production_director"])
        self.assertEqual(instance.step_ids.role_code, "production_director")

    def test_task_claim_and_complete_state_machine(self):
        task = self.env["brew.task"].create(
            {"name": "测试任务", "task_type": "test", "company_id": self.env.company.id, "role_code": "operator"}
        )
        task.action_claim(self.env.user)
        self.assertEqual(task.state, "claimed")
        with self.assertRaises(ValidationError):
            task.action_claim(self.env.user)
        task.action_start(self.env.user)
        task.action_submit(self.env.user, "完成")
        self.assertEqual(task.state, "done")
