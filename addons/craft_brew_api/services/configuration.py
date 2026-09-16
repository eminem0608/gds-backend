import json

from odoo import fields

from ..controllers.base import ApiError


RULE_PATHS = {
    "number-rules": "number",
    "barcode-rules": "barcode",
    "alert-rules": "alert",
    "approval-rules": "approval",
}


class ConfigurationRuleService:
    def __init__(self, env, user, company):
        self.env = env
        self.user = user
        self.company = company
        if not user.has_group("craft_brew_core.group_brew_admin"):
            raise ApiError("PERMISSION_DENIED", "只有租户管理员可以维护业务规则。", 403)

    def _kind(self, resource):
        kind = RULE_PATHS.get(resource)
        if not kind:
            raise ApiError("RESOURCE_NOT_FOUND", "规则资源不存在。", 404)
        return kind

    def _domain(self, resource):
        return [("company_id", "=", self.company.id), ("rule_kind", "=", self._kind(resource))]

    def _record(self, resource, record_id):
        record = self.env["brew.configuration.rule"].sudo().with_context(active_test=False).search(
            self._domain(resource) + [("id", "=", int(record_id))], limit=1
        )
        if not record:
            raise ApiError("RESOURCE_NOT_FOUND", "规则不存在。", 404)
        return record

    def serialize(self, rule):
        return {
            "id": str(rule.id),
            "name": rule.name,
            "code": rule.code,
            "rule_kind": rule.rule_kind,
            "company_id": str(rule.company_id.id),
            "factory_id": str(rule.factory_id.id) if rule.factory_id else None,
            "version": rule.version,
            "state": rule.state,
            "effective_at": rule.effective_at.isoformat() if rule.effective_at else None,
            "config": json.loads(rule.config_json or "{}"),
            "record_version": rule.write_date.isoformat() if rule.write_date else None,
        }

    def list(self, resource, page, page_size, state=None):
        model = self.env["brew.configuration.rule"].sudo().with_context(active_test=False)
        domain = self._domain(resource)
        if state:
            domain += [("state", "=", state)]
        total = model.search_count(domain)
        records = model.search(
            domain,
            offset=(page - 1) * page_size,
            limit=page_size,
            order="code, version desc, id desc",
        )
        return [self.serialize(item) for item in records], total

    def _factory_id(self, value):
        if not value:
            return False
        factory = self.env["brew.factory"].sudo().search(
            [("id", "=", int(value)), ("company_id", "=", self.company.id), ("active", "=", True)],
            limit=1,
        )
        if not factory:
            raise ApiError("DATA_SCOPE_DENIED", "规则厂区不属于当前企业或已停用。", 403)
        return factory.id

    def create(self, resource, payload):
        name = str(payload.get("name") or "").strip()
        code = str(payload.get("code") or "").strip()
        config = payload.get("config")
        if not name or not code or not isinstance(config, dict):
            raise ApiError("VALIDATION_REQUIRED", "规则名称、编码和配置对象不能为空。", 422)
        factory_id = self._factory_id(payload.get("factory_id"))
        model = self.env["brew.configuration.rule"].sudo()
        previous = model.search(
            self._domain(resource) + [("factory_id", "=", factory_id), ("code", "=", code)],
            order="version desc",
            limit=1,
        )
        rule = model.create(
            {
                "name": name,
                "code": code,
                "rule_kind": self._kind(resource),
                "company_id": self.company.id,
                "factory_id": factory_id,
                "version": previous.version + 1 if previous else 1,
                "config_json": json.dumps(config, ensure_ascii=False, sort_keys=True),
            }
        )
        return rule, self.serialize(rule)

    def update(self, resource, record_id, payload):
        source = self._record(resource, record_id)
        expected = payload.get("record_version")
        current = source.write_date.isoformat() if source.write_date else None
        if not expected or expected != current:
            raise ApiError("VERSION_CONFLICT", "数据版本不一致，请刷新后重试。", 409, {"current_version": current})
        if source.state != "draft":
            raise ApiError("STATE_ACTION_NOT_ALLOWED", "已生效或停用的规则不能覆盖修改，请创建新版本。", 422)
        values = {}
        if "name" in payload:
            values["name"] = str(payload.get("name") or "").strip()
        if "config" in payload:
            if not isinstance(payload["config"], dict):
                raise ApiError("VALIDATION_FORMAT", "规则配置必须是 JSON 对象。", 422)
            values["config_json"] = json.dumps(payload["config"], ensure_ascii=False, sort_keys=True)
        if "factory_id" in payload:
            values["factory_id"] = self._factory_id(payload.get("factory_id"))
        if not values:
            raise ApiError("VALIDATION_REQUIRED", "没有可更新的字段。", 422)
        source.write(values)
        return source, self.serialize(source)

    def action(self, resource, record_id, action, record_version):
        rule = self._record(resource, record_id)
        current = rule.write_date.isoformat() if rule.write_date else None
        if not record_version or record_version != current:
            raise ApiError("VERSION_CONFLICT", "数据版本不一致，请刷新后重试。", 409, {"current_version": current})
        if action == "activate":
            rule.action_activate()
        elif action == "deactivate":
            rule.action_deactivate()
        else:
            raise ApiError("RESOURCE_NOT_FOUND", "规则动作不存在。", 404)
        return rule, self.serialize(rule)
