import json
from datetime import datetime
import uuid
from zoneinfo import ZoneInfo

from .fermentation_tasks import build_schedule, evaluate, parse_time, validate_metrics, COMPARE

from odoo import fields
from odoo.addons.craft_brew_core.services.role_service import ROLE_DEFINITIONS

from ..controllers.base import ApiError


class WorkflowService:
    def __init__(self, env, user, company): self.env, self.user, self.company = env, user, company

    def _role_codes(self): return set(self.user.brew_role_codes())

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

    def list_tasks(self, page, page_size, state=None):
        # Repair returned versions created before submitter revision tasks existed.
        self._ensure_returned_recipe_tasks()
        model = self.env["brew.task"].sudo()
        domain = [("company_id", "=", self.company.id), "|", ("assignee_id", "=", self.user.id), "&", ("assignee_id", "=", False), ("role_code", "in", list(self._role_codes()))]
        if state: domain += [("state", "=", state)]
        total = model.search_count(domain)
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="priority desc,due_at,id")
        return [self.serialize_task(item) for item in records], total

    def _task(self, task_id):
        task = self.env["brew.task"].sudo().search([("id", "=", int(task_id)), ("company_id", "=", self.company.id)], limit=1)
        if not task: raise ApiError("RESOURCE_NOT_FOUND", "任务不存在。", 404)
        if task.assignee_id and task.assignee_id != self.user and not self.user.has_group("craft_brew_core.group_brew_production_director"): raise ApiError("PERMISSION_DENIED", "无权处理该任务。", 403)
        if not task.assignee_id and task.role_code not in self._role_codes(): raise ApiError("PERMISSION_DENIED", "任务角色与当前账号不匹配。", 403)
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
        if task.target_model == "brew.production.plan":
            plan = self.env["brew.production.plan"].sudo().browse(task.target_id).exists()
            if plan:
                plan_data = {"id": str(plan.id), "name": plan.name, "product": plan.product_tmpl_id.name, "planned_volume_l": plan.planned_volume_l, "state": plan.state}
                batch = self.env["brew.production.batch"].sudo().search([("production_plan_id", "=", plan.id)], limit=1)
                if batch: batch_data = {"id": str(batch.id), "name": batch.name}
                label = {"mashing": "糖化任务", "fermentation": "发酵任务", "material_preparation": "备料任务"}.get(task.task_type)
                if label: name = f"{plan.product_tmpl_id.name} · {label}"
                if plan.state not in {"dispatched", "in_progress"}:
                    blocked_reason = "等待生产计划下发"
                elif task.task_type == "fermentation":
                    mash = self.env["brew.task"].sudo().search([("target_model", "=", plan._name), ("target_id", "=", plan.id), ("task_type", "=", "mashing"), ("state", "!=", "cancelled")], limit=1)
                    if not mash or mash.state != "done": blocked_reason = "等待本批次糖化完成"

        return {"id": str(task.id), "is_assignee": task.assignee_id == self.user, "execution_blocked_reason": blocked_reason, "can_execute": task.assignee_id == self.user or self.user.has_group("craft_brew_core.group_brew_production_director") or self.user.has_group("craft_brew_core.group_brew_admin") or (not task.assignee_id and task.role_code in self._role_codes()), "plan": plan_data, "batch": batch_data, "name": name, "task_type": task.task_type, "role_code": task.role_code, "assignee": {"id": str(task.assignee_id.id), "name": task.assignee_id.name} if task.assignee_id else None, "priority": task.priority, "due_at": task.due_at.isoformat() if task.due_at else None, "state": task.state, "target_model": task.target_model, "target_id": str(task.target_id) if task.target_id else None, "last_comment": task.last_comment, "fermentation_execution": self.fermentation_execution(task), "execution_checkpoints": task.mash_execution_checkpoints(), "version": task.write_date.isoformat() if task.write_date else None}

    def fermentation_execution(self, task):
        if task.task_type != "fermentation":
            return None
        data = json.loads(task.execution_checkpoints_json or "[]")
        if not isinstance(data, dict) or data.get("schema") != "fermentation-v1":
            return {"legacy": task.state == "done", "started_at": None, "entries": [], "records": [], "can_complete": False}
        return evaluate(data, fields.Datetime.now())

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
            start = entered or parse_time(payload.get("started_at") or fields.Datetime.now())
            if start > fields.Datetime.now() or (task.create_date and start < task.create_date):
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
            measured = parse_time(payload.get('measured_at') or fields.Datetime.now())
            if not parse_time(data['started_at']) <= measured <= fields.Datetime.now():
                raise ValueError("测量时间须在发酵开始之后，且不能晚于当前时间。")
            condition = entry['rule']['condition']
            if condition not in metrics:
                raise ValueError("请填写触发条件对应的实测数据。")
            if not COMPARE[entry['rule']['operator']](metrics[condition], float(entry['rule']['condition_value'])):
                raise ValueError("实测数据尚未达到该任务的触发条件。")
            complete = bool(payload.get('complete_action'))
            if complete and entry['metric'] and entry['metric'] not in metrics:
                raise ValueError("请填写对应操作的测量结果。")
            if complete and not entry['metric'] and not payload.get('confirmed'):
                raise ValueError("请确认已实际完成对应工艺操作。")
        except (ValueError, TypeError, AttributeError) as exc:
            raise ApiError("VALIDATION_FORMAT", str(exc), 422) from exc
        now = fields.Datetime.now().isoformat()
        local_date = measured.replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo(data['timezone'])).date().isoformat()
        record = {'id': uuid.uuid4().hex, 'kind': 'condition', 'complete_action': complete, 'entry_ids': [entry['id']], 'metrics': metrics, 'measured_at': measured.isoformat(), 'submitted_at': now, 'by': self.user.name, 'user_id': str(self.user.id), 'note': note, 'backfill': local_date < data['today']}
        data['records'].append(record)
        entry.update(state='done' if complete else 'pending', date=data['today'], record=record['id'] if complete else None, trigger={'manual': True, 'by': self.user.name, 'note': note, 'metric': condition, 'value': metrics[condition], 'at': measured.isoformat(), 'record_id': record['id']})
        evaluate(data, fields.Datetime.now())
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
            measured = parse_time(payload.get('measured_at') or fields.Datetime.now())
            if measured < parse_time(data['started_at']):
                raise ValueError("填写日期不能早于发酵开始时间。")
            if measured > fields.Datetime.now() and not (entries and all(entry['day'] for entry in entries)):
                raise ValueError("补充测量和条件操作的时间不能晚于当前时间。")
            local_date = measured.replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo(data['timezone'])).date().isoformat()
            for entry in entries:
                if entry['metric'] and entry['metric'] not in metrics:
                    raise ValueError(f"请填写{entry['action_label']}的数值。")
                if entry['day'] and local_date != entry['date']:
                    raise ValueError("实际测量日期必须与所选任务日期一致，补录请填写当日实际测量时间。")
                if entry['trigger'] and measured < parse_time(entry['trigger']['at']):
                    raise ValueError("条件任务的执行时间不能早于触发时间。")
            if not entries and not metrics:
                raise ValueError("请填写测量数据或选择执行任务。")
            if any(not entry['metric'] for entry in entries) and not str(payload.get('note') or '').strip():
                raise ValueError("执行工艺动作时请填写操作结果及实际参数。")
        except (ValueError, TypeError, AttributeError) as exc:
            raise ApiError("VALIDATION_FORMAT", str(exc), 422) from exc
        record = {'id': uuid.uuid4().hex, 'entry_ids': ids, 'metrics': metrics, 'measured_at': measured.isoformat(), 'submitted_at': fields.Datetime.now().isoformat(), 'by': self.user.name, 'user_id': str(self.user.id), 'note': str(payload.get('note') or '').strip(), 'backfill': local_date < data['today']}
        data['records'].append(record)
        for entry in entries:
            entry.update(state='done', record=record['id'])
        evaluate(data, fields.Datetime.now())
        task.write({'execution_checkpoints_json': json.dumps(data, ensure_ascii=False)})
        return task

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
        previous = next((item for item in checkpoints if item.get("state") != "done"), None)
        if previous is not node:
            raise ApiError("STATE_PRECONDITION_FAILED", f"请先完成“{previous.get('title') if previous else '前序节点'}”。", 422)

        now = fields.Datetime.now()
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
                value = float(value)
            except (TypeError, ValueError):
                raise ApiError("VALIDATION_FORMAT", "该节点需要录入数值。", 422)

        node.update({"state": "done", "completed_at": now.isoformat(), "completed_by": self.user.name, "value": value})
        values = {"execution_checkpoints_json": json.dumps(checkpoints, ensure_ascii=False)}
        if task.state in {"pending", "claimed"}:
            values.update({"state": "in_progress", "started_at": task.started_at or now})
        task.write(values)
        return task

    def task_action(self, task_id, action, payload):
        task = self._task(task_id)
        self.env.cr.execute("SELECT id FROM brew_task WHERE id = %s FOR UPDATE", [task.id])
        task.invalidate_recordset()
        current = task.write_date.isoformat() if task.write_date else None
        if not payload.get("version") or payload["version"] != current: raise ApiError("VERSION_CONFLICT", "任务版本不一致。", 409, {"current_version": current})
        if task.target_model == "brew.production.plan" and action in {"claim", "start", "checkpoint", "submit", "fermentation-record", "fermentation-condition"}:
            plan = self.env["brew.production.plan"].sudo().browse(task.target_id).exists()
            if not plan or plan.state not in {"dispatched", "in_progress"}:
                raise ApiError("STATE_PRECONDITION_FAILED", "计划尚未下发，完成备货并下发任务后才能执行。", 422)
            if task.task_type == "fermentation":
                mash = self.env["brew.task"].sudo().search([("target_model", "=", plan._name), ("target_id", "=", plan.id), ("task_type", "=", "mashing"), ("state", "!=", "cancelled")], limit=1)
                if not mash or mash.state != "done":
                    raise ApiError("STATE_PRECONDITION_FAILED", "请先完成本批次糖化任务，再开始发酵。", 422)
            if task.task_type == "mashing" and action in {"start", "checkpoint"} and plan.state == "dispatched":
                batch = self.env["brew.production.batch"].sudo().search([("production_plan_id", "=", plan.id)], limit=1)
                if batch and batch.state == "draft": batch.action_start()
        if action == "claim": task.action_claim(self.user)
        elif action == "start":
            if task.task_type == "fermentation": self._start_fermentation(task, plan, payload)
            else: task.action_start(self.user)
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
                execution = self.fermentation_execution(task)
                if not execution or not execution.get('can_complete'):
                    raise ApiError("STATE_PRECONDITION_FAILED", "请先完成全部按天任务和条件任务，再结束发酵。", 422)
                if not payload.get('confirmed'):
                    raise ApiError("VALIDATION_REQUIRED", "请确认已完成发酵及必要的出酒或转罐检查。", 422)
            if task.task_type == "mashing":
                checkpoints = task.mash_execution_checkpoints()
                incomplete = next((node for node in checkpoints if node.get("required", True) and node.get("state") != "done"), None)
                if incomplete:
                    raise ApiError("STATE_PRECONDITION_FAILED", f"请先完成“{incomplete.get('title')}”。", 422)
            task.action_submit(self.user, payload.get("comment"))
        elif action == "return": task.action_return(self.user, payload.get("comment"))
        elif action == "reassign":
            if not self.user.has_group("craft_brew_core.group_brew_production_director"): raise ApiError("PERMISSION_DENIED", "只有生产总监可以转派任务。", 403)
            target = self.env["res.users"].sudo().search([("id", "=", int(payload.get("user_id") or 0)), ("company_ids", "in", [self.company.id]), ("active", "=", True)], limit=1)
            if not target: raise ApiError("RESOURCE_NOT_FOUND", "目标用户不存在。", 404)
            task.action_reassign(target, payload.get("comment"))
        else: raise ApiError("RESOURCE_NOT_FOUND", "任务动作不存在。", 404)
        return task, self.serialize_task(task)

    def list_approvals(self, page, page_size, state=None):
        role_codes = list(self._role_codes())
        model = self.env["brew.approval.step"].sudo()
        domain = [("company_id", "=", self.company.id), ("role_code", "in", role_codes), "|", ("assignee_id", "=", False), ("assignee_id", "=", self.user.id)]
        if state: domain += [("state", "=", state)]
        records = model.search(domain, offset=(page - 1) * page_size, limit=page_size, order="instance_id desc,sequence")
        current = []
        for item in records:
            instance = item.instance_id
            if instance.target_model == "brew.production.plan":
                continue
            target = self.env[instance.target_model].sudo().browse(instance.target_id).exists() if instance.target_model in self.env else None
            current_instance = target.approval_instance_id if target and "approval_instance_id" in target._fields else target.brew_approval_instance_id if target and "brew_approval_instance_id" in target._fields else instance
            if current_instance == instance:
                current.append(item)
        return [self.serialize_approval(item) for item in current], len(current)

    def serialize_approval(self, step):
        instance = step.instance_id
        if instance.target_model == 'brew.purchase.requisition':
            requisition = self.env[instance.target_model].sudo().browse(instance.target_id).exists()
            if requisition:
                return {'id': str(step.id), 'instance_id': str(instance.id), 'name': '采购需求审批 · ' + requisition.display_name, 'approval_type': instance.approval_type, 'request_type': requisition.request_type, 'role_code': step.role_code, 'sequence': step.sequence, 'state': step.state, 'instance_state': instance.state, 'comment': step.comment, 'target_model': instance.target_model, 'target_id': str(instance.target_id), 'version': step.write_date.isoformat() if step.write_date else None}
        if instance.target_model == "purchase.order":
            order = self.env["purchase.order"].sudo().browse(instance.target_id).exists()
            if order:
                return {"id": str(step.id), "instance_id": str(instance.id), "name": "采购订单审批 · " + order.display_name, "approval_type": instance.approval_type, "role_code": step.role_code, "sequence": step.sequence, "state": step.state, "instance_state": instance.state, "comment": step.comment or instance.submission_comment, "target_model": instance.target_model, "target_id": str(instance.target_id), "version": step.write_date.isoformat() if step.write_date else None}
        return {"id": str(step.id), "instance_id": str(instance.id), "name": instance.name, "approval_type": instance.approval_type, "role_code": step.role_code, "sequence": step.sequence, "state": step.state, "instance_state": instance.state, "comment": step.comment, "version": step.write_date.isoformat() if step.write_date else None}

    def approval_action(self, step_id, action, payload):
        step = self.env["brew.approval.step"].sudo().search([("id", "=", int(step_id)), ("company_id", "=", self.company.id)], limit=1)
        if not step or step.role_code not in self._role_codes(): raise ApiError("PERMISSION_DENIED", "无权处理该审批。", 403)
        if step.instance_id.state != "pending" or step.state != "pending" or step.sequence != step.instance_id.current_sequence:
            raise ApiError("STATE_PRECONDITION_FAILED", "该审批步骤已处理或不再处于当前审批环节。", 409)
        current = step.write_date.isoformat() if step.write_date else None
        if not payload.get("version") or payload["version"] != current: raise ApiError("VERSION_CONFLICT", "审批版本不一致。", 409, {"current_version": current})
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
        elif action == "transfer":
            target = self.env["res.users"].sudo().search([("id", "=", int(payload.get("user_id") or 0)), ("company_ids", "in", [self.company.id]), ("active", "=", True)], limit=1)
            if not target: raise ApiError("RESOURCE_NOT_FOUND", "目标用户不存在。", 404)
            step.action_transfer(self.user, target, payload.get("comment"))
        else: raise ApiError("RESOURCE_NOT_FOUND", "审批动作不存在。", 404)
        return step, self.serialize_approval(step)
