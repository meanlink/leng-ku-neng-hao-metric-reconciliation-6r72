"""总表需量费分摊的数据库存取层。

只负责 SQL 与行记录整理，业务口径在 master_demand_math.py / master_demand.py。
"""
from fastapi import HTTPException


def fetch_import_rows(cur, report_id):
    cur.execute('''SELECT id, site, slot, revision, load_kw, import_batch, imported_by, imported_at
                   FROM master_demand_imports WHERE report_id=%s
                   ORDER BY site, slot, revision''', (report_id,))
    return cur.fetchall()


def insert_import_rows(cur, report_id, loads, import_batch, user_id):
    """批量写入刻钟负荷；同修订号同值重复导入跳过，同号不同值拒绝。"""
    inserted = 0
    duplicates = 0
    for item in loads:
        cur.execute('''SELECT load_kw FROM master_demand_imports
                       WHERE report_id=%s AND site=%s AND slot=%s AND revision=%s''',
                    (report_id, item.site, item.slot, item.revision))
        existing = cur.fetchone()
        if existing is not None:
            if existing['load_kw'] != item.load_kw:
                raise HTTPException(
                    409,
                    detail=f'{item.site} {item.slot} 修订号 {item.revision} 已存在不同负荷值，'
                           '修订记录不可覆盖，请使用更高修订号补发',
                )
            duplicates += 1
            continue
        cur.execute('''INSERT INTO master_demand_imports(report_id,site,slot,revision,load_kw,
                            import_batch,imported_by)
                       VALUES(%s,%s,%s,%s,%s,%s,%s)''',
                    (report_id, item.site, item.slot, item.revision, item.load_kw, import_batch, user_id))
        inserted += 1
    return inserted, duplicates


def mark_approved_stale(cur, report_id):
    """有效源数据更新后，最近一版未冻结的审批失效（旧版留痕为 stale）。"""
    cur.execute('''UPDATE master_demand_reviews SET status='stale'
                   WHERE report_id=%s AND frozen_at IS NULL AND status='approved'
                   AND id=(SELECT * FROM (
                       SELECT MAX(id) FROM master_demand_reviews
                       WHERE report_id=%s AND frozen_at IS NULL) t)''',
                (report_id, report_id))
    return cur.rowcount


def fetch_reviews(cur, report_id):
    cur.execute('''SELECT v.*, u.name AS submitted_name, r.name AS reviewed_name
                   FROM master_demand_reviews v
                   JOIN users u ON u.id=v.submitted_by
                   LEFT JOIN users r ON r.id=v.reviewed_by
                   WHERE v.report_id=%s ORDER BY v.revision DESC''', (report_id,))
    return cur.fetchall()


def fetch_allocations(cur, review_id):
    cur.execute('''SELECT site, peak_load_kw, allocated_amount FROM master_demand_allocations
                   WHERE review_id=%s ORDER BY site''', (review_id,))
    return cur.fetchall()


def insert_review(cur, report_id, revision, fields, user_id):
    cur.execute('''INSERT INTO master_demand_reviews(report_id,revision,demand_rate,peak_slot,
                        combined_kw,total_demand_charge,review_signature,submitted_by)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
                (report_id, revision, fields['demand_rate'], fields['peak_slot'], fields['combined_kw'],
                 fields['total_demand_charge'], fields['signature'], user_id))
    review_id = cur.lastrowid
    cur.executemany('''INSERT INTO master_demand_allocations(review_id,site,peak_load_kw,allocated_amount)
                       VALUES(%s,%s,%s,%s)''',
                    [(review_id, site, load, amount)
                     for site, load, amount in fields['allocation_rows']])
    return review_id


def update_pending_review(cur, review_id, fields):
    cur.execute('''UPDATE master_demand_reviews SET demand_rate=%s,peak_slot=%s,combined_kw=%s,
                       total_demand_charge=%s,review_signature=%s,submitted_at=NOW()
                   WHERE id=%s AND status='pending' ''',
                (fields['demand_rate'], fields['peak_slot'], fields['combined_kw'],
                 fields['total_demand_charge'], fields['signature'], review_id))
    if cur.rowcount != 1:
        raise HTTPException(409, detail='分摊复核单状态已变化，请刷新后重试')
    cur.execute('DELETE FROM master_demand_allocations WHERE review_id=%s', (review_id,))
    cur.executemany('''INSERT INTO master_demand_allocations(review_id,site,peak_load_kw,allocated_amount)
                       VALUES(%s,%s,%s,%s)''',
                    [(review_id, site, load, amount)
                     for site, load, amount in fields['allocation_rows']])


def save_master_decision(cur, review_id, note, user_id):
    cur.execute('''UPDATE master_demand_reviews SET status='approved',review_note=%s,
                       reviewed_by=%s,reviewed_at=NOW() WHERE id=%s AND status='pending' ''',
                (note, user_id, review_id))
    if cur.rowcount != 1:
        raise HTTPException(409, detail='该分摊复核单已复核或已失效')


def freeze_latest_review(cur, review_id):
    cur.execute('''UPDATE master_demand_reviews SET frozen_at=NOW()
                   WHERE id=%s AND status='approved' AND frozen_at IS NULL''', (review_id,))
    if cur.rowcount != 1:
        raise HTTPException(409, detail='总表需量费分摊未能冻结，请确认最新复核已批准')


def count_archived_imports(cur, report_id, frozen_at):
    """签发后到达的修订只留档：统计冻结时点之后导入的记录数。"""
    cur.execute('''SELECT COUNT(*) AS archived FROM master_demand_imports
                   WHERE report_id=%s AND imported_at > %s''', (report_id, frozen_at))
    return cur.fetchone()['archived']
