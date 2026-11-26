import asyncio,json,os,re,subprocess,textwrap,math,struct,wave
import unicodedata
from datetime import datetime,timezone
from pathlib import Path
import requests,edge_tts,time
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload,MediaIoBaseDownload
R=Path(__file__).parent; W=R/"work_artist_intro"; W.mkdir(exist_ok=True)
H=R/"history"/"artist_intro_processed.json"; TYPES={"video/mp4","video/quicktime","video/x-msvideo","video/webm","video/x-matroska"}
def need(k):
 v=os.getenv(k,"").strip()
 if not v: raise RuntimeError("Missing secret "+k)
 return v
def run(a):
 r=subprocess.run(a,check=False,capture_output=True,text=True)
 if r.returncode:
  print("COMMAND FAILED:", " ".join(map(str,a)))
  if r.stdout: print("FFMPEG STDOUT:",r.stdout[-4000:])
  if r.stderr: print("FFMPEG STDERR:",r.stderr[-12000:])
  raise RuntimeError(f"Media command failed with exit code {r.returncode}; see FFmpeg diagnostics above")
 if r.stdout: print(r.stdout[-2000:])
 if r.stderr and "ffmpeg" not in str(a[0]).lower(): print(r.stderr[-2000:])
def dur(p):
 return float(subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(p)],check=True,capture_output=True,text=True).stdout.strip())
def services():
 d=Credentials(None,refresh_token=need("FANCAMVERSE_REFRESH_TOKEN"),token_uri="https://oauth2.googleapis.com/token",client_id=need("FANCAMVERSE_CLIENT_ID"),client_secret=need("FANCAMVERSE_CLIENT_SECRET"),scopes=["https://www.googleapis.com/auth/drive.readonly"])
 y=Credentials(None,refresh_token=need("YOUTUBE_REFRESH_TOKEN"),token_uri="https://oauth2.googleapis.com/token",client_id=need("YOUTUBE_CLIENT_ID"),client_secret=need("YOUTUBE_CLIENT_SECRET"),scopes=["https://www.googleapis.com/auth/youtube.upload"])
 return build("drive","v3",credentials=d),build("youtube","v3",credentials=y)
def scan(drive,folder):
 q=[folder]; seen=set(); out=[]
 while q:
  parent=q.pop(0)
  if parent in seen: continue
  seen.add(parent); token=None
  while True:
   a={"q":f"'{parent}' in parents and trashed=false","pageSize":100,"orderBy":"createdTime","fields":"nextPageToken,files(id,name,mimeType,createdTime,size)","supportsAllDrives":True,"includeItemsFromAllDrives":True}
   if token:a["pageToken"]=token
   r=drive.files().list(**a).execute()
   for f in r.get("files",[]):
    if f.get("mimeType")=="application/vnd.google-apps.folder":q.append(f["id"])
    elif f.get("mimeType") in TYPES:out.append(f)
   token=r.get("nextPageToken")
   if not token:break
 return sorted(out,key=lambda x:(x.get("createdTime",""),x["id"]),reverse=True)
def download(drive,item):
 p=W/(re.sub(r"[^A-Za-z0-9._-]+","_",item["name"]).strip("._") or "source.mp4")
 req=drive.files().get_media(fileId=item["id"],supportsAllDrives=True)
 with p.open("wb") as f:
  dl=MediaIoBaseDownload(f,req,chunksize=8*1024*1024);done=False
  while not done:_,done=dl.next_chunk()
 if not p.exists() or not p.stat().st_size:raise RuntimeError("Empty Drive download")
 return p
def profile(filename, target_hint=""):
 # Read the public NamuWiki article directly; no Gemini/API key is used.
 # Keep requests polite and sequential (at least one second between requests).
 from html.parser import HTMLParser
 from urllib.parse import quote, unquote
 class SearchLinks(HTMLParser):
  def __init__(self):
   super().__init__(); self.links=[]; self.current=None
  def handle_starttag(self,tag,attrs):
   if tag=="a":
    a=dict(attrs); href=a.get("href","")
    if href.startswith("/w/"): self.current=[href,""]
  def handle_data(self,data):
   if self.current is not None:self.current[1]+=data
  def handle_endtag(self,tag):
   if tag=="a" and self.current is not None:
    self.links.append(tuple(self.current));self.current=None
 session=requests.Session()
 session.headers.update({"User-Agent":"CurioLoopArtistIntro/1.0 (public biographical reference lookup)","Accept":"text/html, text/plain;q=0.9"})
 stem=Path(filename).stem
 target=(target_hint or "").strip()
 group_data=[
  ("IVE",["아이브","IVE"],["IVE"],2021),
  ("aespa",["에스파","aespa"],["aespa"],2020),
  ("LE SSERAFIM",["르세라핌","LE SSERAFIM"],["LE SSERAFIM"],2022),
  ("VIVIZ",["비비지","VIVIZ"],["VIVIZ"],2022),
  ("GFRIEND",["여자친구","GFRIEND"],["GFRIEND"],2015),
  ("IZ*ONE",["아이즈원","IZ*ONE","IZONE"],["IZ*ONE"],2018),
  ("TWICE",["트와이스","TWICE"],["TWICE"],2015),
  ("BLACKPINK",["블랙핑크","BLACKPINK"],["BLACKPINK"],2016),
  ("Red Velvet",["레드벨벳","Red Velvet"],["Red Velvet"],2014),
  ("Girls' Generation",["소녀시대","Girls' Generation"],["Girls' Generation"],2007),
  ("OH MY GIRL",["오마이걸","OH MY GIRL"],["OH MY GIRL"],2015),
  ("ITZY",["있지","ITZY"],["ITZY"],2019),
  ("NMIXX",["엔믹스","NMIXX"],["NMIXX"],2022),
  ("NewJeans",["뉴진스","NewJeans"],["NewJeans"],2022),
  ("ILLIT",["아일릿","ILLIT"],["ILLIT"],2024),
  ("STAYC",["스테이씨","STAYC"],["STAYC"],2020),
  ("KISS OF LIFE",["키스오브라이프","KISS OF LIFE"],["KISS OF LIFE"],2023),
  ("BABYMONSTER",["베이비몬스터","BABYMONSTER"],["BABYMONSTER"],2024),
  ("fromis_9",["프로미스나인","fromis_9"],["fromis_9"],2018),
  ("tripleS",["트리플에스","tripleS"],["tripleS"],2023),
 ]
 artist_names={
  "장원영":"Jang Wonyoung","원영":"Wonyoung","wonyoung":"Wonyoung",
  "은하":"Eunha","eunha":"Eunha","jennie":"Jennie","권은비":"Kwon Eunbi","카리나":"Karina","닝닝":"Ningning",
  "윈터":"Winter","지젤":"Giselle","사쿠라":"Sakura","김채원":"Kim Chaewon",
  "허윤진":"Huh Yunjin","카즈하":"Kazuha","홍은채":"Hong Eunchae",
  "안유진":"An Yujin","유진":"Yujin","레이":"Rei","리즈":"Liz","이서":"Leeseo",
  "민지":"Minji","하니":"Hanni","다니엘":"Danielle","해린":"Haerin","혜인":"Hyein",
  "나연":"Nayeon","정연":"Jeongyeon","모모":"Momo","사나":"Sana","지효":"Jihyo",
  "미나":"Mina","다현":"Dahyun","채영":"Chaeyoung","쯔위":"Tzuyu",
  "아이린":"Irene","슬기":"Seulgi","웬디":"Wendy","조이":"Joy","예리":"Yeri",
  "예지":"Yeji","리아":"Lia","류진":"Ryujin","채령":"Chaeryeong","유나":"Yuna",
  "지수":"Jisoo","제니":"Jennie","로제":"Rosé","리사":"Lisa",
  "엄지":"Umji","신비":"SinB","은하":"Eunha","소원":"Sowon","예린":"Yerin","유주":"Yuju",
  "츄":"Chuu","희진":"Heejin","김립":"Kim Lip","진솔":"Jinsoul","최리":"Choerry",
 }
 stop_words={"직캠","안방","음악","중심","인기가요","뮤직","뱅크","엠카","쇼챔","팬캠","페이스캠","풀캠","가로캠","세로캠","무대","컴백","타이틀","티저","비하인드","공식","프로필","생일","멤버","아이돌"}
 if not target:
  # Only accept an explicit, known artist alias in the source filename.
  matches=[alias for alias in artist_names if alias.casefold() in stem.casefold()]
  target=max(matches,key=len) if matches else ""
 if not target or target.casefold() not in {name.casefold() for name in artist_names}:
  raise RuntimeError("SKIPPED_UNKNOWN_ARTIST: no recognized artist name in source filename: "+filename)
 target=next(name for name in artist_names if name.casefold()==target.casefold())
 # Resolve the exact NamuWiki mirror article directly. The primary namu.wiki
 # host returns HTTP 403 from GitHub-hosted runners, so use the public mirror.
 from urllib.parse import quote, unquote
 chosen=target
 raw=None
 source_base=None
 errors=[]
 for base in ("https://namu.moe","https://d.namu.moe"):
  headers={"Referer":base+"/","User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36","Accept":"text/html, text/plain, */*"}
  # The seed engine's raw endpoint is /raw/<document>, not /raw?doc=...
  raw_url=base+"/raw/"+quote(chosen,safe="")
  try:
   rr=session.get(raw_url,headers=headers,timeout=40)
   if rr.status_code==404:
    # Some mirrors expose rendered pages but not the raw route.
    page_url=base+"/w/"+quote(chosen,safe="")
    rr=session.get(page_url,headers=headers,timeout=40)
   rr.raise_for_status()
   candidate=rr.text.strip()
   if "<html" in candidate[:500].lower():
    from html import unescape
    candidate=re.sub(r"<(script|style)\b[^>]*>.*?</\1>"," ",candidate,flags=re.I|re.S)
    candidate=unescape(re.sub(r"<[^>]+>"," ",candidate))
   if len(candidate)>=300 and "문서를 찾을 수 없습니다" not in candidate:
    raw=candidate;source_base=base;break
   errors.append(f"{base}: article response was empty or too short")
  except requests.RequestException as e:
   errors.append(f"{base}: {type(e).__name__}: {e}")
  time.sleep(1.2)
 if raw is None:
  raise RuntimeError("NamuWiki mirror article retrieval failed. "+"; ".join(errors))
 def plain(s):
  s=re.sub(r"\[\[파일:[^\]]+\]\]"," ",s,flags=re.I)
  s=re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]",r"\2",s)
  s=re.sub(r"\[\[([^\]]+)\]\]",r"\1",s)
  s=re.sub(r"\[(https?://[^\s\]]+)\s+([^\]]+)\]",r"\2",s)
  s=re.sub(r"\{\{\{#!wiki.*?\}\}\}"," ",s,flags=re.S)
  s=re.sub(r"\{\{.*?\}\}"," ",s,flags=re.S)
  s=re.sub(r"'''|''|__|~~","",s)
  s=re.sub(r"<[^>]+>"," ",s)
  s=re.sub(r"\[\*[^]]*\]"," ",s)
  s=re.sub(r"&nbsp;"," ",s)
  return re.sub(r"\s+"," ",s).strip()
 cleaned=plain(raw)
 # Use a known official Romanized stage name where available; otherwise
 # retain the Hangul stage name rather than inventing an English spelling.
 artist=artist_names.get(target,artist_names.get(target.casefold(),target))
 for alias,english in artist_names.items():
  if alias in stem and (target==alias or not target_hint):
   artist=english;break
 if not re.search(r"[A-Za-z]",artist):
  # Revised-Romanization fallback for artists without a listed English alias.
  initials=["g","kk","n","d","tt","r","m","b","pp","s","ss","","j","jj","ch","k","t","p","h"]
  vowels=["a","ae","ya","yae","eo","e","yeo","ye","o","wa","wae","oe","yo","u","wo","we","wi","yu","eu","ui","i"]
  finals=["","k","k","ks","n","nj","nh","t","l","lk","lm","lb","ls","lt","lp","lh","m","p","ps","t","t","ng","t","t","k","t","p","t"]
  def syllable(ch):
   code=ord(ch)-0xAC00
   if code<0 or code>11171:return ch
   return initials[code//588]+vowels[(code%588)//28]+finals[code%28]
  surnames={"김":"Kim","이":"Lee","박":"Park","최":"Choi","정":"Jung","강":"Kang","조":"Cho","윤":"Yoon","장":"Jang","임":"Lim","한":"Han","오":"Oh","서":"Seo","신":"Shin","권":"Kwon","황":"Hwang","안":"Ahn","송":"Song","류":"Ryu","홍":"Hong","전":"Jeon","문":"Moon","배":"Bae","백":"Baek","허":"Heo","남":"Nam","심":"Shim","노":"Noh"}
  if len(target)==3 and target[0] in surnames:
   artist=surnames[target[0]]+" "+''.join(syllable(ch) for ch in target[1:]).capitalize()
  else:
   artist=''.join(syllable(ch) for ch in target).capitalize()
 # Require a verified group match in the filename or retrieved biography.
 group=""; group_year=None
 for haystack in (stem,cleaned[:12000]):
  found=False
  for english,aliases,_,year in group_data:
   if any(alias.casefold() in haystack.casefold() for alias in aliases):
    group=english;group_year=year;found=True;break
  if found:break
 if not group:
  raise RuntimeError("SKIPPED_UNKNOWN_GROUP: could not verify group affiliation for "+artist+" from source: "+filename)
 # Extract birth date only when it appears next to a biographical label.
 birth=None
 birth_patterns=[
  r"(?:생년월일|출생일|출생)\s*(?:\|\|?|:|：|：)?\s*[^|]{0,100}?(\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일)",
  r"(?:생년월일|출생일|출생)[^0-9]{0,80}(\d{4}[.\-/]\s*\d{1,2}[.\-/]\s*\d{1,2})"
 ]
 for pattern in birth_patterns:
  m=re.search(pattern,raw,re.I)
  if m:birth=re.sub(r"\s+","",m.group(1));break
 if birth and re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}",birth):
  birth=birth.replace(".","-")
 # Build an engaging, fact-led script. Keep only concrete details verified in the retrieved profile.
 def labeled_value(labels):
  for label in labels:
   pattern=r"(?:^|\\s)"+label+r"\\s*(?:\\||:|：)?\\s*([^|]{2,90}?)(?=\\s{2,}|\\||$)"
   m=re.search(pattern,raw,re.I)
   if m:
    value=re.sub(r"\\s+"," ",m.group(1)).strip(" -:：")
    value=re.sub(r"\\[[^]]*\\]","",value).strip()
    if value and not re.search(r"(문서|편집|각주|출처|미상|불명)",value):
     return value[:65]
  return None
 position=labeled_value(["포지션","담당 포지션","보컬 포지션"])
 nickname=labeled_value(["별명","애칭"])
 facts=[f"Think you know {artist}? Here are a few details worth knowing."]
 if group!="Solo Artist":
  facts.append(f"{artist} debuted with {group} in {group_year}.")
 if position:
  facts.append(f"The profile lists their position as {position}.")
 if nickname:
  facts.append(f"One nickname listed in the profile is {nickname}.")
 if birth:
  facts.append(f"Their birth date is {birth}.")
 if len(facts)==1:
  facts.append(f"{artist} is a South Korean performer associated with {group}.")
  facts.append("Check the profile for confirmed details, and tell us what we should cover next.")
 else:
  facts.append("Which fact surprised you most?")
 narration=" ".join(facts)
 if len(narration.split())>55:
  narration=" ".join(narration.split()[:55])
 source_url=(source_base or "https://namu.moe")+"/w/"+quote(chosen,safe="")
 print(f"NamuWiki research complete: {chosen}; artist={artist}; group={group}; birth={birth or 'not listed'}; narration_words={len(narration.split())}.")
 return {
  "artist":artist,"group":group,"narration":narration,
  "title":f"{artist} | {group} Artist Spotlight"[:85],
  "tags":[x for x in ["K-pop",group.replace(" ",""),artist.replace(" ",""),"Artist Spotlight"] if x],
  "sources":[{"title":f"NamuWiki: {chosen}","url":source_url}],
  "research_queries":[target]
 }


def create_background_music(path, seconds=36, sample_rate=22050):
 # Original, royalty-free ambient pop bed synthesized locally for this channel.
 # Soft major-seventh chords and sparse plucks; intentionally unobtrusive.
 progression=[
  ([261.63,329.63,392.00,493.88], [523.25,659.25]),
  ([220.00,261.63,329.63,392.00], [440.00,523.25]),
  ([174.61,220.00,261.63,329.63], [349.23,440.00]),
  ([196.00,246.94,293.66,392.00], [392.00,493.88])
 ]
 total=int(seconds*sample_rate)
 with wave.open(str(path),"wb") as wf:
  wf.setnchannels(2); wf.setsampwidth(2); wf.setframerate(sample_rate)
  frames=bytearray()
  for i in range(total):
   t=i/sample_rate
   bar=int(t/4)%len(progression)
   chord,plucks=progression[bar]
   local=t%4
   # Gentle chord pad with a slow, unobtrusive swell.
   swell=0.72+0.28*math.sin(math.pi*min(local/4,1))
   pad=sum(math.sin(2*math.pi*hz*t) for hz in chord)/len(chord)
   pad+=0.18*sum(math.sin(2*math.pi*hz*2*t) for hz in chord)/len(chord)
   # Sparse, rounded high notes add a light contemporary pop texture.
   beat=t%1.0
   note=plucks[int((t%4)//1)%2]
   decay=math.exp(-beat*5.5)
   pluck=math.sin(2*math.pi*note*beat)*decay
   fade=min(1.0,t/1.2,max(0.0,(seconds-t)/2.0))
   sample=(0.72*pad*swell+0.12*pluck)*0.10*fade
   sample=max(-0.95,min(0.95,sample))
   value=int(sample*32767)
   frames.extend(struct.pack("<hh",value,value))
  wf.writeframes(frames)
 print("Original background music generated:",path.name,"duration:",seconds,"seconds")

def render(src,p,hist):
 length=36.; intro=1.5; footage=length-intro; source_len=dur(src)
 if source_len<footage+1:raise RuntimeError(f"Source too short ({source_len:.1f}s); need at least 36.5s")
 audio=W/"voice.mp3";bgm=W/"background_music.wav";vtt=W/"voice.srt"
 async def make_voice_and_subtitles():
  # Generate each spoken sentence separately so subtitle timing is based on
  # the measured duration of that exact sentence, not estimated word counts.
  sentences=[x.strip() for x in re.split(r"(?<=[.!?])\s+",p["narration"].strip()) if x.strip()]
  if not sentences:
   raise RuntimeError("Cannot create subtitles: narration is empty")
  parts=[]
  durations=[]
  for i,sentence in enumerate(sentences):
   part=W/f"voice_part_{i:03d}.mp3"
   communicate=edge_tts.Communicate(sentence,"en-US-AriaNeural",rate="-3%")
   await communicate.save(str(part))
   if not part.exists() or part.stat().st_size==0:
    raise RuntimeError(f"Edge TTS returned an empty audio segment: {i}")
   parts.append(part)
   durations.append(dur(part))
  concat_list=W/"voice_concat.txt"
  concat_list.write_text(chr(10).join("file '" + str(x.resolve()).replace("\\","/") + "'" for x in parts) + chr(10), encoding="utf-8")
  run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat_list),"-c:a","libmp3lame","-q:a","3",str(audio)])
  def stamp(seconds):
   ms=max(0,int(round(seconds*1000)))
   hh,ms=divmod(ms,3600000); mm,ms=divmod(ms,60000); ss,ms=divmod(ms,1000)
   return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"
  cues=[]; cue_data=[]; cursor=1.5; cue_no=1
  for sentence,seconds in zip(sentences,durations):
   # Keep each caption to at most two short lines. Long sentences are
   # split into consecutive cues and timed by their spoken word counts.
   # Normalize to supported Latin glyphs so unsupported symbols cannot render as empty squares.
   clean_caption=unicodedata.normalize("NFKD",sentence)
   clean_caption=clean_caption.translate(str.maketrans({"’":"'","‘":"'","“":'"',"”":'"',"–":"-","—":"-","…":"...","•":"-"," ":" ","‐":"-","‑":"-"}))
   clean_caption=clean_caption.encode("ascii","ignore").decode("ascii")
   clean_caption=re.sub(r"\s+"," ",clean_caption).strip()
   wrapped=textwrap.wrap(clean_caption,width=30,break_long_words=False,break_on_hyphens=False)
   groups=["\n".join(wrapped[j:j+2]) for j in range(0,len(wrapped),2)]
   if not groups: continue
   weights=[max(1,len(group.replace("\n"," ").split())) for group in groups]
   total=sum(weights)
   for group,weight in zip(groups,weights):
    segment_duration=seconds*weight/total
    end_time=cursor+segment_duration
    cues.extend([str(cue_no),f"{stamp(cursor)} --> {stamp(end_time)}",group,""])
    cue_data.append({"start":round(cursor,3),"end":round(end_time,3),"text":group})
    cursor=end_time; cue_no+=1
  subtitle=chr(10).join(cues)
  vtt.write_text(subtitle,encoding="utf-8-sig")
  (W/"subtitle_cues.json").write_text(json.dumps(cue_data,ensure_ascii=False),encoding="utf-8")
  if not vtt.exists() or vtt.stat().st_size<20 or not cue_data:
   raise RuntimeError("Subtitle file was not written correctly")
  print("Sentence-synced subtitle file ready:",vtt.name,"segments:",len(sentences))
  print("Subtitle preview:",subtitle[:400].replace("\n"," | "))
 asyncio.run(make_voice_and_subtitles())
 create_background_music(bgm,length)
 # Select a different still-frame timestamp for repeat appearances of the same artist.
 used=hist.setdefault("opening_frames",{}).setdefault(p["artist"].casefold(),[])
 choices=[max(0.5,min(source_len-0.5,source_len*x)) for x in (0.08,0.18,0.30,0.43,0.57,0.70,0.82,0.92)]
 choices=[round(x,2) for x in choices]
 t=next((x for x in choices if x not in used),choices[len(used)%len(choices)])
 still=W/"opening.jpg"
 run(["ffmpeg","-y","-ss",f"{t:.2f}","-i",str(src),"-frames:v","1","-q:v","2",str(still)])
 hist["pending_opening_frame"]={"artist":p["artist"].casefold(),"timestamp":t}
 start=max(0,(source_len-footage)/2);name=p["artist"].replace("'","").replace(":","");group=p["group"].replace("'","").replace(":","")
 out=W/(re.sub(r"[^A-Za-z0-9_-]+","_",name).lower()+"_artist_intro.mp4")
 font="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf";bold="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
 cue_data=json.loads((W/"subtitle_cues.json").read_text(encoding="utf-8"))
 subtitle_filters=[]
 for i,cue in enumerate(cue_data):
  cue_file=W/f"subtitle_{i:03d}.txt"
  cue_file.write_text(cue["text"],encoding="utf-8")
  cue_start=max(1.5,float(cue["start"]));cue_end=min(36.0,float(cue["end"]))
  if cue_end<=cue_start: continue
  enable=f"between(t\\,{cue_start:.3f}\\,{cue_end:.3f})"
  subtitle_filters.append(f",drawtext=fontfile={font}:textfile='{cue_file}':fontcolor=white:fontsize=40:borderw=3:bordercolor=black@0.85:shadowcolor=black@0.5:shadowx=1:shadowy=1:box=1:boxcolor=black@0.58:boxborderw=18:text_align=center:x=(w-text_w)/2:y=h-text_h-340:line_spacing=8:enable='{enable}'")
 if not subtitle_filters: raise RuntimeError("No timed subtitle overlays were generated")
 print("Burn-in subtitle overlays:",len(subtitle_filters))
 fc=("[0:v]scale=1080:1800:force_original_aspect_ratio=increase,crop=1080:1800,setsar=1,fps=30,format=yuv420p,settb=AVTB,trim=duration=1.5,setpts=PTS-STARTPTS,"
 f"drawtext=fontfile={bold}:text='{name}':fontcolor=white:fontsize=76:borderw=3:bordercolor=black@0.8:x=(w-text_w)/2:y=h-390,"
 f"drawtext=fontfile={font}:text='{group}':fontcolor=white:fontsize=38:borderw=2:bordercolor=black@0.8:x=(w-text_w)/2:y=h-300[v0];"
 f"[1:v]trim=start={start:.3f}:duration={footage:.3f},setpts=PTS-STARTPTS,scale=1080:1800:force_original_aspect_ratio=increase,crop=1080:1800,setsar=1,fps=30,format=yuv420p,settb=AVTB[v1];"
 f"[v0][v1]concat=n=2:v=1:a=0,format=yuv420p{''.join(subtitle_filters)}[v];"
 "[2:a]adelay=1500|1500,apad,atrim=duration=36[voice];"
 "[3:a]volume=0.16,afade=t=in:st=0:d=1.2,afade=t=out:st=33:d=3,atrim=duration=36[bgm];"
 "[voice][bgm]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,alimiter=limit=0.95[a]")
 run(["ffmpeg","-y","-loop","1","-t","1.5","-i",str(still),"-i",str(src),"-i",str(audio),"-i",str(bgm),"-filter_complex_threads","1","-filter_complex",fc,"-map","[v]","-map","[a]","-t","36","-r","30","-c:v","libx264","-preset","medium","-crf","20","-c:a","aac","-b:a","192k","-movflags","+faststart",str(out)])
 check=json.loads(subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height","-of","json",str(out)],check=True,capture_output=True,text=True).stdout)["streams"][0]
 if (int(check["width"]),int(check["height"]))!=(1080,1800) or not 30<=dur(out)<=40:raise RuntimeError("Output validation failed")
 return out
def publish(yt,out,p,source):
 brand=os.getenv("FANCAMVERSE_CHANNEL_BRAND","CurioLoop").strip()
 if brand.casefold() not in {"curioloop","dailyneedle"}:raise RuntimeError("Artist intro upload blocked: unexpected channel brand "+brand)
 print("Using existing CurioLoop/DailyNeedle YouTube upload credentials.")
 approval_file=R/"INTRO_PUBLICATION_APPROVED.txt"
 approved_once=os.getenv("INTRO_PUBLIC_ONCE","").strip().lower() in {"1","true","yes"}
 approved_file=approval_file.exists() and approval_file.read_text(encoding="utf-8").strip().lower() in {"1","true","yes","approved"}
 approved=approved_once or approved_file
 privacy="public" if approved else "private"
 print("One-time public approval:", "enabled" if approved_once else "disabled")
 research_links="\n".join(f"{x.get('title','Source')}: {x['url']}" for x in p.get("sources",[]))
 description=f"Meet {p['artist']} from {p['group']}.\nArtist profile, career highlights, and recent update.\nOriginal footage source: {source}\nResearch sources:\n{research_links}\n#Kpop #ArtistSpotlight #Shorts"
 body={"snippet":{"title":p["title"][:100],"description":description[:4900],"tags":p.get("tags",[])+["K-pop","artist spotlight","Shorts"],"categoryId":"10"},"status":{"privacyStatus":privacy,"selfDeclaredMadeForKids":False}}
 v=yt.videos().insert(part="snippet,status",body=body,media_body=MediaFileUpload(str(out),mimetype="video/mp4",resumable=True)).execute()
 print(f"{privacy.upper()} UPLOAD https://youtube.com/shorts/"+v["id"]);return v["id"],privacy
def main():
 drive,yt=services();folder=need("FANCAMVERSE_DRIVE_FOLDER_ID")
 h=json.loads(H.read_text(encoding="utf-8")) if H.exists() else {"processed":[]}
 done={x.get("drive_file_id") for x in h["processed"]}
 target=os.getenv("TARGET_ARTIST","").strip()
 candidates=[x for x in scan(drive,folder) if x["id"] not in done]
 if not candidates:raise RuntimeError("No new unprocessed Drive video")
 # Fail closed: random selection only considers filenames with a known artist and group.
 known_artists=["장원영","원영","wonyoung","은하","권은비","카리나","닝닝","윈터","지젤","사쿠라","김채원","허윤진","카즈하","홍은채","안유진","유진","레이","리즈","이서","민지","하니","다니엘","해린","혜인","나연","정연","모모","사나","지효","미나","다현","채영","쯔위","아이린","슬기","웬디","조이","예리","예지","리아","류진","채령","유나","지수","제니","jennie","로제","리사","엄지","신비","소원","예린","유주","츄","희진","김립","진솔","최리"]
 known_groups=["아이브","ive","에스파","aespa","르세라핌","le sserafim","비비지","viviz","여자친구","gfriend","아이즈원","iz*one","izone","트와이스","twice","블랙핑크","blackpink","레드벨벳","red velvet","소녀시대","girls' generation","오마이걸","oh my girl","있지","itzy","엔믹스","nmixx","뉴진스","newjeans","아일릿","illit","스테이씨","stayc","키스오브라이프","kiss of life","베이비몬스터","babymonster","프로미스나인","fromis_9","트리플에스","triples"]
 def identifiable(item):
  name=item["name"].casefold()
  return any(a.casefold() in name for a in known_artists) and any(g.casefold() in name for g in known_groups)
 # Manual target may prioritize one artist, but identity validation remains mandatory.
 if target:
  aliases={"wonyoung":["장원영","원영","wonyoung"],"eunha":["은하","eunha"]}
  needles=aliases.get(target.casefold(),[target.casefold()])
  candidates=[x for x in candidates if any(term in x["name"].casefold() for term in needles) and identifiable(x)]
  if not candidates:raise RuntimeError(f"No identifiable, unprocessed Drive video found for requested test artist: {target}")
 else:
  candidates=[x for x in candidates if identifiable(x)]
  print(f"Identifiable source videos: {len(candidates)}; unknown/ambiguous videos skipped.")
  if not candidates:raise RuntimeError("No unprocessed Drive video has both a recognized artist and group name; skipped all ambiguous sources.")
  import random
  random.shuffle(candidates)
 item=candidates[0];print("Selected Drive source:",item["name"])
 src=download(drive,item)
 # Identify the artist from the selected filename, then retrieve the NamuWiki biography.
 p=profile(item["name"],target_hint=target)
 research_path=W/(re.sub(r"[^A-Za-z0-9_-]+","_",p["artist"].lower())+"_research.json")
 research_path.write_text(json.dumps({"artist":p["artist"],"group":p["group"],"narration":p["narration"],"sources":p.get("sources",[]),"research_queries":p.get("research_queries",[])},ensure_ascii=False,indent=2),encoding="utf-8")
 print("Verified research saved:",research_path.name)
 out=render(src,p,h)
 vid,privacy=publish(yt,out,p,item["name"])
 pending=h.pop("pending_opening_frame",None)
 if pending:
  h.setdefault("opening_frames",{}).setdefault(pending["artist"],[]).append(pending["timestamp"])
 h["processed"].append({"drive_file_id":item["id"],"source_name":item["name"],"artist":p["artist"],"youtube_video_id":vid,"privacy":privacy,"published_at":datetime.now(timezone.utc).isoformat()})
 H.parent.mkdir(parents=True,exist_ok=True);H.write_text(json.dumps(h,ensure_ascii=False,indent=2),encoding="utf-8")
if __name__=="__main__":main()
