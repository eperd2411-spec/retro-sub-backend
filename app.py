import os,re,time,json,tempfile,urllib.request,urllib.parse,threading
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp

app=FastAPI(title='Retro Sub API',version='1.0.0')
origins=os.getenv('ALLOWED_ORIGINS','*').split(',')
app.add_middleware(CORSMiddleware,allow_origins=origins,allow_credentials=False,allow_methods=['GET','POST'],allow_headers=['*'])
CACHE={}; CACHE_TTL=600; CACHE_LOCK=threading.Lock()

class Req(BaseModel):
    url:str
    lang:Optional[str]=None
    auto:bool=False
    format:str='srt'
    clean:bool=True

def video_id(url):
    pats=[r'(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{6,})']
    for p in pats:
        m=re.search(p,url)
        if m:return m.group(1)
    return None

def clean_url(url):
    vid=video_id(url)
    if not vid: raise HTTPException(400,'Invalid YouTube URL.')
    return 'https://www.youtube.com/watch?v='+vid

def extract(url,lang=None,auto=False):
    canonical=clean_url(url)
    key=(canonical,lang,auto)
    now=time.time()
    with CACHE_LOCK:
        hit=CACHE.get(key)
        if hit and now-hit[0] < CACHE_TTL:return hit[1]
    opts={'quiet':True,'no_warnings':True,'skip_download':True,'writesubtitles':not auto,'writeautomaticsubtitles':auto,'subtitleslangs':[lang] if lang else None,'noplaylist':True,'extract_flat':False}
    if lang is None:
        opts['writesubtitles']=False;opts['writeautomaticsubtitles']=False
    opts={k:v for k,v in opts.items() if v is not None}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try: result=ydl.extract_info(canonical,download=False)
        except Exception as e: raise HTTPException(502,'YouTube extraction failed: '+str(e)[:300])
    with CACHE_LOCK:CACHE[key]=(time.time(),result)
    return result

def tracks(info):
    out=[]
    for auto,src in [(False,info.get('subtitles') or {}),(True,info.get('automatic_captions') or {})]:
        for lang,items in src.items():
            name=lang
            out.append({'lang':lang,'name':name,'auto':auto})
    # Prefer common languages first, then alphabetical; remove duplicates by auto/lang pair
    pref={'id':0,'en':1,'ja':2,'zh-Hans':3,'zh':4,'ko':5}
    return sorted(out,key=lambda x:(pref.get(x['lang'],50),x['auto'],x['lang']))

def fetch_vtt(url):
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(req,timeout=20) as r:return r.read().decode('utf-8','replace')

def clean_text(s):
    s=re.sub(r'<[^>]+>','',s)
    s=re.sub(r'&nbsp;',' ',s);s=re.sub(r'&amp;','&',s)
    return re.sub(r'\s+',' ',s).strip()

def parse_vtt(vtt):
    lines=vtt.replace('\r','').split('\n'); cues=[];i=0
    while i<len(lines):
        line=lines[i].strip()
        if '-->' in line:
            a,b=[x.strip().split(' ')[0] for x in line.split('-->',1)]
            txt=[];i+=1
            while i<len(lines) and lines[i].strip():txt.append(lines[i].strip());i+=1
            text=clean_text(' '.join(txt))
            if text:cues.append({'start':to_sec(a),'end':to_sec(b),'time':to_srt_time(a),'text':text})
        i+=1
    return cues

def to_sec(x):
    x=x.replace(',','.');p=x.split(':')
    try:
        if len(p)==3:return int(p[0])*3600+int(p[1])*60+float(p[2])
        return int(p[0])*60+float(p[1])
    except:return 0

def to_srt_time(x):
    sec=to_sec(x);h=int(sec//3600);m=int(sec%3600//60);s=int(sec%60);ms=int(round((sec-int(sec))*1000));
    if ms>=1000:s+=1;ms=0
    return f'{h:02}:{m:02}:{s:02}'

def dedupe(cues):
    out=[];last=''
    for c in cues:
        t=c['text']
        if t==last:continue
        out.append(c);last=t
    return out

def srt(cues):
    a=[]
    for i,c in enumerate(cues,1):
        start=sec_to_srt(c['start']);end=sec_to_srt(c['end']);a += [str(i),f'{start} --> {end}',c['text'],'']
    return '\n'.join(a)

def sec_to_srt(sec):
    h=int(sec//3600);m=int(sec%3600//60);s=int(sec%60);ms=int(round((sec-int(sec))*1000))
    if ms>=1000:s+=1;ms=0
    return f'{h:02}:{m:02}:{s:02},{ms:03}'

def sec_to_stamp(sec):
    h=int(sec//3600);m=int(sec%3600//60);s=sec%60;return f'{h:02}:{m:02}:{s:06.3f}'

def vtt(cues):
    a=['WEBVTT','']
    for c in cues:a += [f"{sec_to_stamp(c['start'])} --> {sec_to_stamp(c['end'])}",c['text'],'']
    return '\n'.join(a)

@app.get('/health')
def health():return {'ok':True,'service':'retro-sub'}

@app.post('/api/info')
def info(req:Req):
    inf=extract(req.url)
    return {'video':{'id':inf.get('id'),'title':inf.get('title'),'channel':inf.get('channel') or inf.get('uploader'),'duration':inf.get('duration'),'thumbnail':inf.get('thumbnail')},'tracks':tracks(inf)}

@app.post('/api/caption')
def caption(req:Req):
    if req.format not in ('srt','vtt','txt'):raise HTTPException(400,'Format must be srt, vtt or txt.')
    if not req.lang:raise HTTPException(400,'Language is required.')
    inf=extract(req.url,req.lang,req.auto)
    rs=inf.get('requested_subtitles') or {}
    sub=rs.get(req.lang) or next(iter(rs.values()),None)
    if not sub or not sub.get('url'):raise HTTPException(404,'Caption track could not be retrieved.')
    raw=fetch_vtt(sub['url'])
    cues=parse_vtt(raw)
    if req.clean:cues=dedupe(cues)
    if req.format=='srt':content=srt(cues)
    elif req.format=='vtt':content=vtt(cues)
    else:content='\n'.join(c['text'] for c in cues)
    return {'content':content,'cues':cues,'language':req.lang,'auto':req.auto,'format':req.format,'count':len(cues)}
