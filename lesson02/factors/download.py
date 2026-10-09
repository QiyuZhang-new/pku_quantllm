"""下载固定股票池的公开 Yahoo 日线；只保留截止 2025-12-31 的数据。"""
import csv, datetime as dt, hashlib, json, pathlib, urllib.request, time
ROOT = pathlib.Path(__file__).resolve().parent
SYMBOLS = ['SPY', 'QQQ', 'IWM', 'DIA', 'XLF', 'XLK', 'XLE', 'XLV', 'XLI', 'XLP', 'XLY', 'XLU']
def main():
    rows, manifest = [], []
    for symbol in SYMBOLS:
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1=1546300800&period2=1767225600&interval=1d'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        cache = ROOT/'data'/f'{symbol}.json'
        if cache.exists():
            content = cache.read_bytes()
        else:
            for attempt in range(4):
                try:
                    content = urllib.request.urlopen(req, timeout=15).read()
                    cache.write_bytes(content)
                    break
                except Exception:
                    if attempt == 3: raise
                    time.sleep(1)
        result = json.loads(content)['chart']['result'][0]
        quote = result['indicators']['quote'][0]
        adjusted = result['indicators']['adjclose'][0]['adjclose']
        for i, stamp in enumerate(result['timestamp']):
            date = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).strftime('%Y-%m-%d')
            values = [quote[k][i] for k in ['open','high','low','close','volume']]
            if any(v is None for v in values) or adjusted[i] is None or not ('2019-01-01' <= date <= '2025-12-31'): continue
            ratio = adjusted[i] / values[3]
            rows.append([date,symbol,*[round(v * ratio, 8) for v in values[:4]],values[4]])
        manifest.append({'symbol':symbol,'url':url,'response_sha256':hashlib.sha256(content).hexdigest()})
        print(symbol, 'downloaded', flush=True)
    rows.sort(key=lambda r:(r[0],r[1]))
    ROOT.joinpath('data').mkdir(exist_ok=True)
    path = ROOT/'data'/'ohlcv.csv'
    with path.open('w') as f:
        writer=csv.writer(f); writer.writerow(['date','symbol','open','high','low','close','volume']); writer.writerows(rows)
    output={'downloaded_at':dt.datetime.now(dt.timezone.utc).isoformat(),'source':'Yahoo Finance chart API','price_adjustment':'OHLC multiplied by adjclose/close; retrospective total-return adjustment','rows':len(rows),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'symbols':manifest}
    (ROOT/'data'/'manifest.json').write_text(json.dumps(output,indent=2))
    print('rows',len(rows),'sha256',output['sha256'])
if __name__ == '__main__': main()
