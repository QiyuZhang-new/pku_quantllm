import contextlib, importlib.util, io, json, pathlib, threading, unittest
from ctp_demo import Callbacks
ROOT=pathlib.Path(__file__).resolve().parent
class Fake(Callbacks):
    def __init__(self): self.setup({'userid':'local','password':'local','brokerid':'local'},['rb-test','ag-test']); self.subscriptions=[]
    def reqUserLogin(self,req,reqid): self.request=req; return 0
    def subscribeMarketData(self,symbol): self.subscriptions.append(symbol); return 0
class HomeworkTests(unittest.TestCase):
    def test_login_failure_does_not_subscribe(self):
        api=Fake()
        with contextlib.redirect_stdout(io.StringIO()): api.onRspUserLogin({}, {'ErrorID':3},1,True)
        self.assertFalse(api.logged_in); self.assertEqual(api.subscriptions,[]); self.assertTrue(api.failed)
    def test_reconnect_resubscribes_and_counts_only_valid_ticks(self):
        api=Fake()
        with contextlib.redirect_stdout(io.StringIO()):
            api.onFrontConnected(); api.onRspUserLogin({}, {'ErrorID':0},1,True)
            api.onFrontDisconnected(1); api.onRspUserLogin({}, {},2,True)
            for p in [0,float('inf'),1.797e308,101.5]: api.onRtnDepthMarketData({'InstrumentID':'rb-test','LastPrice':p})
        self.assertEqual(api.subscriptions,['rb-test','ag-test']*2); self.assertEqual(api.count,1)
    def test_subscription_rejection_is_failure(self):
        api=Fake()
        with contextlib.redirect_stdout(io.StringIO()): api.onRspSubMarketData({'InstrumentID':'rb-test'},{'ErrorID':4},1,True)
        self.assertTrue(api.failed)
    def test_official_event_engine_dispatches(self):
        spec=importlib.util.spec_from_file_location('official_event',ROOT/'raw/vnpy/vnpy/event/engine.py')
        m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        engine=m.EventEngine(); done=threading.Event(); received=[]
        engine.register('tick',lambda e:(received.append(e.data),done.set()))
        engine.start()
        try: engine.put(m.Event('tick',{'price':101})); self.assertTrue(done.wait(3))
        finally: engine.stop()
        self.assertEqual(received,[{'price':101}])
    def test_features_do_not_use_future(self):
        import numpy as np, pandas as pd
        from factors.research import features
        d=pd.DataFrame({'open':np.arange(100,180),'close':np.arange(101,181),'high':np.arange(103,183),'low':np.arange(99,179),'volume':np.arange(1000,1080)})
        original=features(d); changed=d.copy(); changed.loc[50:,'close']*=3
        pd.testing.assert_frame_equal(original.iloc[:50],features(changed).iloc[:50])
    def test_gap_and_next_day_label(self):
        import pandas as pd
        from factors.research import features
        d=pd.DataFrame({'open':[100,110,120],'close':[100,115,126],'high':[101,116,127],'low':[99,109,119],'volume':[1000]*3})
        self.assertAlmostEqual(features(d).gap.iloc[1],.1)
        self.assertAlmostEqual((d.close.shift(-1)/d.open.shift(-1)-1).iloc[1],.05)
if __name__=='__main__': unittest.main(verbosity=2)
