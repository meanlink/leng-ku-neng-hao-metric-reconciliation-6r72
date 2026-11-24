"""总表需量费分摊的业务规则：导入、失效重算、复核、签发冻结。"""
from fastapi import HTTPException

from . import master_demand_store as store
from .master_demand_math import evaluate
from .tariffs import editable_billing

MASTER_ENTITY = 'master_demand'


def _report_sites(cur, report):
    from .budgets import site_actuals
    return list(site_actuals(cur, report).keys())


def live_evaluation(cur, report):
    """按当前有效源数据（同站同刻取最高修订号）重算分摊口径。"""
    sites = _report_sites(cur, report)
    rows = store.fetch_import_rows(cur, report['id'])
    valid_sites = {row['site'] for row in rows}
    unknown = sorted(valid_sites.difference(sites))
    if unknown:
        raise HTTPException(422, detail='以下站点不在本月报差异复核单中：' + '、'.join(unknown))
    # 总表需量单价取最近一版复核单登记值；尚未登记时先不算金额，只看刻钟覆盖。
    rate = None
    reviews = store.fetch_reviews(cur, report['id'])
    if reviews:
        rate = reviews[0]['demand_rate']
    result = evaluate(rows, sites, rate if rate is not None else 0)
    result['has_imports'] = bool(rows)
    result['demand_rate'] = rate
    return result


def _attach_allocations(cur, review):
    rows = store.fetch_allocations(cur, review['id'])
    review = {**review, 'allocations': [
        {'site': row['site'], 'peak_load_kw': row['peak_load_kw'],
         'allocated_amount': row['allocated_amount']} for row in rows]}
    return review


def master_demand_sheet(cur, report):
    """组装总表需量费分摊工作台数据与就绪状态。"""
    sites = _report_sites(cur, report)
    live = live_evaluation(cur, report)
    reviews = [_attach_allocations(cur, row) for row in store.fetch_reviews(cur, report['id'])]
    latest = reviews[0] if reviews else None

    if not live['has_imports']:
        readiness = 'no_imports'
    elif latest and latest['frozen_at']:
        readiness = 'frozen'
    elif latest is None:
        readiness = 'incomplete' if live['readiness'] == 'incomplete' else 'ready_to_review'
    elif latest['status'] == 'stale':
        readiness = 'stale'
    elif latest['status'] == 'pending':
        # 待复核单仍按当前源数据实时判定，数据补齐即从 incomplete 转可复核。
        readiness = 'incomplete' if live['readiness'] == 'incomplete' else 'pending'
    elif latest['status'] == 'approved':
        if latest['review_signature'] != live['signature']:
            # 该分支理论上已在导入时标记 stale，这里做防御性兜底。
            readiness = 'stale'
        else:
            readiness = 'approved'
    else:
        readiness = 'incomplete'

    archived_after_freeze = 0
    if latest and latest['frozen_at']:
        archived_after_freeze = store.count_archived_imports(cur, report['id'], latest['frozen_at'])

    return {
        'report_id': report['id'],
        'period': report['period'],
        'status': report['status'],
        'sites': sites,
        'enabled': live['has_imports'],
        'imports_disabled': report['status'] == 'published',
        'readiness': readiness,
        'live': live,
        'reviews': reviews,
        'latest': latest,
        'archived_after_freeze': archived_after_freeze,
    }


def import_loads(cur, report, payload, user_id):
    editable_billing(cur, report, payload.loads[0].site)
    sites = _report_sites(cur, report)
    for item in payload.loads:
        if item.site not in sites:
            raise HTTPException(422, detail=f'站点 {item.site} 不在本月报差异复核单中，请先在差异复核建单')
    before = live_evaluation(cur, report)
    inserted, duplicates = store.insert_import_rows(
        cur, report['id'], payload.loads, payload.import_batch, user_id)
    # 只有有效源数据真正改变（签名变化）才让未冻结的旧审批失效；
    # 乱序补发的旧修订号或同值重复导入不影响当前口径，不得使审批失效。
    after = live_evaluation(cur, report)
    invalidated = 0
    if before['signature'] != after['signature']:
        invalidated = store.mark_approved_stale(cur, report['id'])
    from .audit import record_action
    record_action(cur, user_id, 'import_master_demand', MASTER_ENTITY, report['id'],
                  f'{payload.import_batch} 新增{inserted}条 重复{duplicates}条 旧审批失效{invalidated}版')
    return master_demand_sheet(cur, report)


def submit_master_review(cur, report, payload, user_id):
    sites = _report_sites(cur, report)
    if not sites:
        raise HTTPException(422, detail='本月报暂无站点差异复核单，无法分摊总表需量费')
    editable_billing(cur, report, sites[0])
    live = live_evaluation(cur, report)
    if not live['has_imports']:
        raise HTTPException(409, detail='请先导入两站每刻钟负荷')
    if live['readiness'] == 'incomplete':
        missing_text = '；'.join(
            f'{site} 缺 {len(slots)} 个刻钟（{", ".join(slots[:5])}{"…" if len(slots) > 5 else ""}）'
            for site, slots in live['missing_slots'].items() if slots)
        raise HTTPException(409, detail='任一站缺时点即暂停复核：' + missing_text)
    fields = {
        'demand_rate': payload.demand_rate,
        'peak_slot': live['peak_slot'],
        'combined_kw': live['combined_kw'],
        'total_demand_charge': live_evaluate_charge(live, payload.demand_rate),
        'signature': live['signature'],
        'allocation_rows': [
            (site, live['peak_loads'][site], live_recompute_allocations(live, payload.demand_rate)[site])
            for site in live['sites']],
    }
    reviews = store.fetch_reviews(cur, report['id'])
    latest = reviews[0] if reviews else None
    if latest and latest['frozen_at']:
        raise HTTPException(409, detail='月报已签发，分摊复核单已冻结，新到修订只留档')
    if latest and latest['status'] == 'pending':
        # 仍是同一待复核单，按当前有效数据重算覆盖。
        store.update_pending_review(cur, latest['id'], fields)
        review_id = latest['id']
    else:
        review_id = store.insert_review(cur, report['id'], len(reviews) + 1, fields, user_id)
    from .audit import record_action
    record_action(cur, user_id, 'submit_master_demand_review', MASTER_ENTITY, review_id,
                  f'峰值 {live["peak_slot"]} 合计 {live["combined_kw"]} kW')
    return master_demand_sheet(cur, report)


def live_evaluate_charge(live, demand_rate):
    from .master_demand_math import cents
    from decimal import Decimal
    return cents(live['combined_kw'] * Decimal(str(demand_rate)))


def live_recompute_allocations(live, demand_rate):
    from .master_demand_math import allocate
    total = live_evaluate_charge(live, demand_rate)
    return allocate(live['peak_loads'], total)


def review_master(cur, report, review_id, payload, user_id):
    reviews = store.fetch_reviews(cur, report['id'])
    review = next((row for row in reviews if row['id'] == review_id), None)
    if review is None:
        raise HTTPException(404, detail='本月报中没有该分摊复核单')
    latest = reviews[0]
    if review['id'] != latest['id'] or review['status'] != 'pending':
        raise HTTPException(409, detail='只有最新待复核的分摊单可以复核')
    if review['submitted_by'] == user_id:
        raise HTTPException(409, detail='请由另一人复核该分摊单')
    live = live_evaluation(cur, report)
    if live['readiness'] == 'incomplete':
        raise HTTPException(409, detail='两站刻钟未对齐，暂停复核')
    if live['signature'] != review['review_signature']:
        raise HTTPException(409, detail='有效源数据已更新，请按当前口径重新提交分摊复核')
    store.save_master_decision(cur, review_id, payload.note, user_id)
    from .audit import record_action
    record_action(cur, user_id, 'approve_master_demand', MASTER_ENTITY, review_id, payload.note)
    return master_demand_sheet(cur, report)


def freeze_master_demand(cur, report):
    """月报签发时冻结峰值时点、两站读数和分摊金额。"""
    sheet = master_demand_sheet(cur, report)
    if not sheet['enabled']:
        return
    if sheet['readiness'] != 'approved':
        raise HTTPException(409, detail='总表需量费分摊尚未完成复核，不能签发月报')
    store.freeze_latest_review(cur, sheet['latest']['id'])
