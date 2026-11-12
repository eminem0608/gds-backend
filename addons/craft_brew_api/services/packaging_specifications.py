"""Type-specific packaging specifications shared by catalogue forms and validation."""
ACTIVE_TYPES = {'bottle', 'can', 'keg', 'carton'}
SIMPLE_FIELDS = {
    'bottle': ['capacity', 'material', 'color', 'height_mm', 'diameter_mm'],
    'can': ['capacity', 'material', 'height_mm', 'diameter_mm'],
    'keg': ['capacity', 'material'],
    'carton': ['length_mm', 'width_mm', 'height_mm', 'pack_count', 'fit_container_type', 'fit_capacity_ml'],
}
FIELDS = {
    'capacity': ('容量', 'capacity', '', []),
    'material': ('材质', 'text', '', []),
    'closure': ('瓶口 / 盖口 / 接口规格', 'text', '', []),
    'color': ('颜色', 'text', '', []),
    'height_mm': ('高度', 'number', 'mm', []),
    'diameter_mm': ('瓶身 / 罐身 / 桶身直径', 'number', 'mm', []),
    'mouth_diameter_mm': ('口部直径', 'number', 'mm', []),
    'weight_g': ('单件重量', 'number', 'g', []),
    'wall_thickness_mm': ('壁厚 / 料厚', 'number', 'mm', []),
    'bottle_shape': ('瓶型', 'select', '', ['长颈瓶', '矮颈瓶', '异形瓶', '其他']),
    'closure_type': ('封口方式', 'select', '', ['皇冠盖', '旋盖', '摆扣盖', '软木塞', '其他']),
    'can_shape': ('罐型', 'select', '', ['标准罐', '细长罐', '矮罐', '其他']),
    'lid_type': ('盖型', 'select', '', ['普通盖', '易拉盖', '全开盖', '其他']),
    'liner': ('内衬 / 内涂层', 'text', '', []),
    'keg_type': ('桶型', 'select', '', ['可回收钢桶', '一次性桶', 'PET 桶', '其他']),
    'valve_type': ('阀 / 酒矛接口型号', 'text', '', []),
    'pressure_spec': ('耐压要求 / 检测说明', 'text', '', []),
    'position': ('标签位置', 'select', '', ['正标', '背标', '颈标', '桶标', '桶身', '箱标', '其他']),
    'length_mm': ('长度', 'number', 'mm', []),
    'width_mm': ('宽度', 'number', 'mm', []),
    'dimensions': ('其他尺寸说明 / 旧规格', 'text', '', []),
    'dimension_basis': ('尺寸口径', 'select', '', ['内尺寸', '外尺寸']),
    'box_style': ('箱 / 盒结构', 'select', '', ['瓦楞纸箱', '彩盒', '礼盒', '手提盒', '其他']),
    'fefco_code': ('箱型编号（选填）', 'text', '', []),
    'board_layers': ('纸板层数', 'select', '', ['三层', '五层', '七层', '其他']),
    'flute': ('楞型', 'select', '', ['A', 'B', 'C', 'E', 'F', 'AB', 'BC', 'BE', '其他']),
    'paper_grade': ('纸板材质 / 克重', 'text', '', []),
    'pack_count': ('每箱装载数量', 'number', '瓶 / 罐', []),
    'fit_container_type': ('适用容器', 'select', '', ['酒瓶', '易拉罐', '酒桶', '其他']),
    'fit_capacity_ml': ('适用单容器容量', 'number', 'mL', []),
    'pack_layout': ('装箱排列', 'text', '', []),
    'load_spec': ('承重 / 抗压要求', 'text', '', []),
    'grid_count': ('隔板格数', 'number', '格', []),
    'adhesive': ('胶黏剂 / 粘贴方式', 'text', '', []),
    'surface_finish': ('表面工艺 / 覆膜', 'text', '', []),
    'printing': ('印刷工艺 / 色数', 'text', '', []),
    'thickness_um': ('薄膜厚度', 'number', 'μm', []),
    'roll_length_m': ('每卷长度', 'number', 'm', []),
}
TYPE_FIELDS = {
    'bottle': ['capacity', 'material', 'color', 'bottle_shape', 'height_mm', 'diameter_mm', 'weight_g', 'closure', 'mouth_diameter_mm', 'closure_type', 'wall_thickness_mm', 'pressure_spec'],
    'can': ['capacity', 'material', 'can_shape', 'height_mm', 'diameter_mm', 'weight_g', 'closure', 'liner', 'printing'],
    'keg': ['capacity', 'material', 'keg_type', 'height_mm', 'diameter_mm', 'weight_g', 'closure', 'valve_type', 'pressure_spec'],
    'cap': ['material', 'closure', 'diameter_mm', 'height_mm', 'liner', 'color', 'printing'],
    'can_lid': ['material', 'closure', 'diameter_mm', 'lid_type', 'liner', 'printing'],
    'keg_seal': ['material', 'closure', 'diameter_mm', 'color', 'printing'],
    'label': ['position', 'material', 'length_mm', 'width_mm', 'dimensions', 'adhesive', 'surface_finish', 'printing'],
    'carton': ['box_style', 'dimension_basis', 'length_mm', 'width_mm', 'height_mm', 'dimensions', 'material', 'board_layers', 'flute', 'paper_grade', 'pack_count', 'fit_container_type', 'fit_capacity_ml', 'pack_layout', 'load_spec', 'printing', 'fefco_code'],
    'divider': ['material', 'length_mm', 'width_mm', 'height_mm', 'dimensions', 'grid_count', 'wall_thickness_mm'],
    'film': ['material', 'width_mm', 'thickness_um', 'roll_length_m', 'dimensions'],
    'strap': ['material', 'width_mm', 'wall_thickness_mm', 'roll_length_m', 'dimensions', 'load_spec'],
}


def field_schema(kind, required):
    return [dict(key=key, label=FIELDS[key][0], input=FIELDS[key][1], unit=FIELDS[key][2], options=FIELDS[key][3], required=(key in required and key != 'dimensions') or (kind == 'carton' and key in {'length_mm', 'width_mm', 'height_mm'})) for key in SIMPLE_FIELDS.get(kind, [])]


def normalize_dimensions(kind, details):
    axes = ['length_mm', 'width_mm', 'height_mm'] if kind in {'carton', 'divider'} else ['length_mm', 'width_mm'] if kind == 'label' else []
    supplied = [key for key in axes if details.get(key) not in [None, '']]
    if supplied and len(supplied) != len(axes):
        return False
    if supplied:
        details['dimensions'] = ' × '.join(f'{float(details[key]):g}' for key in axes) + ' mm'
    return True
