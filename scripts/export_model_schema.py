#!/usr/bin/env python3
"""Export the logical database schema declared by the local Odoo models."""

from __future__ import annotations

import argparse
import ast
import csv
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path


RELATION_FIELDS = {"Many2one", "One2many", "Many2many"}
NO_LOCAL_COLUMN = {"One2many", "Many2many"}
SQL_TYPES = {
    "Boolean": "boolean",
    "Integer": "integer",
    "Float": "numeric",
    "Monetary": "numeric",
    "Char": "varchar",
    "Selection": "varchar",
    "Text": "text",
    "Html": "text",
    "Date": "date",
    "Datetime": "timestamp without time zone",
    "Binary": "bytea / ir_attachment",
    "Json": "jsonb",
    "Many2one": "bigint",
    "Reference": "varchar",
}


def literal(node, default=None):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return default


def expression(node):
    if node is None:
        return ""
    try:
        value = ast.unparse(node)
    except Exception:
        return ""
    return value if len(value) <= 100 else value[:97] + "..."


def assignment_map(class_node):
    result = {}
    for statement in class_node.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    result[target.id] = statement.value
    return result


def model_base(class_node):
    for base in class_node.bases:
        if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            if base.value.id == "models":
                return base.attr
    return ""


def parse_field(name, call, module, source):
    field_type = call.func.attr
    keywords = {item.arg: item.value for item in call.keywords if item.arg}
    relation = ""
    inverse = ""
    relation_table = ""
    label = literal(keywords.get("string"), "")
    if field_type in RELATION_FIELDS:
        relation = literal(call.args[0], "") if call.args else ""
        if field_type == "One2many" and len(call.args) > 1:
            inverse = literal(call.args[1], "")
        if field_type == "Many2many" and len(call.args) > 1:
            relation_table = literal(call.args[1], "")
    elif call.args:
        label = label or literal(call.args[0], "")
    selection_node = keywords.get("selection")
    if field_type == "Selection":
        selection_node = selection_node or (call.args[0] if call.args else None)
    selection_value = literal(selection_node)
    if isinstance(selection_value, (list, tuple)):
        selection = ", ".join(
            str(item[0])
            for item in selection_value
            if isinstance(item, (list, tuple)) and item
        )
    elif selection_node is not None:
        selection = expression(selection_node)
    else:
        selection = ""
    related_value = literal(keywords.get("related"), "")
    if isinstance(related_value, (list, tuple)):
        related_value = ".".join(related_value)
    compute = literal(keywords.get("compute"), "") or expression(keywords.get("compute"))
    stored = literal(keywords.get("store"), False) is True
    attachment = literal(keywords.get("attachment"), False) is True
    has_column = (
        field_type not in NO_LOCAL_COLUMN
        and not ((related_value or compute) and not stored)
        and not (field_type == "Binary" and attachment)
    )
    return {
        "name": name,
        "type": field_type,
        "sql_type": SQL_TYPES.get(field_type, field_type.lower()),
        "column": name if has_column else "",
        "relation": relation,
        "inverse": inverse,
        "relation_table": relation_table,
        "required": literal(keywords.get("required"), False) is True,
        "index": literal(keywords.get("index"), False) is True,
        "ondelete": literal(keywords.get("ondelete"), ""),
        "related": related_value,
        "stored": stored,
        "compute": compute,
        "attachment": attachment,
        "default": expression(keywords.get("default")),
        "selection": selection,
        "label": label,
        "module": module,
        "source": source,
    }


def parse_models(addons_dir):
    records = []
    for path in sorted(addons_dir.glob("craft_brew_*/models/*.py")):
        if path.name == "__init__.py":
            continue
        module = path.parents[1].name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            base = model_base(node)
            if base not in {"Model", "AbstractModel", "TransientModel"}:
                continue
            assignments = assignment_map(node)
            model_name = literal(assignments.get("_name"), "")
            inherited = literal(assignments.get("_inherit"), "")
            if isinstance(inherited, str):
                inherits = [inherited]
            elif isinstance(inherited, (list, tuple)):
                inherits = list(inherited)
            else:
                inherits = []
            source = str(path.relative_to(addons_dir.parent)).replace("\\", "/")
            fields = []
            for field_name, value in assignments.items():
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute)
                    and isinstance(value.func.value, ast.Name)
                    and value.func.value.id == "fields"
                ):
                    fields.append(parse_field(field_name, value, module, source))
            constraints = literal(assignments.get("_sql_constraints"), []) or []
            records.append(
                {
                    "class": node.name,
                    "base": base,
                    "abstract": base == "AbstractModel",
                    "transient": base == "TransientModel",
                    "model": model_name,
                    "inherits": inherits,
                    "description": literal(assignments.get("_description"), ""),
                    "table": literal(assignments.get("_table"), "")
                    or (model_name.replace(".", "_") if model_name and base != "AbstractModel" else ""),
                    "module": module,
                    "source": source,
                    "fields": fields,
                    "constraints": [
                        {"name": item[0], "sql": item[1], "message": item[2]}
                        for item in constraints
                        if isinstance(item, (list, tuple)) and len(item) >= 3
                    ],
                }
            )
    return records


def consolidate(records):
    models = {}
    extensions = []
    mixins = {}
    for record in records:
        if record["abstract"] and record["model"]:
            mixins[record["model"]] = record
        elif record["model"]:
            models[record["model"]] = {
                **record,
                "fields": list(record["fields"]),
                "constraints": list(record["constraints"]),
                "sources": [record["source"]],
            }
        elif record["inherits"]:
            extensions.append(record)

    standard_extensions = defaultdict(
        lambda: {"fields": [], "sources": [], "modules": set()}
    )
    for extension in extensions:
        for target in extension["inherits"]:
            if target in models:
                models[target]["fields"].extend(extension["fields"])
                models[target]["constraints"].extend(extension["constraints"])
                models[target]["sources"].append(extension["source"])
            else:
                item = standard_extensions[target]
                item["fields"].extend(extension["fields"])
                item["sources"].append(extension["source"])
                item["modules"].add(extension["module"])

    for model in models.values():
        for inherited in model["inherits"]:
            if inherited in mixins:
                inherited_fields = []
                for field in mixins[inherited]["fields"]:
                    copied = dict(field)
                    copied["inherited_from"] = inherited
                    inherited_fields.append(copied)
                model["fields"] = inherited_fields + model["fields"]

    for item in standard_extensions.values():
        item["modules"] = sorted(item["modules"])
        item["sources"] = sorted(set(item["sources"]))
    return models, dict(standard_extensions), mixins


def field_row(model_name, table, field):
    return {
        "model": model_name,
        "table": table,
        "field": field["name"],
        "column": field["column"],
        "field_type": field["type"],
        "sql_type": field["sql_type"],
        "relation": field["relation"],
        "inverse": field["inverse"],
        "relation_table": field["relation_table"],
        "required": field["required"],
        "index": field["index"],
        "ondelete": field["ondelete"],
        "related": field["related"],
        "stored": field["stored"],
        "attachment": field["attachment"],
        "compute": field["compute"],
        "selection": field["selection"],
        "module": field["module"],
        "source": field["source"],
    }


def write_csv(path, models, standard_extensions):
    rows = []
    for name, model in sorted(models.items()):
        rows.extend(field_row(name, model["table"], field) for field in model["fields"])
    for name, extension in sorted(standard_extensions.items()):
        rows.extend(
            field_row(name, name.replace(".", "_"), field)
            for field in extension["fields"]
        )
    columns = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def bool_mark(value):
    return "Y" if value else ""


def escape(value):
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def write_markdown(path, models, standard_extensions, mixins, field_count):
    module_counts = Counter(model["module"] for model in models.values())
    lines = [
        "# 精酿啤酒 SaaS 数据库结构导出",
        "",
        f"> 导出日期：{date.today().isoformat()}  ",
        "> 来源：gds-backend/addons 下的 Odoo 模型静态解析  ",
        "> 性质：逻辑模型结构，不是已安装 PostgreSQL 实例的物理 DDL",
        "",
        "## 说明",
        "",
        "- Odoo 模型名默认映射为同名下划线表，例如 brew.production.plan 对应 brew_production_plan。",
        "- 每个普通 Odoo 表还会由框架加入 id、create_uid、create_date、write_uid、write_date 等系统列。",
        "- One2many 不产生本表列；Many2many 使用关系表；Binary 使用 attachment=True 时数据主体存放在 ir_attachment/filestore。",
        "- related/compute 字段只有 store=True 时才产生物理列。",
        "- Odoo 标准模块自身的完整表结构未复制到本文，只列出本项目追加的字段。",
        "",
        "## 汇总",
        "",
        f"- 自定义持久模型：{len(models)}",
        f"- 标准模型扩展：{len(standard_extensions)}",
        f"- 抽象 Mixin：{len(mixins)}",
        f"- 声明字段：{field_count}",
        "",
        "| 模块 | 自定义模型数 |",
        "|---|---:|",
    ]
    lines.extend(
        f"| {module} | {count} |" for module, count in sorted(module_counts.items())
    )
    lines.extend(["", "## 标准模型扩展", ""])
    for name, item in sorted(standard_extensions.items()):
        lines.extend(
            [
                f"### {name}（表 {name.replace('.', '_')}）",
                "",
                f"来源模块：{', '.join(item['modules'])}",
                "",
                "| 字段 | 列 | 类型 | 关联 | 必填 | 索引 | Related/计算 |",
                "|---|---|---|---|:---:|:---:|---|",
            ]
        )
        for field in sorted(item["fields"], key=lambda value: value["name"]):
            related = field["related"] or field["compute"]
            lines.append(
                f"| {escape(field['name'])} | {escape(field['column'] or '-')} | "
                f"{escape(field['type'])} / {escape(field['sql_type'])} | "
                f"{escape(field['relation'] or '-')} | {bool_mark(field['required'])} | "
                f"{bool_mark(field['index'])} | {escape(related or '-')} |"
            )
        lines.append("")

    lines.extend(["## 自定义业务模型", ""])
    grouped = defaultdict(list)
    for model in models.values():
        grouped[model["module"]].append(model)
    for module in sorted(grouped):
        lines.extend([f"### 模块 {module}", ""])
        for model in sorted(grouped[module], key=lambda value: value["model"]):
            inherit_note = (
                f"；继承 {', '.join(model['inherits'])}" if model["inherits"] else ""
            )
            lines.extend(
                [
                    f"#### {model['model']}（表 {model['table']}）",
                    "",
                    f"{model['description'] or '未声明模型说明'}{inherit_note}",
                    "",
                    "| 字段 | 数据库列 | Odoo/SQL 类型 | 关联模型 | 必填 | 索引 | 其他 |",
                    "|---|---|---|---|:---:|:---:|---|",
                ]
            )
            for field in sorted(model["fields"], key=lambda value: value["name"]):
                extras = []
                if field["inverse"]:
                    extras.append(f"inverse={field['inverse']}")
                if field["relation_table"]:
                    extras.append(f"rel={field['relation_table']}")
                if field["related"]:
                    extras.append(f"related={field['related']}")
                if field["compute"]:
                    extras.append(f"compute={field['compute']}")
                if field["selection"]:
                    extras.append(f"选项={field['selection']}")
                if field.get("inherited_from"):
                    extras.append(f"来自 {field['inherited_from']}")
                lines.append(
                    f"| {escape(field['name'])} | {escape(field['column'] or '-')} | "
                    f"{escape(field['type'])} / {escape(field['sql_type'])} | "
                    f"{escape(field['relation'] or '-')} | {bool_mark(field['required'])} | "
                    f"{bool_mark(field['index'])} | {escape('; '.join(extras) or '-')} |"
                )
            if model["constraints"]:
                lines.extend(["", "约束："])
                for constraint in model["constraints"]:
                    lines.append(
                        f"- {constraint['name']}: {constraint['sql']}（{constraint['message']}）"
                    )
            lines.append("")

    lines.extend(
        [
            "## 物理结构导出",
            "",
            "安装并升级全部模块后，在已配置 pg_dump 和 PGPASSWORD 的环境执行：",
            "",
            "~~~powershell",
            ".\\scripts\\export_postgres_schema.ps1 -Database <数据库名>",
            "~~~",
            "",
            "物理导出将包含 Odoo 标准表、自动生成的关系表、实际索引、序列、外键和约束，应作为部署后的最终数据库结构依据。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--addons",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "addons",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "docs" / "database",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    records = parse_models(args.addons.resolve())
    models, standard_extensions, mixins = consolidate(records)
    csv_path = args.output_dir / "craft-brew-schema-fields.csv"
    json_path = args.output_dir / "craft-brew-schema.json"
    md_path = args.output_dir / "craft-brew-schema.md"
    field_count = write_csv(csv_path, models, standard_extensions)
    payload = {
        "generated_at": date.today().isoformat(),
        "source": "Odoo model static analysis",
        "custom_models": models,
        "standard_extensions": standard_extensions,
        "abstract_mixins": mixins,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_markdown(md_path, models, standard_extensions, mixins, field_count)
    print(
        f"Exported {len(models)} custom models, {len(standard_extensions)} "
        f"standard extensions and {field_count} fields to {args.output_dir}"
    )


if __name__ == "__main__":
    main()
