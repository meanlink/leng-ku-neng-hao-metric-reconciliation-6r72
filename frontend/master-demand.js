// 总表需量费分摊：两座冷库共用总表，按两站同刻负荷之和的月峰值核验需量费并分摊。

const readiness = {
  no_imports: ['warn', '未导入负荷'],
  incomplete: ['red', '刻钟缺失 · 暂停复核'],
  ready_to_review: ['warn', '可提交复核'],
  pending: ['warn', '待复核'],
  approved: ['green', '已批准'],
  stale: ['red', '源数据更新 · 旧审批失效'],
  frozen: ['green', '已冻结'],
};
const tag = key => {
  const [cls, text] = readiness[key] || ['warn', key];
  return `<span class="tag ${cls}">${text}</span>`;
};

// 导出解析逻辑，便于在无浏览器环境下做边界单测。
export function parseCsvLoads(text) {
  const rows = String(text || '').split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  const loads = [];
  rows.forEach((line, index) => {
    const parts = line.split(',').map(part => part.trim());
    if (parts.length !== 4) throw new Error(`第 ${index + 1} 行格式应为：站点,刻钟,修订号,负荷kW`);
    const [site, slot, revisionText, loadText] = parts;
    const revision = Number(revisionText);
    const loadKw = Number(loadText);
    if (!/^\d{2}:\d{2}$/.test(slot) || ![0, 15, 30, 45].includes(Number(slot.slice(3)))) {
      throw new Error(`第 ${index + 1} 行刻钟须为 HH:MM 且分钟为 00/15/30/45`);
    }
    if (!Number.isInteger(revision) || revision < 1) throw new Error(`第 ${index + 1} 行修订号须为正整数`);
    if (!Number.isFinite(loadKw) || loadKw < 0) throw new Error(`第 ${index + 1} 行负荷须为非负数字`);
    loads.push({ site, slot, revision, load_kw: loadKw });
  });
  if (!loads.length) throw new Error('未解析到任何负荷记录');
  return loads;
}

export function masterDemand({ api, esc, fmt, openForm, toast, render, state }) {
  let sheet;

  const root = () => `/utility-billing/${sheet.report_id}/master-demand`;
  const role = () => state.user.role;
  const canEdit = () => ['analyst', 'manager'].includes(role());
  const canReview = () => ['reviewer', 'manager'].includes(role());

  function missingNotice() {
    const entries = Object.entries(sheet.live.missing_slots || {}).filter(([, slots]) => slots.length);
    if (!entries.length) return '';
    return `<div class="notice warn">任一站缺时点即暂停复核：${entries.map(([site, slots]) =>
      `${esc(site)} 缺 ${slots.length} 个刻钟（${slots.slice(0, 6).map(esc).join('、')}${slots.length > 6 ? '…' : ''}）`).join('；')}</div>`;
  }

  function coverageTable() {
    const live = sheet.live;
    return `<table class="billing-compare"><thead><tr><th>站点</th><th>已导入刻钟数</th><th>共同刻钟数</th><th>缺失</th></tr></thead><tbody>
      ${sheet.sites.map(site => `<tr><th scope="row">${esc(site)}</th>
        <td>${fmt(live.imported_counts[site] || 0, 0)}</td>
        <td>${fmt(live.common_count, 0)}</td>
        <td>${fmt((live.missing_slots[site] || []).length, 0)}</td></tr>`).join('')}
    </tbody></table>`;
  }

  function allocationTable(review) {
    if (!review) return '';
    return `<table class="billing-compare"><thead><tr><th>站点</th><th>峰值时刻负荷 kW</th><th>分摊需量费 元</th></tr></thead><tbody>
      ${review.allocations.map(a => `<tr><th scope="row">${esc(a.site)}</th>
        <td>${fmt(a.peak_load_kw, 3)}</td><td>${fmt(a.allocated_amount, 2)}</td></tr>`).join('')}
      <tr><th scope="row">合计</th><td>${fmt(review.combined_kw, 3)}</td><td><b>${fmt(review.total_demand_charge, 2)}</b></td></tr>
      </tbody></table>`;
  }

  function reviewHistory() {
    if (!sheet.reviews.length) return '';
    return `<details class="billing-history"><summary>分摊复核记录（${sheet.reviews.length} 版）</summary><ol>
      ${sheet.reviews.map(v => `<li><b>第 ${v.revision} 版</b> ${tag(v.status)}
        <div>峰值时点 ${esc(v.peak_slot)} · 两站合计 ${fmt(v.combined_kw, 3)} kW · 需量单价 ${fmt(v.demand_rate, 4)} 元/kW</div>
        <div>总表需量费 ${fmt(v.total_demand_charge, 2)} 元 · 签名 <code>${esc(v.review_signature)}</code></div>
        ${v.review_note ? `<div>复核 ${esc(v.reviewed_name)} · ${esc(v.review_note)}</div>` : ''}
        ${v.frozen_at ? `<div class="kpi-label">冻结于 ${esc(v.frozen_at.replace('T', ' '))}</div>` : ''}</li>`).join('')}
      </ol></details>`;
  }

  function actions() {
    if (sheet.status === 'published') {
      return `<div class="kpi-label">月报已签发，导入入口关闭；签发后到的修订只留档（${sheet.archived_after_freeze} 条）。</div>`;
    }
    const buttons = [];
    if (canEdit()) buttons.push(`<button class="btn" data-master="import">导入每刻钟负荷</button>`);
    const latest = sheet.latest;
    const canSubmit = canEdit() && sheet.readiness !== 'no_imports';
    if (canSubmit && !latest?.frozen_at) {
      buttons.push(`<button class="btn secondary" data-master="submit">${latest?.status === 'pending' ? '按当前数据重算' : '提交分摊复核'}</button>`);
    }
    if (latest?.status === 'pending' && canReview() && latest.submitted_by !== state.user.id && sheet.readiness === 'pending') {
      buttons.push(`<button class="btn" data-master="approve">批准分摊复核</button>`);
    }
    return `<div class="actions billing-actions">${buttons.join('')}</div>`;
  }

  function livePreview() {
    const live = sheet.live;
    if (live.readiness !== 'ready') return '';
    return `<div class="billing-tariff"><b>当前有效源数据测算</b>
      <div>月峰值时点 <b>${esc(live.peak_slot)}</b> · 两站同刻合计 ${fmt(live.combined_kw, 3)} kW</div>
      <div>${Object.entries(live.peak_loads).map(([site, kw]) => `${esc(site)} ${fmt(kw, 3)} kW`).join(' · ')}</div>
      ${live.demand_rate !== null ? `<div>按已登记需量单价 ${fmt(live.demand_rate, 4)} 元/kW 测算总额 ${fmt(live.total_demand_charge, 2)} 元，分摊合计与总额一致。</div>` : ''}
    </div>`;
  }

  async function page(reportId) {
    sheet = await api(`/utility-billing/${reportId}/master-demand`);
    const latest = sheet.latest;
    return `<section class="card" aria-label="总表需量费分摊">
      <div class="section-title">总表需量费分摊（两站共用总表） ${tag(sheet.readiness)}</div>
      <div class="notice">按两站同一刻钟负荷之和的月峰值核验总表需量费，再按峰值时刻各站负荷比例分摊；同站同刻采纳最高修订号，乱序补发的旧记录不会覆盖新版。</div>
      ${missingNotice()}
      ${coverageTable()}
      ${livePreview()}
      ${latest ? `<div class="billing-tariff" style="margin-top:14px"><b>第 ${latest.revision} 版分摊复核单</b>
        <div>峰值时点 ${esc(latest.peak_slot)} · 两站合计 ${fmt(latest.combined_kw, 3)} kW · 需量单价 ${fmt(latest.demand_rate, 4)} 元/kW</div>
        ${allocationTable(latest)}
        ${latest.frozen_at ? `<div class="kpi-label">已随月报签发冻结：峰值时点、两站读数与分摊金额均留痕。</div>` : ''}
      </div>` : '<p class="kpi-label" style="margin-top:14px">尚无分摊复核单。</p>'}
      ${reviewHistory()}
      ${actions()}
    </section>`;
  }

  function openImport() {
    const siteHint = sheet.sites.join('、');
    openForm('导入两站每刻钟负荷', `
      <div class="notice full">每行一条：站点,刻钟(HH:MM),修订号,负荷kW。同一刻钟补发请使用更高修订号；支持乱序粘贴。站点须为：${esc(siteHint)}</div>
      <label class="field full">导入批次
        <input name="import_batch" required minlength="4" maxlength="60" value="master-q1-export"></label>
      <label class="field full">负荷记录（CSV，可含表头行）
        <textarea name="loads_text" rows="10" required
          placeholder="站点,刻钟,修订号,负荷kW&#10;${esc(sheet.sites[0] || '站点甲')},08:00,1,168.5"></textarea></label>`,
      '导入负荷', async fields => {
        const loads = parseLoads(fields.loads_text);
        await api(`${root()}/imports`, {
          method: 'POST',
          body: JSON.stringify({ import_batch: fields.import_batch, loads }),
        });
        toast(`已导入 ${loads.length} 条刻钟负荷`);
        render();
      }, '负荷已导入');
  }

  function parseLoads(text) {
    return parseCsvLoads(text);
  }

  function openSubmit() {
    const lastRate = sheet.latest?.demand_rate ?? 36;
    openForm('提交总表需量费分摊复核', `
      <div class="notice full">系统按当前有效源数据（最高修订号）自动取月峰值并分摊，只需登记总表需量单价。金额四舍五入到分，分摊合计恒等于总表需量费。</div>
      <label class="field full">总表需量单价（元/kW）
        <input type="number" name="demand_rate" step="0.0001" min="0.0001" value="${esc(lastRate)}" required></label>`,
      '提交复核', async fields => {
        await api(`${root()}/reviews`, {
          method: 'POST',
          body: JSON.stringify({ demand_rate: Number(fields.demand_rate) }),
        });
        toast('分摊复核单已提交');
        render();
      }, '复核单已提交');
  }

  function openApprove() {
    const latest = sheet.latest;
    openForm('批准总表需量费分摊', `
      <div class="notice full">第 ${latest.revision} 版 · 峰值 ${esc(latest.peak_slot)} · 合计 ${fmt(latest.combined_kw, 3)} kW · 总额 ${fmt(latest.total_demand_charge, 2)} 元</div>
      <label class="field full">复核意见<textarea name="note" rows="3" required minlength="4" maxlength="500">已与总表最大需量记录及两站刻钟曲线核对一致。</textarea></label>`,
      '批准复核', async fields => {
        await api(`${root()}/reviews/${latest.id}/decision`, {
          method: 'POST',
          body: JSON.stringify({ decision: 'approved', note: fields.note }),
        });
        toast('分摊复核已批准');
        render();
      }, '复核意见已记录');
  }

  function bind() {
    document.querySelectorAll('[data-master]').forEach(button => {
      button.onclick = () => {
        const command = button.dataset.master;
        if (command === 'import') return openImport();
        if (command === 'submit') return openSubmit();
        if (command === 'approve') return openApprove();
      };
    });
  }

  return { page, bind };
}
