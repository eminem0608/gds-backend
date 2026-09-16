import json
from datetime import timedelta

from odoo import Command, api, fields, models
from odoo.exceptions import ValidationError


CONTROLLED_ACTIONS = {
    "dry_hop",
    "discharge_yeast",
    "cool_down",
    "cold_store",
    "add_adjunct",
    "add_fruit",
    "filter",
    "centrifuge",
    "pasteurize",
    "taste",
}


class BrewProductionBatch(models.Model):
    _inherit = "brew.production.batch"

    fermentation_session_id = fields.Many2one(
        "brew.fermentation.session", check_company=True, copy=False
    )

    def action_enter_tank(self, actual_volume_l):
        result = super().action_enter_tank(actual_volume_l)
        for batch in self:
            if not batch.fermentation_session_id:
                session = self.env["brew.fermentation.session"].create_from_batch(batch)
                batch.fermentation_session_id = session
        return result


class BrewFermentationSession(models.Model):
    _name = "brew.fermentation.session"
    _description = "Craft Brew Fermentation Session"
    _order = "started_at desc, id desc"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False, index=True)
    company_id = fields.Many2one(
        "res.company", related="batch_id.company_id", store=True, index=True
    )
    factory_id = fields.Many2one(
        "brew.factory", related="batch_id.factory_id", store=True, index=True
    )
    batch_id = fields.Many2one(
        "brew.production.batch", required=True, check_company=True, index=True, ondelete="restrict"
    )
    source_tank_id = fields.Many2one(
        "brew.tank", required=True, check_company=True, index=True
    )
    tank_id = fields.Many2one("brew.tank", required=True, check_company=True, index=True)
    state = fields.Selection(
        [
            ("fermenting", "发酵中"),
            ("cold_crash", "降温中"),
            ("conditioning", "冷贮中"),
            ("ready", "待包装"),
            ("transferred", "已转罐"),
            ("completed", "已完成"),
            ("terminated", "已终止"),
        ],
        required=True,
        default="fermenting",
        index=True,
    )
    current_volume_l = fields.Float(required=True)
    started_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    completed_at = fields.Datetime()
    process_snapshot_json = fields.Text(required=True, default="{}")
    node_ids = fields.One2many("brew.fermentation.node", "session_id")
    action_ids = fields.One2many("brew.fermentation.action", "session_id")
    reading_ids = fields.One2many("brew.fermentation.reading", "session_id")
    anomaly_ids = fields.One2many("brew.production.anomaly", "session_id")
    latest_reading_id = fields.Many2one(
        "brew.fermentation.reading", compute="_compute_latest_reading"
    )

    _sql_constraints = [
        ("batch_unique", "unique(batch_id)", "一个生产批次只能有一个发酵会话。"),
        ("volume_positive", "check(current_volume_l > 0)", "发酵会话体积必须大于零。"),
    ]

    @api.depends("reading_ids.state", "reading_ids.recorded_at")
    def _compute_latest_reading(self):
        for session in self:
            session.latest_reading_id = session.reading_ids.filtered(
                lambda item: item.state == "approved"
            ).sorted("recorded_at", reverse=True)[:1]

    @api.model
    def create_from_batch(self, batch):
        snapshot = json.loads(batch.recipe_snapshot_json or "{}")
        steps = snapshot.get("process_steps") or []
        commands = []
        enter_found = False
        for step in steps:
            action_type = step.get("action_type") or "other"
            completed = action_type == "enter_tank"
            enter_found = enter_found or completed
            commands.append(
                Command.create(
                    {
                        "sequence": int(step.get("sequence") or 10),
                        "name": step.get("name") or action_type,
                        "phase": step.get("phase"),
                        "action_type": action_type,
                        "mandatory_confirmation": bool(
                            step.get("mandatory_confirmation")
                        ),
                        "config_json": json.dumps(
                            step.get("config") or {}, ensure_ascii=False, sort_keys=True
                        ),
                        "state": "completed" if completed else "pending",
                        "confirmed_by_id": self.env.user.id if completed else False,
                        "confirmed_at": fields.Datetime.now() if completed else False,
                    }
                )
            )
        if not enter_found:
            commands.insert(
                0,
                Command.create(
                    {
                        "sequence": 1,
                        "name": "进罐",
                        "phase": "fermentation",
                        "action_type": "enter_tank",
                        "mandatory_confirmation": True,
                        "state": "completed",
                        "confirmed_by_id": self.env.user.id,
                        "confirmed_at": fields.Datetime.now(),
                    }
                ),
            )
        return self.create(
            {
                "name": self.env["ir.sequence"].next_by_code(
                    "brew.fermentation.session"
                )
                or "New",
                "batch_id": batch.id,
                "source_tank_id": batch.tank_id.id,
                "tank_id": batch.tank_id.id,
                "current_volume_l": batch.actual_volume_l,
                "started_at": batch.entered_tank_at,
                "process_snapshot_json": batch.recipe_snapshot_json or "{}",
                "node_ids": commands,
            }
        )

    def _complete_node(self, action_type, user):
        self.ensure_one()
        node = self.node_ids.filtered(
            lambda item: item.action_type == action_type and item.state == "pending"
        ).sorted("sequence")[:1]
        if node:
            node.write(
                {
                    "state": "completed",
                    "confirmed_by_id": user.id,
                    "confirmed_at": fields.Datetime.now(),
                }
            )
        return node

    def add_yeast(self, user, product, lot, quantity, uom, note=None):
        self.ensure_one()
        if self.state not in {"fermenting", "cold_crash", "conditioning"}:
            raise ValidationError("当前发酵会话不能添加酵母。")
        actual = self.env["brew.production.material.actual"].create(
            {
                "batch_id": self.batch_id.id,
                "product_id": product.id,
                "lot_id": lot.id,
                "quantity": quantity,
                "uom_id": uom.id,
                "action_type": "yeast",
            }
        )
        action = self.env["brew.fermentation.action"].create(
            {
                "session_id": self.id,
                "action_type": "add_yeast",
                "performed_by_id": user.id,
                "parameters_json": json.dumps(
                    {"quantity": quantity, "uom": uom.name, "note": note},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "material_actual_ids": [Command.link(actual.id)],
            }
        )
        return action

    def perform_action(self, user, action_type, parameters=None, material_values=None):
        self.ensure_one()
        if action_type not in CONTROLLED_ACTIONS:
            raise ValidationError("发酵动作类型无效。")
        if self.state in {"completed", "terminated"}:
            raise ValidationError("已结束发酵会话不能执行动作。")
        actual_commands = []
        for values in material_values or []:
            actual_commands.append(
                Command.create(
                    {
                        "batch_id": self.batch_id.id,
                        "product_id": values["product_id"],
                        "lot_id": values["lot_id"],
                        "quantity": values["quantity"],
                        "uom_id": values["uom_id"],
                        "action_type": (
                            "adjunct"
                            if action_type in {"add_adjunct", "add_fruit", "dry_hop"}
                            else "consumed"
                        ),
                    }
                )
            )
        action = self.env["brew.fermentation.action"].create(
            {
                "session_id": self.id,
                "action_type": action_type,
                "performed_by_id": user.id,
                "parameters_json": json.dumps(
                    parameters or {}, ensure_ascii=False, sort_keys=True
                ),
                "material_actual_ids": actual_commands,
            }
        )
        state_values = {}
        if action_type == "cool_down":
            state_values["state"] = "cold_crash"
            self.tank_id.status = "cold_crash"
        elif action_type == "cold_store":
            state_values["state"] = "conditioning"
            self.tank_id.status = "conditioning"
        elif action_type == "taste" and (parameters or {}).get("passed"):
            state_values["state"] = "ready"
            self.tank_id.status = "ready"
        if state_values:
            self.write(state_values)
        self._complete_node(action_type, user)
        return action

    def transfer(self, user, target_tank, volume_l, confirmed):
        self.ensure_one()
        volume = float(volume_l or 0)
        if confirmed is not True:
            raise ValidationError("转罐必须显式确认。")
        if self.state in {"completed", "terminated"}:
            raise ValidationError("已结束会话不能转罐。")
        if (
            target_tank.factory_id != self.factory_id
            or target_tank.status != "empty"
            or target_tank.cleaning_status != "clean"
            or target_tank.current_production_batch_id
        ):
            raise ValidationError("目标罐必须属于同厂区、为空罐且已清洗。")
        if volume <= 0 or volume > min(self.current_volume_l, target_tank.capacity_l):
            raise ValidationError("转罐体积无效或超过来源体积/目标罐容。")
        source_tank = self.tank_id
        self.env["brew.fermentation.action"].create(
            {
                "session_id": self.id,
                "action_type": "transfer",
                "performed_by_id": user.id,
                "source_tank_id": source_tank.id,
                "target_tank_id": target_tank.id,
                "parameters_json": json.dumps(
                    {"volume_l": volume, "confirmed": True}, sort_keys=True
                ),
            }
        )
        source_tank.write(
            {
                "status": "empty",
                "cleaning_status": "dirty",
                "current_product": False,
                "current_batch_no": False,
                "current_volume_l": 0,
                "current_production_batch_id": False,
            }
        )
        target_tank.write(
            {
                "status": "conditioning",
                "current_product": self.batch_id.product_tmpl_id.display_name,
                "current_batch_no": self.batch_id.name,
                "current_volume_l": volume,
                "current_production_batch_id": self.batch_id.id,
            }
        )
        self.write(
            {"tank_id": target_tank.id, "current_volume_l": volume, "state": "transferred"}
        )
        self._complete_node("transfer", user)
        return True

    def action_complete(self, user, confirmed):
        self.ensure_one()
        if confirmed is not True:
            raise ValidationError("完成发酵会话必须显式确认。")
        if self.state not in {"ready", "transferred"}:
            raise ValidationError("只有待包装或已转罐会话可以完成。")
        pending_mandatory = self.node_ids.filtered(
            lambda item: item.mandatory_confirmation
            and item.action_type != "packaging_release"
            and item.state != "completed"
        )
        if pending_mandatory:
            raise ValidationError("仍有必须确认的工艺节点未完成。")
        self.write({"state": "completed", "completed_at": fields.Datetime.now()})
        self.tank_id.write(
            {
                "status": "empty",
                "cleaning_status": "dirty",
                "current_product": False,
                "current_batch_no": False,
                "current_volume_l": 0,
                "current_production_batch_id": False,
            }
        )
        self.batch_id.write({"state": "completed", "completed_at": fields.Datetime.now()})
        self.batch_id.production_plan_id.with_context(brew_state_transition=True).write(
            {"state": "done"}
        )
        return True


class BrewFermentationNode(models.Model):
    _name = "brew.fermentation.node"
    _description = "Craft Brew Fermentation Process Node"
    _order = "sequence,id"

    session_id = fields.Many2one(
        "brew.fermentation.session", required=True, ondelete="cascade", index=True
    )
    company_id = fields.Many2one(
        "res.company", related="session_id.company_id", store=True, index=True
    )
    sequence = fields.Integer(required=True, default=10)
    name = fields.Char(required=True)
    phase = fields.Char()
    action_type = fields.Char(required=True, index=True)
    mandatory_confirmation = fields.Boolean(default=False)
    config_json = fields.Text(default="{}")
    state = fields.Selection(
        [("pending", "待执行"), ("completed", "已完成"), ("skipped", "已跳过")],
        required=True,
        default="pending",
        index=True,
    )
    confirmed_by_id = fields.Many2one("res.users", check_company=True)
    confirmed_at = fields.Datetime()


class BrewFermentationAction(models.Model):
    _name = "brew.fermentation.action"
    _description = "Craft Brew Fermentation Action"
    _order = "performed_at,id"

    session_id = fields.Many2one(
        "brew.fermentation.session", required=True, ondelete="restrict", index=True
    )
    company_id = fields.Many2one(
        "res.company", related="session_id.company_id", store=True, index=True
    )
    action_type = fields.Char(required=True, index=True)
    performed_by_id = fields.Many2one("res.users", required=True, check_company=True)
    performed_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    source_tank_id = fields.Many2one("brew.tank", check_company=True)
    target_tank_id = fields.Many2one("brew.tank", check_company=True)
    parameters_json = fields.Text(required=True, default="{}")
    material_actual_ids = fields.Many2many(
        "brew.production.material.actual",
        "brew_fermentation_action_material_rel",
        "action_id",
        "material_actual_id",
    )
    attachment_ids = fields.Many2many("ir.attachment")

    @api.constrains("parameters_json")
    def _check_parameters_json(self):
        for action in self:
            try:
                value = json.loads(action.parameters_json or "{}")
            except (TypeError, ValueError) as exc:
                raise ValidationError("发酵动作参数必须是有效 JSON。") from exc
            if not isinstance(value, dict):
                raise ValidationError("发酵动作参数必须是 JSON 对象。")


class BrewFermentationReading(models.Model):
    _name = "brew.fermentation.reading"
    _description = "Craft Brew Fermentation Reading"
    _order = "recorded_at,id"
    _check_company_auto = True

    session_id = fields.Many2one(
        "brew.fermentation.session", required=True, ondelete="restrict", index=True
    )
    company_id = fields.Many2one(
        "res.company", related="session_id.company_id", store=True, index=True
    )
    factory_id = fields.Many2one(
        "brew.factory", related="session_id.factory_id", store=True, index=True
    )
    recorded_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    recorded_by_id = fields.Many2one(
        "res.users", required=True, default=lambda self: self.env.user, check_company=True
    )
    temperature_c = fields.Float()
    pressure_bar = fields.Float()
    gravity_plato = fields.Float()
    ph = fields.Float()
    volume_l = fields.Float()
    state = fields.Selection(
        [
            ("draft", "草稿"),
            ("submitted", "待审核"),
            ("approved", "已审核"),
            ("returned", "已退回"),
            ("superseded", "已更正"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    is_backfill = fields.Boolean(default=False)
    version = fields.Integer(required=True, default=1)
    corrects_reading_id = fields.Many2one(
        "brew.fermentation.reading", check_company=True, index=True, ondelete="restrict"
    )
    correction_reason = fields.Text()
    note = fields.Text()
    attachment_ids = fields.Many2many("ir.attachment")
    submitted_at = fields.Datetime(readonly=True)
    approved_by_id = fields.Many2one("res.users", check_company=True, readonly=True)
    approved_at = fields.Datetime(readonly=True)
    return_reason = fields.Text()

    def write(self, values):
        protected = {
            "recorded_at",
            "temperature_c",
            "pressure_bar",
            "gravity_plato",
            "ph",
            "volume_l",
            "note",
            "attachment_ids",
        }
        if not self.env.context.get("brew_state_transition") and protected.intersection(values):
            if any(item.state not in {"draft", "returned"} for item in self):
                raise ValidationError("只有草稿或退回读数可以修改。")
        return super().write(values)

    @api.constrains(
        "temperature_c", "pressure_bar", "gravity_plato", "ph", "volume_l"
    )
    def _check_physical_ranges(self):
        for item in self:
            if not -50 <= item.temperature_c <= 150:
                raise ValidationError("温度超出物理可录入范围。")
            if item.pressure_bar < 0 or item.gravity_plato < 0 or item.volume_l < 0:
                raise ValidationError("压力、糖度和体积不能小于零。")
            if item.ph and not 0 <= item.ph <= 14:
                raise ValidationError("pH 必须在 0 到 14 之间。")

    def action_submit(self):
        self.ensure_one()
        if self.state not in {"draft", "returned"}:
            raise ValidationError("当前发酵读数不能提交。")
        self.with_context(brew_state_transition=True).write(
            {"state": "submitted", "submitted_at": fields.Datetime.now(), "return_reason": False}
        )
        return True

    def action_approve(self, user):
        self.ensure_one()
        if self.state != "submitted":
            raise ValidationError("只有待审核读数可以审核。")
        self.with_context(brew_state_transition=True).write(
            {
                "state": "approved",
                "approved_by_id": user.id,
                "approved_at": fields.Datetime.now(),
            }
        )
        if self.corrects_reading_id:
            self.corrects_reading_id.with_context(brew_state_transition=True).write(
                {"state": "superseded"}
            )
        self.session_id.tank_id.write(
            {
                "current_temperature": self.temperature_c,
                "current_pressure": self.pressure_bar,
                "current_gravity": self.gravity_plato,
                "current_volume_l": self.volume_l or self.session_id.current_volume_l,
            }
        )
        if self.volume_l:
            self.session_id.current_volume_l = self.volume_l
        self.session_id.evaluate_reading_anomalies(self)
        return True

    def action_return(self, reason):
        self.ensure_one()
        if self.state != "submitted" or not str(reason or "").strip():
            raise ValidationError("只有待审核读数可以退回，且必须填写原因。")
        self.with_context(brew_state_transition=True).write(
            {"state": "returned", "return_reason": reason}
        )
        return True

    def create_correction(self, reason):
        self.ensure_one()
        if self.state != "approved" or not str(reason or "").strip():
            raise ValidationError("只有已审核读数可以更正，且必须填写原因。")
        return self.copy(
            {
                "state": "draft",
                "version": self.version + 1,
                "corrects_reading_id": self.id,
                "correction_reason": reason,
                "submitted_at": False,
                "approved_by_id": False,
                "approved_at": False,
            }
        )


class BrewProductionAnomaly(models.Model):
    _name = "brew.production.anomaly"
    _description = "Craft Brew Production Anomaly"
    _order = "severity desc, detected_at desc,id desc"
    _check_company_auto = True

    name = fields.Char(required=True, default="New", copy=False, index=True)
    company_id = fields.Many2one(
        "res.company", related="session_id.company_id", store=True, index=True
    )
    factory_id = fields.Many2one(
        "brew.factory", related="session_id.factory_id", store=True, index=True
    )
    session_id = fields.Many2one(
        "brew.fermentation.session", required=True, check_company=True, index=True
    )
    anomaly_type = fields.Selection(
        [
            ("temperature_low", "温度过低"),
            ("temperature_high", "温度过高"),
            ("pressure_high", "压力过高"),
            ("gravity_stalled", "糖度停滞"),
            ("reading_missing", "读数漏填"),
            ("session_overdue", "发酵超期"),
            ("manual", "人工异常"),
        ],
        required=True,
        index=True,
    )
    severity = fields.Selection(
        [("low", "低"), ("medium", "中"), ("high", "高"), ("critical", "严重")],
        required=True,
        default="medium",
        index=True,
    )
    state = fields.Selection(
        [
            ("open", "待处理"),
            ("assigned", "已指派"),
            ("resolved", "待复核"),
            ("closed", "已关闭"),
        ],
        required=True,
        default="open",
        index=True,
    )
    detected_at = fields.Datetime(required=True, default=fields.Datetime.now)
    last_detected_at = fields.Datetime(required=True, default=fields.Datetime.now)
    occurrence_count = fields.Integer(required=True, default=1)
    metrics_json = fields.Text(required=True, default="{}")
    assignee_id = fields.Many2one("res.users", check_company=True, index=True)
    assigned_at = fields.Datetime()
    resolution = fields.Text()
    resolved_by_id = fields.Many2one("res.users", check_company=True)
    resolved_at = fields.Datetime()
    verified_by_id = fields.Many2one("res.users", check_company=True)
    verified_at = fields.Datetime()
    task_id = fields.Many2one("brew.task", check_company=True)

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get("name", "New") == "New":
                values["name"] = (
                    self.env["ir.sequence"].next_by_code("brew.production.anomaly") or "New"
                )
        return super().create(values_list)

    @api.model
    def detect_or_merge(self, session, anomaly_type, severity, metrics):
        existing = self.search(
            [
                ("session_id", "=", session.id),
                ("anomaly_type", "=", anomaly_type),
                ("state", "in", ["open", "assigned", "resolved"]),
            ],
            limit=1,
        )
        now = fields.Datetime.now()
        if existing:
            existing.write(
                {
                    "last_detected_at": now,
                    "occurrence_count": existing.occurrence_count + 1,
                    "metrics_json": json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    "state": "assigned" if existing.assignee_id else "open",
                }
            )
            return existing
        task = self.env["brew.task"].create(
            {
                "name": f"{session.name} 发酵异常",
                "task_type": "production_anomaly",
                "company_id": session.company_id.id,
                "factory_id": session.factory_id.id,
                "role_code": "operator",
                "target_model": self._name,
                "target_id": 0,
                "priority": "2" if severity in {"high", "critical"} else "1",
            }
        )
        anomaly = self.create(
            {
                "session_id": session.id,
                "anomaly_type": anomaly_type,
                "severity": severity,
                "metrics_json": json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                "task_id": task.id,
            }
        )
        task.write({"target_id": anomaly.id})
        return anomaly

    def action_assign(self, user):
        self.ensure_one()
        if self.state not in {"open", "assigned"}:
            raise ValidationError("当前异常不能指派。")
        self.write(
            {"state": "assigned", "assignee_id": user.id, "assigned_at": fields.Datetime.now()}
        )
        if self.task_id and self.task_id.state == "pending":
            self.task_id.write(
                {
                    "state": "claimed",
                    "assignee_id": user.id,
                    "claimed_at": fields.Datetime.now(),
                }
            )
        return True

    def action_resolve(self, user, resolution):
        self.ensure_one()
        if self.state not in {"open", "assigned"} or not str(resolution or "").strip():
            raise ValidationError("待处理异常提交结果时必须填写处理措施。")
        self.write(
            {
                "state": "resolved",
                "resolution": resolution,
                "resolved_by_id": user.id,
                "resolved_at": fields.Datetime.now(),
            }
        )
        return True

    def action_verify(self, user):
        self.ensure_one()
        if self.state != "resolved":
            raise ValidationError("只有待复核异常可以关闭。")
        self.write(
            {
                "state": "closed",
                "verified_by_id": user.id,
                "verified_at": fields.Datetime.now(),
            }
        )
        if self.task_id and self.task_id.state not in {"done", "cancelled"}:
            self.task_id.write(
                {"state": "done", "completed_at": fields.Datetime.now()}
            )
        return True


class BrewFermentationSessionAnomaly(models.Model):
    _inherit = "brew.fermentation.session"

    def _alert_config(self):
        self.ensure_one()
        rule = self.env["brew.configuration.rule"].sudo().search(
            [
                ("company_id", "=", self.company_id.id),
                ("rule_kind", "=", "alert"),
                ("code", "=", "fermentation"),
                ("state", "=", "active"),
                "|",
                ("factory_id", "=", self.factory_id.id),
                ("factory_id", "=", False),
            ],
            order="factory_id desc,version desc",
            limit=1,
        )
        return json.loads(rule.config_json or "{}") if rule else {}

    def evaluate_reading_anomalies(self, reading):
        self.ensure_one()
        config = self._alert_config()
        checks = [
            (
                "temperature_low",
                "temperature_min",
                reading.temperature_c < float(config["temperature_min"])
                if "temperature_min" in config
                else False,
            ),
            (
                "temperature_high",
                "temperature_max",
                reading.temperature_c > float(config["temperature_max"])
                if "temperature_max" in config
                else False,
            ),
            (
                "pressure_high",
                "pressure_max",
                reading.pressure_bar > float(config["pressure_max"])
                if "pressure_max" in config
                else False,
            ),
        ]
        severity_map = config.get("severity") or {}
        for anomaly_type, threshold_key, matched in checks:
            if matched:
                self.env["brew.production.anomaly"].detect_or_merge(
                    self,
                    anomaly_type,
                    severity_map.get(anomaly_type, "high"),
                    {
                        "reading_id": reading.id,
                        "temperature_c": reading.temperature_c,
                        "pressure_bar": reading.pressure_bar,
                        "gravity_plato": reading.gravity_plato,
                        "threshold": config.get(threshold_key),
                    },
                )
        stall_hours = float(config.get("gravity_stall_hours") or 0)
        delta_min = float(config.get("gravity_delta_min") or 0)
        if stall_hours and delta_min:
            cutoff = fields.Datetime.now() - timedelta(hours=stall_hours)
            readings = self.reading_ids.filtered(
                lambda item: item.state == "approved"
                and item.recorded_at >= cutoff
                and item.gravity_plato
            ).sorted("recorded_at")
            if len(readings) >= 2 and abs(
                readings[-1].gravity_plato - readings[0].gravity_plato
            ) < delta_min:
                self.env["brew.production.anomaly"].detect_or_merge(
                    self,
                    "gravity_stalled",
                    severity_map.get("gravity_stalled", "medium"),
                    {
                        "hours": stall_hours,
                        "gravity_delta": abs(
                            readings[-1].gravity_plato - readings[0].gravity_plato
                        ),
                        "minimum_delta": delta_min,
                    },
                )
        return True

    @api.model
    def detect_scheduled_anomalies(self):
        sessions = self.search(
            [
                ("state", "in", ["fermenting", "cold_crash", "conditioning", "ready", "transferred"])
            ]
        )
        now = fields.Datetime.now()
        count = 0
        for session in sessions:
            config = session._alert_config()
            severity_map = config.get("severity") or {}
            missing_hours = float(config.get("reading_missing_hours") or 0)
            latest = session.reading_ids.filtered(
                lambda item: item.state == "approved"
            ).sorted("recorded_at", reverse=True)[:1]
            latest_at = latest.recorded_at if latest else session.started_at
            if missing_hours and latest_at < now - timedelta(hours=missing_hours):
                self.env["brew.production.anomaly"].detect_or_merge(
                    session,
                    "reading_missing",
                    severity_map.get("reading_missing", "high"),
                    {
                        "last_reading_at": latest_at.isoformat(),
                        "threshold_hours": missing_hours,
                    },
                )
                count += 1
            max_hours = float(config.get("max_session_hours") or 0)
            if max_hours and session.started_at < now - timedelta(hours=max_hours):
                self.env["brew.production.anomaly"].detect_or_merge(
                    session,
                    "session_overdue",
                    severity_map.get("session_overdue", "medium"),
                    {
                        "started_at": session.started_at.isoformat(),
                        "threshold_hours": max_hours,
                    },
                )
                count += 1
        return count
