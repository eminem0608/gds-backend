import json

from odoo import fields

from ..controllers.base import ApiError
from .master_data import MasterDataService, RESOURCE_CONFIG


IMPORT_COLUMNS = {
    "factories": ["name", "code", "address", "manager_id"],
    "equipment": ["name", "brew_factory_id", "brew_equipment_type", "brew_capacity_l", "brew_status"],
    "tanks": ["name", "factory_id", "maintenance_equipment_id", "tank_type", "capacity_l", "cleaning_status"],
    "warehouses": ["name", "code", "brew_factory_id"],
    "locations": ["name", "brew_factory_id", "brew_location_type", "location_id", "usage"],
    "products": ["name", "default_code", "brew_item_type", "brew_brand", "brew_specification", "uom_id", "uom_po_id", "tracking", "standard_price"],
    "materials": ["name", "default_code", "brew_item_type", "brew_material_category", "brew_brand", "brew_specification", "uom_id", "uom_po_id", "tracking", "standard_price", "brew_shelf_life_days", "brew_extract_rate", "brew_color_ebc", "brew_alpha_acid", "brew_flavor_desc"],
    "packaging-specs": ["name", "product_id", "qty", "barcode", "brew_barcode_level", "brew_volume_l", "brew_tare_weight_kg"],
    "suppliers": ["name", "phone", "email", "street", "street2", "city", "brew_supply_category"],
    "customers": ["name", "phone", "email", "street", "street2", "city", "brew_customer_level", "user_id"],
}

NUMERIC_FIELDS = {
    "manager_id",
    "brew_factory_id",
    "factory_id",
    "maintenance_equipment_id",
    "capacity_l",
    "location_id",
    "uom_id",
    "uom_po_id",
    "standard_price",
    "brew_shelf_life_days",
    "brew_extract_rate",
    "brew_color_ebc",
    "brew_alpha_acid",
    "product_id",
    "qty",
    "brew_volume_l",
    "brew_tare_weight_kg",
    "user_id",
}


class ImportService:
    def __init__(self, env, user, company):
        self.env = env
        self.user = user
        self.company = company
        if not user.has_group("craft_brew_core.group_brew_admin"):
            raise ApiError("PERMISSION_DENIED", "只有租户管理员可以执行主数据导入。", 403)

    def _batch(self, batch_id):
        batch = self.env["brew.import.batch"].sudo().search(
            [("id", "=", int(batch_id)), ("company_id", "=", self.company.id)], limit=1
        )
        if not batch:
            raise ApiError("RESOURCE_NOT_FOUND", "导入批次不存在。", 404)
        return batch

    def serialize(self, batch):
        return {
            "id": str(batch.id),
            "name": batch.name,
            "resource": batch.resource,
            "state": batch.state,
            "file_name": batch.file_name,
            "total_rows": batch.total_rows,
            "valid_rows": batch.valid_rows,
            "error_rows": batch.error_rows,
            "created_by": {"id": str(batch.created_by_id.id), "name": batch.created_by_id.name},
            "created_at": batch.create_date.isoformat() if batch.create_date else None,
            "committed_at": batch.committed_at.isoformat() if batch.committed_at else None,
            "version": batch.write_date.isoformat() if batch.write_date else None,
        }

    def template(self, resource):
        if resource not in IMPORT_COLUMNS:
            raise ApiError("VALIDATION_FORMAT", "不支持该导入资源。", 422)
        return {"resource": resource, "format": "csv", "encoding": "utf-8-sig", "columns": IMPORT_COLUMNS[resource], "required_columns": ["name"], "max_rows": 5000, "max_bytes": 5 * 1024 * 1024}

    def create(self, payload):
        resource = payload.get("resource")
        if resource not in RESOURCE_CONFIG:
            raise ApiError("VALIDATION_FORMAT", "不支持该导入资源。", 422)
        file_name = str(payload.get("file_name") or "").strip()
        file_data = payload.get("file_data")
        if not file_name or not file_data:
            raise ApiError("VALIDATION_REQUIRED", "文件名和 CSV 内容不能为空。", 422)
        batch = self.env["brew.import.batch"].sudo().create(
            {
                "name": str(payload.get("name") or file_name),
                "resource": resource,
                "company_id": self.company.id,
                "created_by_id": self.user.id,
                "file_name": file_name,
                "file_data": file_data,
            }
        )
        return batch, self.serialize(batch)

    def validate(self, batch_id, version):
        batch = self._batch(batch_id)
        self._check_version(batch, version)
        try:
            report = batch.parse_and_validate(
                required_columns={"name"}, allowed_columns=set(IMPORT_COLUMNS[batch.resource])
            )
        except Exception as exc:
            if isinstance(exc, ApiError):
                raise
            raise ApiError("IMPORT_PARSE_FAILED", str(exc), 422) from exc
        return batch, {**self.serialize(batch), "report": report}

    def report(self, batch_id):
        batch = self._batch(batch_id)
        return {**self.serialize(batch), "report": json.loads(batch.report_json or "{}")}

    def _coerce(self, data):
        result = {}
        for key, value in data.items():
            if value in (None, ""):
                continue
            if key in NUMERIC_FIELDS:
                try:
                    number = float(value)
                except (TypeError, ValueError) as exc:
                    raise ApiError("VALIDATION_FORMAT", f"字段 {key} 必须是数字。", 422) from exc
                result[key] = int(number) if number.is_integer() else number
            else:
                result[key] = value
        return result

    def commit(self, batch_id, version):
        batch = self._batch(batch_id)
        self._check_version(batch, version)
        if batch.state != "ready" or batch.error_rows:
            raise ApiError("STATE_PRECONDITION_FAILED", "导入批次尚未通过预校验。", 422)
        master = MasterDataService(self.env, self.user, self.company)
        created = []
        for line in batch.line_ids.sorted("row_number"):
            payload = self._coerce(json.loads(line.data_json or "{}"))
            record, _data = master.create(batch.resource, payload)
            line.write(
                {
                    "state": "committed",
                    "target_model": RESOURCE_CONFIG[batch.resource]["model"],
                    "target_id": record.id,
                }
            )
            created.append(str(record.id))
        batch.write({"state": "committed", "committed_at": fields.Datetime.now()})
        return batch, {**self.serialize(batch), "created_ids": created}

    def _check_version(self, record, expected):
        current = record.write_date.isoformat() if record.write_date else None
        if not expected or expected != current:
            raise ApiError("VERSION_CONFLICT", "数据版本不一致，请刷新后重试。", 409, {"current_version": current})
