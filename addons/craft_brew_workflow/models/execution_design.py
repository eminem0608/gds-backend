"""Batch-scaled execution design; source recipe and completed records remain unchanged."""
import copy
import math
try:
    from .water_chemistry import water_volumes, scale_additions, stage_snapshot
except ImportError:  # Pure calculation tests also load this module without Odoo.
    import importlib.util
    from pathlib import Path
    _spec=importlib.util.spec_from_file_location('brew_water_chemistry',Path(__file__).with_name('water_chemistry.py'))
    _water=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_water)
    water_volumes,scale_additions,stage_snapshot=_water.water_volumes,_water.scale_additions,_water.stage_snapshot


def batch_design(snapshot, volume):
    design = copy.deepcopy(snapshot.get('design') or {})
    previous_water = water_volumes(design)
    source = float(design.get('brew_volume_l') or snapshot.get('base_volume_l') or volume or 0)
    factor = float(volume) / source if source > 0 else 1
    if abs(factor - 1) < 1e-9:
        return design
    for group in ('fermentables', 'hops', 'yeast'):
        for row in design.get(group) or []:
            row['quantity'] = round(float(row.get('quantity') or 0) * factor, 6)
    design['brew_volume_l'] = volume
    mash = design.setdefault('mash', {})
    grain = sum(float(r.get('quantity') or 0) for r in design.get('fermentables') or [])
    original = mash.get('water_calculation') == 'original'
    pre = volume * (1 + float(mash.get('evaporation_rate') or 0) / 100 * (1 if original else float(mash.get('boil_time_min') or 0) / 60))
    water = grain * float(mash.get('water_grain_ratio') or 0)
    first = water if original else max(0, water - grain * float(mash.get('absorption_rate') or 0) - float(mash.get('screen_water_l') or 0))
    sparge = max(0, pre - first)
    count = max(1, min(int(float(mash.get('sparge_count') or 1)), 10))
    previous = mash.get('sparge_water_liters') or []
    weights = [max(float(v or 0), 0) for v in previous] if len(previous) == count else []
    total = sum(weights)
    waters = [math.floor(sparge * (weights[i] / total if total else 1 / count) * 100) / 100 for i in range(count - 1)]
    waters.append(round(sparge - sum(waters), 2))
    mash['sparge_water_liters'] = waters
    scale_additions(design, previous_water)
    return design


def node_values(code, design):
    if code in {'water_mash','water_sparge'}:
        snapshot=stage_snapshot(design,'mash' if code=='water_mash' else 'sparge')
        return {'water_adjustment':snapshot} if snapshot else {}
    mash = design.get('mash') or {}
    workflow = mash.get('workflow') or {}
    if code == 'prepare_mash_water':
        qty = sum(float(r.get('quantity') or 0) for r in design.get('fermentables') or []) * float(mash.get('water_grain_ratio') or 0)
        return {'quantity': qty, 'unit': 'L', 'target': f'准备本批糖化水 {qty:g} L，投料后核对料液温度。'}
    if code == 'mash_in':
        rows = design.get('fermentables') or []
        return {'target': '投入：' + '；'.join(f"{r.get('name') or '麦芽'} {float(r.get('quantity') or 0):g} kg" for r in rows)} if rows else {}
    if code == 'prepare_sparge_water':
        qty = sum(float(v or 0) for v in mash.get('sparge_water_liters') or [])
        return {'target': f"准备洗糟水 {qty:g} L，目标 {float(workflow.get('sparge_temperature_c') or 78):g}℃。", **({'parallel_after': 'mash_in'} if workflow.get('execution_mode') != 'manual' else {})}
    if code.startswith('sparge_') and code[7:].isdigit():
        values = mash.get('sparge_water_liters') or []
        index = int(code[7:]) - 1
        return {'quantity': values[index], 'unit': 'L'} if 0 <= index < len(values) else {}
    for prefix in ('boil_hop_', 'fwh_', 'wp_hop_'):
        if code.startswith(prefix) and code[len(prefix):].isdigit():
            rows = design.get('hops') or []
            index = int(code[len(prefix):])
            return {'quantity': rows[index].get('quantity'), 'unit': 'g', 'material_id': str(rows[index].get('material_id') or '')} if index < len(rows) else {}
    if code.startswith('rest_') and code.endswith('_start') and code.split('_')[1].isdigit():
        index = int(code.split('_')[1]); steps = mash.get('steps') or []
        if index < len(steps):
            temperature = float(steps[index].get('temperature') or 0)
            verb = '降温' if index and temperature < float(steps[index - 1].get('temperature') or 0) else '调整温度'
            return {'title': f'{verb}至 {temperature:g}℃，开始保温', 'temperature': temperature, 'input_mode': 'number', 'unit': '℃'}
    return {}


def operation_highlights(node, design):
    """Presentation only: do not parse requirements from prose or change execution."""
    mash = design.get('mash') or {}
    workflow = mash.get('workflow') or {}
    code = node.get('code', '')
    result = []
    def add(label, value, unit=''):
        if value is not None and value != '':
            result.append({'label': label, 'value': value, 'unit': unit})
    if code in {'prepare_mash_water', 'prepare_sparge_water'}:
        if code == 'prepare_mash_water':
            grain = sum(float(r.get('quantity') or 0) for r in design.get('fermentables') or [])
            add('糖化用水', grain * float(mash.get('water_grain_ratio') or 0), 'L')
            steps = mash.get('steps') or []
            if steps:
                add('入料水温参考', float(steps[0].get('temperature') or 0) + float(workflow.get('preheat_offset_c') or 0), '℃')
        else:
            add('洗糟用水', sum(float(v or 0) for v in mash.get('sparge_water_liters') or []), 'L')
            add('目标水温', workflow.get('sparge_temperature_c', 78), '℃')
    elif code.startswith('rest_'):
        add('目标温度', node.get('temperature'), '℃')
        add('保持时间', node.get('duration_min', node.get('due_after_min')), '分钟')
    elif code.startswith('sparge_') and code[7:].isdigit():
        add('本次洗糟用水', node.get('quantity'), 'L')
        add('目标水温', workflow.get('sparge_temperature_c', 78), '℃')
    elif code in {'boil_start', 'boil_end'}:
        add('煮沸时长', mash.get('boil_time_min'), '分钟')
    elif code.startswith('boil_hop_'):
        add('煮沸开始后', node.get('due_after_min'), '分钟投放')
        add('距煮沸结束', node.get('boil_duration_min'), '分钟')
    elif code == 'whirlpool_hold_end':
        add('旋沉保持', node.get('due_after_min'), '分钟')
    elif code == 'cooling_target':
        add('目标冷却温度', workflow.get('cooling_temperature_c', 18), '℃')
    return result


def ready_mash_nodes(nodes):
    """Sequential main process, with explicitly anchored parallel preparation."""
    pending = [node for node in nodes if node.get('state') != 'done']
    if not pending:
        return []
    completed = {node['code'] for node in nodes if node.get('state') == 'done'}
    parallel = [node for node in pending if node.get('parallel_after')]
    main = next((node for node in pending if not node.get('parallel_after')), None)
    available_parallel = [node for node in parallel if node['parallel_after'] in completed]
    if main and main['code'].startswith('sparge_') and main['code'][7:].isdigit():
        # Physical sparging requires the prepared water, unlike temperature rests.
        if parallel:
            return available_parallel
    if not main:
        return available_parallel
    mains = [node for node in pending if node['code'].startswith('boil_hop_')] if main['code'].startswith('boil_hop_') else [main]
    return mains + available_parallel
