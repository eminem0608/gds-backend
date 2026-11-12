"""Day-based fermentation execution; records stay with the batch's task snapshot."""
from datetime import datetime, timedelta, timezone
import math
import operator
from zoneinfo import ZoneInfo

METRICS = {'temperature': ('温度', '℃'), 'gravity': ('糖度', '°P'), 'pressure': ('压力', 'bar'), 'diacetyl': ('双乙酰', 'mg/L'), 'attenuation': ('实际发酵度', '%')}
ACTIONS = {'measure_temperature': '测量温度', 'measure_gravity': '测量糖度', 'measure_pressure': '测量压力', 'measure_diacetyl': '测量双乙酰', 'seal': '封罐', 'dry_hop': '干投', 'cool_to': '降温', 'dump_hops': '排出酒花', 'centrifuge': '离心', 'increase_pressure': '升压', 'package': '出酒'}
COMPARE = {'=': operator.eq, '>': operator.gt, '>=': operator.ge, '<': operator.lt, '<=': operator.le}


def parse_time(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def day_date(start, day, timezone):
    local = start.replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo(timezone))
    return (local.date() + timedelta(days=day - 1)).isoformat()


def build_schedule(design, start, timezone='Asia/Shanghai'):
    days = int(design.get('target_days') or 1)
    if not 1 <= days <= 366:
        raise ValueError('目标发酵天数须为 1–366 天。')
    entries = []
    for index, rule in enumerate(design.get('steps') or []):
        if rule.get('action') not in ACTIONS:
            raise ValueError('发酵动作无效，请检查工艺配置。')
        if rule.get('condition') == 'time':
            mode = rule.get('schedule_type') or 'daily'
            number = int(rule.get('day_value') or 1) if mode == 'day' else int(rule.get('interval_days') or 1)
            if number < 1 or (mode == 'day' and number > days) or mode not in {'daily', 'day', 'interval'}:
                raise ValueError('时间规则必须在目标发酵天数范围内。')
            scheduled = [number] if mode == 'day' else range(1, days + 1) if mode == 'daily' else range(number, days + 1, number)
        else:
            if rule.get('condition') not in METRICS or rule.get('operator') not in COMPARE:
                raise ValueError('发酵触发条件无效。')
            if not math.isfinite(float(rule.get('condition_value'))):
                raise ValueError('条件阈值必须是有限数值。')
            scheduled = [None]
        for day in scheduled:
            entries.append({'id': f'{index}:{day or "condition"}', 'rule_index': index, 'day': day, 'date': day_date(start, day, timezone) if day else None, 'rule': dict(rule), 'state': 'pending' if day else 'waiting', 'trigger': None, 'record': None})
    return {'schema': 'fermentation-v1', 'started_at': start.isoformat(), 'timezone': timezone, 'target_days': days, 'entries': entries, 'records': []}


def extend_daily_schedule(data, now):
    """Keep routine monitoring alive until the operator actually closes the stage."""
    today = day_date(now, 1, data['timezone'])
    day = (datetime.fromisoformat(today).date() - datetime.fromisoformat(day_date(parse_time(data['started_at']), 1, data['timezone'])).date()).days + 1
    if day < data['target_days']:
        return
    existing = {entry['id'] for entry in data['entries']}
    rules = {entry['rule_index']: entry['rule'] for entry in data['entries'] if entry['rule'].get('condition') == 'time' and entry['rule'].get('schedule_type', 'daily') in {'daily', 'interval'}}
    for index, rule in rules.items():
        interval = int(rule.get('interval_days') or 1)
        for number in range(data['target_days'] + 1, day + 2):
            if rule.get('schedule_type') == 'interval' and number % interval:
                continue
            key = f'{index}:{number}'
            if key not in existing:
                data['entries'].append({'id': key, 'rule_index': index, 'day': number, 'date': day_date(parse_time(data['started_at']), number, data['timezone']), 'rule': dict(rule), 'state': 'pending', 'trigger': None, 'record': None})


def evaluate(data, now):
    today = day_date(now, 1, data['timezone'])
    latest = {}
    for record in sorted(data['records'], key=lambda item: (item['measured_at'], item['submitted_at'])):
        for key, value in record['metrics'].items():
            latest[key] = (value, record)
    records_by_id = {record['id']: record for record in data['records']}
    for entry in data['entries']:
        rule = entry['rule']
        metric = rule.get('condition')
        if entry['state'] == 'waiting' and metric in latest:
            value, record = latest[metric]
            if COMPARE[rule['operator']](value, float(rule['condition_value'])):
                entry.update(state='pending', date=day_date(parse_time(record['submitted_at']), 1, data['timezone']), trigger={'metric': metric, 'value': value, 'record_id': record['id'], 'at': record['submitted_at']})
        action_metric = rule['action'].removeprefix('measure_') if rule['action'].startswith('measure_') else None
        trigger = entry.get('trigger') or {}
        source = records_by_id.get(trigger.get('record_id'))
        # A measurement that already satisfies its own condition is also the
        # action's result; do not ask the operator to measure it a second time.
        if (not entry['record'] and entry['state'] == 'pending' and not trigger.get('manual')
                and action_metric == metric and source and metric in source['metrics']
                and COMPARE[rule['operator']](source['metrics'][metric], float(rule['condition_value']))):
            entry.update(state='done', record=source['id'])
        entry['status'] = 'done' if entry['record'] else 'waiting' if entry['state'] == 'waiting' else 'future' if entry['date'] > today else 'overdue' if entry['date'] < today else 'pending'
        entry['action_label'] = ACTIONS[rule['action']]
        entry['metric'] = action_metric
    data['today'] = today
    data['day'] = max(1, (datetime.fromisoformat(today).date() - datetime.fromisoformat(day_date(parse_time(data['started_at']), 1, data['timezone'])).date()).days + 1)
    data['can_complete'] = bool(data['entries']) and all(entry['status'] == 'done' for entry in data['entries'] if not (entry.get('day', 0) and entry['day'] > data['day'] and entry['day'] > data['target_days']))
    return data


def validate_metrics(values):
    result = {}
    for key, value in values.items():
        if key not in METRICS or isinstance(value, bool):
            raise ValueError('测量指标无效。')
        number = float(value)
        if not math.isfinite(number) or (key != 'temperature' and number < 0) or (key == 'attenuation' and number > 100):
            raise ValueError('请输入有效的测量值，发酵度范围为 0–100%。')
        result[key] = number
    return result
