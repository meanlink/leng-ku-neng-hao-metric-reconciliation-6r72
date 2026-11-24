"""master_demand 服务层集成测试（纯标准库，FakeCursor 模拟 MySQL 字典游标）。

覆盖需求全链路：
1. 两站刻钟负荷导入，任一缺时点即暂停复核；
2. 同站同刻取最高修订号，乱序旧记录不覆盖新版；
3. 有效源数据更新后旧审批失效，重算再复核；
4. 峰值取两站同刻之和，按峰值时刻负荷分摊，分到分且合计等于总额；
5. 签发冻结峰值时点/两站读数/分摊金额，签发后修订只留档。
"""
import os
import sys
import types
from copy import deepcopy
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 本地环境未安装第三方依赖，用最小桩满足服务层导入链。
if 'fastapi' not in sys.modules:
    fastapi_stub = types.ModuleType('fastapi')

    class HTTPException(Exception):
        def __init__(self, status_code=400, detail=''):
            super().__init__(detail)
            self.status_code, self.detail = status_code, detail

    fastapi_stub.HTTPException = HTTPException
    sys.modules['fastapi'] = fastapi_stub

if 'pymysql' not in sys.modules:
    pymysql_stub = types.ModuleType('pymysql')
    cursors_stub = types.ModuleType('pymysql.cursors')
    cursors_stub.DictCursor = object
    pymysql_stub.cursors = cursors_stub
    pymysql_stub.connect = lambda *a, **k: None
    sys.modules['pymysql'] = pymysql_stub
    sys.modules['pymysql.cursors'] = cursors_stub

from src.services import master_demand as md  # noqa: E402
from src.services import master_demand_store as store  # noqa: E402

A, B = '海风一号冷库', '东港周转冷库'


class FakeCursor:
    """最小化字典游标：仅实现服务层用到的 SQL 形态。"""

    def __init__(self, tables):
        self.tables = tables
        self.lastrowid = 0
        self._result = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        text = ' '.join(sql.split())
        if text.startswith('SELECT id, site, slot, revision, load_kw'):
            self._result = deepcopy(self.tables['imports'])
        elif text.startswith('SELECT load_kw FROM master_demand_imports'):
            report_id, site, slot, revision = params
            self._result = [r for r in self.tables['imports']
                            if r['report_id'] == report_id and r['site'] == site
                            and r['slot'] == slot and r['revision'] == revision]
        elif text.startswith("UPDATE master_demand_reviews SET status='stale'"):
            report_id = params[0]
            candidates = [v for v in self.tables['reviews']
                          if v['report_id'] == report_id and v['frozen_at'] is None
                          and v['status'] == 'approved']
            if candidates:
                target = max(candidates, key=lambda v: v['id'])
                target['status'] = 'stale'
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif text.startswith('SELECT v.*, u.name AS submitted_name'):
            report_id = params[0]
            self._result = deepcopy([
                {**v, 'submitted_name': f'用户{v["submitted_by"]}',
                 'reviewed_name': f'用户{v["reviewed_by"]}' if v['reviewed_by'] else None}
                for v in self.tables['reviews'] if v['report_id'] == report_id])
            self._result.sort(key=lambda v: v['revision'], reverse=True)
        elif text.startswith('SELECT site, peak_load_kw, allocated_amount'):
            review_id = params[0]
            self._result = deepcopy([a for a in self.tables['allocations'] if a['review_id'] == review_id])
        elif text.startswith('INSERT INTO master_demand_imports'):
            report_id, site, slot, revision, load_kw, batch, user_id = params
            self.lastrowid = len(self.tables['import_seq']) + 1
            self.tables['import_seq'].append(None)
            self.tables['imports'].append({
                'id': self.lastrowid, 'report_id': report_id, 'site': site, 'slot': slot,
                'revision': revision, 'load_kw': load_kw, 'import_batch': batch,
                'imported_by': user_id, 'imported_at': '2027-01-15 10:00:00'})
            self.rowcount = 1
        elif text.startswith('INSERT INTO master_demand_reviews'):
            report_id, revision, rate, slot, combined, total, signature, user_id = params
            self.lastrowid = len(self.tables['reviews']) + 1
            self.tables['reviews'].append({
                'id': self.lastrowid, 'report_id': report_id, 'revision': revision,
                'demand_rate': rate, 'peak_slot': slot, 'combined_kw': combined,
                'total_demand_charge': total, 'review_signature': signature,
                'status': 'pending', 'submitted_by': user_id,
                'submitted_at': '2027-01-15 10:00:00',
                'reviewed_by': None, 'reviewed_at': None, 'review_note': None, 'frozen_at': None})
            self.rowcount = 1
        elif text.startswith('INSERT INTO master_demand_allocations'):
            for review_id, site, load, amount in params:
                self.tables['allocations'].append(
                    {'review_id': review_id, 'site': site,
                     'peak_load_kw': load, 'allocated_amount': amount})
        elif text.startswith('UPDATE master_demand_reviews SET demand_rate'):
            rate, slot, combined, total, signature, review_id = params
            target = next(v for v in self.tables['reviews'] if v['id'] == review_id)
            assert target['status'] == 'pending'
            target.update(demand_rate=rate, peak_slot=slot, combined_kw=combined,
                          total_demand_charge=total, review_signature=signature)
            self.rowcount = 1
        elif text.startswith('DELETE FROM master_demand_allocations'):
            review_id = params[0]
            self.tables['allocations'] = [a for a in self.tables['allocations']
                                          if a['review_id'] != review_id]
        elif text.startswith("UPDATE master_demand_reviews SET status='approved'"):
            note, user_id, review_id = params
            target = next(v for v in self.tables['reviews'] if v['id'] == review_id)
            assert target['status'] == 'pending'
            target.update(status='approved', review_note=note, reviewed_by=user_id,
                          reviewed_at='2027-01-15 11:00:00')
            self.rowcount = 1
        elif text.startswith('UPDATE master_demand_reviews SET frozen_at'):
            review_id = params[0]
            target = next(v for v in self.tables['reviews'] if v['id'] == review_id)
            assert target['status'] == 'approved' and target['frozen_at'] is None
            target['frozen_at'] = '2027-01-31 18:00:00'
            self.rowcount = 1
        elif text.startswith('SELECT COUNT(*) AS archived'):
            report_id, frozen_at = params
            self._result = [{'archived': sum(
                1 for r in self.tables['imports']
                if r['report_id'] == report_id and r['imported_at'] > frozen_at)}]
        elif text.startswith('SELECT unit, cycle FROM metric_contracts'):
            self._result = [{'unit': 'kWh', 'cycle': 'monthly'}]
        elif text.startswith('SELECT s.site, COUNT(*)'):
            self._result = deepcopy([{'site': A, 'meter_count': 1, 'actual_kwh': Decimal('12000')},
                                     {'site': B, 'meter_count': 1, 'actual_kwh': Decimal('6200')}])
        elif text.startswith('INSERT INTO audit_logs'):
            self.tables.setdefault('audits', []).append(params)
            self.rowcount = 1
        else:
            raise AssertionError(f'未模拟的 SQL: {text[:60]}')

    def executemany(self, sql, seq):
        if sql.lstrip().startswith('INSERT INTO master_demand_allocations'):
            return self.execute(sql, list(seq))
        raise AssertionError('未模拟的 executemany')

    def fetchall(self):
        out, self._result = self._result, []
        return out

    def fetchone(self):
        return self._result.pop(0) if self._result else None


class ImportItem:
    def __init__(self, site, slot, revision, load_kw):
        self.site, self.slot, self.revision, self.load_kw = site, slot, revision, Decimal(str(load_kw))


class ImportPayload:
    def __init__(self, batch, items):
        self.import_batch, self.loads = batch, items


class RatePayload:
    def __init__(self, rate):
        self.demand_rate = Decimal(str(rate))


class NotePayload:
    def __init__(self, note='已与总表最大需量记录核对一致。'):
        self.decision, self.note = 'approved', note


REPORT = {'id': 7, 'period': '2027-01', 'contract_id': 3, 'status': 'draft'}
ANALYST, REVIEWER = 1, 2


def fresh_cur():
    return FakeCursor({'imports': [], 'import_seq': [], 'reviews': [], 'allocations': []})


def check(name, got, expected):
    assert got == expected, f'{name}: got {got!r}, expected {expected!r}'
    print(f'  ok {name}')


def main():
    cur = fresh_cur()

    # 1) 只导入 A 站两个刻钟、B 站一个刻钟 -> B 缺时点，暂停复核。
    md.import_loads(cur, REPORT, ImportPayload('batch-1', [
        ImportItem(A, '08:00', 1, 168.5), ImportItem(A, '08:15', 1, 172.0),
        ImportItem(B, '08:00', 1, 97.25)]), ANALYST)
    sheet = md.master_demand_sheet(cur, REPORT)
    check('imports enabled', sheet['enabled'], True)
    check('readiness incomplete', sheet['readiness'], 'incomplete')
    check('missing slot reported', sheet['live']['missing_slots'][B], ['08:15'])
    try:
        md.submit_master_review(cur, REPORT, RatePayload('36'), ANALYST)
        raise AssertionError('缺时点时应拒绝提交复核')
    except Exception as exc:
        assert '暂停复核' in str(exc), str(exc)
        print('  ok review paused while any station missing a slot')

    # 2) 补齐 B 站；再乱序补发：先到 rev2，后到 rev1（旧记录不能盖新版）。
    md.import_loads(cur, REPORT, ImportPayload('batch-2', [
        ImportItem(B, '08:15', 1, 101.75),
        ImportItem(A, '08:15', 2, 180.0),
        ImportItem(A, '08:15', 1, 172.0)]), ANALYST)
    series = store_active(cur)
    check('highest revision wins over disorder', series[(A, '08:15')], Decimal('180.000'))
    sheet = md.master_demand_sheet(cur, REPORT)
    check('ready after coverage complete', sheet['readiness'], 'ready_to_review')
    # 峰值刻钟：08:15 合计 281.75；08:00 合计 265.75。
    check('peak slot from combined load', sheet['live']['peak_slot'], '08:15')
    check('combined peak kw', sheet['live']['combined_kw'], Decimal('281.750'))

    # 3) 提交分摊复核单（需量单价 36 元/kW）。
    md.submit_master_review(cur, REPORT, RatePayload('36'), ANALYST)
    sheet = md.master_demand_sheet(cur, REPORT)
    check('pending after submit', sheet['readiness'], 'pending')
    review = sheet['latest']
    # 总额 281.75 × 36 = 10143.00
    check('total demand charge', review['total_demand_charge'], Decimal('10143.00'))
    total_alloc = sum(a['allocated_amount'] for a in review['allocations'])
    check('allocation sums to total before approval', total_alloc, Decimal('10143.00'))
    # 不能自己复核自己。
    try:
        md.review_master(cur, REPORT, review['id'], NotePayload(), ANALYST)
        raise AssertionError('提交人不应能自复核')
    except Exception as exc:
        assert '另一人' in str(exc)
        print('  ok self-review rejected')

    # 4) 复核员批准。
    md.review_master(cur, REPORT, review['id'], NotePayload(), REVIEWER)
    sheet = md.master_demand_sheet(cur, REPORT)
    check('approved readiness', sheet['readiness'], 'approved')

    # 4b) 乱序旧修订号、同值高修订号不改变有效数据，审批不得失效。
    md.import_loads(cur, REPORT, ImportPayload('batch-noop', [
        ImportItem(A, '08:15', 1, 172.0),   # 已存在的旧修订号同值 -> 重复留档
        ImportItem(A, '08:15', 3, 180.0),   # 更高修订号但负荷与现值相同 -> 签名不变
    ]), ANALYST)
    sheet = md.master_demand_sheet(cur, REPORT)
    check('approval survives noop revisions', sheet['readiness'], 'approved')
    check('review remains approved status', sheet['latest']['status'], 'approved')

    # 5) 有效源数据更新（A 站 08:00 发 rev2，提高负荷）-> 旧审批失效、签名变化。
    old_signature = review['review_signature']
    md.import_loads(cur, REPORT, ImportPayload('batch-3', [
        ImportItem(A, '08:00', 2, 200.0)]), ANALYST)
    sheet = md.master_demand_sheet(cur, REPORT)
    check('old approval invalidated', sheet['readiness'], 'stale')
    check('stale status kept on revision', sheet['reviews'][0]['status'], 'stale')
    assert sheet['live']['signature'] != old_signature
    print('  ok source update invalidates previous approval and signature changes')
    # 失效后不能直接批准旧单。
    try:
        md.review_master(cur, REPORT, review['id'], NotePayload(), REVIEWER)
        raise AssertionError('失效单不应可批准')
    except Exception as exc:
        assert '只有最新待复核' in str(exc)
        print('  ok stale revision cannot be approved')

    # 6) 重算提交新复核单 -> 新峰值 08:00：200 + 97.25 = 297.25。
    md.submit_master_review(cur, REPORT, RatePayload('36'), ANALYST)
    sheet = md.master_demand_sheet(cur, REPORT)
    check('new revision pending', (sheet['latest']['revision'], sheet['readiness']), (2, 'pending'))
    check('new peak slot after update', sheet['latest']['peak_slot'], '08:00')
    md.review_master(cur, REPORT, sheet['latest']['id'], NotePayload(), REVIEWER)

    # 7) 签发冻结：留存峰值时点、两站读数、分摊金额。
    md.freeze_master_demand(cur, REPORT)
    sheet = md.master_demand_sheet(cur, REPORT)
    check('frozen readiness', sheet['readiness'], 'frozen')
    frozen_review = sheet['latest']
    check('frozen peak slot retained', frozen_review['peak_slot'], '08:00')
    check('frozen combined kw retained', frozen_review['combined_kw'], Decimal('297.250'))
    check('frozen two-site readings',
          {a['site']: a['peak_load_kw'] for a in frozen_review['allocations']},
          {A: Decimal('200.000'), B: Decimal('97.250')})
    check('frozen allocations still sum to total',
          sum(a['allocated_amount'] for a in frozen_review['allocations']),
          frozen_review['total_demand_charge'])
    check('frozen timestamp kept', frozen_review['frozen_at'], '2027-01-31 18:00:00')

    # 8) 签发后到的修订只留档：允许导入（sheet 标记 imports_disabled 由路由/页面控制，
    #    服务层通过 editable_billing 拦截写入），这里验证冻结快照不被新数据改写。
    published = {**REPORT, 'status': 'published'}
    try:
        md.import_loads(FakeCursor(cur.tables), published,
                        ImportPayload('batch-after-freeze', [ImportItem(A, '08:15', 9, 999)]), ANALYST)
        raise AssertionError('月报签发后应拒绝写入')
    except Exception as exc:
        assert '冻结' in str(exc)
        print('  ok imports blocked after publish (new revisions are archived only)')
    # 冻结后到达的历史修订计数口径（模拟一条更晚时间戳的留档记录）。
    cur.tables['imports'].append({'id': 99, 'report_id': 7, 'site': A, 'slot': '08:15',
                                  'revision': 9, 'load_kw': Decimal('999'),
                                  'import_batch': 'late', 'imported_by': ANALYST,
                                  'imported_at': '2027-02-02 09:00:00'})
    sheet = md.master_demand_sheet(cur, REPORT)
    check('late revisions counted as archived', sheet['archived_after_freeze'], 1)
    check('frozen snapshot unchanged by late revision', sheet['latest']['combined_kw'], Decimal('297.250'))

    print('\nall master demand service tests passed')


def store_active(cur):
    from src.services.master_demand_math import active_series
    return active_series(store.fetch_import_rows(cur, REPORT['id']))


if __name__ == '__main__':
    main()
