"""直接继承 vnpy_ctp 二进制 MdApi，无 GUI、无发单接口。"""
import argparse, importlib.metadata, importlib.util, json, math, os, pathlib, signal, threading, time

def load_md_api():
    # 顶层 vnpy_ctp 自动导入 Gateway/GUI 依赖，底层实验直接加载原厂扩展。
    dist=importlib.metadata.distribution('vnpy_ctp')
    files=[p for p in dist.files if p.name.startswith('vnctpmd.') and p.suffix=='.so']
    if len(files)!=1: raise RuntimeError('找不到唯一 vnctpmd 扩展')
    spec=importlib.util.spec_from_file_location('vnctpmd',dist.locate_file(files[0]))
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.MdApi

def emit(event,**data):
    print(json.dumps({'time':time.time(),'event':event,**data},ensure_ascii=False),flush=True)

class Callbacks:
    def setup(self, config, symbols):
        self.config=config; self.symbols=symbols; self.reqid=0
        self.logged_in=False; self.count=0; self.failed=False
    def onFrontConnected(self):
        emit('connected'); self.reqid+=1
        code=self.reqUserLogin({'UserID':self.config['userid'],'Password':self.config['password'],'BrokerID':self.config['brokerid']},self.reqid)
        if code: self.failed=True; emit('login_request_failed',code=code)
    def onFrontDisconnected(self,reason):
        self.logged_in=False; emit('disconnected',reason=reason)
    def onRspUserLogin(self,data,error,reqid,last):
        if error and error.get('ErrorID',0):
            self.failed=True; emit('login_failed',error_id=error['ErrorID']); return
        self.logged_in=True; emit('login_ok')
        for symbol in self.symbols:
            code=self.subscribeMarketData(symbol)
            emit('subscribe_request',symbol=symbol,code=code)
            if code: self.failed=True
    def onRspSubMarketData(self,data,error,reqid,last):
        code=(error or {}).get('ErrorID',0)
        if code: self.failed=True
        emit('subscribe_response',symbol=data.get('InstrumentID',''),error_id=code)
    def onRspError(self,error,reqid,last):
        self.failed=True; emit('api_error',error_id=(error or {}).get('ErrorID',-1))
    def onRtnDepthMarketData(self,data):
        price=data.get('LastPrice',0)
        if not isinstance(price,(int,float)) or not math.isfinite(price) or not 0<price<1e15: return
        self.count+=1
        emit('tick',symbol=data.get('InstrumentID'),price=price,volume=data.get('Volume'),update_time=data.get('UpdateTime'),trading_day=data.get('TradingDay'))

def main():
    p=argparse.ArgumentParser(); p.add_argument('--config',default='config.local.json'); p.add_argument('--symbols',required=True); p.add_argument('--seconds',type=int,default=0)
    a=p.parse_args(); config=json.loads(pathlib.Path(a.config).read_text())
    for k in ['userid','password','brokerid','md_address']:
        if not config.get(k): raise SystemExit(f'缺少本地配置项 {k}')
    # 默认只允许题目指定 SimNow 行情前置；生产地址不接入。
    if config['md_address']!='tcp://182.254.243.31:30011': raise SystemExit('仅允许课程 SimNow 行情前置')
    api_class=load_md_api()
    class Demo(Callbacks,api_class): pass
    api=Demo(); api.setup(config,[s.strip() for s in a.symbols.split(',') if s.strip()])
    stop=threading.Event()
    for sig in [signal.SIGINT,signal.SIGTERM]: signal.signal(sig,lambda *_:stop.set())
    flow=pathlib.Path('.ctp_flow'); flow.mkdir(exist_ok=True,mode=0o700)
    api.createFtdcMdApi(str(flow.resolve())+'/',False)
    api.registerFront(config['md_address']); api.init()
    start=time.monotonic()
    try:
        while not stop.wait(.2):
            if a.seconds and time.monotonic()-start>=a.seconds: break
    finally:
        api.exit(); emit('summary',ticks=api.count,logged_in=api.logged_in,failed=api.failed)
    return 0 if api.count and not api.failed else 2
if __name__=='__main__': raise SystemExit(main())
