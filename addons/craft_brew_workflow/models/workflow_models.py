import json

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class BrewApprovalInstance(models.Model):
    _name = "brew.approval.instance"
    _description = "Craft Brew Approval Instance"
    _order = "create_date desc"
    _check_company_auto = True

    name = fields.Char(required=True)
    approval_type = fields.Char(required=True, index=True)
    company_id = fields.Many2one("res.company", required=True, index=True)
    factory_id = fields.Many2one("brew.factory", check_company=True, index=True)
    target_model = fields.Char(required=True, index=True)
    target_id = fields.Integer(required=True, index=True)
    requester_id = fields.Many2one("res.users", required=True, check_company=True, index=True)
    rule_id = fields.Many2one("brew.configuration.rule", check_company=True)
    rule_snapshot_json = fields.Text(required=True, default="{}")
    state = fields.Selection(
        [("pending", "待审批"), ("approved", "已通过"), ("returned", "已退回"), ("cancelled", "已取消")],
        required=True,
        default="pending",
        index=True,
    )
    current_sequence = fields.Integer(default=10)
    step_ids = fields.One2many("brew.approval.step", "instance_id")
    completed_at = fields.Datetime()

    _sql_constraints = [
        ("active_target_unique", "unique(company_id, target_model, target_id, state)", "同一对象不能存在重复状态的审批实例。"),
    ]

    @api.model
    def create_for_target(self, approval_type, target, requester, factory=None, metrics=None):
        company = target.company_id
        domain = [
            ("company_id", "=", company.id),
            ("rule_kind", "=", "approval"),
            ("code", "=", approval_type),
            ("state", "=", "active"),
            "|",
            ("factory_id", "=", factory.id if factory else False),
            ("factory_id", "=", False),
        ]
        rules = self.env["brew.configuration.rule"].sudo().search(domain, order="factory_id desc, version desc")
        metrics = metrics or {}
        matched = self.env["brew.configuration.rule"]
        snapshot = {}
        for rule in rules:
            config = json.loads(rule.config_json or "{}")
            conditions = config.get("conditions") or {}
            if all(float(metrics.get(key, 0)) >= float(value) for key, value in conditions.items()):
                matched = rule
                snapshot = config
                break
        roles = snapshot.get("roles") or ["production_director"]
        instance = self.sudo().create(
            {
                "name": f"{approval_type}:{target.display_name}",
                "approval_type": approval_type,
                "company_id": company.id,
                "factory_id": factory.id if factory else False,
                "target_model": target._name,
                "target_id": target.id,
                "requester_id": requester.id,
                "rule_id": matched.id if matched else False,
                "rule_snapshot_json": json.dumps({"config": snapshot, "metrics": metrics}, ensure_ascii=False, sort_keys=True),
            }
        )
        for index, role_code in enumerate(roles, start=1):
            self.env["brew.approval.step"].sudo().create(
                {"instance_id": instance.id, "sequence": index * 10, "role_code": role_code}
            )
        return instance

    def _advance(self):
        self.ensure_one()
        pending = self.step_ids.filtered(lambda step: step.state == "pending").sorted("sequence")
        if pending:
            self.current_sequence = pending[0].sequence
        else:
            self.write({"state": "approved", "completed_at": fields.Datetime.now()})
        return True


class BrewApprovalStep(models.Model):
    _name = "brew.approval.step"
    _description = "Craft Brew Approval Step"
    _order = "instance_id, sequence, id"

    instance_id = fields.Many2one("brew.approval.instance", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("res.company", related="instance_id.company_id", store=True, index=True)
    sequence = fields.Integer(required=True, default=10)
    role_code = fields.Char(required=True, index=True)
    assignee_id = fields.Many2one("res.users", check_company=True, index=True)
    state = fields.Selection(
        [("pending", "待处理"), ("approved", "已同意"), ("returned", "已退回"), ("transferred", "已转交")],
        required=True,
        default="pending",
        index=True,
    )
    acted_by_id = fields.Many2one("res.users", check_company=True)
    acted_at = fields.Datetime()
    comment = fields.Text()

    def action_approve(self, user, comment=None):
        self.ensure_one()
        if self.state != "pending" or self.sequence != self.instance_id.current_sequence:
            raise ValidationError("该审批步骤当前不可处理。")
        self.write({"state": "approved", "acted_by_id": user.id, "acted_at": fields.Datetime.now(), "comment": comment})
        self.instance_id._advance()
        return True

    def action_return(self, user, comment):
        self.ensure_one()
        if self.state != "pending" or not str(comment or "").strip():
            raise ValidationError("待审批步骤退回时必须填写意见。")
        self.write({"state": "returned", "acted_by_id": user.id, "acted_at": fields.Datetime.now(), "comment": comment})
        self.instance_id.write({"state": "returned", "completed_at": fields.Datetime.now()})
        return True

    def action_transfer(self, user, target_user, comment):
        self.ensure_one()
        if self.state != "pending" or not str(comment or "").strip():
            raise ValidationError("转交审批必须填写原因。")
        self.write({"assignee_id": target_user.id, "comment": comment})
        return True


class BrewTask(models.Model):
    _name = "brew.task"
    _description = "Craft Brew Unified Task"
    _order = "priority desc, due_at, id"
    _check_company_auto = True

    name = fields.Char(required=True)
    task_type = fields.Char(required=True, index=True)
    company_id = fields.Many2one("res.company", required=True, index=True)
    factory_id = fields.Many2one("brew.factory", check_company=True, index=True)
    role_code = fields.Char(index=True)
    assignee_id = fields.Many2one("res.users", check_company=True, index=True)
    target_model = fields.Char(index=True)
    target_id = fields.Integer(index=True)
    priority = fields.Selection([("0", "普通"), ("1", "重要"), ("2", "紧急")], default="0", required=True)
    due_at = fields.Datetime(index=True)
    state = fields.Selection(
        [("pending", "待领取"), ("claimed", "已领取"), ("in_progress", "进行中"), ("done", "已完成"), ("returned", "已退回"), ("cancelled", "已取消")],
        required=True,
        default="pending",
        index=True,
    )
    claimed_at = fields.Datetime()
    started_at = fields.Datetime()
    completed_at = fields.Datetime()
    last_comment = fields.Text()

    def action_claim(self, user):
        self.ensure_one()
        self.env.cr.execute("SELECT id FROM brew_task WHERE id = %s FOR UPDATE NOWAIT", [self.id])
        self.invalidate_recordset()
        if self.state != "pending" or self.assignee_id:
            raise ValidationError("任务已被领取。")
        self.write({"state": "claimed", "assignee_id": user.id, "claimed_at": fields.Datetime.now()})
        return True

    def action_start(self, user):
        self.ensure_one()
        if self.state != "claimed" or self.assignee_id != user:
            raise ValidationError("只有任务处理人可以开始任务。")
        self.write({"state": "in_progress", "started_at": fields.Datetime.now()})
        return True

    def action_submit(self, user, comment=None):
        self.ensure_one()
        if self.state not in {"claimed", "in_progress"} or self.assignee_id != user:
            raise ValidationError("只有任务处理人可以提交任务。")
        self.write({"state": "done", "completed_at": fields.Datetime.now(), "last_comment": comment})
        return True

    def action_return(self, user, comment):
        self.ensure_one()
        if self.state != "done" or not str(comment or "").strip():
            raise ValidationError("只有已完成任务可以退回，且必须填写原因。")
        self.write({"state": "returned", "last_comment": comment, "assignee_id": user.id})
        return True

    def action_reassign(self, target_user, comment):
        self.ensure_one()
        if self.state in {"done", "cancelled"} or not str(comment or "").strip():
            raise ValidationError("当前任务不能转派，或未填写原因。")
        self.write({"assignee_id": target_user.id, "state": "claimed", "last_comment": comment, "claimed_at": fields.Datetime.now()})
        return True
