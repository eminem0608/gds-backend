"""Brewing-water mass balance. No pH prediction or assumed zero source ions."""
import copy
import math

IONS = {'ca':'钙', 'mg':'镁', 'na':'钠', 'cl':'氯离子', 'so4':'硫酸根', 'hco3':'碳酸氢根'}
# mg of ion per gram of the stated hydrated compound, at 100% purity.
SALTS = {
 'gypsum': {'name':'二水硫酸钙（石膏）', 'formula':'CaSO₄·2H₂O', 'ions':{'ca':232.78,'so4':557.90}},
 'calcium_chloride': {'name':'二水氯化钙', 'formula':'CaCl₂·2H₂O', 'ions':{'ca':272.60,'cl':482.30}},
 'epsom': {'name':'七水硫酸镁（泻盐）', 'formula':'MgSO₄·7H₂O', 'ions':{'mg':98.61,'so4':389.75}},
 'salt': {'name':'氯化钠（食盐）', 'formula':'NaCl', 'ions':{'na':393.40,'cl':606.60}},
 'bicarbonate': {'name':'碳酸氢钠（小苏打）', 'formula':'NaHCO₃', 'ions':{'na':273.67,'hco3':726.33}},
}
VERSION='water-2026-10-v1'

def numeric(value,label,minimum=0,maximum=None):
 if isinstance(value,bool) or value in (None,''):raise ValueError(f'请填写{label}。')
 try:value=float(value)
 except (ValueError,TypeError):raise ValueError(f'{label}须为有效数值。')
 if not math.isfinite(value) or value<minimum or (maximum is not None and value>maximum):raise ValueError(f'{label}超出有效范围。')
 return value

def validate_source(source):
 if not isinstance(source,dict):raise ValueError('请先选择并填写水源水质。')
 ions=source.get('ions')
 if not isinstance(ions,dict):raise ValueError('请填写原水六项离子数据，未知值不能当作零。')
 for key,label in IONS.items():numeric(ions.get(key),f'原水{label}',0,100000)
 for key,label,high in [('ph','原水 pH',14),('alkalinity','总碱度',100000)]:
  if source.get(key) not in (None,''):numeric(source[key],label,0,high)
 return source

def water_volumes(design):
 mash=design.get('mash') or {}
 grain=sum(float(row.get('quantity') or 0) for row in design.get('fermentables') or [])
 return {'mash':grain*float(mash.get('water_grain_ratio') or 0),'sparge':sum(float(v or 0) for v in mash.get('sparge_water_liters') or [])}

def calculate_stage(water,stage,volume=None):
 validate_source(water.get('source'))
 volume=numeric(volume if volume is not None else stage.get('volume_l'),'调节水量',0.001)
 blend=numeric(water.get('dilution_percent',0),'稀释比例',0,100)/100
 dilution=water.get('dilution_ions') or {key:0 for key in IONS}
 if blend:
  for key,label in IONS.items():numeric(dilution.get(key),f'稀释水{label}',0,100000)
 base={key:float(water['source']['ions'][key])*(1-blend)+float(dilution.get(key) or 0)*blend for key in IONS}
 final=dict(base);additions=stage.get('additions') or {}
 if set(additions)-set(SALTS):raise ValueError('包含不支持的调节盐，请核对水合物类型。')
 for key,salt in SALTS.items():
  addition=additions.get(key) or {}
  amount=numeric(addition.get('amount',0),salt['name']+'用量',0,1000000)
  purity=numeric(addition.get('purity',100),salt['name']+'纯度',0.001,100)/100
  for ion,mg in salt['ions'].items():final[ion]+=amount*purity*mg/volume
 acid=stage.get('acid') or {}
 acid_ml=numeric(acid.get('amount_ml',0),'乳酸用量',0,1000000)
 concentration=numeric(acid.get('concentration',88),'乳酸质量浓度',0.001,100)
 density=numeric(acid.get('density',1.21),'乳酸密度',0.001,3)
 alk=water['source'].get('alkalinity')
 if blend:
  dilution_alk=water.get('dilution_alkalinity')
  alk=(float(alk)*(1-blend)+float(dilution_alk)*blend) if alk not in (None,'') and dilution_alk not in (None,'') else None
 else:alk=float(alk) if alk not in (None,'') else None
 bicarbonate_delta=final['hco3']-base['hco3']
 acid_demand=acid_ml*density*concentration/100/90.078*50000/volume
 before_alk=alk+bicarbonate_delta*50/61.016 if alk is not None else None
 after_alk=max(0,before_alk-acid_demand) if before_alk is not None else None
 warnings=[]
 if acid_ml:
  final['hco3']=None
  warnings.append('加酸后的碳酸氢根及 pH 需检测；中和当量不能代替糖化 pH。')
  if before_alk is not None and acid_demand>before_alk:warnings.append('乳酸中和当量超过估算碱度，须分次投加并实测 pH。')
 if blend:warnings.append('稀释按所填稀释水离子计算；零值为显式假设，不代表 RO 实测结果。')
 target=(water.get('target') or {}).get('ions') or {};comparison={}
 for key,label in IONS.items():
  bounds=target.get(key) or {};low=bounds.get('min');high=bounds.get('max')
  if low not in (None,''):low=numeric(low,label+'目标下限')
  else:low=None
  if high not in (None,''):high=numeric(high,label+'目标上限')
  else:high=None
  if low is not None and high is not None and low>high:raise ValueError(label+'目标下限不能大于上限。')
  value=final[key]
  status='unknown' if value is None else 'unset' if low is None and high is None else 'low' if low is not None and value<low else 'high' if high is not None and value>high else 'ok'
  comparison[key]={'before':base[key],'after':value,'min':low,'max':high,'status':status}
  if status=='high' and high is not None and base[key]>high:warnings.append(f'原水{label}已超目标上限，加盐不能降低；请考虑稀释或更换水源。')
 return {'volume_l':volume,'source_volume_l':volume*(1-blend),'dilution_volume_l':volume*blend,'ions':final,'comparison':comparison,'hardness':2.497*final['ca']+4.118*final['mg'],'alkalinity_estimate':after_alk,'acid_neutralization':acid_demand,'ratio':final['so4']/final['cl'] if final['cl']>0 else None,'warnings':warnings,'calculation_version':VERSION}

def validate_adjustment(water,design):
 if not water:return
 if not isinstance(water,dict):raise ValueError('水质方案格式无效。')
 if not water.get('enabled'):return
 validate_source(water.get('source'))
 if water.get('source_mode')=='override' and not str(water.get('override_note') or '').strip():raise ValueError('手动修改原水时请填写修改依据。')
 if not (water.get('target') or {}).get('name'):raise ValueError('请填写本配方的目标水质方案名称。')
 for key,volume in water_volumes(design).items():
  if volume<=0:continue
  if key not in (water.get('stages') or {}):raise ValueError('请填写糖化水和洗糟水的调节方案。')
  calculate_stage(water,water['stages'][key],volume)

def stage_snapshot(design,key):
 water=design.get('water_adjustment') or {}
 if not water.get('enabled'):return None
 volume=water_volumes(design)[key]
 if volume<=0:return None
 stage=copy.deepcopy((water.get('stages') or {}).get(key) or {})
 stage['volume_l']=volume
 stage.setdefault('additions',{})
 for salt in SALTS:stage['additions'][salt]={'amount':0,'purity':100,**(stage['additions'].get(salt) or {})}
 stage['acid']={'amount_ml':0,'concentration':88,'density':1.21,**(stage.get('acid') or {})}
 return {'stage':key,'source':copy.deepcopy(water['source']),'target':copy.deepcopy(water.get('target') or {}),'source_mode':water.get('source_mode'),'override_note':water.get('override_note',''),'dilution_percent':water.get('dilution_percent',0),'dilution_ions':copy.deepcopy(water.get('dilution_ions') or {}),'dilution_alkalinity':water.get('dilution_alkalinity'),'recipe_stage':stage,'planned_result':calculate_stage(water,stage,volume),'calculation_version':VERSION}

def scale_additions(design,previous_volumes):
 water=design.get('water_adjustment') or {}
 if not water.get('enabled'):return
 current=water_volumes(design)
 for key,stage in (water.get('stages') or {}).items():
  old=previous_volumes.get(key,0)
  factor=current.get(key,0)/old if old>0 else 1
  for addition in (stage.get('additions') or {}).values():addition['amount']=round(float(addition.get('amount') or 0)*factor,6)
  if stage.get('acid'):stage['acid']['amount_ml']=round(float(stage['acid'].get('amount_ml') or 0)*factor,6)
  stage['volume_l']=current.get(key,0)
