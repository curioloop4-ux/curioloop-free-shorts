import asyncio,json,os,re,subprocess,textwrap,math,struct,wave
import unicodedata
from datetime import datetime,timezone,timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import requests,edge_tts,time
from PIL import Image, ImageDraw, ImageFont
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
 y=Credentials(None,refresh_token=need("YOUTUBE_REFRESH_TOKEN"),token_uri="https://oauth2.googleapis.com/token",client_id=need("YOUTUBE_CLIENT_ID"),client_secret=need("YOUTUBE_CLIENT_SECRET"),scopes=["https://www.googleapis.com/auth/youtube.readonly","https://www.googleapis.com/auth/youtube.upload"])
 return build("drive","v3",credentials=d),build("youtube","v3",credentials=y)
def verify_upload_channel(yt):
 expected=need("YOUTUBE_EXPECTED_CHANNEL_TITLE")
 response=yt.channels().list(part="id,snippet",mine=True,maxResults=5).execute()
 channels=response.get("items",[])
 if len(channels)!=1:
  names=[(x.get("snippet") or {}).get("title","unknown") for x in channels]
  raise RuntimeError(f"Could not uniquely verify CurioLoop YouTube channel (found {len(channels)}: {names}); refusing upload.")
 channel=channels[0]; snippet=channel.get("snippet") or {}
 actual=(snippet.get("title") or "").strip()
 channel_id=(channel.get("id") or "").strip()
 print(f"YouTube OAuth preflight: expected_channel={expected!r}, actual_channel={actual!r}, channel_id={channel_id!r}")
 if actual.casefold()!=expected.casefold():
  raise RuntimeError(f"YouTube OAuth channel mismatch: expected {expected!r}, but authenticated as {actual!r} ({channel_id}). No video was uploaded.")
 print(f"YouTube OAuth channel verified: {actual} ({channel_id}).")
 return channel_id
def scan(drive,folder):
 q=[folder]; seen=set(); out=[]
 while q:
  parent=q.pop(0)
  if parent in seen: continue
  seen.add(parent); token=None
  while True:
   a={"q":f"'{parent}' in parents and trashed=false","pageSize":100,"orderBy":"createdTime","fields":"nextPageToken,files(id,name,mimeType,createdTime,size,md5Checksum)","supportsAllDrives":True,"includeItemsFromAllDrives":True}
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
 def plain(s):
  if not isinstance(s,str):return ""
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
  ("MEOVV",["미야오","MEOVV"],["MEOVV"],2025),
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
 # Resolve the actual person article first. Idol pages often use titles such as
 # "카리나(aespa)", while the bare stage name may lead to a disambiguation page.
 group_aliases=[]
 for group_name,aliases,_,_ in group_data:
  if any(alias.casefold() in stem.casefold() for alias in aliases):
   group_aliases=aliases;break
 for base in ("https://namu.wiki","https://namu.moe","https://d.namu.moe"):
  headers={"Referer":base+"/","User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36","Accept":"text/html, text/plain, */*"}
  candidates=[]
  try:
   search=session.get(base+"/Search",params={"q":target},headers=headers,timeout=25)
   if search.ok:
    parser=SearchLinks();parser.feed(search.text)
    found=[]
    for href,label in parser.links:
     title=unquote(href[len("/w/"):]).replace("_"," ").strip()
     label=plain(label)
     if target.casefold() in (title+" "+label).casefold() and not any(x in title for x in ("동음이의어","분류:","틀:")):
      found.append(title)
    if group_aliases:
     group_terms=[x.casefold() for x in group_aliases]
     found.sort(key=lambda x:(0 if any(g in x.casefold() for g in group_terms) else 1,len(x)))
    candidates.extend(found)
  except requests.RequestException as e:
   errors.append(f"{base} search: {type(e).__name__}: {e}")
  candidates.extend([target])
  unique=[]
  for name in candidates:
   if name and name not in unique:unique.append(name)
  for candidate_name in unique[:8]:
   raw_url=base+"/raw/"+quote(candidate_name,safe="")
   try:
    rr=session.get(raw_url,headers=headers,timeout=40)
    if rr.status_code==404:
     page_url=base+"/w/"+quote(candidate_name,safe="")
     rr=session.get(page_url,headers=headers,timeout=40)
    rr.raise_for_status()
    candidate=rr.text.strip()
    if "<html" in candidate[:500].lower():
     from html import unescape
     candidate=re.sub(r"<(script|style)\\b[^>]*>.*?</\\1>"," ",candidate,flags=re.I|re.S)
     candidate=unescape(re.sub(r"<[^>]+>"," ",candidate))
    if len(candidate)>=300 and "문서를 찾을 수 없습니다" not in candidate and "동음이의어" not in candidate[:1000]:
     raw=candidate;chosen=candidate_name;source_base=base;break
    errors.append(f"{base}/{candidate_name}: article response was empty, disambiguation, or too short")
   except requests.RequestException as e:
    errors.append(f"{base}/{candidate_name}: {type(e).__name__}: {e}")
   time.sleep(0.7)
  if raw is not None:break
  time.sleep(1.2)
 if raw is None:
  print("NamuWiki page unavailable; continuing with independently searched public profile and news sources.")
 else:
  print("NamuWiki article retrieved:",chosen,"via",source_base)
 cleaned=plain(raw or "")
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
 # Determine group affiliation from the source filename only. Searching the
 # full biography can incorrectly assign a group mentioned in unrelated text.
 for english,aliases,_,year in group_data:
  if any(alias.casefold() in stem.casefold() for alias in aliases):
   group=english;group_year=year;break
 if not group:
  group="Group affiliation not confirmed"
  group_year=None
 from xml.etree import ElementTree as ET
 from urllib.parse import urlparse
 # Search the open web for profile evidence instead of relying on the NamuWiki
 # mirror's markup alone. Search snippets and fetched source pages are both parsed.
 profile_documents=([{"title":"NamuWiki: "+chosen,"url":source_base+"/w/"+quote(chosen,safe=""),"text":raw,"priority":0}] if source_base and raw else [])
 profile_queries=[
  f'"{target}" "{group}" 프로필 키 몸무게 생년월일'.replace('"Group affiliation not confirmed"',"").strip(),
  f'"{target}" "{group}" 신체사이즈 취미 특기'.replace('"Group affiliation not confirmed"',"").strip(),
  f'"{artist}" {group} height weight birthday profile'.replace("Group affiliation not confirmed","").strip(),
  f'site:namu.wiki "{target}" 키 몸무게 생년월일',
  f'"{target}" {group} 신발사이즈 발사이즈 허리둘레 가슴둘레 엉덩이둘레',
  f'"{artist}" {group} shoe size foot size bust chest waist hips body measurements',
  f'"{target}" {group} official profile biography career debut member agency',
  f'"{artist}" K-pop official biography activities variety show performance interview'
 ]
 class ProfilePageText(HTMLParser):
  def __init__(self):
   super().__init__();self.parts=[];self.meta=[];self.skip=0
  def handle_starttag(self,tag,attrs):
   a=dict(attrs)
   if tag in ("script","style","noscript"):self.skip+=1
   if tag=="meta" and (a.get("name","").casefold()=="description" or a.get("property","").casefold() in ("og:description","twitter:description")) and a.get("content"):self.meta.append(a["content"])
  def handle_endtag(self,tag):
   if tag in ("script","style","noscript") and self.skip:self.skip-=1
  def handle_data(self,data):
   if not self.skip and data.strip():self.parts.append(data.strip())
 for query in profile_queries:
  try:
   search=session.get("https://www.bing.com/search",params={"format":"rss","q":query},timeout=18)
   search.raise_for_status()
   for node in ET.fromstring(search.content).findall(".//item"):
    title=plain(node.findtext("title") or "")
    url=(node.findtext("link") or "").strip()
    snippet=plain(node.findtext("description") or "")
    if not url.startswith("http") or not (target.casefold() in (title+" "+snippet).casefold() or artist.casefold() in (title+" "+snippet).casefold()):continue
    if any(x["url"]==url for x in profile_documents):continue
    document={"title":title[:180],"url":url,"text":snippet,"priority":2}
    try:
     page=session.get(url,timeout=14,headers={"User-Agent":"Mozilla/5.0"})
     if page.ok and "html" in page.headers.get("content-type","").casefold():
      parser=ProfilePageText();parser.feed(page.text[:500000])
      page_text=plain(" ".join(parser.meta+parser.parts))
      if len(page_text)>100:document["text"]=page_text[:30000]
      domain=urlparse(url).netloc.casefold()
      if any(x in domain for x in ("smtown.com","official","weverse.io")):document["priority"]=1
    except Exception as e:print("Profile source fetch unavailable:",url,type(e).__name__)
    profile_documents.append(document)
    if len(profile_documents)>=13:break
  except Exception as e:print("Profile web search unavailable:",type(e).__name__)
  time.sleep(0.6)
 # Add Korean and English Wikipedia as independent biography fallbacks. Search
 # with both the stage name and group to avoid same-name collisions.
 for wiki_lang,wiki_query in [("ko",f"{target} {group} 가수"),("en",f"{artist} {group} singer")]:
  try:
   api=f"https://{wiki_lang}.wikipedia.org/w/api.php"
   found=session.get(api,params={"action":"query","list":"search","srsearch":wiki_query,"srlimit":5,"format":"json"},timeout=15)
   found.raise_for_status()
   for hit in found.json().get("query",{}).get("search",[]):
    page_id=hit.get("pageid")
    if not page_id:continue
    detail=session.get(api,params={"action":"query","pageids":page_id,"prop":"extracts","explaintext":1,"exsectionformat":"plain","format":"json"},timeout=15)
    detail.raise_for_status()
    page=detail.json().get("query",{}).get("pages",{}).get(str(page_id),{})
    extract=plain(page.get("extract",""))
    title=plain(page.get("title",""))
    if len(extract)<300:continue
    identity=(target.casefold() in (title+" "+extract).casefold() or artist.casefold() in (title+" "+extract).casefold())
    group_match=group=="Group affiliation not confirmed" or group.casefold() in (title+" "+extract).casefold()
    if not identity or not group_match:continue
    url=f"https://{wiki_lang}.wikipedia.org/?curid={page_id}"
    if not any(x["url"]==url for x in profile_documents):
     profile_documents.append({"title":f"Wikipedia ({wiki_lang}): {title}","url":url,"text":extract[:30000],"priority":1})
     print(f"Wikipedia biography retrieved: {wiki_lang} / {title} ({len(extract)} characters).")
    break
  except Exception as e:print(f"Wikipedia biography search unavailable ({wiki_lang}):",type(e).__name__)
 # Keep a compact audit trail; the individual values are extracted below
 # only when the source explicitly labels the relevant attribute.
 web_facts=[{"title":x["title"],"url":x["url"]} for x in profile_documents[1:] if x.get("title") and x.get("url")]
 print("Profile web research: queries=",len(profile_queries),"; source_pages=",len(profile_documents)-1)
 if raw is None and not profile_documents:
  raise RuntimeError("No usable biography source found for "+target+"; wiki mirrors and independent search providers returned no matching public profile pages.")
 print("Biography source mode:", "NamuWiki + web cross-check" if raw else "independent public web sources (NamuWiki unavailable)")
 # Extract birth date only when it appears next to a biographical label.
 birth=None
 birth_patterns=[
  r"(?:생년월일|출생일|출생)\s*(?:\|\|?|:|：|：)?\s*[^|]{0,100}?(\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일)",
  r"(?:생년월일|출생일|출생)[^0-9]{0,80}(\d{4}[.\-/]\s*\d{1,2}[.\-/]\s*\d{1,2})"
 ]
 for document in sorted(profile_documents,key=lambda x:x.get("priority",2)):
  for pattern in birth_patterns:
   m=re.search(pattern,document["text"],re.I)
   if m:birth=re.sub(r"\s+","",m.group(1));break
  if birth:break
 if birth and re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}",birth):
  birth=birth.replace(".","-")
 # Extract physical profile data and personal characteristics only when explicitly labelled.
 def labelled_number(labels, unit_pattern):
  # Parse NamuWiki table cells first: a profile label is commonly followed
  # by its value in the next cell. Then use bounded text fallbacks.
  readable=plain(raw)
  cells=[plain(cell) for cell in re.split(r"\|\|",raw or "")]
  for i,cell in enumerate(cells):
   normalized=re.sub(r"\s+","",cell).casefold()
   for label in labels:
    key=re.sub(r"\s+","",label).casefold()
    if key and key in normalized:
     for candidate in cells[i+1:i+4]:
      match=re.search(unit_pattern,candidate,re.I)
      if match:
       return re.sub(r"\s+","",match.group(0))
  for document in sorted(profile_documents,key=lambda x:x.get("priority",2)):
   source=document["text"]
   for label in labels:
    escaped=re.escape(label)
    patterns=[
     r"(?:^|\|\|)\s*"+escaped+r"\s*\|\|\s*("+unit_pattern+r")",
     r"(?:^|\n)\s*"+escaped+r"\s*(?::|：|\|\|?)\s*("+unit_pattern+r")",
     r"\b"+escaped+r"\b[^0-9]{0,40}("+unit_pattern+r")"
    ]
    for pattern in patterns:
     match=re.search(pattern,source,re.I)
     if match and match.lastindex and match.group(1):
      return re.sub(r"\s+","",match.group(1))
  return None
 height=labelled_number(["신장","키","height"],r"\d{2,3}(?:\.\d+)?\s*(?:cm|센티미터)")
 weight=labelled_number(["몸무게","체중","weight"],r"\d{2,3}(?:\.\d+)?\s*(?:kg|킬로그램)")
 bust=labelled_number(["가슴둘레","가슴 둘레","가슴 사이즈","가슴사이즈","흉부둘레","bust","chest circumference"],r"\d{2,3}(?:\.\d+)?\s*(?:cm|센티미터)")
 waist=labelled_number(["허리둘레","허리 사이즈","허리사이즈","waist circumference"],r"\d{2,3}(?:\.\d+)?\s*(?:cm|센티미터)")
 hips=labelled_number(["엉덩이둘레","엉덩이 둘레","힙둘레","힙 사이즈","hip circumference"],r"\d{2,3}(?:\.\d+)?\s*(?:cm|센티미터)")
 shoe_size=labelled_number(["발 사이즈","신발 사이즈","발사이즈","신발사이즈","shoe size","foot size"],r"\d{2,3}(?:\.5)?\s*(?:mm|cm)")
 def labeled_value(labels):
  # Read explicitly labelled values from NamuWiki and discovered profile pages.
  for document in sorted(profile_documents,key=lambda x:x.get("priority",2)):
   source=document["text"]
   for label in labels:
    escaped=re.escape(label)
    m=re.search(r"(?:^|\|\|)\s*"+escaped+r"\s*\|\|\s*([^|\n]{1,100})",source,re.I)
    if m:
     value=plain(m.group(1))
     if value:return value[:100]
    m=re.search(r"(?:^|\n)\s*"+escaped+r"\s*[:：]\s*([^|\n]{1,100})",source,re.I)
    if m:
     value=plain(m.group(1))
     if value:return value[:100]
  return None
 birth_year=None
 if birth:
  year_match=re.search(r"(19|20)\d{2}",birth)
  if year_match:birth_year=year_match.group(0)
 body_size=labeled_value(["신체사이즈","신체 사이즈","쓰리사이즈","3사이즈","쓰리 사이즈"])
 cup_size=labeled_value(["컵사이즈","컵 사이즈","브라사이즈","브라 사이즈","cup size","bra size"])
 position=labeled_value(["포지션","담당 포지션","보컬 포지션"])
 nickname=labeled_value(["별명","애칭"])
 hobby=labeled_value(["취미","특기"])
 personality=labeled_value(["성격","특징","매력 포인트"])
 # Extract concrete career/episode details from the resolved NamuWiki article.
 # Prefer narrative sections over infoboxes; translate Korean source text for the English voice-over.
 article_highlights=[]
 if raw:
  section_parts=re.split(r"(?m)^={2,}\s*(.*?)\s*={2,}\s*$",raw)
  wanted_sections=re.compile(r"활동|경력|데뷔|음악|방송|출연|여담|특징|개인",re.I)
  for section_index in range(1,len(section_parts),2):
   heading=plain(section_parts[section_index])
   if not wanted_sections.search(heading):continue
   body=section_parts[section_index+1]
   for line in re.split(r"(?m)^\s*(?:\*+|#+)\s*",body):
    candidate=plain(line)
    if len(candidate)<45 or len(candidate)>280:continue
    if candidate in article_highlights:continue
    if re.search(r"나무위키|편집|문서|분류|각주|출처|틀:",candidate):continue
    article_highlights.append(candidate)
    if len(article_highlights)>=12:break
   if len(article_highlights)>=12:break
 # When the wiki page is blocked, recover biographical detail from independently
 # fetched public pages/snippets. Keep only substantial text tied to this artist.
 # Supplement sparse wiki sections with independently fetched profile pages.
 # Previously this fallback ran only when zero wiki highlights existed, so a
 # page with 1-3 highlights ignored all other fetched profile sources.
 def highlight_key(value):
  return re.sub(r"[^가-힣a-z0-9]","",value.casefold())
 existing_highlight_keys={highlight_key(x) for x in article_highlights}
 for document in sorted(profile_documents,key=lambda x:x.get("priority",2)):
  if len(article_highlights)>=12:break
  if document.get("priority",2)==0:continue
  title=plain(document.get("title",""))
  detail=plain(document.get("text",""))
  if len(detail)<90 or not any(name.casefold() in (title+" "+detail).casefold() for name in (target,artist)):
   continue
  candidates=re.split(r"(?<=[.!?。])\s+|(?<=[다요죠함음])\s{2,}",detail)
  for candidate in candidates:
   candidate=plain(candidate)
   if len(candidate)<55 or len(candidate)>280:continue
   # The page itself has already been matched to this artist; do not require
   # the artist's name to be repeated in every biographical sentence.
   key=highlight_key(candidate)
   if not key or key in existing_highlight_keys:continue
   article_highlights.append(candidate)
   existing_highlight_keys.add(key)
   break
 translated_highlights=[]
 for detail in article_highlights:
  # English profile text can be used directly; it must not depend on the
  # optional Google translation endpoint being available.
  if not re.search(r"[가-힣]",detail):
   if len(detail.split())>=8:translated_highlights.append(detail.rstrip(". "))
  else:
   try:
    tr=session.get("https://translate.googleapis.com/translate_a/single",params={"client":"gtx","sl":"ko","tl":"en","dt":"t","q":detail[:900]},timeout=12)
    tr.raise_for_status()
    translated="".join(part[0] for part in tr.json()[0] if part and part[0]).strip()
    if len(translated.split())>=8:translated_highlights.append(translated.rstrip(". "))
   except Exception as e:print("Biography highlight translation unavailable; retaining other English source material:",type(e).__name__)
  if len(translated_highlights)>=10:break
 print("Biography highlights extracted:",len(article_highlights),"; translated:",len(translated_highlights),"from NamuWiki and independent profile pages")
 print("PROFILE RESEARCH RESULT")
 print("Artist:",artist,"| Group:",group)
 print("Birth:",birth or "not verified","| Height:",height or "not verified","| Weight:",weight or "not verified")
 print("Body measurements:",body_size or "not verified","| Bust:",bust or "not verified","| Waist:",waist or "not verified","| Hips:",hips or "not verified","| Cup size:",cup_size or "not verified","| Shoe size:",shoe_size or "not verified")
 print("Position:",position or "not verified","| Nickname:",nickname or "not verified","| Hobby:",hobby or "not verified")
 print("Profile sources:")
 for source in profile_documents[:8]:print("-",source.get("title","Profile source"),source.get("url",""))
 # Search recent coverage across Korean and English editions and rank issue clusters.
 from xml.etree import ElementTree as ET
 from urllib.parse import urlparse
 news_title=None;news_url=None;news_date=None;news_source=None
 news_explanation=None;news_outlets=[];news_article_count=0
 articles=[]
 facts=[f"Meet {artist}, a K-pop artist."]
 if group!="Group affiliation not confirmed":
  facts.append(f"She is a member of {group}.")
  if group_year:facts.append(f"The group made its debut in {group_year}.")
 if birth_year:facts.append(f"Born in {birth_year}.")
 if height:facts.append(f"She is {height} tall.")
 if weight:facts.append(f"Her listed weight is {weight}.")
 if bust:facts.append(f"Her publicly listed bust measurement is {bust}.")
 if waist:facts.append(f"Her listed waist measurement is {waist}.")
 if hips:facts.append(f"Her listed hip measurement is {hips}.")
 if cup_size:facts.append(f"Her listed bra or cup size is {cup_size}.")
 if shoe_size:facts.append(f"Her listed shoe size is {shoe_size}.")
 if body_size:facts.append(f"The listed body measurements are {body_size}.")
 if position:facts.append(f"Her position in the group is {position}.")
 if nickname:facts.append(f"Fans also call her {nickname}.")
 if hobby:facts.append(f"Outside performing, she enjoys {hobby}.")
 if personality:facts.append(f"Her profile describes her as {personality}.")
 for detail in translated_highlights:
  facts.append("Her biography also notes that "+detail+".")
 def relevant(title,description):
  blob=(title+" "+description).casefold()
  # Reject obvious same-name collisions before accepting a news result.
  if re.search(r"lisa leslie|lisa leslie award|leslie award",blob):return False
  name_match=any(x and re.search(r"(?<![a-z0-9])"+re.escape(x.casefold())+r"(?![a-z0-9])",blob) for x in (target,artist))
  if not name_match:return False
  if group!="Group affiliation not confirmed" and group.casefold() in blob:return True
  context_terms=("k-pop","kpop","blackpink","girl group","idol","singer","music","debut","comeback","tour","entertainment","가수","아이돌","걸그룹","그룹","소속사","컴백","무대","음악","공연")
  return any(term in blob for term in context_terms)
 def publisher_key(item):
  value=(item.get("publisher") or "").strip().casefold()
  return re.sub(r"[^a-z0-9가-힣]","",value) if value else urlparse(item.get("url","")).netloc.casefold().removeprefix("www.")
 def tokens(value):
  stop={"the","and","for","with","from","that","this","has","have","was","are","her","his","their","about","after","before","into","over","under","news","report","reports","video","photos","photo","인터뷰","관련","근황","공개","화제","뉴스","소식","그룹"}
  ignored={target.casefold(),artist.casefold(),group.casefold()}
  return {w for w in re.findall(r"[a-z0-9가-힣]{2,}",re.sub(r"\s+-\s+[^-]{2,55}$","",value).casefold()) if w not in stop and w not in ignored}
 def similarity(a,b):
  x=tokens(a);y=tokens(b)
  return len(x&y)/len(x|y) if x and y else 0.0
 def add_article(title,description,url,publisher,published,language):
  title=plain(title or "");description=plain(description or "")
  if not title or not url or not relevant(title,description) or len(title)<16:return
  if re.search(r"coverage report|recent coverage|search results|top stories",title,re.I):return
  key=(title.casefold(),(publisher or "").casefold())
  if any((a["title"].casefold(),a["publisher"].casefold())==key for a in articles):return
  articles.append({"title":title[:220],"description":description[:600],"url":url,"publisher":publisher or "Unknown outlet","published":published or "","language":language})
 # Profile links were collected and page text parsed above.
 # Search several distinct news angles in Korean and English. The wider
 # query set prevents a single narrow headline search from deciding the topic.
 group_query="" if group=="Group affiliation not confirmed" else group
 feeds=[
  (f'"{target}" {group_query} when:30d',"ko","KR","KR:ko"),
  (f'"{target}" 인터뷰 OR 발언 OR 입장 OR 논란 OR 화제 when:30d',"ko","KR","KR:ko"),
  (f'"{target}" 방송 OR 출연 OR 수상 OR 컴백 OR 신곡 OR SNS OR 투어 when:30d',"ko","KR","KR:ko"),
  (f'"{target}" 팬 반응 OR 공식입장 OR 소속사 OR 활동 when:30d',"ko","KR","KR:ko"),
  (f'"{artist}" {group_query} when:30d',"en-US","US","US:en"),
  (f'"{artist}" interview OR statement OR controversy OR reaction when:30d',"en-US","US","US:en"),
  (f'"{artist}" appearance OR award OR comeback OR tour OR announcement when:30d',"en-US","US","US:en"),
  (f'"{artist}" agency OR fans OR response OR activity when:30d',"en-US","US","US:en")]
 for query,hl,gl,ceid in feeds:
  try:
   rr=session.get("https://news.google.com/rss/search",params={"q":query,"hl":hl,"gl":gl,"ceid":ceid,"sort":"date"},timeout=20);rr.raise_for_status()
   for node in ET.fromstring(rr.content).findall(".//item"):
    src=node.find("source")
    add_article(node.findtext("title") or "",node.findtext("description") or "",(node.findtext("link") or "").strip(),src.text.strip() if src is not None and src.text else "Google News",node.findtext("pubDate") or "",hl)
  except Exception as e:print("Google News feed unavailable:",hl,type(e).__name__)
  time.sleep(0.8)
 # Bing contributes direct publisher links and, where available, explanatory snippets.
 for query in [f'"{target}" {group_query} news interview statement when:30d',f'"{artist}" {group_query} latest news controversy interview when:30d']:
  try:
   rr=session.get("https://www.bing.com/search",params={"format":"rss","q":query},timeout=20);rr.raise_for_status()
   for node in ET.fromstring(rr.content).findall(".//item"):
    url=(node.findtext("link") or "").strip()
    add_article(node.findtext("title") or "",node.findtext("description") or "",url,urlparse(url).netloc.replace("www.",""),"","en")
  except Exception as e:print("Bing news search unavailable:",type(e).__name__)
  time.sleep(0.8)
 # Merge paraphrased headlines; rank by distinct outlets, then article count.
 clusters=[]
 for article in articles:
  best=None;best_score=0.0
  for cluster in clusters:
   score=max(similarity(article["title"],x["title"]) for x in cluster)
   if score>best_score:best=cluster;best_score=score
  if best is not None and best_score>=0.25:best.append(article)
  else:clusters.append([article])
 ranked=[]
 for cluster in clusters:
  outlets={publisher_key(x) for x in cluster if publisher_key(x)}
  if outlets:ranked.append((len(outlets),len(cluster),cluster))
 ranked.sort(key=lambda x:(x[0],x[1]),reverse=True)
 if ranked:
  outlet_count,article_count,cluster=ranked[0]
  # Keep the narration in English, but translate Korean reporting rather than
  # silently excluding domestic coverage from the issue explanation.
  english=[]
  for item in cluster:
   title=item["title"];description=item["description"];translated=False
   if re.search(r"[가-힣]",title+" "+description):
    try:
     query_text=(title+"\n"+description[:500]).strip()
     tr=session.get("https://translate.googleapis.com/translate_a/single",params={"client":"gtx","sl":"auto","tl":"en","dt":"t","q":query_text},timeout=12)
     tr.raise_for_status()
     payload=tr.json()
     converted="".join(part[0] for part in payload[0] if part and part[0]).strip()
     if converted:
      pieces=converted.split("\n",1);title=pieces[0].strip() or title
      description=pieces[1].strip() if len(pieces)>1 else ""
      translated=True
    except Exception as e:print("Korean news translation unavailable:",type(e).__name__)
   if title and not re.search(r"[가-힣]",title):
    english.append({**item,"title":title,"description":description,"translated":translated,"language":"en"})
  # Google RSS descriptions are often just headlines; retrieve publisher meta descriptions.
  class ArticleMeta(HTMLParser):
   def __init__(self):
    super().__init__();self.description="";self.paragraphs=[];self.in_p=False;self.current=[]
   def handle_starttag(self,tag,attrs):
    attrs=dict(attrs)
    if tag=="meta" and (attrs.get("name","").casefold()=="description" or attrs.get("property","").casefold() in ("og:description","twitter:description")) and not self.description:self.description=attrs.get("content","").strip()
    if tag=="p":self.in_p=True;self.current=[]
   def handle_data(self,data):
    if self.in_p:self.current.append(data)
   def handle_endtag(self,tag):
    if tag=="p" and self.in_p:
     value=re.sub(r"\s+"," "," ".join(self.current)).strip()
     if len(value)>60:self.paragraphs.append(value)
     self.in_p=False
  for article in english[:8]:
   if len(article["description"])>90 or article.get("translated") or "news.google.com" in article["url"]:continue
   try:
    page=session.get(article["url"],timeout=12)
    if page.ok and "html" in page.headers.get("content-type","").casefold():
     parser=ArticleMeta();parser.feed(page.text[:450000])
     detail=plain(parser.description or (parser.paragraphs[0] if parser.paragraphs else ""))
     if len(detail)>60:article["description"]=detail[:600]
   except Exception as e:print("Article detail unavailable:",type(e).__name__)
   time.sleep(0.8)
  details=[];used=set()
  for article in sorted(english,key=lambda x:len(x["description"]),reverse=True):
   outlet=publisher_key(article);detail=plain(article["description"])
   if not outlet or outlet in used or len(detail)<55 or similarity(detail,article["title"])>0.72 or re.search(r"[가-힣]",detail):continue
   used.add(outlet);details.append(detail)
   if len(details)==2:break
  # Omit unsupported stories: require two publishers and explanatory copy.
  if outlet_count>=2 and len(details)>=2:
   lead=english[0] if english else cluster[0]
   news_title=re.sub(r"\s+-\s+[^-]{2,55}$","",lead["title"]).strip()[:150]
   news_url=lead["url"];news_date=lead["published"] or None;news_source=lead["publisher"]
   news_outlets=sorted({x["publisher"] for x in cluster})[:8];news_article_count=article_count
   sentences=[f"The most widely covered recent story about {artist} concerns {news_title.rstrip('. ')}."]
   detail1=re.split(r"(?<=[.!?])\s+",details[0])[0][:230]
   sentences.append(f"Coverage from {outlet_count} independent outlets reports that {detail1}")
   if len(details)>1:
    detail2=re.split(r"(?<=[.!?])\s+",details[1])[0][:190]
    sentences.append(f"Other reporting also noted that {detail2}")
   news_explanation=" ".join(sentences);facts.append(news_explanation)
  print(f"News research: collected={len(articles)}; top_issue={news_title or 'omitted'}; articles={news_article_count}; independent_outlets={len(news_outlets)}.")
 # Build a concise intro, reserve room for the leading news issue, then add extra profile facts.
 news_fact=facts[-1] if news_explanation else ""
 base_facts=facts[:-1] if news_explanation else facts
 intro_facts=base_facts
 extra_facts=[]
 narration_parts=[]
 word_budget=125
 def add_if_fits(sentence, force=False):
  candidate=" ".join(narration_parts+[sentence])
  if len(candidate.split())<=word_budget or (force and not narration_parts):
   narration_parts.append(sentence)
   return True
  return False
 for sentence in intro_facts:
  add_if_fits(sentence)
 if news_fact:
  news_sentences=[x.strip() for x in re.split(r"(?<=[.!?])\s+",news_fact) if x.strip()]
  # Keep the main issue and its key verified explanation ahead of secondary profile trivia.
  compact_news=" ".join(news_sentences[:2])
  if not add_if_fits(compact_news):
   # Shorten only the news wording if needed; retain the issue rather than dropping it.
   compact_news=" ".join(news_sentences[:1])
   if not add_if_fits(compact_news):
    compact_news="The main recent story is "+(news_title or "a widely covered development")+"."
    add_if_fits(compact_news)
 for sentence in extra_facts:
  add_if_fits(sentence)
 narration=" ".join(narration_parts)
 # Sparse research must not produce an unusably short video. Add a transparent
 # editorial bridge (not invented biography) only when verified facts are scarce.
 if len(narration.split())<75:
  raise RuntimeError(
   f"Insufficient verified biography for a 30-second intro: only {len(narration.split())} narration words "
   f"from {len(profile_documents)} profile sources and {len(translated_highlights)} translated career highlights. "
   "Research must be expanded; generic filler will not be used to reach the duration target."
  )
 print(f"Research narration collected: {len(narration.split())} words; final acceptance is based on measured video duration (minimum 30 seconds).")
 print(f"Narration budget: {len(narration.split())} words; news included: {'yes' if news_fact and any(news_title and news_title.casefold() in x.casefold() for x in narration_parts) else 'priority news summary' if news_fact else 'no supported issue'}; target video maximum: 50 seconds.")
 sources=[]
 if source_base and raw:
  source_url=source_base+"/w/"+quote(chosen,safe="")
  sources.append({"title":f"Profile: {chosen}","url":source_url})
 for item in articles:
  if item.get("url") and item["url"].startswith("http") and item["url"] not in [x["url"] for x in sources] and len(sources)<10:
   sources.append({"title":("News: "+item.get("title",""))[:100],"url":item["url"]})
 for item in web_facts[:3]:
  if item.get("url") and item["url"] not in [x["url"] for x in sources]:
   sources.append({"title":item.get("title","Public source")[:100],"url":item["url"]})
 print(f"Profile research complete: {chosen}; artist={artist}; group={group}; birth_year={birth_year or 'not listed'}; height={height or 'not listed'}; weight={weight or 'not listed'}; recent_news={'yes' if news_title else 'no'}.")
 return {
  "artist":artist,"group":group,"narration":narration,
  "title":f"{artist} | {group} Artist Spotlight"[:85],
  "tags":[x for x in ["K-pop","Kpop",group.replace(" ",""),group,artist.replace(" ",""),artist,"Artist Spotlight","K-pop idol profile","K-pop member profile","K-pop Shorts","Korean idol","아이돌 프로필","걸그룹 멤버","Shorts"] if x],
  "sources":sources,
  "recent_news":{"title":news_title,"url":news_url,"date":news_date,"publisher":news_source,"explanation":news_explanation,"article_count":news_article_count,"outlets":news_outlets},
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
 intro=1.5; source_len=dur(src)
 audio=W/"voice.mp3";vtt=W/"voice.srt"
 async def make_voice_and_subtitles():
  # Generate each spoken sentence separately so subtitle timing is based on
  # the measured duration of that exact sentence, not estimated word counts.
  raw_sentences=[x.strip() for x in re.split(r"(?<=[.!?])\s+",p["narration"].strip()) if x.strip()]
  if not raw_sentences:
   raise RuntimeError("Cannot create subtitles: narration is empty")
  # Short requests reduce Edge TTS NoAudioReceived failures on long clauses.
  sentences=[]
  for raw_sentence in raw_sentences:
   current=""
   for word in raw_sentence.split():
    candidate=(current+" "+word).strip()
    if len(candidate)>160 and current:
     sentences.append(current)
     current=word
    else:
     current=candidate
   if current: sentences.append(current)
  async def generate_segments(rate):
   parts=[]; durations=[]
   voices=["en-US-AriaNeural","en-US-JennyNeural","en-GB-SoniaNeural"]
   async def generate_one(text,label):
    part=W/f"voice_part_{label}.mp3"
    last_error=None
    for voice in voices:
     for attempt in range(1,4):
      try:
       if part.exists(): part.unlink()
       communicate=edge_tts.Communicate(text,voice,rate=rate)
       await communicate.save(str(part))
       if not part.exists() or part.stat().st_size<1024:
        raise RuntimeError("TTS returned an empty or incomplete audio segment")
       seconds=dur(part)
       if seconds<=0: raise RuntimeError("TTS audio segment has invalid duration")
       print(f"TTS segment {label} generated with {voice}, rate {rate} (attempt {attempt}).")
       return part,seconds
      except Exception as exc:
       last_error=exc
       print(f"TTS attempt failed for segment {label}, voice {voice}, rate {rate}, attempt {attempt}/3: {type(exc).__name__}: {exc}")
       if part.exists(): part.unlink()
       if attempt<3: await asyncio.sleep(2*attempt)
    raise RuntimeError(f"All Edge TTS voices failed for segment {label}: {last_error}") from last_error
   async def generate_sentence(text,index,depth=0):
    label=f"{index:03d}_{depth}_{len(text)}"
    try:
     return await generate_one(text,label)
    except Exception as exc:
     words=text.split()
     if depth>=2 or len(words)<4:
      raise
     midpoint=len(words)//2
     left=" ".join(words[:midpoint]); right=" ".join(words[midpoint:])
     print(f"TTS recovery: splitting failed narration segment {index+1} into two shorter requests (split depth {depth+1}). Original error: {exc}")
     left_part,left_seconds=await generate_sentence(left,index*2,depth+1)
     right_part,right_seconds=await generate_sentence(right,index*2+1,depth+1)
     joined=W/f"voice_recovered_{index:03d}_{depth}.mp3"
     listing=W/f"voice_recovered_{index:03d}_{depth}.txt"
     listing.write_text("file '"+str(left_part.resolve()).replace("\\","/")+"'\n"+"file '"+str(right_part.resolve()).replace("\\","/")+"'\n",encoding="utf-8")
     run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(listing),"-c:a","libmp3lame","-q:a","3",str(joined)])
     return joined,dur(joined)
   for i,sentence in enumerate(sentences):
    part,seconds=await generate_sentence(sentence,i)
    parts.append(part);durations.append(seconds)
   concat_list=W/"voice_concat.txt"
   concat_list.write_text(chr(10).join("file '" + str(x.resolve()).replace("\\\\","/") + "'" for x in parts) + chr(10),encoding="utf-8")
   run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat_list),"-c:a","libmp3lame","-q:a","3",str(audio)])
   return durations,dur(audio)
  # Intro card and ending take 3 seconds total, so narration must fit 47 seconds.
  # First use a natural pace, then regenerate the complete voice track progressively faster.
  tts_rates=["-3%","+8%","+16%","+24%","+32%","+40%","+50%"]
  durations,voice_seconds=await generate_segments(tts_rates[0])
  # A valid script can still be too short if a voice provider speaks unusually
  # quickly. Re-synthesize more slowly before rejecting the video.
  if voice_seconds<28.5:
   # Retry progressively slower natural speech rates until narration meets
   # the 30-second minimum. Never pad a short script with empty footage.
   for slow_rate in ["-10%","-18%","-25%","-35%","-45%","-50%"]:
    print(f"Narration is shorter than the 30-second video minimum ({voice_seconds:.1f}s); retrying at {slow_rate}.")
    durations,voice_seconds=await generate_segments(slow_rate)
    print(f"Measured narration after {slow_rate}: {voice_seconds:.1f}s.")
    if voice_seconds>=28.5:break
  for rate in tts_rates[1:]:
   if voice_seconds<=47.0:break
   print(f"Narration would exceed 50 seconds ({voice_seconds+3.0:.1f}s total); regenerating at {rate}.")
   durations,voice_seconds=await generate_segments(rate)
  if voice_seconds>47.0:
   # Last-resort time compression guarantees the requested 50-second ceiling.
   factor=voice_seconds/47.0
   run(["ffmpeg","-y","-i",str(audio),"-filter:a",f"atempo={factor:.5f}","-vn",str(W/"voice_capped.mp3")])
   os.replace(str(W/"voice_capped.mp3"),str(audio))
   durations=[seconds/factor for seconds in durations]
   voice_seconds=dur(audio)
   print(f"Applied final speech time-compression factor {factor:.3f}; measured narration {voice_seconds:.1f}s.")
  print(f"Measured narration duration: {voice_seconds:.1f}s; total target: {intro+voice_seconds+1.5:.1f}s.")
  if intro+voice_seconds+1.5<30.0:
   raise RuntimeError(f"Measured video duration would be only {intro+voice_seconds+1.5:.1f} seconds; minimum is 30 seconds.")
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
   clean_caption=re.sub(r"[!！()（）\[\]{}:：;；]+","",clean_caption)
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
 # Runtime follows spoken content: title + narration + brief ending.
 voice_seconds=dur(audio)
 length=min(50.0,max(10.0,intro+voice_seconds+1.5))
 footage=length-intro
 if source_len<footage+1:
  raise RuntimeError(f"Source too short ({source_len:.1f}s); need at least {footage+1:.1f}s for this {length:.1f}s intro")
 print(f"Dynamic video duration: {length:.1f}s (hard maximum 50s; narration {voice_seconds:.1f}s + title and ending).")
 # Select a different still-frame timestamp for repeat appearances of the same artist.
 used=hist.setdefault("opening_frames",{}).setdefault(p["artist"].casefold(),[])
 choices=[max(0.5,min(source_len-0.5,source_len*x)) for x in (0.08,0.18,0.30,0.43,0.57,0.70,0.82,0.92)]
 choices=[round(x,2) for x in choices]
 t=next((x for x in choices if x not in used),choices[len(used)%len(choices)])
 still=W/"opening.jpg"
 run(["ffmpeg","-y","-ss",f"{t:.2f}","-i",str(src),"-frames:v","1","-q:v","2",str(still)])
 hist["pending_opening_frame"]={"artist":p["artist"].casefold(),"timestamp":t}
 # Choose the most visually active continuous segment instead of always taking
 # the middle of the source, which can be a static close-up.
 start=max(0,(source_len-footage)/2)
 try:
  motion=subprocess.run(["ffmpeg","-v","error","-i",str(src),"-vf","fps=2,scale=48:80:flags=area,format=gray","-f","rawvideo","-pix_fmt","gray","pipe:1"],capture_output=True,check=True).stdout
  frame_size=48*80
  frame_count=len(motion)//frame_size
  window=max(2,min(frame_count,int(footage*2)))
  diffs=[0]
  for idx in range(1,frame_count):
   a=motion[(idx-1)*frame_size:idx*frame_size]
   b=motion[idx*frame_size:(idx+1)*frame_size]
   diffs.append(sum(abs(x-y) for x,y in zip(a,b))/frame_size)
  if frame_count>=window and len(diffs)>window:
   scores=[(sum(diffs[i+1:i+window]),i) for i in range(frame_count-window+1)]
   motion_score,best_index=max(scores)
   start=min(best_index/2,max(0,source_len-footage))
   print(f"Selected high-motion fancam segment: {start:.1f}s–{start+footage:.1f}s; motion score={motion_score:.2f}")
   hist["last_render_source_start"]=round(start,3);hist["last_render_source_duration"]=round(footage,3)
  else:
   print("Motion scan had too few frames; using centered source segment.")
   hist["last_render_source_start"]=round(start,3);hist["last_render_source_duration"]=round(footage,3)
 except Exception as e:
  print("Motion scan unavailable; using centered source segment:",type(e).__name__)
  hist["last_render_source_start"]=round(start,3);hist["last_render_source_duration"]=round(footage,3)
 name=p["artist"].replace("'","").replace(":","");group=p["group"].replace("'","").replace(":","")
 out=W/(re.sub(r"[^A-Za-z0-9_-]+","_",name).lower()+"_artist_intro.mp4")
 font="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf";bold="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
 # Show all call-to-action lines together, centered, during only the final 3 seconds.
 cta=W/"cta_artist_intro.png"
 card=Image.new("RGBA",(1080,1800),(0,0,0,0));draw=ImageDraw.Draw(card)
 regular=ImageFont.truetype(bold,42);heavy=ImageFont.truetype(bold,36)
 def draw_cta_line(text,y,face,fill):
  b=draw.textbbox((0,0),text,font=face,stroke_width=3)
  tw=b[2]-b[0];pad_x,pad_y=28,18
  left=(1080-tw)//2-pad_x;right=(1080+tw)//2+pad_x
  top=y-pad_y;bottom=y+(b[3]-b[1])+pad_y
  draw.rounded_rectangle((left,top,right,bottom),radius=24,fill=(5,8,20,175))
  draw.text(((1080-tw)/2,y-b[1]),text,font=face,fill=fill,stroke_width=3,stroke_fill=(5,5,12,255))
 draw_cta_line("WHO SHOULD WE FEATURE NEXT?",735,regular,(255,255,255,255))
 draw_cta_line("COMMENT BELOW!",815,heavy,(255,255,255,255))
 draw_cta_line("SUBSCRIBE FOR MORE K-POP!",915,heavy,(75,224,255,255))
 card.save(cta)
 cta_start=max(1.5,length-3.0)
 cue_data=json.loads((W/"subtitle_cues.json").read_text(encoding="utf-8"))
 subtitle_filters=[]
 for i,cue in enumerate(cue_data):
  cue_file=W/f"subtitle_{i:03d}.txt"
  cue_file.write_text(cue["text"],encoding="utf-8")
  cue_start=max(1.5,float(cue["start"]));cue_end=min(length,float(cue["end"]))
  if cue_end<=cue_start: continue
  enable=f"between(t\\,{cue_start:.3f}\\,{cue_end:.3f})"
  subtitle_filters.append(f",drawtext=fontfile={font}:textfile='{cue_file}':fontcolor=white:fontsize=40:borderw=3:bordercolor=black@0.85:shadowcolor=black@0.5:shadowx=1:shadowy=1:box=1:boxcolor=black@0.58:boxborderw=18:text_align=center:x=(w-text_w)/2:y=h-text_h-340:line_spacing=8:enable='{enable}'")
 if not subtitle_filters: raise RuntimeError("No timed subtitle overlays were generated")
 print("Burn-in subtitle overlays:",len(subtitle_filters))
 fc=(
  "[0:v]scale=1080:1800:force_original_aspect_ratio=increase,crop=1080:1800,setsar=1,fps=30,format=yuv420p,settb=AVTB,trim=duration=1.5,setpts=PTS-STARTPTS,"
  f"drawtext=fontfile={bold}:text='{name}':fontcolor=white:fontsize=76:borderw=3:bordercolor=black@0.8:x=(w-text_w)/2:y=h-390,"
  f"drawtext=fontfile={font}:text='{group}':fontcolor=white:fontsize=38:borderw=2:bordercolor=black@0.8:x=(w-text_w)/2:y=h-300[v0];"
  f"[1:v]trim=start={start:.3f}:duration={footage:.3f},setpts=PTS-STARTPTS,scale=1080:1800:force_original_aspect_ratio=increase,crop=1080:1800,setsar=1,fps=30,format=yuv420p,settb=AVTB[v1];"
  f"[v0][v1]concat=n=2:v=1:a=0,format=yuv420p{''.join(subtitle_filters)}[vbase];"
 )
 fc+=f"[3:v]format=rgba,fade=t=in:st={cta_start:.3f}:d=0.4:alpha=1[cta];"
 fc+=f"[vbase][cta]overlay=0:0:enable='between(t,{cta_start:.3f},{length:.3f})'[v];"
 fc+=f"[2:a]adelay=1500|1500,apad,atrim=duration={length:.3f}[voice];"
 # Retain the source performance sound at a reduced level under the narration.
 probe=json.loads(subprocess.run(["ffprobe","-v","error","-show_entries","stream=codec_type","-of","json",str(src)],check=True,capture_output=True,text=True).stdout)
 has_source_audio=any(x.get("codec_type")=="audio" for x in probe.get("streams",[]))
 if has_source_audio:
  fc+=f"[1:a]atrim=start={start:.3f}:duration={footage:.3f},asetpts=PTS-STARTPTS,adelay=1500|1500,volume=0.20,apad,atrim=duration={length:.3f}[original];"
  fc+="[original][voice]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,alimiter=limit=0.95[a]"
 else:
  fc+="[voice]anull[a]"
 run(["ffmpeg","-y","-loop","1","-t","1.5","-i",str(still),"-i",str(src),"-i",str(audio),"-loop","1","-i",str(cta),"-filter_complex_threads","1","-filter_complex",fc,"-map","[v]","-map","[a]","-t",f"{length:.3f}","-r","30","-c:v","libx264","-preset","medium","-crf","20","-c:a","aac","-b:a","192k","-movflags","+faststart",str(out)])
 check=json.loads(subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height","-of","json",str(out)],check=True,capture_output=True,text=True).stdout)["streams"][0]
 if (int(check["width"]),int(check["height"]))!=(1080,1800) or not length-0.5<=dur(out)<=length+0.5:raise RuntimeError(f"Output validation failed: expected {length:.1f}s, got {dur(out):.1f}s")
 return out
def intro_publish_at_kst(publish_time):
 try:
  hour,minute=[int(part) for part in publish_time.split(":",1)]
  if not (0<=hour<=23 and 0<=minute<=59):raise ValueError
 except (TypeError,ValueError):
  raise ValueError("INTRO_PUBLISH_TIME_KST must use HH:MM format.")
 now=datetime.now(ZoneInfo("Asia/Seoul"))
 publish_at=now.replace(hour=hour,minute=minute,second=0,microsecond=0)+timedelta(minutes=30)
 if publish_at<=now+timedelta(minutes=2):
  raise RuntimeError(f"Artist-intro slot {publish_time} + 30 minutes ({publish_at:%H:%M} KST) has passed or is too close. Refusing immediate publication.")
 print(f"Artist-intro fixed publication slot: {publish_time} + 30 minutes = {publish_at:%H:%M} KST.")
 return publish_at.isoformat(timespec="seconds")
def publish(yt,out,p,source):
 brand=os.getenv("FANCAMVERSE_CHANNEL_BRAND","CurioLoop").strip()
 if brand.casefold() not in {"curioloop","dailyneedle"}:raise RuntimeError("Artist intro upload blocked: unexpected channel brand "+brand)
 print("Using existing CurioLoop/DailyNeedle YouTube upload credentials.")
 approval_file=R/"INTRO_PUBLICATION_APPROVED.txt"
 approved_once=os.getenv("INTRO_PUBLIC_ONCE","").strip().lower() in {"1","true","yes"}
 approved_file=approval_file.exists() and approval_file.read_text(encoding="utf-8").strip().lower() in {"1","true","yes","approved"}
 approved=approved_once or approved_file
 scheduled_slot=os.getenv("INTRO_PUBLISH_TIME_KST","").strip()
 publish_at=intro_publish_at_kst(scheduled_slot) if scheduled_slot else None
 privacy="private" if publish_at else ("public" if approved else "private")
 print("One-time public approval:", "enabled" if approved_once else "disabled")
 research_links="\\n".join(f"{x.get('title','Source')}: {x['url']}" for x in p.get("sources",[]))
 description=f"Meet {p['artist']} from {p['group']}.\\nArtist profile, career highlights, and recent update.\\nOriginal footage source: {source}\\nResearch sources:\\n{research_links}\\n#Kpop #ArtistSpotlight #Shorts"
 upload_stamp=datetime.now(ZoneInfo("Asia/Seoul")).strftime("%m%d-%H%M%S")
 unique_title=f"{p['title']} | {upload_stamp}"[:100]
 status={"privacyStatus":privacy,"selfDeclaredMadeForKids":False}
 if publish_at:status["publishAt"]=publish_at
 body={"snippet":{"title":unique_title,"description":description[:4900],"tags":[x for x in p.get("tags",[]) if "curioloop" not in str(x).casefold()]+["K-pop","artist spotlight","Shorts"],"categoryId":"10"},"status":status}
 v=yt.videos().insert(part="snippet,status",body=body,media_body=MediaFileUpload(str(out),mimetype="video/mp4",resumable=True)).execute()
 returned_status=v.get("status") or {}
 returned_publish_at=returned_status.get("publishAt")
 if publish_at and (returned_status.get("privacyStatus")!="private" or returned_publish_at!=publish_at):
  raise RuntimeError(f"Artist-intro upload {v.get('id')} was accepted but scheduled publication was not confirmed (requested {publish_at}, returned {returned_publish_at}).")
 print(f"{'SCHEDULED '+publish_at if publish_at else privacy.upper()} UPLOAD https://youtube.com/shorts/"+v["id"])
 return v["id"],("scheduled" if publish_at else privacy)
def main():
 drive,yt=services();verify_upload_channel(yt);folder=need("FANCAMVERSE_DRIVE_FOLDER_ID")
 h=json.loads(H.read_text(encoding="utf-8")) if H.exists() else {"processed":[]}
 done={x.get("drive_file_id") for x in h["processed"]}
 done_hashes={x.get("source_md5") for x in h["processed"] if x.get("source_md5")}
 def source_name_key(value):
  stem=Path(value or "").stem.casefold()
  stem=re.sub(r"(?:[_ .-]*(?:ver(?:sion)?[_ .-]*[a-z0-9]*|fancam|fan[_ .-]*cam|직캠|팬캠|페이스캠|풀캠|가로캠|세로캠|4k|8k|60fps|long[_ .-]*ver))+$","",stem,flags=re.I)
  return re.sub(r"[^0-9a-z가-힣]+","",stem)
 done_names={source_name_key(x.get("source_name","")) for x in h["processed"] if x.get("source_name")}
 target=os.getenv("TARGET_ARTIST","").strip()
 scanned=scan(drive,folder)
 # Keep prior-used originals eligible, but place them after all never-used sources.
 candidates=scanned
 def source_was_used(item):
  return (item["id"] in done
   or bool(item.get("md5Checksum") and item.get("md5Checksum") in done_hashes)
   or source_name_key(item["name"]) in done_names)
 print(f"Drive source pool: {len(candidates)} total; previously used originals will be fallback-only.")
 # Only choose sources that the companion fancam pipeline can identify too.
 from curioloop_fancam import identify_artist as identify_fancam_artist
 blocked=("김채원","chaewon")
 compatible=[]
 for candidate in candidates:
  if any(term in candidate["name"].casefold() for term in blocked):
   continue
  artist_name,group_name,_=identify_fancam_artist(candidate["name"])
  if artist_name and group_name:
   compatible.append(candidate)
 candidates=compatible
 print(f"Sources compatible with both intro and fancam pipelines: {len(candidates)}")
 if not candidates:raise RuntimeError("No compatible Drive video sources found")
 # Fail closed: random selection only considers filenames with a known artist and group.
 known_artists=["장원영","원영","wonyoung","은하","권은비","카리나","닝닝","윈터","지젤","사쿠라","김채원","허윤진","카즈하","홍은채","안유진","유진","레이","리즈","이서","민지","하니","다니엘","해린","혜인","나연","정연","모모","사나","지효","미나","다현","채영","쯔위","아이린","슬기","웬디","조이","예리","예지","리아","류진","채령","유나","지수","제니","jennie","로제","리사","엄지","신비","소원","예린","유주","츄","희진","김립","진솔","최리"]
 known_groups=["아이브","ive","에스파","aespa","르세라핌","le sserafim","비비지","viviz","여자친구","gfriend","아이즈원","iz*one","izone","트와이스","twice","블랙핑크","blackpink","레드벨벳","red velvet","소녀시대","girls' generation","오마이걸","oh my girl","있지","itzy","엔믹스","nmixx","뉴진스","newjeans","아일릿","illit","스테이씨","stayc","키스오브라이프","kiss of life","베이비몬스터","babymonster","프로미스나인","fromis_9","트리플에스","triples"]
 alias_to_key={a.casefold():a.casefold() for a in known_artists}
 for canonical,aliases in {"wonyoung":["장원영","원영","wonyoung"],"eunha":["은하","eunha"],"sakura":["사쿠라","sakura"],"kazuha":["카즈하","kazuha"],"jennie":["제니","jennie"],"haerin":["해린","혜린","haerin"]}.items():
  for alias in aliases:alias_to_key[alias.casefold()]=canonical
 def source_artist_key(item):
  name=item["name"].casefold()
  matches=[alias for alias in alias_to_key if alias in name]
  return alias_to_key[max(matches,key=len)] if matches else ""
 def identifiable(item):
  return bool(source_artist_key(item))
 # Manual target may prioritize one artist, but identity validation remains mandatory.
 if target:
  aliases={"wonyoung":["장원영","원영","wonyoung"],"eunha":["은하","eunha"]}
  needles=aliases.get(target.casefold(),[target.casefold()])
  candidates=[x for x in candidates if any(term in x["name"].casefold() for term in needles) and identifiable(x)]
  if not candidates:raise RuntimeError(f"No identifiable Drive video found for requested test artist: {target}")
 else:
  candidates=[x for x in candidates if identifiable(x)]
  print(f"Identifiable source videos: {len(candidates)}; unknown/ambiguous videos skipped.")
  if not candidates:raise RuntimeError("No Drive video has a recognizable artist name in its filename; source identity is required to avoid misattribution.")
  recent_artist_keys=[x.get("source_artist_key") or x.get("artist","").casefold() for x in h["processed"][-2:]]
  diversified=[x for x in candidates if source_artist_key(x) not in recent_artist_keys]
  if diversified:
   candidates=diversified
   print("Recent-artist repeat prevention active; excluded artists:",", ".join(recent_artist_keys))
  # Random selection is the default; every never-used original ranks ahead of all used ones.
  import random
  fresh_candidates=[x for x in candidates if not source_was_used(x)]
  used_candidates=[x for x in candidates if source_was_used(x)]
  random.shuffle(fresh_candidates)
  random.shuffle(used_candidates)
  candidates=fresh_candidates+used_candidates
  print(f"Random source rotation: {len(fresh_candidates)} never-used first, {len(used_candidates)} previously-used fallback sources second.")
 # Retry another unused Drive original whenever research, download, rendering,
 # or validation fails. A source is not marked processed until YouTube accepts it.
 # This allows newly added Drive videos to act as same-run fallback candidates.
 failures=[]
 uploaded=False
 for attempt,item in enumerate(candidates,1):
  selected_artist_key=source_artist_key(item)
  print(f"FALLBACK ATTEMPT {attempt}/{len(candidates)}: {item['name']}")
  print("Selected source artist key:",selected_artist_key or "manual target")
  try:
   p=profile(item["name"],target_hint=target)
   print("Selected Drive source:",item["name"])
   src=download(drive,item)
   research_path=W/(re.sub(r"[^A-Za-z0-9_-]+","_",p["artist"].lower())+"_research.json")
   research_path.write_text(json.dumps({"artist":p["artist"],"group":p["group"],"narration":p["narration"],"sources":p.get("sources",[]),"recent_news":p.get("recent_news",{}),"research_queries":p.get("research_queries",[])},ensure_ascii=False,indent=2),encoding="utf-8")
   print("Verified research saved:",research_path.name)
   out=render(src,p,h)
   rendered_source_start=float(h.get("last_render_source_start",0.0))
   rendered_source_duration=float(h.get("last_render_source_duration",0.0))
   if rendered_source_duration<=0:
    raise RuntimeError("Could not determine the exact source segment used in the artist intro")
  except Exception as exc:
   failure=f"{item['name']}: {type(exc).__name__}: {exc}"
   failures.append(failure)
   print("SOURCE FAILED; moving to the next unused Drive video:",failure)
   continue
  # Do not retry another source after entering the YouTube insert call: a
  # network timeout can occur after YouTube accepted the upload, risking duplicates.
  try:
   vid,privacy=publish(yt,out,p,item["name"])
  except Exception as exc:
   raise RuntimeError(
    f"YouTube upload result is uncertain for {item['name']}; stopping to prevent duplicate uploads. "
    f"Earlier source failures: {' | '.join(failures[-5:])}"
   ) from exc
  pending=h.pop("pending_opening_frame",None)
  if pending:
   h.setdefault("opening_frames",{}).setdefault(pending["artist"],[]).append(pending["timestamp"])
  h["processed"].append({"drive_file_id":item["id"],"source_md5":item.get("md5Checksum"),"source_artist_key":selected_artist_key,"source_name":item["name"],"artist":p["artist"],"youtube_video_id":vid,"privacy":privacy,"intro_footage_start":round(rendered_source_start,3),"intro_footage_end":round(rendered_source_start+rendered_source_duration,3),"published_at":datetime.now(timezone.utc).isoformat()})
  H.parent.mkdir(parents=True,exist_ok=True);H.write_text(json.dumps(h,ensure_ascii=False,indent=2),encoding="utf-8")
  # Hand off the exact same Drive original to the companion fancam workflow.
  queue_path=R/"history"/"artist_intro_fancam_queue.json"
  queue_path.write_text(json.dumps({"drive_file_id":item["id"],"source_name":item["name"],"intro_video_id":vid,"intro_footage_start":round(rendered_source_start,3),"intro_footage_end":round(rendered_source_start+rendered_source_duration,3),"created_at":datetime.now(timezone.utc).isoformat()},ensure_ascii=False,indent=2),encoding="utf-8")
  print("Fancam handoff queued for the exact same Drive source:",item["name"])
  print(f"UPLOAD SUCCESS on fallback attempt {attempt}: {vid}")
  uploaded=True
  break
 if not uploaded:
  summary=" | ".join(failures[-8:])
  raise RuntimeError(f"All {len(candidates)} eligible Drive sources failed before upload. No source was marked processed. Failures: {summary}")

if __name__=="__main__":main()
