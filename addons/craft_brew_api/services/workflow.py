import json
from datetime import datetime, timedelta
import math
import uuid
from zoneinfo import ZoneInfo

from .fermentation_tasks import build_schedule, evaluate, parse_time, validate_metrics, COMPARE, extend_daily_schedule
from .task_visibility import unique_review_tasks

from odoo import fields
from odoo.addons.craft_brew_core.services.role_service import ROLE_DEFINITIONS

from ..controllers.base import ApiError


class WorkflowService:
    def __init__(self, env, user, company): self.env, self.user, self.company = env, user, company

    def _role_codes(self):
        if self.user.has_group("craft_brew_core.group_brew_admin"):
            return set(ROLE_DEFINITIONS)
        return set(self.user.brew_role_codes())

    def _ensure_returned_recipe_tasks(self):
        versions = self.env["brew.recipe.version"].sudo().search(
            [
                ("company_id", "=", self.company.id),
                ("submitted_by_id", "=", self.user.id),
                ("state", "=", "returned"),
            ]
        )
        if not versions:
            return
        task_model = self.env["brew.task"].sudo()
        active_tasks = task_model.search(
            [
                ("company_id", "=", self.company.id),
                ("task_type", "=", "recipe_revision"),
                ("target_model", "=", "brew.recipe.version"),
                ("target_id", "in", versions.ids),
                ("state", "not in", ["done", "cancelled"]),
            ]
        )
        existing_version_ids = set(active_tasks.mapped("target_id"))
        if len(existing_version_ids) == len(versions):
            return
        submissions = self.env["brew.recipe.review.event"].sudo().search(
            [("version_id", "in", versions.ids), ("action", "=", "submitted")],
            order="review_round desc,id desc",
        )
        rounds = {}
        for event in submissions:
            rounds.setdefault(event.version_id.id, event.review_round or 1)
        for version in versions:
            if version.id in existing_version_ids:
                continue
            review_round = rounds.get(version.id, 1)
            task_model.create(
                {
                    "name": f"配方修改 · {version.recipe_id.name} · {version.name} · 第 {review_round} 轮退回",
                    "task_type": "recipe_revision",
                    "company_id": self.company.id,
                    "assignee_id": self.user.id,
                    "target_model": version._name,
                    "target_id": version.id,
                    "last_comment": version.return_reason,
                }
            )

    def record_in_scope(self, record):
        if record.company_id != self.company:
            return False
        factory = getattr(record, 'factory_id', self.env['brew.factory'])
        if not factory and getattr(record, 'target_model', False) and record.target_model in self.env and record.target_id:
            target = self.env[record.target_model].sudo().browse(record.target_id).exists()
            if target:
                if hasattr(target, 'company_id') and target.company_id != self.company:
                    return False
                factory = getattr(target, 'factory_id', None) or getattr(target, 'brew_factory_id', self.env['brew.factory'])
        return factory in self.user.brew_allowed_factories(self.company) if factory else (self.user.brew_has_headquarters_scope(self.company) or getattr(record, 'assignee_id', False) == self.user)

    def list_tasks(self, page, page_size, state=None, task_type=None, active_only=False):
        # Repair returned versions created before submitter revision tasks existed.
        self._ensure_returned_recipe_tasks()
        model = self.env["brew.task"].sudo()
        domain = [("company_id", "=", self.company.id), "|", ("assignee_id", "=", self.user.id), "&", ("assignee_id", "=", False), ("role_code", "in", list(self._role_codes()))]
        if task_type:
            if task_type == "material_preparation" and self.user.has_group("craft_brew_core.group_brew_admin"):
                domain = [("company_id", "=", self.company.id)]
            domain += [("task_type", "=", task_type)]
            if task_type == "material_preparation":
                from .inventory import InventoryService
                domain += [("factory_id", "in", InventoryService(self.env, self.user, self.company, enforce_access=False)._allowed_factory_ids())]
        if active_only: domain += [("state", "not in", ["done", "cancelled", "returned"])]
        if state: domain += [("state", "=", state)]
        records = unique_review_tasks(model.search(domain, order="priority desc,due_at,id").filtered(self.record_in_scope), self.user.id)
        total = len(records)
        start = (page - 1) * page_size
        return [self.serialize_task(item) for item in records[start:start + page_size]], total

    def _task(self, task_id):
        task = self.env["brew.task"].sudo().search([("id", "=", int(task_id)), ("company_id", "=", self.company.id)], limit=1)
        if not task: raise ApiError("RESOURCE_NOT_FOUND", "任务不存在。", 404)
        if not self.record_in_scope(task): raise ApiError("DATA_SCOPE_DENIED", "任务不属于当前厂区范围。", 403)
        if task.assignee_id and task.assignee_id != self.user and not self.user.has_group("craft_brew_core.group_brew_production_director"): raise ApiError("PERMISSION_DENIED", "无权处理该任务。", 403)
        if not task.assignee_id and task.role_code not in self._role_codes() and not (task.task_type == "material_preparation" and self.user.has_group("craft_brew_core.group_brew_admin")): raise ApiError("PERMISSION_DENIED", "任务角色与当前账号不匹配。", 403)
        return task

    def serialize_task(self, task):
        name = task.name
        if task.task_type == "demand_review" and task.target_model == "brew.product.demand" and task.target_id:
            demand = self.env["brew.product.demand"].sudo().browse(task.target_id).exists()
            if demand:
                name = f"产品需求确认 · {demand.product_tmpl_id.name}"
        plan_data = None
        batch_data = None
        blocked_reason = ""
        material_receipt = None
        if task.target_model == "brew.production.plan":
            plan = self.env["brew.production.plan"].sudo().browse(task.target_id).exists()
            if plan:
                plan_data = {"id": str(plan.id), "name": plan.name, "product": plan.product_tmpl_id.name, "planned_volume_l": plan.planned_volume_l, "state": plan.state}
                batch = self.env["brew.production.batch"].sudo().search([("production_plan_id", "=", plan.id)], limit=1)
                if batch: batch_data = {"id": str(batch.id), "name": batch.name}
                plan_data["packaging_plan"] = json.loads(plan.packaging_plan_json or "{}")
                label = {"mashing": "糖化任务", "fermentation": "发酵任务", "material_preparation": "原料出库任务", "packaging": "包装任务"}.get(task.task_type)
                if label: name = f"{plan.product_tmpl_id.name} · {label}"
                if task.task_type == "packaging":
                    latest = self.env['brew.task'].sudo().search([('target_model', '=', plan._name), ('target_id', '=', plan.id), ('task_type', '=', 'packaging'), ('state', '!=', 'cancelled')], order='id desc', limit=1)
                    plan_data['packaging_is_current'] = task == latest
                    if task != latest:
                        order = self.env['brew.packaging.order'].sudo().search([('task_id', '=', task.id)], limit=1)
                        materials = {}
                        for line in order.material_line_ids:
                            key = str(line.product_id.product_tmpl_id.id)
                            item = materials.setdefault(key, {'material_id': key, 'name': line.product_id.name, 'quantity': 0, 'unit': line.uom_id.name})
                            item['quantity'] += line.actual_qty + line.loss_qty
                        plan_data['packaging_plan'] = {'materials': list(materials.values()), 'execution': {
                            'completed_at': task.completed_at.isoformat() if task.completed_at else None,
                            'completed_by': task.assignee_id.name, 'notes': order.note or '',
                            **({'packaging_order_id': str(order.id), 'actual_qty': order.output_qty,
                                'output_volume_l': order.output_volume_l, 'loss_volume_l': order.loss_volume_l,
                                'materials': list(materials.values())} if order else {})}}
                    if plan.state != "done": blocked_reason = "等待发酵任务完成"
                elif plan.state not in {"dispatched", "in_progress"}:
                    blocked_reason = "等待生产计划下发"
                elif task.task_type == "mashing":
                    from .production import ProductionService
                    records = self.env["brew.material.requisition"].sudo().search([("production_plan_id", "=", plan.id), ("state", "!=", "cancelled")])
                    material_receipt = {
                        "ready": bool(records) and all(record.state in {"acknowledged", "completed"} for record in records),
                        "can_acknowledge": bool(records) and all(record.state in {"issued", "acknowledged", "completed"} for record in records) and any(record.state == "issued" for record in records),
                        "records": [ProductionService(self.env, self.user, self.company)._serialize_requisition(record) for record in records],
                    }
                    if not material_receipt["ready"]:
                        blocked_reason = "请核对批次和数量后确认实领" if material_receipt["can_acknowledge"] else "等待库管发料至领料库"
                elif task.task_type == "fermentation":
                    mash = self.env["brew.task"].sudo().search([("target_model", "=", plan._name), ("target_id", "=", plan.id), ("task_type", "=", "mashing"), ("state", "!=", "cancelled")], limit=1)
                    if not mash or mash.state != "done": blocked_reason = "等待本批次糖化完成"

        checkpoints = task.mash_execution_checkpoints()
        from odoo.addons.craft_brew_workflow.models.execution_design import ready_mash_nodes
        ready_codes = {node['code'] for node in ready_mash_nodes(checkpoints)}
        checkpoints = [{**node, 'can_execute_now': node['code'] in ready_codes} for node in checkpoints]
        return {"id": str(task.id), "is_assignee": task.assignee_id == self.user, "execution_blocked_reason": blocked_reason, "material_receipt": material_receipt, "can_execute": task.assignee_id == self.user or self.user.has_group("craft_brew_core.group_brew_production_director") or self.user.has_group("craft_brew_core.group_brew_admin") or (not task.assignee_id and task.role_code in self._role_codes()), "plan": plan_data, "batch": batch_data, "name": name, "task_type": task.task_type, "role_code": task.role_code, "assignee": {"id": str(task.assignee_id.id), "name": task.assignee_id.name} if task.assignee_id else None, "priority": task.priority, "due_at": task.due_at.isoformat() if task.due_at else None, "state": task.state, "target_model": task.target_model, "target_id": str(task.target_id) if task.target_id else None, "last_comment": task.last_comment, "started_at": task.started_at.isoformat() if task.started_at else None, "completed_at": task.completed_at.isoformat() if task.completed_at else None, "fermentation_execution": self.fermentation_execution(task), "execution_checkpoints": checkpoints, "version": task.write_date.isoformat() if task.write_date else None}

    def _fermentation_now(self, task):
        data = json.loads(task.execution_checkpoints_json or "{}")
        offset = data.get("test_day_offset", 0) if isinstance(data, dict) and self.env.cr.dbname == "craft_brew_dev" else 0
        return fields.Datetime.now() + timedelta(days=int(offset))

    def fermentation_execution(self, task):
        if task.task_type != "fermentation":
            return None
        data = json.loads(task.execution_checkpoints_json or "[]")
        if not isinstance(data, dict) or data.get("schema") != "fermentation-v1":
            return {"legacy": task.state == "done", "started_at": None, "entries": [], "records": [], "can_complete": False}
        if task.state == "in_progress":
            extend_daily_schedule(data, self._fermentation_now(task))
        return evaluate(data, self._fermentation_now(task))

    def _start_fermentation(self, task, plan, payload):
        if task.assignee_id != self.user or task.state not in {"pending", "claimed", "in_progress"}:
            raise ApiError("STATE_PRECONDITION_FAILED", "只有负责人可以开始待执行的发酵任务。", 422)
        if self.fermentation_execution(task).get("started_at"):
            raise ApiError("STATE_PRECONDITION_FAILED", "发酵计时已经开始，不能重新初始化。", 422)
        if not payload.get("confirmed"):
            raise ApiError("VALIDATION_REQUIRED", "请确认实际进罐并开始发酵。", 422)
        batch = self.env["brew.production.batch"].sudo().search([("production_plan_id", "=", plan.id)], limit=1)
        entered = batch.entered_tank_at if batch else None
        try:
            start = entered or parse_time(payload.get("started_at") or self._fermentation_now(task))
            if start > self._fermentation_now(task) or (task.create_date and start < task.create_date):
                raise ValueError("开始时间须在任务创建之后，且不能晚于当前时间。")
            snapshot = json.loads(plan.recipe_snapshot_json or "{}")
            design = snapshot.get("design", {}).get("fermentation", {})
            data = build_schedule(design, start, "Asia/Shanghai")
            if not data['entries']:
                raise ValueError("配方没有发酵执行规则，请先配置工艺要求。")
        except (ValueError, TypeError) as exc:
            raise ApiError("VALIDATION_FORMAT", str(exc), 422) from exc
        if task.state != "in_progress":
            task.action_start(self.user)
        task.write({"started_at": start, "execution_checkpoints_json": json.dumps(data, ensure_ascii=False)})

    def _confirm_fermentation_condition(self, task, payload):
        if task.assignee_id != self.user or task.state != "in_progress":
            raise ApiError("PERMISSION_DENIED", "只有任务负责人可以确认条件。", 403)
        data = self.fermentation_execution(task)
        entry = next((e for e in data['entries'] if e['id'] == payload.get('entry_id')), None)
        if not entry or entry['status'] != 'waiting':
            raise ApiError("STATE_PRECONDITION_FAILED", "该条件已经触发或任务项不存在。", 422)
        note = str(payload.get('note') or '').strip()
        if not note:
            raise ApiError("VALIDATION_REQUIRED", "人工确认须填写判断依据。", 422)
        try:
            metrics = validate_metrics(payload.get('metrics') or {})
            measured = parse_time(payload.get('measured_at') or self._fermentation_now(task))
            if not parse_time(data['started_at']) <= measured <= self._fermentation_now(task):
                raise ValueError("测量时间须在发酵开始之后，且不能晚于当前时间。")
            condition = entry['rule']['condition']
            if condition not in metrics:
                raise ValueError("请填写触发条件对应的实测数据。")
            condition_met = COMPARE[entry['rule']['operator']](metrics[condition], float(entry['rule']['condition_value']))
            override = payload.get('manual_override') is True
            reason = str(payload.get('override_reason') or '').strip()
            if not condition_met and not (override and reason and payload.get('confirmed') is True):
                raise ValueError("条件未达到，人工确认须填写原因并确认已完成操作。")
            complete = bool(payload.get('complete_action'))
            if complete and entry['metric'] and entry['metric'] not in metrics:
                raise ValueError("请填写对应操作的测量结果。")
            if complete and not entry['metric'] and not payload.get('confirmed'):
                raise ValueError("请确认已实际完成对应工艺操作。")
        except (ValueError, TypeError, AttributeError) as exc:
            raise ApiError("VALIDATION_FORMAT", str(exc), 422) from exc
        now = self._fermentation_now(task).isoformat()
        local_date = measured.replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo(data['timezone'])).date().isoformat()
        record = {'id': uuid.uuid4().hex, 'kind': 'condition', 'complete_action': complete, 'entry_ids': [entry['id']], 'metrics': metrics, 'measured_at': measured.isoformat(), 'submitted_at': now, 'by': self.user.name, 'user_id': str(self.user.id), 'note': note, 'backfill': local_date < data['today']}
        record.update(manual_override=bool(not condition_met), override_reason=reason if not condition_met else '', condition_met=bool(condition_met))
        data['records'].append(record)
        entry.update(state='done' if complete else 'pending', date=data['today'], record=record['id'] if complete else None, trigger={'manual': True, 'by': self.user.name, 'note': note, 'metric': condition, 'value': metrics[condition], 'at': measured.isoformat(), 'record_id': record['id']})
        evaluate(data, self._fermentation_now(task))
        task.write({'execution_checkpoints_json': json.dumps(data, ensure_ascii=False)})
        return task

    def _record_fermentation(self, task, payload):
        if task.assignee_id != self.user or task.state != "in_progress":
            raise ApiError("PERMISSION_DENIED", "只有进行中任务的负责人可以填写记录。", 403)
        data = self.fermentation_execution(task)
        if not data.get('started_at'):
            raise ApiError("STATE_PRECONDITION_FAILED", "该任务尚未建立按天执行计划。", 422)
        ids = payload.get('entry_ids') or []
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids) or len(set(ids)) != len(ids):
            raise ApiError("VALIDATION_FORMAT", "任务项无效。", 422)
        entries = [entry for entry in data['entries'] if entry['id'] in ids]
        if len(entries) != len(ids) or any(entry['status'] not in {'pending', 'overdue'} and not (entry['day'] and entry['status'] == 'future') for entry in entries):
            raise ApiError("STATE_PRECONDITION_FAILED", "任务项未触发或已经完成。", 422)
        try:
            metrics = validate_metrics(payload.get('metrics') or {})
            measured = parse_time(payload.get('measured_at') or self._fermentation_now(task))
            if measured < parse_time(data['started_at']):
                raise ValueError("填写日期不能早于发酵开始时间。")
            if measured > self._fermentation_now(task):
                raise ValueError("补充测量和条件操作的时间不能晚于当前时间。")
            local_date = measured.replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo(data['timezone'])).date().isoformat()
            for entry in entries:
                if entry['metric'] and entry['metric'] not in metrics:
                    raise ValueError(f"请填写{entry['action_label']}的数值。")
                if entry['day'] and (entry['date'] > data['today'] or local_date != entry['date']):
                    raise ValueError("实际测量日期必须与所选任务日期一致，补录请填写当日实际测量时间。")
                if entry['trigger'] and measured < parse_time(entry['trigger']['at']):
                    raise ValueError("条件任务的执行时间不能早于触发时间。")
            if not entries and not metrics:
                raise ValueError("请填写测量数据或选择执行任务。")
            if any(not entry['metric'] for entry in entries) and not str(payload.get('note') or '').strip():
                raise ValueError("执行工艺动作时请填写操作结果及实际参数。")
        except (ValueError, TypeError, AttributeError) as exc:
            raise ApiError("VALIDATION_FORMAT", str(exc), 422) from exc
        record = {'id': uuid.uuid4().hex, 'entry_ids': ids, 'metrics': metrics, 'measured_at': measured.isoformat(), 'submitted_at': self._fermentation_now(task).isoformat(), 'by': self.user.name, 'user_id': str(self.user.id), 'note': str(payload.get('note') or '').strip(), 'backfill': local_date < data['today']}
        data['records'].append(record)
        for entry in entries:
            entry.update(state='done', record=record['id'])
        evaluate(data, self._fermentation_now(task))
        task.write({'execution_checkpoints_json': json.dumps(data, ensure_ascii=False)})
        return task

    def _acknowledge_materials(self, task):
        from .production import ProductionService
        records = self.env["brew.material.requisition"].sudo().search([
            ("production_plan_id", "=", task.target_id), ("state", "!=", "cancelled"),
        ])
        if not records or any(record.state not in {"issued", "acknowledged", "completed"} for record in records):
            raise ApiError("STATE_PRECONDITION_FAILED", "请先由库管发料至领料库，再核对确认实领。", 422)
        service = ProductionService(self.env, self.user, self.company)
        for record in records.filtered(lambda item: item.state == "issued"):
            service.requisition_action(record.id, "acknowledge", {"version": service._version(record)})

    def _confirm_mash_checkpoint(self, task, payload):
        if task.task_type != "mashing" or task.target_model != "brew.production.plan":
            raise ApiError("STATE_PRECONDITION_FAILED", "该任务没有糖化执行节点。", 422)
        if task.state in {"done", "cancelled", "returned"}:
            raise ApiError("STATE_PRECONDITION_FAILED", "当前糖化任务已结束，不能再确认节点。", 422)
        if task.assignee_id and task.assignee_id != self.user and not self.user.has_group("craft_brew_core.group_brew_production_director"):
            raise ApiError("PERMISSION_DENIED", "只有糖化任务负责人可以确认节点。", 403)
        checkpoints = task.mash_execution_checkpoints()
        key = str(payload.get("checkpoint_key") or "")
        index = next((i for i, node in enumerate(checkpoints) if node.get("code") == key), -1)
        if index < 0:
            raise ApiError("RESOURCE_NOT_FOUND", "糖化执行节点不存在。", 404)
        node = checkpoints[index]
        if node.get("state") == "done":
            raise ApiError("STATE_PRECONDITION_FAILED", "该节点已经确认。", 422)
        from odoo.addons.craft_brew_workflow.models.execution_design import ready_mash_nodes
        ready = ready_mash_nodes(checkpoints)
        if key not in {item['code'] for item in ready}:
            prerequisite = ready[0].get('title') if ready else '前序节点'
            raise ApiError("STATE_PRECONDITION_FAILED", f"请先完成“{prerequisite}”。", 422)

        now = fields.Datetime.now()
        if node.get("timer_from"):
            anchor = next((item for item in checkpoints if item.get("code") == node["timer_from"]), None)
            if not anchor or not anchor.get("completed_at"):
                raise ApiError("STATE_PRECONDITION_FAILED", "前置计时尚未开始。", 422)
            due = parse_time(anchor["completed_at"]) + timedelta(minutes=float(node.get("due_after_min") or 0))
            if now < due:
                if payload.get("skip_timer") is not True:
                    raise ApiError("STATE_PRECONDITION_FAILED", "尚未到操作时间，请等待计时结束。", 422)
                node["timer_skipped"] = True
                node["timer_skipped_at"] = now.isoformat()
                node["timer_skipped_by"] = self.user.id
        if node.get('input_mode') == 'water':
            from odoo.addons.craft_brew_workflow.models.water_chemistry import SALTS, calculate_stage, numeric
            snapshot=node.get('water_adjustment')
            actual=payload.get('water_actual')
            if not snapshot or not isinstance(actual,dict) or payload.get('confirmed') is not True:
                raise ApiError('VALIDATION_REQUIRED','请填写实际调节水量、各项投加量，并确认已完成水质调节。',422)
            try:
                import copy
                stage=copy.deepcopy(snapshot['recipe_stage'])
                stage['volume_l']=numeric(actual.get('volume_l'),'实际调节水量',0.001)
                amounts=actual.get('additions')
                if not isinstance(amounts,dict) or set(amounts)!=set(SALTS):raise ValueError('请完整填写每种调节盐的实际用量，未投加请填零。')
                difference=abs(stage['volume_l']-float(snapshot['recipe_stage']['volume_l']))>0.001
                for salt in SALTS:
                    amount=numeric(amounts[salt].get('amount'),'实际'+SALTS[salt]['name']+'用量',0,1000000)
                    planned=float((stage.get('additions',{}).get(salt) or {}).get('amount',0))
                    difference=difference or abs(amount-planned)>0.000001
                    stage.setdefault('additions',{}).setdefault(salt,{'purity':100})['amount']=amount
                acid=numeric(actual.get('acid_ml'),'实际乳酸用量',0,1000000)
                difference=difference or abs(acid-float((stage.get('acid') or {}).get('amount_ml',0)))>0.000001
                stage.setdefault('acid',{})['amount_ml']=acid
                ph=numeric(actual['measured_ph'],'调节后水实测 pH',0,14) if actual.get('measured_ph') not in (None,'') else None
                if acid>0 and ph is None:raise ValueError('投加乳酸后请检测并填写调节后水 pH。')
                comment=str(payload.get('comment') or '').strip()
                if difference and not comment:raise ValueError('实际水量或投加量与计划不同，请填写差异说明。')
                result=calculate_stage(snapshot,stage,stage['volume_l'])
                node['water_actual']={'stage':stage,'result':result,'measured_ph':ph,'by':self.user.name,'at':now.isoformat(),'note':comment,'lots':{k:str((amounts[k] or {}).get('lot') or '') for k in SALTS}}
                node['actual_note']=comment
            except (ValueError,TypeError,KeyError,AttributeError) as exc:raise ApiError('VALIDATION_FORMAT',str(exc),422)
        value = payload.get("value")
        if node.get("input_mode") == "choice":
            if value not in node.get("choices", []):
                raise ApiError("VALIDATION_SELECTION", "请选择有效的检查结果。", 422)
            if key == "iodine_check" and value != "合格":
                node["last_result"] = value
                node["last_attempt_at"] = now.isoformat()
                node["last_attempt_by"] = self.user.name
                task.write({"execution_checkpoints_json": json.dumps(checkpoints, ensure_ascii=False)})
                return task
        elif node.get("input_mode") == "number":
            try:
                if isinstance(value, bool):
                    raise ValueError()
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError()
            except (TypeError, ValueError):
                raise ApiError("VALIDATION_FORMAT", "该节点需要录入数值。", 422)

        if key == "confirm_materials":
            if task.assignee_id != self.user:
                raise ApiError("PERMISSION_DENIED", "只有糖化任务负责人可以确认实领。", 403)
            self._acknowledge_materials(task)
            batch = self.env["brew.production.batch"].sudo().search([("production_plan_id", "=", task.target_id)], limit=1)
            if batch and batch.state == "draft":
                batch.action_start()

        if node.get("quantity") is not None:
            try:
                actual = float(payload.get("actual_quantity", node["quantity"]))
                planned = float(node["quantity"])
                if not math.isfinite(actual) or actual < 0 or actual > planned + 1e-8:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ApiError("VALIDATION_RANGE", "实际数量须为有效非负数，不能超过本次计划用量。", 422)
            if abs(actual - planned) > 1e-8 and not str(payload.get("comment") or "").strip():
                raise ApiError("VALIDATION_REQUIRED", "实际数量与计划不一致，请填写差异说明。", 422)
            node.update({"actual_quantity": actual, "actual_note": str(payload.get("comment") or "").strip()})
        node.update({"state": "done", "completed_at": now.isoformat(), "completed_by": self.user.name, "completed_by_id": str(self.user.id), "value": value})
        # Keep the operation's displayed requirements and material details with
        # the result, rather than reconstructing history from mutable master data.
        from .workbench import WorkbenchService
        from odoo.addons.craft_brew_workflow.models.execution_design import batch_design, operation_highlights
        plan = self.env["brew.production.plan"].sudo().browse(task.target_id).exists()
        if plan:
            view = WorkbenchService(self.env, self.user, self.company)
            batch = self.env["brew.production.batch"].sudo().search([("production_plan_id", "=", plan.id)], limit=1)
            node["execution_details"] = {
                "materials": view._material_cards(plan, node),
                "requirements": operation_highlights(node, batch_design(json.loads(plan.recipe_snapshot_json or "{}"), plan.planned_volume_l)),
                "production": view._production_context(plan, {"name": batch.name} if batch else None),
            }
        values = {"execution_checkpoints_json": json.dumps(checkpoints, ensure_ascii=False)}
        if task.state in {"pending", "claimed"}:
            values.update({"state": "in_progress", "started_at": task.started_at or now})
        task.write(values)
        return task

    def sync_recorded_batch_volume(self, plan):
        """Preserve measured output; otherwise use the confirmed transfer volume."""
        import math
        batch = self.env["brew.production.batch"].sudo().search([("production_plan_id", "=", plan.id), ("state", "!=", "cancelled")], limit=1)
        if not batch or batch.actual_volume_l > 0:
            return batch
        mash = self.env["brew.task"].sudo().search([("target_model", "=", plan._name), ("target_id", "=", plan.id), ("task_type", "=", "mashing"), ("state", "!=", "cancelled")], limit=1)
        for node in mash.mash_execution_checkpoints() if mash else []:
            if node.get("code") != "actual_volume" or node.get("state") != "done":
                continue
            try:
                volume = float(node.get("value"))
            except (TypeError, ValueError):
                continue
            if not isinstance(node.get("value"), bool) and math.isfinite(volume) and volume > 0:
                batch.write({"actual_volume_l": volume})
                break
        return batch

    def complete_fermentation_plan(self, plan):
        """Advance brewing to packaging after the assigned execution tasks finish."""
        self.env.cr.execute("SELECT id FROM brew_production_plan WHERE id = %s FOR UPDATE", [plan.id])
        plan.invalidate_recordset()
        if plan.state not in {"dispatched", "in_progress"}:
            return False
        tasks = self.env["brew.task"].sudo().search([("target_model", "=", plan._name), ("target_id", "=", plan.id), ("task_type", "in", ["mashing", "fermentation"]), ("state", "!=", "cancelled")])
        if set(tasks.mapped("task_type")) != {"mashing", "fermentation"} or any(task.state != "done" for task in tasks):
            return False
        now = fields.Datetime.now()
        batch = self.env["brew.production.batch"].sudo().search([("production_plan_id", "=", plan.id), ("state", "!=", "cancelled")], limit=1)
        if batch:
            self.sync_recorded_batch_volume(plan)
            batch.write({"state": "completed", "completed_at": now})
            sessions = self.env["brew.fermentation.session"].sudo().search([("batch_id", "=", batch.id), ("state", "not in", ["completed", "terminated"])])
            sessions.write({"state": "ready"})
            tank = plan.tank_id
            self.env.cr.execute("SELECT id FROM brew_tank WHERE id = %s FOR UPDATE", [tank.id])
            tank.invalidate_recordset()
            volume = batch.actual_volume_l or tank.current_volume_l
            occupied_by_other = tank.current_production_batch_id and tank.current_production_batch_id != batch
            if occupied_by_other:
                raise ApiError("STATE_PRECONDITION_FAILED", "罐体已被其他批次占用，请核对酒液位置。", 409)
            if volume > 0 and not occupied_by_other:
                tank.write({"status":"ready", "current_production_batch_id":batch.id,
                            "current_batch_no":batch.name, "current_product":plan.product_tmpl_id.name,
                            "current_volume_l":volume})
        completion = {"automatic": True, "completed_at": now.isoformat(), "completed_by": self.user.name, "completed_by_id": str(self.user.id)}
        if batch and batch.actual_volume_l:
            completion["actual_volume_l"] = batch.actual_volume_l
            completion.update(disposition="in_tank", destination=plan.tank_id.name, tank={"id":str(plan.tank_id.id),"name":plan.tank_id.name})
        plan.with_context(brew_state_transition=True).write({"state": "done", "brewing_completion_json": json.dumps(completion, ensure_ascii=False)})
        return True

    def task_action(self, task_id, action, payload):
        task = self._task(task_id)
        if task.target_model == "brew.production.plan":
            self.env.cr.execute("SELECT id FROM brew_production_plan WHERE id = %s FOR UPDATE", [task.target_id])
        self.env.cr.execute("SELECT id FROM brew_task WHERE id = %s FOR UPDATE", [task.id])
        task.invalidate_recordset()
        current = task.write_date.isoformat() if task.write_date else None
        if not payload.get("version") or payload["version"] != current: raise ApiError("VERSION_CONFLICT", "任务版本不一致。", 409, {"current_version": current})
        if task.task_type == "packaging" and action in {"start", "submit", "checkpoint", "reassign", "return"}:
            raise ApiError("STATE_PRECONDITION_FAILED", "请通过包装计划和包装执行入口操作该任务。", 422)
        if action in {"packaging-start", "packaging-complete", "packaging-register-output"}:
            if task.task_type != "packaging" or task.target_model != "brew.production.plan" or task.assignee_id != self.user:
                raise ApiError("PERMISSION_DENIED", "只有指定的包装执行人可以操作包装任务。", 403)
            plan = self.env["brew.production.plan"].sudo().browse(task.target_id).exists()
            if not plan or plan.state != "done":
                raise ApiError("STATE_PRECONDITION_FAILED", "请先完成发酵任务，再执行包装任务。", 422)
            plan.invalidate_recordset()
            latest = self.env['brew.task'].sudo().search([('target_model', '=', plan._name), ('target_id', '=', plan.id), ('task_type', '=', 'packaging'), ('state', '!=', 'cancelled')], order='id desc', limit=1)
            if task != latest:
                raise ApiError('STATE_PRECONDITION_FAILED', '历史包装任务仅供查看，请操作当前包装任务。', 422)
            data = json.loads(plan.packaging_plan_json or "{}")
            if not data:
                raise ApiError("STATE_PRECONDITION_FAILED", "请先制定包装计划。", 422)
            if action == "packaging-start":
                if task.state not in {"pending", "claimed"}:
                    raise ApiError("STATE_PRECONDITION_FAILED", "该包装任务已经开始或结束。", 422)
                task.action_start(self.user)
            elif action == "packaging-register-output":
                if task.state != "done" or payload.get("confirmed") is not True:
                    raise ApiError("STATE_PRECONDITION_FAILED", "仅已完成的历史包装任务可补登记产出。", 422)
                from .packaging_execution import register_output
                register_output(self.env, self.user, self.company, plan, task, data, payload, repair=True)
            else:
                if task.state != "in_progress" or payload.get("confirmed") is not True:
                    raise ApiError("STATE_PRECONDITION_FAILED", "请先开始包装，并确认已核对实际包装数量。", 422)
                from .packaging_execution import register_output
                register_output(self.env, self.user, self.company, plan, task, data, payload)
                task.action_submit(self.user, data["execution"]["notes"])
            return task, self.serialize_task(task)
        if action == "acknowledge-materials":
            if task.task_type != "mashing" or task.target_model != "brew.production.plan" or task.assignee_id != self.user:
                raise ApiError("PERMISSION_DENIED", "只有本批糖化负责人可以确认实领。", 403)
            plan = self.env["brew.production.plan"].sudo().browse(task.target_id).exists()
            if not plan or plan.state not in {"dispatched", "in_progress"} or task.state in {"done", "cancelled", "returned"}:
                raise ApiError("STATE_PRECONDITION_FAILED", "当前任务不能确认领料。", 422)
            self._acknowledge_materials(task)
            checkpoints = task.mash_execution_checkpoints()
            for node in checkpoints:
                if node.get("code") == "confirm_materials" and node.get("state") != "done":
                    node.update({"state": "done", "completed_at": fields.Datetime.now().isoformat(), "completed_by": self.user.name, "value": "已按批次核对实领"})
            task.write({"execution_checkpoints_json": json.dumps(checkpoints, ensure_ascii=False)})
            return task, self.serialize_task(task)
        if action in {"prepare-materials", "issue-materials"}:
            from .production import ProductionService
            service = ProductionService(self.env, self.user, self.company)
            service._require_warehouse()
            if task.task_type != "material_preparation" or task.target_model != "brew.production.plan" or task.state in {"done", "cancelled", "returned"}:
                raise ApiError("STATE_PRECONDITION_FAILED", "只有待执行的备料任务可以办理出库。", 422)
            plan = service.inventory._plan(task.target_id)
            self.env.cr.execute("SELECT id FROM brew_production_plan WHERE id = %s FOR UPDATE", [plan.id])
            plan.invalidate_recordset()
            if plan.state not in {"dispatched", "in_progress"}:
                raise ApiError("STATE_PRECONDITION_FAILED", "生产计划下发后才能办理出库。", 422)
            records = self.env["brew.material.requisition"].sudo().search([("production_plan_id", "=", plan.id), ("state", "!=", "cancelled")])
            if len(records) > 1:
                raise ApiError("STATE_PRECONDITION_FAILED", "该计划存在多张领料单，请在领料单中逐一办理出库。", 422)
            record = records
            if not record:
                source = service.inventory._location(payload["source_location_id"]) if payload.get("source_location_id") else self.env["stock.location"].sudo().search([("company_id", "=", self.company.id), ("brew_factory_id", "=", plan.factory_id.id), ("usage", "=", "internal"), ("brew_location_type", "=", "raw_material")], limit=1)
                if not source:
                    raise ApiError("VALIDATION_REQUIRED", "该厂区尚未设置原料库位。", 422)
                destination = service.inventory._location(payload.get("production_location_id"))
                if source.usage != "internal" or source.brew_location_type != "raw_material" or destination.usage != "internal" or destination.brew_location_type != "material_issue":
                    raise ApiError("VALIDATION_RELATION", "请选择原料库位和领料库位。", 422)
                record, _data = service.create_requisition({**payload, "source_location_id": source.id, "production_plan_id": plan.id})
            if record.state == "draft":
                record, _data = service.recommend_lots(record.id, {"version": service._version(record), "auto_source_locations": not payload.get("source_location_id")})
            if action == "prepare-materials":
                if record.state != "recommended":
                    raise ApiError("STATE_PRECONDITION_FAILED", "当前领料单已发料或尚未满足备料条件。", 422)
                return task, self.serialize_task(task)
            if record.state == "recommended":
                service.requisition_action(record.id, "issue", {"version": service._version(record)})
            elif record.state not in {"issued", "acknowledged", "completed"}:
                raise ApiError("STATE_PRECONDITION_FAILED", "领料单尚未满足出库条件，请先处理领料单。", 422)
            if not task.assignee_id:
                task.action_claim(self.user)
            if task.state != "in_progress":
                task.action_start(self.user)
            task.action_submit(self.user, "原料已出库，等待糖化负责人确认实领。")
            return task, self.serialize_task(task)
        if task.target_model == "brew.production.plan" and action in {"claim", "start", "checkpoint", "submit", "fermentation-record", "fermentation-condition", "fermentation-test-next-day"}:
            if task.task_type in {"mashing", "fermentation"} and task.assignee_id != self.user and not (action == "submit" and task.task_type == "fermentation" and self.user.has_group("craft_brew_core.group_brew_admin")):
                raise ApiError("PERMISSION_DENIED", "只有当前任务的负责人可以执行该任务。", 403)
            if task.task_type == "material_preparation":
                raise ApiError("STATE_PRECONDITION_FAILED", "请通过原料出库入口办理备料任务。", 422)
            plan = self.env["brew.production.plan"].sudo().browse(task.target_id).exists()
            if not plan or plan.state not in {"dispatched", "in_progress"}:
                raise ApiError("STATE_PRECONDITION_FAILED", "计划尚未下发，完成备货并下发任务后才能执行。", 422)
            if task.task_type == "fermentation":
                mash = self.env["brew.task"].sudo().search([("target_model", "=", plan._name), ("target_id", "=", plan.id), ("task_type", "=", "mashing"), ("state", "!=", "cancelled")], limit=1)
                if not mash or mash.state != "done":
                    raise ApiError("STATE_PRECONDITION_FAILED", "请先完成本批次糖化任务，再开始发酵。", 422)
            if task.task_type == "mashing" and action in {"start", "checkpoint", "submit"}:
                records = self.env["brew.material.requisition"].sudo().search([("production_plan_id", "=", plan.id), ("state", "!=", "cancelled")])
                confirming = action == "checkpoint" and payload.get("checkpoint_key") == "confirm_materials"
                allowed_states = {"issued", "acknowledged", "completed"} if confirming else {"acknowledged", "completed"}
                if not records or any(record.state not in allowed_states for record in records):
                    raise ApiError("STATE_PRECONDITION_FAILED", "请先由库管发料，并由本批负责人确认实领后再开始生产。", 422)
            if task.task_type == "mashing" and action in {"start", "checkpoint"} and plan.state == "dispatched" and not confirming:
                batch = self.env["brew.production.batch"].sudo().search([("production_plan_id", "=", plan.id)], limit=1)
                if batch and batch.state == "draft": batch.action_start()
        if action == "claim": task.action_claim(self.user)
        elif action == "start":
            if task.task_type == "fermentation": self._start_fermentation(task, plan, payload)
            else: task.action_start(self.user)
        elif action == "fermentation-test-next-day":
            if self.env.cr.dbname != 'craft_brew_dev':
                raise ApiError("PERMISSION_DENIED", "仅本地开发数据库支持测试推进。", 403)
            if task.task_type != 'fermentation' or task.state != 'in_progress' or task.assignee_id != self.user:
                raise ApiError("STATE_PRECONDITION_FAILED", "只能推进本人进行中的发酵任务。", 422)
            data = self.fermentation_execution(task)
            if not data.get('started_at'):
                raise ApiError("STATE_PRECONDITION_FAILED", "请先开始发酵。", 422)
            data['test_day_offset'] = int(data.get('test_day_offset', 0)) + 1
            task.write({'execution_checkpoints_json': json.dumps(data, ensure_ascii=False)})
            extend_daily_schedule(data, self._fermentation_now(task))
            task.write({'execution_checkpoints_json': json.dumps(data, ensure_ascii=False)})
        elif action == "fermentation-condition":
            if task.task_type != "fermentation":
                raise ApiError("STATE_PRECONDITION_FAILED", "该任务不是发酵任务。", 422)
            task = self._confirm_fermentation_condition(task, payload)
        elif action == "fermentation-record":
            if task.task_type != "fermentation":
                raise ApiError("STATE_PRECONDITION_FAILED", "该任务不是发酵任务。", 422)
            task = self._record_fermentation(task, payload)
        elif action == "checkpoint": task = self._confirm_mash_checkpoint(task, payload)
        elif action == "submit":
            if task.task_type == "fermentation":
                if task.state != "in_progress" or (task.assignee_id != self.user and not self.user.has_group("craft_brew_core.group_brew_admin")):
                    raise ApiError("PERMISSION_DENIED", "只有发酵任务负责人或系统管理员可以确认完成。", 403)
                execution = self.fermentation_execution(task)
                if not execution or not execution.get('started_at'):
                    raise ApiError("STATE_PRECONDITION_FAILED", "请先开始发酵并建立执行日程。", 422)
                if payload.get('confirmed') is not True:
                    raise ApiError("VALIDATION_REQUIRED", "请确认已完成发酵及必要的出酒或转罐检查。", 422)
                remaining = [entry['id'] for entry in execution['entries'] if entry['status'] != 'done' and not (entry.get('day') and entry['day'] > execution['day'] and entry['day'] > execution['target_days'])]
                if remaining and payload.get('allow_incomplete') is not True:
                    raise ApiError("VALIDATION_REQUIRED", "仍有未完成任务，请确认提前结束发酵。", 422)
                execution['completion'] = {'at': fields.Datetime.now().isoformat(), 'by': self.user.name,
                                           'user_id': str(self.user.id), 'early': bool(remaining),
                                           'remaining_entry_ids': remaining,
                                           'completed_count': len(execution['entries']) - len(remaining),
                                           'total_count': len(execution['entries'])}
                task.write({'execution_checkpoints_json': json.dumps(execution, ensure_ascii=False)})
            if task.task_type == "mashing":
                checkpoints = task.mash_execution_checkpoints()
                incomplete = next((node for node in checkpoints if node.get("required", True) and node.get("state") != "done"), None)
                if incomplete:
                    raise ApiError("STATE_PRECONDITION_FAILED", f"请先完成“{incomplete.get('title')}”。", 422)
            task.action_submit(self.user, payload.get("comment"))
            if task.task_type == "fermentation" and task.target_model == "brew.production.plan":
                self.complete_fermentation_plan(plan)
        elif action == "return": task.action_return(self.user, payload.get("comment"))
        elif action == "reassign":
            if not self.user.has_group("craft_brew_core.group_brew_production_director"): raise ApiError("PERMISSION_DENIED", "只有生产总监可以转派任务。", 403)
            target = self.env["res.users"].sudo().search([("id", "=", int(payload.get("user_id") or 0)), ("company_ids", "in", [self.company.id]), ("active", "=", True)], limit=1)
            if not target: raise ApiError("RESOURCE_NOT_FOUND", "目标用户不存在。", 404)
            if task.factory_id and task.factory_id not in target.brew_allowed_factories(self.company): raise ApiError("DATA_SCOPE_DENIED", "目标人员不属于任务厂区。", 403)
            task.action_reassign(target, payload.get("comment"))
        else: raise ApiError("RESOURCE_NOT_FOUND", "任务动作不存在。", 404)
        return task, self.serialize_task(task)

    def list_approvals(self, page, page_size, state=None):
        role_codes = list(self._role_codes())
        model = self.env["brew.approval.step"].sudo()
        domain = [("company_id", "=", self.company.id), ("role_code", "in", role_codes), "|", ("assignee_id", "=", False), ("assignee_id", "=", self.user.id)]
        if self.user.has_group("craft_brew_core.group_brew_admin"):
            domain = [("company_id", "=", self.company.id)]
        domain += ["|", ("instance_id.target_model", "!=", "brew.quality.inspection"), ("instance_id.state", "!=", "pending")]
        if state: domain += [("state", "=", state)]
        records = model.search(domain, order="instance_id desc,sequence").filtered(lambda step: self.record_in_scope(step.instance_id))
        current = []
        for item in records:
            instance = item.instance_id
            if instance.target_model == "brew.production.plan":
                continue
            target = self.env[instance.target_model].sudo().browse(instance.target_id).exists() if instance.target_model in self.env else None
            current_instance = target.approval_instance_id if target and "approval_instance_id" in target._fields else target.brew_approval_instance_id if target and "brew_approval_instance_id" in target._fields else instance
            if current_instance == instance:
                current.append(item)
        return [self.serialize_approval(item) for item in current[(page - 1) * page_size:page * page_size]], len(current)

    def serialize_approval(self, step):
        instance = step.instance_id
        is_current = (
            instance.state == "pending"
            and step.state == "pending"
            and step.sequence == instance.current_sequence
        )
        if instance.target_model == 'brew.purchase.requisition':
            requisition = self.env[instance.target_model].sudo().browse(instance.target_id).exists()
            if requisition:
                return {'id': str(step.id), 'instance_id': str(instance.id), 'name': '采购需求审批 · ' + requisition.display_name, 'approval_type': instance.approval_type, 'request_type': requisition.request_type, 'role_code': step.role_code, 'sequence': step.sequence, 'state': step.state, 'instance_state': instance.state, 'is_current': is_current, 'comment': step.comment, 'target_model': instance.target_model, 'target_id': str(instance.target_id), 'version': step.write_date.isoformat() if step.write_date else None}
        if instance.target_model == "purchase.order":
            order = self.env["purchase.order"].sudo().browse(instance.target_id).exists()
            if order:
                return {"id": str(step.id), "instance_id": str(instance.id), "name": "采购订单审批 · " + order.display_name, "approval_type": instance.approval_type, "role_code": step.role_code, "sequence": step.sequence, "state": step.state, "instance_state": instance.state, "is_current": is_current, "comment": step.comment or instance.submission_comment, "target_model": instance.target_model, "target_id": str(instance.target_id), "version": step.write_date.isoformat() if step.write_date else None}
        if instance.target_model == "brew.purchase.return":
            ret = self.env["brew.purchase.return"].sudo().browse(instance.target_id).exists()
            if ret:
                return {"id": str(step.id), "instance_id": str(instance.id), "name": "采购退货审批 · " + ret.display_name, "approval_type": instance.approval_type, "role_code": step.role_code, "sequence": step.sequence, "state": step.state, "instance_state": instance.state, "is_current": is_current, "comment": step.comment or instance.submission_comment, "target_model": instance.target_model, "target_id": str(instance.target_id), "version": step.write_date.isoformat() if step.write_date else None}
        if instance.target_model == "brew.receipt":
            receipt = self.env["brew.receipt"].sudo().browse(instance.target_id).exists()
            if receipt:
                return {"id": str(step.id), "instance_id": str(instance.id), "name": "收货超收审批 · " + receipt.display_name, "approval_type": instance.approval_type, "role_code": step.role_code, "sequence": step.sequence, "state": step.state, "instance_state": instance.state, "is_current": is_current, "comment": step.comment or instance.submission_comment, "target_model": instance.target_model, "target_id": str(instance.target_id), "version": step.write_date.isoformat() if step.write_date else None}
        if instance.target_model == "brew.quality.inspection":
            inspection = self.env["brew.quality.inspection"].sudo().browse(instance.target_id).exists()
            if inspection:
                return {"id": str(step.id), "instance_id": str(instance.id), "name": "来料质检审批 · " + inspection.display_name, "approval_type": instance.approval_type, "role_code": step.role_code, "sequence": step.sequence, "state": step.state, "instance_state": instance.state, "is_current": is_current, "comment": step.comment or instance.submission_comment, "target_model": instance.target_model, "target_id": str(instance.target_id), "version": step.write_date.isoformat() if step.write_date else None}
        return {"id": str(step.id), "instance_id": str(instance.id), "name": instance.name, "approval_type": instance.approval_type, "role_code": step.role_code, "sequence": step.sequence, "state": step.state, "instance_state": instance.state, "is_current": is_current, "comment": step.comment, "target_model": instance.target_model, "target_id": str(instance.target_id), "version": step.write_date.isoformat() if step.write_date else None}

    def approval_action(self, step_id, action, payload):
        step = self.env["brew.approval.step"].sudo().search([("id", "=", int(step_id)), ("company_id", "=", self.company.id)], limit=1)
        if step and not self.record_in_scope(step.instance_id): raise ApiError("DATA_SCOPE_DENIED", "审批不属于当前厂区范围。", 403)
        if not step or step.role_code not in self._role_codes(): raise ApiError("PERMISSION_DENIED", "无权处理该审批。", 403)
        if step.instance_id.target_model == 'brew.inventory.count':
            from .inventory import InventoryService
            InventoryService(self.env, self.user, self.company)._document(
                'brew.inventory.count', step.instance_id.target_id)._locked()
            step.invalidate_recordset()
            step.instance_id.invalidate_recordset()
        if step.instance_id.state != "pending" or step.state != "pending" or step.sequence != step.instance_id.current_sequence:
            raise ApiError("STATE_PRECONDITION_FAILED", "该审批步骤已处理或不再处于当前审批环节。", 409)
        current = step.write_date.isoformat() if step.write_date else None
        if not payload.get("version") or payload["version"] != current: raise ApiError("VERSION_CONFLICT", "审批版本不一致。", 409, {"current_version": current})
        if step.instance_id.target_model == "brew.quality.inspection":
            raise ApiError("STATE_PRECONDITION_FAILED", "来料质检已改为直接入库，请在质检入库页面处理。", 409)
        if action == "approve":
            step.action_approve(self.user, payload.get("comment"))
            if step.instance_id.state == "approved":
                instance = step.instance_id
                if instance.target_model == "brew.purchase.requisition":
                    target = self.env["brew.purchase.requisition"].sudo().browse(instance.target_id).exists()
                    if target and target.state == 'submitted':
                        target.action_approve()
                elif instance.target_model == "purchase.order":
                    target = self.env["purchase.order"].sudo().browse(instance.target_id).exists()
                    if target and target.brew_approval_state == 'pending':
                        target.action_brew_approve()
                elif instance.target_model == "brew.purchase.return":
                    target = self.env["brew.purchase.return"].sudo().browse(instance.target_id).exists()
                    if target and target.state == 'submitted':
                        target.action_approve()
                elif instance.target_model == "brew.receipt":
                    target = self.env["brew.receipt"].sudo().browse(instance.target_id).exists()
                    if target and target.state == "pending_approval":
                        target.action_confirm_arrival(self.user)
                elif instance.target_model == "brew.quality.inspection":
                    target = self.env["brew.quality.inspection"].sudo().browse(instance.target_id).exists()
                    if target and target.state == "submitted":
                        target.action_approve()
        elif action == "return":
            comment = str(payload.get("comment") or "").strip()
            step.action_return(self.user, comment)
            instance = step.instance_id
            if instance.target_model == "brew.purchase.requisition":
                target = self.env["brew.purchase.requisition"].sudo().browse(instance.target_id).exists()
                if target and target.state == 'submitted':
                    target.action_return(comment)
                elif target and target.state == 'returned':
                    target.write({"return_reason": comment})
            elif instance.target_model == "purchase.order":
                target = self.env["purchase.order"].sudo().browse(instance.target_id).exists()
                if target and target.brew_approval_state == 'pending':
                    target.write({"brew_approval_state": "returned", "brew_approval_return_reason": comment})
            elif instance.target_model == "brew.purchase.return":
                target = self.env["brew.purchase.return"].sudo().browse(instance.target_id).exists()
                if target and target.state in ('submitted', 'returned'):
                    target.action_return(comment)
            elif instance.target_model == "brew.receipt":
                target = self.env["brew.receipt"].sudo().browse(instance.target_id).exists()
                if target and target.state == 'pending_approval':
                    target.write({"return_reason": comment})
            elif instance.target_model == "brew.quality.inspection":
                target = self.env["brew.quality.inspection"].sudo().browse(instance.target_id).exists()
                if target and target.state == "submitted":
                    target.action_return(comment)
        elif action == "transfer":
            target = self.env["res.users"].sudo().search([("id", "=", int(payload.get("user_id") or 0)), ("company_ids", "in", [self.company.id]), ("active", "=", True)], limit=1)
            if not target: raise ApiError("RESOURCE_NOT_FOUND", "目标用户不存在。", 404)
            if task.factory_id and task.factory_id not in target.brew_allowed_factories(self.company): raise ApiError("DATA_SCOPE_DENIED", "目标人员不属于任务厂区。", 403)
            step.action_transfer(self.user, target, payload.get("comment"))
        else: raise ApiError("RESOURCE_NOT_FOUND", "审批动作不存在。", 404)
        return step, self.serialize_approval(step)
