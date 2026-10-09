"""按已记录的固定 SHA 恢复三个官方源码模块。"""
import io,json,pathlib,tarfile,urllib.request
ROOT=pathlib.Path(__file__).resolve().parent
for source in json.loads((ROOT/'evidence/sources.json').read_text()):
    name,sha=source['repo'],source['sha']; target=ROOT/'raw'/name
    if target.exists(): print(name,'already exists'); continue
    body=urllib.request.urlopen(f'https://codeload.github.com/vnpy/{name}/tar.gz/{sha}',timeout=60).read()
    (ROOT/'raw').mkdir(exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(body)) as tar: tar.extractall(ROOT/'raw',filter='data')
    (ROOT/'raw'/f'{name}-{sha}').rename(target)
    print(name,sha)
