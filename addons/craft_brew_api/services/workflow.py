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
        return {"id": str(task.id), "name": name, "task_type": task.task_type, "role_code": task.role_code, "assignee": {"id": str(task.assignee_id.id), "name": task.assignee_id.name} if task.assignee_id else None, "priority": task.priority, "due_at": task.due_at.isoformat() if task.due_at else None, "state": task.state, "target_model": task.target_model, "target_id": str(task.target_id) if task.target_id else None, "last_comment": task.last_comment, "version": task.write_date.isoformat() if task.write_date else None}

    def task_action(self, task_id, action, payload):
        task = self._task(task_id)
        current = task.write_date.isoformat() if task.write_date else None
        if not payload.get("version") or payload["version"] != current: raise ApiError("VERSION_CONFLICT", "任务版本不一致。", 409, {"current_version": current})
        if action == "claim": task.action_claim(self.user)
        elif action == "start": task.action_start(self.user)
        elif action == "submit": task.action_submit(self.user, payload.get("comment"))
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
                return {"id": str(step.id), "instance_id": str(instance.id), "name": "采购订单审批 · " + order.display_name, "approval_type": instance.approval_type, "role_code": step.role_code, "sequence": step.sequence, "state": step.state, "instance_state": instance.state, "comment": step.comment or order.brew_approval_return_reason, "target_model": instance.target_model, "target_id": str(instance.target_id), "version": step.write_date.isoformat() if step.write_date else None}
        return {"id": str(step.id), "instance_id": str(instance.id), "name": instance.name, "approval_type": instance.approval_type, "role_code": step.role_code, "sequence": step.sequence, "state": step.state, "instance_state": instance.state, "comment": step.comment, "version": step.write_date.isoformat() if step.write_date else None}

    def approval_action(self, step_id, action, payload):
        step = self.env["brew.approval.step"].sudo().search([("id", "=", int(step_id)), ("company_id", "=", self.company.id)], limit=1)
        if not step or step.role_code not in self._role_codes(): raise ApiError("PERMISSION_DENIED", "无权处理该审批。", 403)
        if step.instance_id.state != "pending" or step.state != "pending" or step.sequence != step.instance_id.current_sequence:
            raise ApiError("STATE_PRECONDITION_FAILED", "该审批步骤已处理或不再处于当前审批环节。", 409)
        current = step.write_date.isoformat() if step.write_date else None
        if not payload.get("version") or payload["version"] != current: raise ApiError("VERSION_CONFLICT", "审批版本不一致。", 409, {"current_version": current})
        if action == "approve":
            stale = self.env["brew.approval.instance"].sudo().search([
                ("company_id", "=", self.company.id),
                ("target_model", "=", step.instance_id.target_model),
                ("target_id", "=", step.instance_id.target_id),
                ("state", "=", "approved"),
                ("id", "!=", step.instance_id.id),
            ])
            if stale:
                stale.write({"state": "cancelled", "completed_at": fields.Datetime.now()})
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
