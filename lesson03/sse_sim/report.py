"""生成不依赖网络的可交互回放报告。"""
import json


def write_report(path, summary, frames, events, trades):
    payload = json.dumps({"summary": summary, "frames": frames, "events": events, "trades": trades}, ensure_ascii=False).replace("</", "<\\/")
    document = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>上交所数据回放 · 模拟交易系统</title>
<style>
:root{font-family:system-ui,sans-serif;color:#172b45;background:#f2f5fa}body{max-width:1250px;margin:30px auto;padding:0 20px}h1{font-size:27px;margin-bottom:8px}p{line-height:1.7;color:#52657c}.cards,.grid{display:grid;gap:16px}.cards{grid-template-columns:repeat(4,1fr);margin:22px 0}.card,.panel{background:white;border:1px solid #dde5ee;border-radius:12px;padding:18px}.value{font-size:24px;font-weight:650;color:#174f8c}.label{font-size:13px;color:#52657c;margin-bottom:8px}.grid{grid-template-columns:1fr 1fr}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:right;padding:8px;border-bottom:1px solid #edf1f5}th:first-child,td:first-child{text-align:left}select,button{padding:8px 12px;border:1px solid #b8c9dc;background:white;border-radius:6px}button{cursor:pointer}input[type=range]{width:100%}.controls{display:flex;gap:14px;align-items:center;margin:16px 0}canvas{width:100%;height:200px}.notice{background:#fff4df;border-left:4px solid #ddab42;padding:12px 16px;font-size:13px;line-height:1.8}.scroll{max-height:350px;overflow:auto}.buy{color:#c63c46}.sell{color:#198a67}pre{white-space:pre-wrap;font-size:12px}@media(max-width:800px){.grid{grid-template-columns:1fr}.cards{grid-template-columns:1fr 1fr}}
</style>
<h1>上交所数据回放 · 模拟交易系统</h1>
<p id="subtitle"></p>
<div class="notice">模拟成交使用订单提交延迟之后的下一份可见十档快照；不会再次撮合历史剩余委托。它是无市场冲击的教学模型，成交记录均为模拟结果。独立撮合内核另行演示集合竞价与价格/时间优先。资金及价格以整数分记账。</div>
<div class="cards" id="cards"></div>
<div class="panel"><div class="controls"><label>证券 <select id="symbol"></select></label><button id="play">播放</button><span id="clock"></span></div>
<input id="cursor" type="range" min="0" value="0"><canvas id="chart" width="1100" height="220"></canvas><p id="account"></p></div>
<div class="grid" style="margin-top:16px"><section class="panel"><h3>已可见的行情快照 · 十档盘口</h3><div id="snapshot"></div></section><section class="panel"><h3>逐笔数据重建的历史剩余订单簿</h3><div id="historical"></div><p id="alignment"></p></section></div>
<div class="grid" style="margin-top:16px"><section class="panel"><h3>模拟成交回报</h3><div id="fills" class="scroll"></div></section><section class="panel"><h3>委托、撤单与规则拒绝</h3><div id="events" class="scroll"></div></section></div>
<section class="panel" style="margin-top:16px"><h3>最终账户与验证边界</h3><div id="positions"></div><p id="limits"></p><details><summary>数据诊断、独立撮合演示及完整统计</summary><pre id="details"></pre></details></section>
<script>
const DATA=__PAYLOAD__, S=DATA.summary; const el=id=>document.getElementById(id);
const money=n=>(n/100).toFixed(2), time=n=>{let h=Math.floor(n/3600000),m=Math.floor(n%3600000/60000),s=Math.floor(n%60000/1000);return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${String(n%1000).padStart(3,'0')}`};
function table(target,headers,rows){const t=document.createElement('table'),head=document.createElement('tr');for(const h of headers){const th=document.createElement('th');th.textContent=h;head.appendChild(th)}t.appendChild(head);for(const row of rows){const tr=document.createElement('tr');for(const v of row){const td=document.createElement('td');td.textContent=v;tr.appendChild(td)}t.appendChild(tr)}el(target).replaceChildren(t)}
el('subtitle').textContent=`${S.date} · ${S.symbols.length} 只证券 · 截止 ${time(S.end_ms)} · 上交所2026年规则 · 本地回放`;
for(const [label,value] of [['回放事件',S.events_processed.toLocaleString()],['模拟成交',S.simulated_fills],['最终可用资金（元）',money(S.account.available_cash_cents)],['累计费用（元）',money(S.fees_cents)]]){const d=document.createElement('div');d.className='card';const l=document.createElement('div');l.className='label';l.textContent=label;const v=document.createElement('div');v.className='value';v.textContent=value;d.append(l,v);el('cards').appendChild(d)}
for(const symbol of S.symbols){const option=document.createElement('option');option.value=symbol;option.textContent=symbol;el('symbol').appendChild(option)}
let list=[],ticker=null;
function book(target,bids,asks){let rows=[];for(let i=0;i<10;i++){const b=bids[i],a=asks[i];rows.push([i+1,b?money(b[0]):'—',b?b[1]:'—',a?money(a[0]):'—',a?a[1]:'—'])}table(target,['档位','买价','买量（股）','卖价','卖量（股）'],rows)}
function drawChart(index){const c=el('chart'),ctx=c.getContext('2d');ctx.clearRect(0,0,c.width,c.height);const points=list.slice(0,index+1).filter(f=>f.price>0);if(!points.length)return;let low=Math.min(...points.map(x=>x.price)),high=Math.max(...points.map(x=>x.price));if(low===high){low--;high++}ctx.strokeStyle='#ccd7e4';ctx.beginPath();ctx.moveTo(55,10);ctx.lineTo(55,190);ctx.lineTo(1080,190);ctx.stroke();ctx.fillStyle='#52657c';ctx.font='12px sans-serif';ctx.fillText(money(high),0,20);ctx.fillText(money(low),0,190);ctx.strokeStyle='#245fa2';ctx.lineWidth=2;ctx.beginPath();points.forEach((f,i)=>{const x=55+i/Math.max(1,points.length-1)*1025,y=180-(f.price-low)/(high-low)*160;if(!i)ctx.moveTo(x,y);else ctx.lineTo(x,y)});ctx.stroke()}
function render(){const index=Number(el('cursor').value),f=list[index];if(!f){el('clock').textContent='所选证券没有快照';return}el('clock').textContent=`${time(f.time_ms)} · ${index+1}/${list.length}`;book('snapshot',f.bids,f.asks);book('historical',f.historical_bids,f.historical_asks);el('alignment').textContent=`快照和逐笔数据的同毫秒顺序不能完全确定；该帧买卖最优价一致：${f.best_price_match===null?'未比较':f.best_price_match?'是':'否'}`;el('account').textContent=`此时可用资金 ${money(f.cash_available)} 元，${f.symbol} 可卖 ${f.sellable} 股、今日买入锁定 ${f.today} 股`;drawChart(index);const fills=DATA.trades.filter(t=>t.symbol===f.symbol&&t.time_ms<=f.time_ms);table('fills',['时间','方向','价格','数量','费用（元）'],fills.map(t=>[time(t.time_ms),t.side,money(t.price_cents),t.quantity,money(t.fee_cents)]));const events=DATA.events.filter(e=>e.symbol===f.symbol||!e.symbol).filter(e=>e.time_ms<=f.time_ms).slice(-70);table('events',['时间','事件','订单','说明'],events.map(e=>[time(e.time_ms),e.event,e.order_id||'—',e.reason||e.purpose||'']));}
function select(){list=DATA.frames.filter(f=>f.symbol===Number(el('symbol').value));el('cursor').max=Math.max(0,list.length-1);el('cursor').value=0;render()}
el('symbol').onchange=select;el('cursor').oninput=render;el('play').onclick=()=>{if(ticker){clearInterval(ticker);ticker=null;el('play').textContent='播放';return}el('play').textContent='暂停';ticker=setInterval(()=>{const v=Number(el('cursor').value);if(v>=Number(el('cursor').max)){el('play').click();return}el('cursor').value=v+1;render()},180)};
table('positions',['证券','昨日可卖库存','今日买入（T+1）','冻结股数','可卖股数'],Object.entries(S.account.positions).map(([k,v])=>[k,v.yesterday,v.today,v.frozen,v.sellable]));el('limits').textContent=S.limitations.join('；');el('details').textContent=JSON.stringify(S,null,2);select();
</script></html>'''
    path.write_text(document.replace("__PAYLOAD__", payload), encoding="utf-8")
