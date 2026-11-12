"""Completed responsibilities shown in personal execution history."""
HISTORY_ACTIONS = {
    'approval': {'approve', 'return'},
    'purchase_order': {'place'},
    'purchase_receipt': {'confirm-arrival', 'put-away', 'complete'},
    'quality_inspection': {'complete'},
    'purchase_return': {'execute'},
    'material_requisition': {'issue', 'acknowledge', 'return', 'complete', 'settle'},
    'inventory_count': {'confirm'},
    'inventory_transfer': {'confirm'},
    'inventory_scrap': {'confirm'},
}

def is_task_result(event_type, action):
    return action in HISTORY_ACTIONS.get(event_type, set())
