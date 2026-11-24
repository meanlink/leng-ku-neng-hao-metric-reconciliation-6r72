from src.billing_schemas import BillIn, TariffIn
from src.db import db_cursor
from src.routes.billing import create_tariff, file_invoice
from src.routes.reconcile import ReviewIn, RunIn, review, run
from src.routes.reports import PublishIn, make_draft
from src.services.bill_store import bill_history
from src.services.tariffs import latest_tariffs

PERIOD = '2027-01'
SITE_CODES = ('MTR-HAI-01', 'MTR-HAI-03')


def seed_billing():
    with db_cursor(commit=True) as cur:
        cur.execute('SELECT id AS sub, role FROM users')
        users = {row['role']: row for row in cur.fetchall()}
        cur.execute('SELECT id FROM metric_contracts WHERE metric_code=%s AND version=%s', ('CS-EUI', 'v2026.10'))
        contract = cur.fetchone()['id']
        cur.execute('SELECT id, source_code, site FROM meter_sources WHERE source_code IN (%s, %s)', SITE_CODES)
        sources = {row['source_code']: row for row in cur.fetchall()}
        cur.execute('SELECT * FROM monthly_reports WHERE period=%s', (PERIOD,))
        report = cur.fetchone()
        if not report:
            for code, value, ton_days in zip(SITE_CODES, (12000, 6200), (6800, 3600)):
                cur.execute('''INSERT IGNORE INTO meter_readings(source_id,period,reading_date,value,unit,cycle,
                                   throughput_ton_day,import_batch) VALUES(%s,%s,'2027-01-31',%s,'kWh',
                                   'monthly',%s,'jan-close-meter-export-2027')''',
                            (sources[code]['id'], PERIOD, value, ton_days))
    if not report:
        for code in SITE_CODES:
            case = run(RunIn(period=PERIOD, contract_id=contract, source_id=sources[code]['id']), users['analyst'])
            if case['status'] != 'closed':
                review(case['id'], ReviewIn(action='close', note='已核对一月电表导出和出入库吨日。'), users['reviewer'])
        report = make_draft(PublishIn(period=PERIOD, contract_id=contract), users['analyst'])
    if report['status'] == 'published':
        return
    with db_cursor() as cur:
        tariffs = latest_tariffs(cur, report['id'])
        histories = bill_history(cur, report['id'])
    main_site, east_site = (sources[code]['site'] for code in SITE_CODES)
    examples = [
        TariffIn(site=main_site, contract_ref='BL-2027-01-A', energy_rate='0.8200', demand_rate='36.0000',
                 minimum_kw=180, fixed_fee=120, charge_tolerance=2),
        TariffIn(site=east_site, contract_ref='DG-2027-01-B', energy_rate='0.7600', demand_rate=0,
                 minimum_kw=0, fixed_fee=60, charge_tolerance=2),
    ]
    for tariff in examples:
        if tariff.site not in tariffs:
            create_tariff(report['id'], tariff, users['manager'])
    if main_site not in histories:
        file_invoice(report['id'], BillIn(site=main_site, invoice_number='BL-2701-017', invoiced_kwh=12000,
                     observed_peak_kw=169, invoiced_demand_kw=200, invoice_subtotal=17160,
                     evidence='BL-2701-017.pdf 第 2 页。最大需量记录显示 169 kW，账单计费栏为 200 kW。'), users['analyst'])
    seed_master_demand(cur, report['id'], main_site, east_site, users)
    print('billing seed ok: January 2027, 2 sites, tariff demand floor and pending 720 CNY exception')


def seed_master_demand(cur, report_id, main_site, east_site, users):
    """总表需量费分摊演示：两站每刻钟负荷，含乱序补发的修订号。"""
    cur.execute('SELECT COUNT(*) AS n FROM master_demand_imports WHERE report_id=%s', (report_id,))
    if cur.fetchone()['n']:
        return
    # 两站同刻对齐；峰值出现在 09:15：主站 176.5 + 东站 105.25 = 281.75 kW。
    main_loads = {'08:00': '168.500', '08:15': '172.000', '08:30': '170.250',
                  '08:45': '174.750', '09:00': '173.500', '09:15': '176.500', '09:30': '171.000'}
    east_loads = {'08:00': '97.250', '08:15': '99.000', '08:30': '101.750',
                  '08:45': '100.500', '09:00': '103.250', '09:15': '105.250', '09:30': '102.000'}
    rows = []
    for slot, load in main_loads.items():
        rows.append((report_id, main_site, slot, 1, load, 'jan-quarter-hour-export-2027', users['analyst']['id']))
    for slot, load in east_loads.items():
        rows.append((report_id, east_site, slot, 1, load, 'jan-quarter-hour-export-2027', users['analyst']['id']))
    # 乱序补发：先到主站 09:15 的修订 3，再到更旧的修订 2，验证旧版不能盖掉新版。
    rows.append((report_id, main_site, '09:15', 3, '176.500', 'jan-late-resend-2027', users['analyst']['id']))
    rows.append((report_id, main_site, '09:15', 2, '175.000', 'jan-late-resend-2027', users['analyst']['id']))
    cur.executemany('''INSERT INTO master_demand_imports(report_id,site,slot,revision,load_kw,
                            import_batch,imported_by) VALUES(%s,%s,%s,%s,%s,%s,%s)''', rows)
    print('master demand seed ok: 2 sites x 7 quarter slots, out-of-order revisions at 09:15')
