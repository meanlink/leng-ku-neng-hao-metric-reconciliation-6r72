"""master_demand_math 的标准库单测：python3 -m tests.test_master_demand_math

不依赖数据库或第三方包，覆盖刻钟对齐、乱序修订、峰值选取、
分到分守恒、源数据变化导致签名失效等核心口径。
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.services.master_demand_math import (  # noqa: E402
    active_series, allocate, cents, coverage, evaluate, find_peak,
    is_quarter_hour, review_signature,
)

A, B = '海风一号冷库', '东港周转冷库'
SITES = (A, B)
RATE = Decimal('36.0000')


def row(site, slot, revision, load):
    return {'site': site, 'slot': slot, 'revision': revision, 'load_kw': Decimal(str(load))}


def check(name, got, expected):
    assert got == expected, f'{name}: got {got!r}, expected {expected!r}'
    print(f'  ok {name}')


def test_quarter_hour():
    for slot in ('00:00', '08:15', '23:45', '12:30'):
        assert is_quarter_hour(slot), slot
    for slot in ('08:01', '24:00', '8:15', '0815', '', None):
        assert not is_quarter_hour(slot), slot
    print('test_quarter_hour')


def test_highest_revision_wins_and_disorder_kept():
    # 先到高修订号，再乱序补发低修订号：旧记录不能盖掉新版。
    rows = [
        row(A, '08:00', 3, '110.000'),
        row(A, '08:00', 1, '100.000'),
        row(A, '08:00', 2, '105.000'),
        row(B, '08:00', 1, '90.000'),
    ]
    series = active_series(rows)
    check('highest revision regardless of order', series[(A, '08:00')], Decimal('110.000'))
    check('other site untouched', series[(B, '08:00')], Decimal('90.000'))
    print('test_highest_revision_wins_and_disorder_kept')


def test_missing_slot_pauses_review():
    rows = [row(A, '08:00', 1, 100), row(A, '08:15', 1, 110), row(B, '08:00', 1, 80)]
    series = active_series(rows)
    common, missing = coverage(series, SITES)
    check('common slots', common, ['08:00'])
    check('missing on station B', missing[B], ['08:15'])
    check('no missing on station A', missing[A], [])
    result = evaluate(rows, SITES, RATE)
    check('readiness incomplete', result['readiness'], 'incomplete')
    assert result['peak_slot'] is None and result['allocations'] is None
    print('test_missing_slot_pauses_review')


def test_peak_uses_combined_load_and_tie_earliest():
    rows = [
        row(A, '08:00', 1, '100.000'), row(B, '08:00', 1, '80.000'),   # 180
        row(A, '08:15', 1, '120.000'), row(B, '08:15', 1, '90.000'),   # 210 peak
        row(A, '08:30', 1, '130.000'), row(B, '08:30', 1, '80.000'),   # 210 tie -> earlier slot wins
    ]
    result = evaluate(rows, SITES, RATE)
    check('readiness ready', result['readiness'], 'ready')
    check('earliest tie slot', result['peak_slot'], '08:15')
    check('combined peak kw', result['combined_kw'], Decimal('210.000'))
    check('station A load at peak', result['peak_loads'][A], Decimal('120.000'))
    check('station B load at peak', result['peak_loads'][B], Decimal('90.000'))
    print('test_peak_uses_combined_load_and_tie_earliest')


def test_total_charge_and_cent_conservation():
    # 210 kW × 36 = 7560.00；比例 120:90 = 4:3 -> 4320.00 / 3240.00
    peak = {'peak_loads': {A: Decimal('120.000'), B: Decimal('90.000')}}
    shares = allocate(peak['peak_loads'], Decimal('7560.00'))
    check('A share', shares[A], Decimal('4320.00'))
    check('B share', shares[B], Decimal('3240.00'))
    check('sum equals total', sum(shares.values(), Decimal('0')), Decimal('7560.00'))
    # 除不尽的尾差用最大余数法补到分，合计仍然守恒。
    odd = allocate({A: Decimal('100.000'), B: Decimal('100.000'), }, Decimal('0.01'))
    check('odd remainder total', sum(odd.values(), Decimal('0')), Decimal('0.01'))
    tricky = allocate({A: Decimal('1.000'), B: Decimal('1.000'), }, Decimal('10.05'))
    check('tricky sum', sum(tricky.values(), Decimal('0')), Decimal('10.05'))
    print('test_total_charge_and_cent_conservation')


def test_largest_remainder_distribution():
    # 总额 100.00，比例 1:1:1 -> 33.33 各，余 0.01 给排序首位。
    sites = (A, B, '三号库')
    shares = allocate({s: Decimal('10.000') for s in sites}, Decimal('100.00'))
    check('three-way sum', sum(shares.values(), Decimal('0')), Decimal('100.00'))
    check('first by name gets the extra cent', shares[sorted(sites)[0]], Decimal('33.34'))
    print('test_largest_remainder_distribution')


def test_signature_changes_when_source_updates():
    rows = [row(A, '08:00', 1, '100.000'), row(B, '08:00', 1, '80.000')]
    before = evaluate(rows, SITES, RATE)
    # A 站修订号升高、读数改变
    rows.append(row(A, '08:00', 2, '130.000'))
    after = evaluate(rows, SITES, RATE)
    assert before['signature'] != after['signature'], '有效源数据更新后签名必须变化'
    check('new combined peak', after['combined_kw'], Decimal('210.000'))
    # 同值的更高修订号不改变签名（口径结果不变），但乱序旧版到达也不影响。
    rows.append(row(A, '08:00', 1, '100.000'))
    again = evaluate(rows, SITES, RATE)
    check('stale revision cannot override', again['signature'], after['signature'])
    print('test_signature_changes_when_source_updates')


def test_full_evaluate_conservation():
    rows = [
        row(A, '08:00', 1, '168.500'), row(B, '08:00', 1, '97.250'),
        row(A, '08:15', 1, '172.000'), row(B, '08:15', 1, '101.750'),
    ]
    result = evaluate(rows, SITES, Decimal('38.5000'))
    # 峰值 273.75 kW × 38.5 = 10539.375 -> 10539.38
    check('peak slot', result['peak_slot'], '08:15')
    check('combined', result['combined_kw'], Decimal('273.750'))
    check('total demand charge rounded', result['total_demand_charge'], Decimal('10539.38'))
    check('allocation conservation',
          sum(result['allocations'].values(), Decimal('0')), result['total_demand_charge'])
    print('test_full_evaluate_conservation')


def main():
    test_quarter_hour()
    test_highest_revision_wins_and_disorder_kept()
    test_missing_slot_pauses_review()
    test_peak_uses_combined_load_and_tie_earliest()
    test_total_charge_and_cent_conservation()
    test_largest_remainder_distribution()
    test_signature_changes_when_source_updates()
    test_full_evaluate_conservation()
    print('\nall master demand math tests passed')


if __name__ == '__main__':
    main()
