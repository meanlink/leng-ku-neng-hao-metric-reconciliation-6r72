// 前端 CSV 刻钟负荷解析的边界单测：node tests/parse_loads_test.mjs
import assert from 'node:assert/strict';
import { parseCsvLoads } from '../../frontend/master-demand.js';

// 正常批量（含乱序修订号）
const loads = parseCsvLoads(`
站点甲,08:00,1,168.5
站点甲,08:00,3,180
站点甲,08:00,2,175
站点乙,08:15,1,99.0
`);
assert.equal(loads.length, 4);
const disordered = loads.filter(x => x.site === '站点甲' && x.slot === '08:00')
  .reduce((a, b) => (b.revision > a.revision ? b : a));
assert.equal(disordered.load_kw, 180, '乱序数据应完整保留修订号，由后端取最高修订号');
assert.deepEqual(loads[0], { site: '站点甲', slot: '08:00', revision: 1, load_kw: 168.5 });

// 非法刻钟
assert.throws(() => parseCsvLoads('站点甲,08:07,1,100'), /刻钟须为/);
// 非法修订号
assert.throws(() => parseCsvLoads('站点甲,08:00,0,100'), /修订号须为正整数/);
assert.throws(() => parseCsvLoads('站点甲,08:00,1.5,100'), /修订号须为正整数/);
// 非法负荷
assert.throws(() => parseCsvLoads('站点甲,08:00,1,-5'), /负荷须为非负数字/);
// 列数不对
assert.throws(() => parseCsvLoads('站点甲,08:00,1'), /格式应为/);
// 空输入
assert.throws(() => parseCsvLoads('   \n '), /未解析到任何负荷记录/);
// 零负荷合法
assert.equal(parseCsvLoads('站点甲,00:00,1,0')[0].load_kw, 0);

console.log('frontend parseCsvLoads tests passed');
