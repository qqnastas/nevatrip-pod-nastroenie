#!/usr/bin/env python3
"""Читает страницы прогулок nevatrip.ru и складывает факты в data/pages.json (для разметки тегов)."""
import json, re, sys, time, html, urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
UA="nevatrip-vibe-catalog/1.0"
import os
PICK=[int(x) for x in os.environ['PICK'].split(',')] if os.environ.get('PICK') else [0,1,2,3,4,5,6,7,8,9,11,12,13,15,16,19,20,24,28,29,32,34,35,37,39]
LABELS=['Длительность','Расписание','Отправление','Теплоход','Автобус','Катер','Транспорт','На борту','Язык экскурсии','Остановки','Маршрут','Гид','Группа']
def fetch(uri):
    r=urllib.request.Request(f"https://nevatrip.ru/{uri}",headers={"User-Agent":UA})
    return urllib.request.urlopen(r,timeout=30).read().decode("utf-8")
def parse(h):
    feats=re.findall(r'"title":"([^"]{2,40})","className":"Features_item',h)
    body=re.sub(r'<script.*?</script>|<style.*?</style>','',h,flags=re.S)
    li=[html.unescape(re.sub(r'<[^>]+>','',x)).strip() for x in re.findall(r'<li[^>]*>(.*?)</li>',body,flags=re.S)]
    txt=html.unescape(re.sub(r'<[^>]+>','\n',body)); lines=[l.strip() for l in txt.split('\n') if l.strip()]
    facts={}
    for i,l in enumerate(lines):
        k=l.rstrip(': ').strip()
        if l.endswith(':') and k in LABELS and i+1<len(lines) and k not in facts: facts[k]=lines[i+1]
    o=[i for i,l in enumerate(lines) if l=='О прогулке']
    about=' '.join(lines[o[0]+1:o[0]+8]) if o else ''
    feat_i=[i for i,l in enumerate(lines) if l=='Особенности прогулки' or l.startswith('Особенности')]
    bullets=[]
    if feat_i:
        for l in lines[feat_i[0]+1:feat_i[0]+14]:
            if l.startswith('<') or len(l)<25: continue
            bullets.append(l)
    return {'facts':facts,'features_icons':sorted(set(feats)),'about':about[:1200],'bullets':bullets[:8]}
if __name__=='__main__':
    cat=json.load(open(ROOT/'data/catalog.json'))['items']
    out=json.load(open(ROOT/'data/pages.json')) if (ROOT/'data/pages.json').exists() else {}
    for n,i in enumerate(PICK):
        it=cat[i]
        try:
            d=parse(fetch(it['uri'])); d['title']=it['title']; d['price_from']=it['price_from']; d['duration']=it['duration']; out[it['uri']]=d
            print(n+1,len(PICK),it['uri'][:50],'ok',list(d['facts'].keys()),flush=True)
        except Exception as e:
            print(n+1,it['uri'],'ERR',e,flush=True)
        time.sleep(4)
    (ROOT/'data/pages.json').write_text(json.dumps(out,ensure_ascii=False,indent=1),encoding='utf-8')
