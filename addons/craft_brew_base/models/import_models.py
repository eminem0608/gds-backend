import base64
import csv
import io
import json

from odoo import Command, api, fields, models
from odoo.exceptions import ValidationError


IMPORT_RESOURCES = [
    ("factories", "厂区"),
    ("equipment", "设备"),
    ("tanks", "罐体"),
    ("warehouses", "仓库"),
    ("locations", "库位"),
    ("products", "产品"),
    ("materials", "物料"),
    ("packaging-specs", "包装规格"),
    ("suppliers", "供应商"),
    ("customers", "客户"),
]


class BrewImportBatch(models.Model):
    _name = "brew.import.batch"
    _description = "Craft Brew Master Data Import Batch"
    _order = "create_date desc"
    _check_company_auto = True

    name = fields.Char(required=True)
    resource = fields.Selection(IMPORT_RESOURCES, required=True, index=True)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company, index=True
    )
    factory_id = fields.Many2one("brew.factory", check_company=True, index=True)
    created_by_id = fields.Many2one(
        "res.users", required=True, default=lambda self: self.env.user, check_company=True
    )
    state = fields.Selection(
        [
            ("uploaded", "已上传"),
            ("validated", "校验未通过"),
            ("ready", "待提交"),
            ("committed", "已提交"),
            ("failed", "提交失败"),
        ],
        required=True,
        default="uploaded",
        index=True,
    )
    file_name = fields.Char(required=True)
    file_data = fields.Binary(required=True, attachment=True)
    total_rows = fields.Integer(default=0)
    valid_rows = fields.Integer(default=0)
    error_rows = fields.Integer(default=0)
    report_json = fields.Text(default="{}")
    line_ids = fields.One2many("brew.import.line", "batch_id")
    committed_at = fields.Datetime()

    @api.constrains("file_name", "file_data")
    def _check_csv_file(self):
        for batch in self:
            if not (batch.file_name or "").lower().endswith(".csv"):
                raise ValidationError("V1 导入仅支持 CSV 文件。")
            if batch.file_data and len(base64.b64decode(batch.file_data)) > 5 * 1024 * 1024:
                raise ValidationError("导入文件不能超过 5MB。")

    def parse_and_validate(self, required_columns=None, allowed_columns=None):
        self.ensure_one()
        if self.state == "committed":
            raise ValidationError("已提交的导入批次不能重新校验。")
        raw = base64.b64decode(self.file_data or b"")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValidationError("CSV 必须使用 UTF-8 编码。") from exc
        reader = csv.DictReader(io.StringIO(text))
        headers = [item.strip() for item in (reader.fieldnames or []) if item]
        required_columns = set(required_columns or {"name"})
        allowed_columns = set(allowed_columns or headers)
        missing = sorted(required_columns - set(headers))
        unknown = sorted(set(headers) - allowed_columns)
        if missing or unknown:
            report = {"missing_columns": missing, "unknown_columns": unknown, "errors": []}
            self.write(
                {
                    "state": "validated",
                    "total_rows": 0,
                    "valid_rows": 0,
                    "error_rows": 0,
                    "report_json": json.dumps(report, ensure_ascii=False),
                    "line_ids": [Command.clear()],
                }
            )
            return report

        commands = [Command.clear()]
        errors = []
        total = 0
        valid = 0
        for row_number, source in enumerate(reader, start=2):
            total += 1
            if total > 5000:
                raise ValidationError("单个导入批次不能超过 5000 行。")
            data = {key.strip(): (value.strip() if isinstance(value, str) else value) for key, value in source.items() if key}
            row_errors = []
            for column in required_columns:
                if not data.get(column):
                    row_errors.append({"field": column, "message": "必填字段为空"})
            state = "valid" if not row_errors else "invalid"
            if state == "valid":
                valid += 1
            else:
                errors.append({"row": row_number, "errors": row_errors})
            commands.append(
                Command.create(
                    {
                        "row_number": row_number,
                        "data_json": json.dumps(data, ensure_ascii=False),
                        "state": state,
                        "error_json": json.dumps(row_errors, ensure_ascii=False),
                    }
                )
            )
        report = {
            "total_rows": total,
            "valid_rows": valid,
            "error_rows": total - valid,
            "errors": errors[:200],
            "errors_truncated": len(errors) > 200,
        }
        self.write(
            {
                "state": "ready" if total and total == valid else "validated",
                "total_rows": total,
                "valid_rows": valid,
                "error_rows": total - valid,
                "report_json": json.dumps(report, ensure_ascii=False),
                "line_ids": commands,
            }
        )
        return report


class BrewImportLine(models.Model):
    _name = "brew.import.line"
    _description = "Craft Brew Master Data Import Line"
    _order = "row_number"

    batch_id = fields.Many2one("brew.import.batch", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(
        "res.company", related="batch_id.company_id", store=True, index=True
    )
    row_number = fields.Integer(required=True)
    data_json = fields.Text(required=True)
    state = fields.Selection(
        [("valid", "有效"), ("invalid", "无效"), ("committed", "已提交")],
        required=True,
    )
    error_json = fields.Text(default="[]")
    target_model = fields.Char()
    target_id = fields.Integer()
