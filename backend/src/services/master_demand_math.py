"""总表需量费分摊的纯函数口径。

两座冷库共用总表，按两站同一刻钟负荷之和的月峰值核验总表需量费，
再按峰值时刻各站负荷比例把需量费分摊到各自账单。

本模块只依赖标准库与 Decimal，便于在没有数据库的环境下直接单测；
存储层（master_demand_store.py）负责把行数据整理成这里的输入结构。
"""
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal('0.01')
KW = Decimal('0.001')


def cents(value):
    """四舍五入到分。"""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def is_quarter_hour(slot):
    """刻钟时点格式 HH:MM，分钟必须落在 00/15/30/45。"""
    if not isinstance(slot, str) or len(slot) != 5 or slot[2] != ':':
        return False
    hour_text, minute_text = slot[:2], slot[3:]
    if not (hour_text.isdigit() and minute_text.isdigit()):
        return False
    hour, minute = int(hour_text), int(minute_text)
    return 0 <= hour <= 23 and minute in (0, 15, 30, 45)


def active_series(rows):
    """从导入历史中取每个 (站点, 刻钟) 的最高修订号记录。

    乱序补发的旧修订号不会盖掉新版：无论到达顺序如何，只按 revision 取大值。
    输入行字段：site / slot / revision / load_kw。
    返回 {(site, slot): load_kw}，负荷统一量化到 kW 三位小数。
    """
    chosen = {}
    for row in rows:
        key = (row['site'], row['slot'])
        load = Decimal(str(row['load_kw'])).quantize(KW)
        current = chosen.get(key)
        if current is None or row['revision'] > current['revision']:
            chosen[key] = {'revision': row['revision'], 'load_kw': load}
    return {key: value['load_kw'] for key, value in chosen.items()}


def coverage(series, sites):
    """对齐两站刻钟集合，返回 (共同刻钟, 各站缺失刻钟)。

    任一站缺时点即不能复核，由调用方依据 missing 决定暂停。
    """
    slots_by_site = {site: set() for site in sites}
    for site, slot in series:
        if site in slots_by_site:
            slots_by_site[site].add(slot)
    common = set.intersection(*slots_by_site.values()) if sites else set()
    union = set.union(*slots_by_site.values()) if sites else set()
    # 缺失 = 全量刻钟里本站没有的部分；本站独有的刻钟计入其他站的缺失。
    missing = {site: sorted(union - slots_by_site[site]) for site in sites}
    return sorted(common), missing


def find_peak(series, sites, common_slots):
    """在两站同刻都有读数的时点中找月峰值。

    峰值取两站负荷之和的最大值；并列出现在多个时刻时取最早刻钟，
    保证同一输入可重复计算、口径稳定。
    返回峰值时点与该时刻两站读数。
    """
    peak = None
    for slot in common_slots:
        loads = {site: series[(site, slot)] for site in sites}
        combined = sum(loads.values(), Decimal('0'))
        candidate = (combined, slot, loads)
        if peak is None or combined > peak[0] or (combined == peak[0] and slot < peak[1]):
            peak = candidate
    combined_kw, peak_slot, peak_loads = peak
    return {
        'peak_slot': peak_slot,
        'combined_kw': combined_kw.quantize(KW),
        'peak_loads': {site: peak_loads[site].quantize(KW) for site in sites},
    }


def allocate(peak_loads, total_demand_charge):
    """按峰值时刻各站负荷比例分摊总表需量费，分到分且合计等于总额。

    先按比例各四舍五入到分，再用最大余数法把尾差（分）补给余数最大的站点，
    因此各站分摊金额相加恒等于总表需量费，不会凭空多出或丢失一分钱。
    """
    sites = list(peak_loads.keys())
    combined = sum(peak_loads.values(), Decimal('0'))
    total = cents(Decimal(str(total_demand_charge)))
    if combined <= 0:
        # 峰值为零时各站等额承担，再用最大余数法补齐分差，仍保证合计守恒。
        if not sites:
            return {}
        base = (total / len(sites)).quantize(CENT, rounding=ROUND_HALF_UP)
        share = {site: base for site in sites}
        order = sorted(sites)
        remainder = total - sum(share.values(), Decimal('0'))
        step = CENT if remainder > 0 else -CENT
        index = 0
        while remainder != 0:
            share[order[index % len(order)]] += step
            remainder -= step
            index += 1
        return share
    raw = {}
    rounded = {}
    for site in sites:
        raw[site] = total * peak_loads[site] / combined
        rounded[site] = cents(raw[site])
    remainder = total - sum(rounded.values(), Decimal('0'))
    # 余数 = 原始比例金额 - 已入分金额；余数最大的站点优先补差。
    fractions = sorted(
        ((raw[site] - rounded[site], site) for site in sites),
        key=lambda item: (-item[0], item[1]),
    )
    step = CENT if remainder > 0 else -CENT
    index = 0
    while remainder != 0 and fractions:
        rounded[fractions[index % len(fractions)][1]] += step
        remainder -= step
        index += 1
    return rounded


def review_signature(peak):
    """审批签名：峰值时点与两站读数。有效源数据一变签名即变，旧审批失效。"""
    loads = peak['peak_loads']
    return peak['peak_slot'] + '|' + ';'.join(
        f'{site}={loads[site]}' for site in sorted(loads)
    )


def evaluate(rows, sites, demand_rate):
    """完整核验口径：对齐刻钟 → 找月峰值 → 算总表需量费 → 分摊。

    返回 readiness 与核验明细；任一站缺时点时 readiness='incomplete'，
    不产出峰值与分摊，调用方应暂停复核。
    """
    series = active_series(rows)
    common_slots, missing = coverage(series, sites)
    imported = {site: len({slot for (s, slot) in series if s == site}) for site in sites}
    result = {
        'sites': list(sites),
        'imported_counts': imported,
        'common_count': len(common_slots),
        'missing_slots': missing,
        'readiness': 'incomplete' if any(missing.values()) or not common_slots else 'ready',
        'peak_slot': None,
        'combined_kw': None,
        'peak_loads': None,
        'total_demand_charge': None,
        'allocations': None,
        'signature': None,
    }
    if result['readiness'] != 'ready':
        return result
    peak = find_peak(series, sites, common_slots)
    total = cents(peak['combined_kw'] * Decimal(str(demand_rate)))
    allocations = allocate(peak['peak_loads'], total)
    result.update(
        peak_slot=peak['peak_slot'],
        combined_kw=peak['combined_kw'],
        peak_loads=peak['peak_loads'],
        total_demand_charge=total,
        allocations=allocations,
        signature=review_signature(peak),
    )
    return result
