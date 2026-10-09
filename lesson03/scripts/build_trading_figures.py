"""从原始回放与成交观察生成报告插图，不依赖桌面界面。"""
from pathlib import Path
import argparse
import json
import os
import sys

os.environ.setdefault('MPLCONFIGDIR','/tmp/quantllm-matplotlib')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sse_sim.rules import parse_time,phase_at,Phase

FONT=Path('/usr/local/share/fonts/custom/simhei.ttf')
if FONT.exists():
    font_manager.fontManager.addfont(str(FONT))
    plt.rcParams['font.family']=font_manager.FontProperties(fname=str(FONT)).get_name()
plt.rcParams.update({'axes.unicode_minus':False,'font.size':11,'axes.titlesize':16,
                    'axes.spines.top':False,'axes.spines.right':False,
                    'figure.facecolor':'#ffffff','axes.facecolor':'#ffffff',
                    'savefig.facecolor':'#ffffff','svg.fonttype':'path'})
BLUE='#2474a7'; RED='#c95355'; GREEN='#248b77'; GOLD='#d89b35'; PURPLE='#7765a4'; GRAY='#aab5c3'


def save(fig,out,name):
    fig.savefig(out/(name+'.png'),dpi=180,bbox_inches='tight')
    fig.savefig(out/(name+'.svg'),bbox_inches='tight')
    plt.close(fig)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'reports/trading_knowledge/figures')
    args=parser.parse_args();out=args.output;out.mkdir(parents=True,exist_ok=True)
    obs=json.loads((ROOT/'runs/trade_observations/observations.json').read_text())
    summary=json.loads((ROOT/'runs/runtime_full_day/summary.json').read_text())
    frames=json.loads((ROOT/'runs/runtime_full_day/frames.json').read_text())
    names={601600:'中国铝业 601600',600036:'招商银行 600036',688981:'中芯国际 688981'}
    colors={601600:BLUE,600036:GREEN,688981:PURPLE}
    start=parse_time('09:30:00')

    # 1. 完整可见价格轨迹，午间断线，避免暗示午间成交。
    fig,ax=plt.subplots(figsize=(11,4.5),layout='constrained')
    for sym in summary['symbols']:
        fs=[f for f in frames if f['symbol']==sym and f['price']>0]
        x=[];y=[]
        for i,f in enumerate(fs):
            if i and f['time_ms']-fs[i-1]['time_ms']>60000:
                x.append(np.nan);y.append(np.nan)
            x.append((f['time_ms']-start)/60000)
            y.append((f['price']/summary['limit_prices'][str(sym)]['previous_close']-1)*100)
        ax.plot(x,y,color=colors[sym],lw=1.3,label=names[sym])
    ax.axvspan(120,210,color='#eef1f5',label='午间非连续竞价时段')
    ax.axhline(0,color=GRAY,lw=1,ls='--')
    ax.set_xticks([0,30,60,90,120,210,240,270,300,330],
                  ['09:30','10:00','10:30','11:00','11:30','13:00','13:30','14:00','14:30','15:00'])
    ax.set(xlabel='行情时间',ylabel='相对前收盘价变化（%）',title='市场价格继续变化，演示订单却只在开盘提交一次')
    ax.legend(ncol=2,fontsize=10);ax.grid(axis='y',alpha=.15)
    save(fig,out,'01_market_prices')

    # 2. 将提交、模型接受和实际模拟成交分开。
    fig,ax=plt.subplots(figsize=(11,4.8),layout='constrained')
    rows=[]
    for b in obs['baseline']:
        rows.extend((b['symbol'],t) for t in b['trades'])
    rows.sort(key=lambda pair:(pair[0],pair[1]['side']))
    for i,(sym,t) in enumerate(rows):
        submit=(t['submitted_ms']-start)/1000;accepted=(t['accepted_ms']-start)/1000;fill=(t['time_ms']-start)/1000
        ax.barh(i,accepted-submit,left=submit,height=.28,color=GRAY)
        ax.barh(i,fill-accepted,left=accepted,height=.28,color=colors[sym])
        ax.scatter(submit,i,marker='o',s=45,c='white',edgecolors=colors[sym],zorder=3)
        ax.scatter(accepted,i,marker='s',s=32,c=GRAY,zorder=3)
        ax.scatter(fill,i,marker='D',s=48,c=colors[sym],zorder=4)
        ax.text(fill+.5,i,f"{t['price_cents']/100:.2f} 元 / {t['quantity']} 股",va='center',fontsize=10)
    ax.set_yticks(range(len(rows)),[f"{sym} {'买入' if t['side']=='BUY' else '卖出'}" for sym,t in rows])
    ax.invert_yaxis();ax.set_xlim(-.8,33)
    ax.set(xlabel='距离 09:30:00 的秒数',title='下单、接受、成交是三个不同的时刻')
    ax.scatter([],[],marker='o',facecolors='white',edgecolors=BLUE,label='提交')
    ax.scatter([],[],marker='s',c=GRAY,label='模型接受')
    ax.scatter([],[],marker='D',c=BLUE,label='模拟成交')
    ax.legend(loc='lower right',ncol=3,fontsize=10);ax.grid(axis='x',alpha=.15)
    save(fig,out,'02_order_timeline')

    # 3. 费用对小额价差的影响。
    fig,ax=plt.subplots(figsize=(10,4.5),layout='constrained')
    x=np.arange(3);bs=obs['baseline']
    for offset,key,label,color in [(-.25,'gross_cashflow_cents','价差盈亏',BLUE),
                                   (0,'fees_cents','费用扣除',GOLD),(.25,'net_cashflow_cents','扣费净变化',RED)]:
        values=[(-1 if key=='fees_cents' else 1)*b[key]/100 for b in bs]
        bars=ax.bar(x+offset,values,width=.23,color=color,label=label)
        ax.bar_label(bars,labels=[f'{v:+.2f}' for v in values],padding=4,fontsize=10)
    ax.axhline(0,color=GRAY,lw=1);ax.set_xticks(x,[names[b['symbol']] for b in bs])
    ax.set(ylabel='人民币元',title='价差合计 0 元，费用合计 49.93 元');ax.legend(ncol=3,loc='lower left')
    ax.set_ylim(-35,7);ax.grid(axis='y',alpha=.15)
    save(fig,out,'03_trading_costs')

    # 4. 期末净值归因，全部是增量而非净值本身。
    fig,ax=plt.subplots(figsize=(10,4.6),layout='constrained')
    components=[('中铝初始库存',-140),('招行初始库存',-220),('中芯初始库存',-1010),('交易价差',0),('交易费用',-49.93)]
    total=0
    for i,(label,v) in enumerate(components):
        bottom=total+v if v<0 else total
        ax.bar(i,abs(v),bottom=bottom,color=GOLD if '费用' in label else (BLUE if v>=0 else RED),width=.65)
        if v==0:ax.plot([i-.3,i+.3],[total,total],color=BLUE,lw=3)
        ax.text(i,bottom+abs(v)/2 if v else total+45,f'{v:,.2f}',ha='center',va='center',fontsize=11)
        total+=v
        ax.plot([i+.325,i+.675],[total,total],color=GRAY,ls='--',lw=1)
    assert round(total,2)==summary['marked_pnl_cents']/100
    ax.bar(5,total,color=PURPLE,width=.65)
    ax.text(5,total/2,f'{total:,.2f}',color='white',ha='center',va='center',fontsize=12)
    ax.set_xticks(range(6),[a for a,_ in components]+['总净值变化'])
    ax.set(ylabel='相对前收盘基准的净值变化（元）',title='1,419.93 元净值减少中，1,370 元来自初始库存跌价')
    ax.set_ylim(-1550,130);ax.axhline(0,color=GRAY,lw=1);ax.grid(axis='y',alpha=.12)
    save(fig,out,'04_nav_attribution')

    # 5. 股数不变，可卖库存却发生改变。
    fig,ax=plt.subplots(figsize=(10,4.5),layout='constrained')
    labels=[];ys=[];ts=[]
    for sym in summary['symbols']:
        p=summary['account']['positions'][str(sym)]
        labels.extend([f'{sym}\n交易前',f'{sym}\n交易后'])
        ys.extend([1000,p['yesterday']]);ts.extend([0,p['today']])
    x=np.array([0,1,3,4,6,7])
    ax.bar(x,ys,color=BLUE,label='昨日库存：未冻结时可卖',width=.7)
    ax.bar(x,ts,bottom=ys,color=GOLD,label='今日买入：本模型当日锁定',width=.7)
    for i,(y,t) in enumerate(zip(ys,ts)):
        ax.text(x[i],y/2,str(y),ha='center',va='center',color='white')
        if t:ax.text(x[i],y+t/2,str(t),ha='center',va='center',fontsize=10)
    ax.set_xticks(x,labels);ax.set_ylim(0,1220);ax.set(ylabel='股数',title='总持仓都恢复 1,000 股，今日可卖量却减少')
    ax.legend(ncol=2,loc='upper center',fontsize=10);ax.grid(axis='y',alpha=.15)
    save(fig,out,'05_t1_inventory')

    # 6. 时间分辨率实验。
    fig,ax=plt.subplots(figsize=(10,4.5),layout='constrained')
    runs=obs['latency_experiments'];x=np.arange(len(runs))
    observed=[r['first_fill_median_delay_ms']/1000 for r in runs]
    configured=[r['latency_ms']/1000 for r in runs]
    ax.bar(x-.17,configured,width=.34,color=GRAY,label='配置延迟')
    bars=ax.bar(x+.17,observed,width=.34,color=BLUE,label='首次成交实际等待中位数')
    ax.bar_label(bars,labels=[f'{v:.0f}s' for v in observed],padding=3)
    ax.set_xticks(x,[str(r['latency_ms']) for r in runs]);ax.axhline(3,color=GOLD,ls='--',label='快照间隔中位数：3 秒')
    ax.set(xlabel='配置延迟（毫秒）',ylabel='秒',title='0—3,000 毫秒配置，得到完全相同的成交记录')
    ax.legend(loc='upper left',fontsize=10);ax.set_ylim(0,15);ax.grid(axis='y',alpha=.15)
    save(fig,out,'06_latency_resolution')

    # 7. 分母都是提交的订单，不是成交条数。
    fig,ax=plt.subplots(figsize=(10,4.5),layout='constrained')
    runs=sorted(obs['price_probes'],key=lambda r:['passive','touch','aggressive'].index(r['mode']))
    labels={'passive':'本方最优价','touch':'对手最优价','aggressive':'对手价再积极两跳'}
    y=np.arange(3);left=np.zeros(3)
    for key,label,color in [('full','全部成交',BLUE),('part','部分成交',GOLD),('none','零成交后撤单',GRAY)]:
        values=[r['fully_filled'] if key=='full' else (r['orders_with_fill']-r['fully_filled'] if key=='part' else r['submitted']-r['orders_with_fill']) for r in runs]
        ax.barh(y,values,left=left,color=color,height=.5,label=label)
        for i,v in enumerate(values):
            if v:ax.text(left[i]+v/2,y[i],str(v),ha='center',va='center',color='white' if color==BLUE else '#253549')
        left+=values
    ax.set_yticks(y,[labels[r['mode']] for r in runs]);ax.invert_yaxis()
    ax.set(xlabel='订单数（每组共 54 笔）',title='报价越积极，本次模型中的订单完成率越高')
    ax.legend(ncol=3,loc='lower center',bbox_to_anchor=(.5,-.3),fontsize=10);ax.set_xlim(0,54)
    save(fig,out,'07_execution_completion')

    # 8. 一笔真实探针的状态与冻结资源。
    fig,ax=plt.subplots(figsize=(12,3.4),layout='constrained');ax.axis('off')
    texts=['14:00:01 提交\n卖出 200 股\n冻结昨日库存 200 股',
           '14:00:04 部分成交\n成交 75 股 × 121.70 元\n剩余 125 股继续冻结',
           '14:00:31 撤单\n撤销剩余 125 股\n释放冻结，75 股成交保留']
    for i,text in enumerate(texts):
        x=.02+i*.335
        box=FancyBboxPatch((x,.25),.285,.5,boxstyle='round,pad=0.018',facecolor=['#edf5fb','#fff5df','#eaf5ef'][i],edgecolor=[BLUE,GOLD,GREEN][i],transform=ax.transAxes)
        ax.add_patch(box);ax.text(x+.1425,.50,text,ha='center',va='center',transform=ax.transAxes,fontsize=12,linespacing=1.7)
        if i<2:ax.annotate('',xy=(x+.325,.5),xytext=(x+.303,.5),xycoords='axes fraction',arrowprops={'arrowstyle':'->','color':GRAY,'lw':2})
    ax.set_title('中芯国际 200 股卖单：只有 75 股成交，其余 125 股撤销',pad=14)
    save(fig,out,'08_partial_fill')
    print(json.dumps({'figures':len(list(out.glob('*.png'))),'output':str(out)},ensure_ascii=False))


if __name__=='__main__':main()
