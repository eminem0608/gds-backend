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
    execution_checkpoints_json = fields.Text(default="[]")

    def mash_execution_checkpoints(self):
        self.ensure_one()
        if self.task_type != "mashing" or self.target_model != "brew.production.plan":
            return []
        try:
            checkpoints = json.loads(self.execution_checkpoints_json or "[]")
        except (TypeError, ValueError):
            checkpoints = []
        if checkpoints:
            return checkpoints

        plan = self.env["brew.production.plan"].sudo().search(
            [("id", "=", self.target_id), ("company_id", "=", self.company_id.id)], limit=1
        )
        if not plan:
            return []
        try:
            snapshot = json.loads(plan.recipe_snapshot_json or "{}")
            design = snapshot.get("design") or {}
            mash = design.get("mash") or {}
            hops = design.get("hops") or []
            boil_minutes = max(float(mash.get("boil_time_min") or 0), 0)
        except (TypeError, ValueError, AttributeError):
            design, hops, boil_minutes = {}, [], 0

        nodes = []
        mash = design.get("mash") or {}
        workflow = mash.get("workflow") or {}
        operations = workflow.get("operations") or {}
        steps = mash.get("steps") or []
        iodine_enabled = workflow.get("iodine_check_required", True) is not False
        iodine_after = int(workflow.get("iodine_after_step", 4))
        sparge_count = max(1, min(int(float(mash.get("sparge_count") or 1)), 10))
        sparge_waters = mash.get("sparge_water_liters") or []
        whirlpool_minutes = max(float(workflow.get("whirlpool_time_min") or 0), 0)

        standard = {
            "materials": [{"id": "confirm_materials", "name": "确认本批所需物料", "input_mode": "confirm", "target": "核对配方与生产计划中的物料，领料后确认。", "required": True}],
            "water_mash": [
                {"id": "prepare_mash_water", "name": "准备糖化水并加热", "input_mode": "confirm", "target": "按配方计算水量，加热至第一温度段加预热温差。", "required": True},
                {"id": "mash_in", "name": "混入麦芽并确认", "input_mode": "confirm", "target": "确认投料完成并记录时间。", "required": True},
                {"id": "prepare_sparge_water", "name": "准备洗糟水", "input_mode": "confirm", "target": "按配方准备洗糟水并升至设定温度。", "required": True},
            ],
            "lautering": [
                {"id": "transfer_lauter", "name": "转移至过滤槽", "input_mode": "confirm", "target": "确认开始转槽并记录时间。", "required": True},
                {"id": "filter_start", "name": "开始过滤", "input_mode": "confirm", "target": "确认过滤开始并记录时间。", "required": True},
            ],
            "kettle": [
                {"id": "transfer_kettle", "name": "开始进煮沸锅", "input_mode": "confirm", "target": "确认麦汁开始进入煮沸锅并记录时间。", "required": True},
                {"id": "heat_kettle", "name": "开始加热煮沸锅", "input_mode": "confirm", "target": "确认开始加热并记录时间。", "required": True},
                {"id": "filter_end", "name": "过滤结束", "input_mode": "confirm", "target": "确认过滤结束并记录时间。", "required": True},
            ],
            "boil": [
                {"id": "boil_start", "name": "煮沸锅沸腾，开始计时", "input_mode": "confirm", "target": "确认麦汁达到沸腾；此时开始煮沸计时和酒花投放提醒。", "required": True},
                {"id": "boil_end", "name": "煮沸结束", "input_mode": "confirm", "target": "完成配方设定的煮沸时长后确认并记录时间。", "required": True},
            ],
            "whirlpool": [
                {"id": "whirlpool_start", "name": "麦汁进入回旋沉淀槽", "input_mode": "confirm", "target": "确认开始转入并记录时间。", "required": True},
                {"id": "whirlpool_end", "name": "麦汁转移完成并开始回旋计时", "input_mode": "confirm", "target": "确认转移完成；按设备设定回旋时间计时。", "required": True},
            ],
            "transfer": [
                {"id": "cooling_target", "name": "冷却至目标发酵温度", "input_mode": "confirm", "target": "目标温度来自糖化 / 发酵工艺；达到后确认。", "required": True},
                {"id": "transfer_fermenter", "name": "结束进发酵罐", "input_mode": "confirm", "target": "确认进罐完成并记录时间。", "required": True},
                {"id": "actual_temperature", "name": "记录实际进罐温度", "input_mode": "number", "target": "℃", "required": True},
                {"id": "actual_volume", "name": "记录实际进罐体积", "input_mode": "number", "target": "L", "required": True},
            ],
        }

        def add(code, title, phase, **extra):
            nodes.append({"code": code, "title": title, "phase": phase, "state": "pending", "completed_at": None, "completed_by": None, **extra})

        def add_operations(stage, phase, before_filter_end=False):
            rows = operations.get(stage, standard.get(stage, []))
            inserted_fwh = False
            for index, operation in enumerate(rows or []):
                code = str(operation.get("id") or f"{stage}_{index}")
                if before_filter_end and code == "filter_end" and not inserted_fwh:
                    add_fwh()
                    inserted_fwh = True
                if stage == "whirlpool" and code == "whirlpool_end":
                    add_wp_hops()
                target = operation.get("target", "")
                if code == "prepare_sparge_water":
                    try:
                        total_sparge_water = sum(float(value or 0) for value in sparge_waters)
                    except (TypeError, ValueError):
                        total_sparge_water = 0
                    sparge_temperature = float(workflow.get("sparge_temperature_c") or 78)
                    target = f"准备洗糟水 {total_sparge_water:g} L，升温至 {sparge_temperature:g}℃。"
                elif code == "cooling_target":
                    cooling_temperature = float(workflow.get("cooling_temperature_c") or 18)
                    target = f"按目标温度 {cooling_temperature:g}℃冷却，达到后确认。"
                add(code, operation.get("name") or "未命名操作", phase,
                    input_mode=operation.get("input_mode", "confirm"), target=target,
                    unit=operation.get("unit", ""),
                    choices=[item.strip() for item in str(operation.get("target") or "").split("/") if item.strip()] if operation.get("input_mode") == "choice" else [],
                    required=operation.get("required", True))
                if stage == "boil" and code == "boil_start":
                    add_timed_hops()
                if stage == "boil" and code == "boil_end":
                    nodes[-1]["timer_from"] = "boil_start"
                    nodes[-1]["due_after_min"] = boil_minutes
            if before_filter_end and not inserted_fwh:
                add_fwh()
            if stage == "boil" and not any(item["code"] == "boil_start" for item in nodes):
                add_timed_hops()
            if stage == "whirlpool" and not any(item["code"].startswith("wp_hop_") for item in nodes):
                add_wp_hops()

        def add_fwh():
            for index, hop in enumerate(hops):
                if hop.get("use") == "FWH":
                    add(f"fwh_{index}", f"投入 FWH 酒花：{hop.get('name') or '配方酒花'}", "kettle",
                        input_mode="confirm", quantity=hop.get("quantity"), unit="g", required=True)

        def add_timed_hops():
            timed_hops = []
            for index, hop in enumerate(hops):
                if hop.get("use") != "boil":
                    continue
                try:
                    boil_hop_minutes = float(hop.get("time") or 0)
                except (TypeError, ValueError):
                    continue
                if 0 <= boil_hop_minutes <= boil_minutes:
                    timed_hops.append((boil_minutes - boil_hop_minutes, index, hop, boil_hop_minutes))
            for offset, index, hop, boil_hop_minutes in sorted(timed_hops):
                add(f"boil_hop_{index}", f"投入酒花：{hop.get('name') or '配方酒花'}", "boiling",
                    input_mode="confirm", quantity=hop.get("quantity"), unit="g", timer_from="boil_start",
                    due_after_min=offset, boil_duration_min=boil_hop_minutes, required=True)

        def add_wp_hops():
            for index, hop in enumerate(hops):
                if hop.get("use") == "WP":
                    add(f"wp_hop_{index}", f"回旋沉淀投入酒花：{hop.get('name') or '配方酒花'}", "whirlpool",
                        input_mode="confirm", quantity=hop.get("quantity"), unit="g", required=True)

        add_operations("materials", "materials")
        add_operations("water_mash", "mash")
        if iodine_enabled and iodine_after == 0:
            add("iodine_check", "碘反应检查", "mash", input_mode="choice", choices=["合格", "不合格"], target="合格后继续；不合格时处理后复检。", required=True)
        for index, rest in enumerate(steps):
            try:
                temperature = float(rest.get("temperature"))
                duration = float(rest.get("duration_min"))
            except (TypeError, ValueError, AttributeError):
                continue
            rest_name = rest.get("name") or f"第 {index + 1} 温度段"
            start_code = f"rest_{index}_start"
            add(start_code, f"升温至 {temperature:g}℃并确认，开始计时", "mash",
                input_mode="confirm", target=rest_name, temperature=temperature, duration_min=duration, required=True)
            add(f"rest_{index}_end", f"{temperature:g}℃保持 {duration:g} 分钟结束", "mash",
                input_mode="confirm", timer_from=start_code, due_after_min=duration, temperature=temperature, required=True)
            if iodine_enabled and iodine_after == index + 1:
                add("iodine_check", "碘反应检查", "mash", input_mode="choice", choices=["合格", "不合格"], target="合格后继续下一温度段；不合格时处理后复检。", required=True)
        if iodine_enabled and iodine_after > len(steps):
            add("iodine_check", "碘反应检查", "mash", input_mode="choice", choices=["合格", "不合格"], target="合格后继续；不合格时处理后复检。", required=True)
        add_operations("lautering", "lautering")
        add_operations("sparge", "sparge")
        for index in range(sparge_count):
            quantity = sparge_waters[index] if index < len(sparge_waters) else None
            add(f"sparge_{index + 1}", f"第 {index + 1} 次洗糟", "sparge", input_mode="confirm", quantity=quantity, unit="L", required=True)
        add_operations("kettle", "kettle", before_filter_end=True)
        add_operations("boil", "boiling")
        add_operations("whirlpool", "whirlpool")
        if any(item["code"] == "whirlpool_end" for item in nodes):
            add("whirlpool_hold_end", f"回旋沉淀 {whirlpool_minutes:g} 分钟结束", "whirlpool",
                input_mode="confirm", timer_from="whirlpool_end", due_after_min=whirlpool_minutes, required=True)
        add_operations("transfer", "transfer")
        self.write({"execution_checkpoints_json": json.dumps(nodes, ensure_ascii=False)})
        return nodes

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
        assigned_production = self.target_model == "brew.production.plan" and self.task_type in {"mashing", "fermentation"}
        if self.assignee_id != user or (self.state != "claimed" and not (assigned_production and self.state == "pending")):
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
