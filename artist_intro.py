import asyncio,json,os,re,subprocess,textwrap
from datetime import datetime,timezone
from pathlib import Path
import requests,edge_tts
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
def profile(filename):
 stem=Path(filename).stem.casefold()
 if "eunha" in stem or "은하" in stem:
  narration=("Meet Eunha, born Jung Eun-bi in 1997. She debuted in January 2015 as a member of GFRIEND, "
   "then returned to the stage in 2022 as part of VIVIZ with SinB and Umji. Known for her clear, expressive "
   "vocals, she has also contributed to drama soundtracks. In October 2026, the trio signed with UNCORE, "
   "while their future group name remains under discussion. In November, they won a court ruling over unpaid "
   "earnings from their former agency.")
  return {"artist":"EUNHA","group":"GFRIEND / VIVIZ","narration":narration,
   "title":"EUNHA | Artist Spotlight #Shorts",
   "tags":["EUNHA","GFRIEND","VIVIZ","UNCORE","K-pop","artist spotlight","Shorts"]}
 prompt=f"""Source filename: {Path(filename).stem}
Return JSON only with artist, group, narration, title, tags.
Identify the artist only from clear filename clues, never face recognition. If unclear return artist "Unknown Artist".
Narration 55-70 natural English words for a 30-36 second artist introduction. Only stable widely established facts; do not invent dates, awards or positions. If unsure, keep intro general and describe the artist's performance/appeal without unsupported claims.
Title max 85 chars. tags array 4-8 strings."""
 # Gemini 2.5 access is restricted for some projects; use current models with automatic fallback.
 models=("gemini-3.8-flash","gemini-3.5-flash-lite")
 errors=[]
 for model in models:
  url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
  try:
   r=requests.post(url,params={"key":need("GEMINI_API_KEY")},
    json={"contents":[{"parts":[{"text":prompt}]}],
          "generationConfig":{"responseMimeType":"application/json","temperature":0.2}},
    timeout=60)
   if r.status_code==404:
    errors.append(f"{model}: HTTP 404 model unavailable")
    print(f"Gemini model {model} unavailable (404); switching to fallback.")
    continue
   if r.status_code in (429,500,502,503,504):
    errors.append(f"{model}: HTTP {r.status_code}")
    print(f"Gemini model {model} temporarily unavailable ({r.status_code}); switching to fallback.")
    continue
   r.raise_for_status()
   raw=r.json()["candidates"][0]["content"]["parts"][0]["text"]
   raw=re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$","",raw,flags=re.I)
   p=json.loads(raw)
   if p.get("artist")=="Unknown Artist" or len(p.get("narration","").split())<45:
    raise RuntimeError("Could not confidently identify artist from Drive filename: "+filename)
   print(f"Artist profile generated with {model}.")
   return p
  except requests.RequestException as e:
   errors.append(f"{model}: {type(e).__name__}: {e}")
   print(f"Gemini request failed for {model}: {type(e).__name__}; trying fallback.")
  except (KeyError,IndexError,ValueError) as e:
   errors.append(f"{model}: invalid response ({type(e).__name__})")
   print(f"Gemini response from {model} was invalid; trying fallback.")
 # Offline fallback: known filename aliases can still produce an intro when Gemini is rate-limited.
 aliases = {
  "eunha":("EUNHA","VIVIZ"), "은하":("EUNHA","VIVIZ"),
  "sinb":("SINB","VIVIZ"), "신비":("SINB","VIVIZ"),
  "umji":("UMJI","VIVIZ"), "엄지":("UMJI","VIVIZ"),
  "karina":("KARINA","aespa"), "카리나":("KARINA","aespa"),
  "ningning":("NINGNING","aespa"), "닝닝":("NINGNING","aespa"),
  "winter":("WINTER","aespa"), "윈터":("WINTER","aespa"),
  "giselle":("GISELLE","aespa"), "지젤":("GISELLE","aespa"),
  "jennie":("JENNIE","BLACKPINK"), "제니":("JENNIE","BLACKPINK"),
  "lisa":("LISA","BLACKPINK"), "리사":("LISA","BLACKPINK"),
  "jisoo":("JISOO","BLACKPINK"), "지수":("JISOO","BLACKPINK"),
  "rose":("ROSÉ","BLACKPINK"), "로제":("ROSÉ","BLACKPINK"),
  "sakura":("SAKURA","LE SSERAFIM"), "사쿠라":("SAKURA","LE SSERAFIM"),
  "chaewon":("CHAEWON","LE SSERAFIM"), "채원":("CHAEWON","LE SSERAFIM"),
  "yunjin":("YUNJIN","LE SSERAFIM"), "윤진":("YUNJIN","LE SSERAFIM"),
  "kazuha":("KAZUHA","LE SSERAFIM"), "카즈하":("KAZUHA","LE SSERAFIM"),
  "eunchae":("EUNCHAE","LE SSERAFIM"), "은채":("EUNCHAE","LE SSERAFIM"),
  "wonyoung":("WONYOUNG","IVE"), "원영":("WONYOUNG","IVE"),
  "yujin":("YUJIN","IVE"), "유진":("YUJIN","IVE"),
  "rei":("REI","IVE"), "레이":("REI","IVE"),
  "liz":("LIZ","IVE"), "리즈":("LIZ","IVE"),
  "leeseo":("LEESEO","IVE"), "이서":("LEESEO","IVE"),
  "gaeul":("GAEUL","IVE"), "가을":("GAEUL","IVE"),
  "tzuyu":("TZUYU","TWICE"), "쯔위":("TZUYU","TWICE"),
  "nayeon":("NAYEON","TWICE"), "나연":("NAYEON","TWICE"),
  "sana":("SANA","TWICE"), "사나":("SANA","TWICE"),
  "momo":("MOMO","TWICE"), "모모":("MOMO","TWICE"),
  "yuqi":("YUQI","(G)I-DLE"), "우기":("YUQI","(G)I-DLE"),
  "haerin":("HAERIN","NewJeans"), "해린":("HAERIN","NewJeans"),
  "natty":("NATTY","KISS OF LIFE"), "나띠":("NATTY","KISS OF LIFE"),
  "nancy":("NANCY","MOMOLAND"), "낸시":("NANCY","MOMOLAND"),
  "kwon eunbi":("KWON EUNBI","Solo"), "권은비":("KWON EUNBI","Solo")
 }
 stem=Path(filename).stem.casefold()
 matches=[v for k,v in aliases.items() if k in stem]
 # Prefer the longest alias to avoid matching a short token inside another name.
 if matches:
  artist,group=sorted(matches,key=lambda v:len(v[0]),reverse=True)[0]
  narration=(f"Meet {artist}, a performer from {group}. In this spotlight, the focus is on the energy, "
   f"stage presence, and expressive details that make this performance memorable. Watch the way "
   f"{artist} connects movement with the rhythm and brings confidence to every moment. "
   f"This introduction celebrates the performance itself and offers a quick look at the artist "
   f"for fans discovering or revisiting {artist}.")
  print(f"Gemini unavailable; generated a safe local artist profile for {artist} ({group}).")
  return {"artist":artist,"group":group,"narration":narration,
   "title":f"{artist} | {group} Artist Spotlight #Shorts",
   "tags":[artist,group,"K-pop","artist spotlight","performance","fancam"]}
 raise RuntimeError("All Gemini profile models failed and no safe filename alias matched: "+"; ".join(errors))

def render(src,p,hist):
 length=36.; intro=1.5; footage=length-intro; source_len=dur(src)
 if source_len<footage+1:raise RuntimeError(f"Source too short ({source_len:.1f}s); need at least 36.5s")
 audio=W/"voice.mp3";vtt=W/"voice.srt"
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
   clean_caption=re.sub(r"[\\U0001F000-\\U0001FAFF\\U00002600-\\U000027BF\\U0000FE0F\\U0000200D]","",sentence).strip()\n   wrapped=textwrap.wrap(clean_caption,width=30,break_long_words=False,break_on_hyphens=False)
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
 "[2:a]adelay=1500|1500,apad,atrim=duration=36[a]")
 run(["ffmpeg","-y","-loop","1","-t","1.5","-i",str(still),"-i",str(src),"-i",str(audio),"-filter_complex_threads","1","-filter_complex",fc,"-map","[v]","-map","[a]","-t","36","-r","30","-c:v","libx264","-preset","medium","-crf","20","-c:a","aac","-b:a","192k","-movflags","+faststart",str(out)])
 check=json.loads(subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height","-of","json",str(out)],check=True,capture_output=True,text=True).stdout)["streams"][0]
 if (int(check["width"]),int(check["height"]))!=(1080,1800) or not 30<=dur(out)<=40:raise RuntimeError("Output validation failed")
 return out
def publish(yt,out,p,source):
 brand=os.getenv("FANCAMVERSE_CHANNEL_BRAND","CurioLoop").strip()
 if brand.casefold() not in {"curioloop","dailyneedle"}:raise RuntimeError("Artist intro upload blocked: unexpected channel brand "+brand)
 print("Using existing CurioLoop/DailyNeedle YouTube upload credentials.")
 approval_file=R/"INTRO_PUBLICATION_APPROVED.txt"
 approved=approval_file.exists() and approval_file.read_text(encoding="utf-8").strip().lower() in {"1","true","yes","approved"}
 privacy="public" if approved else "private"
 body={"snippet":{"title":p["title"][:100],"description":f"Meet {p['artist']} from {p['group']}.\nArtist introduction and performance highlight.\nSource: {source}\n#Kpop #ArtistSpotlight #Shorts","tags":p.get("tags",[])+["K-pop","artist spotlight","Shorts"],"categoryId":"10"},"status":{"privacyStatus":privacy,"selfDeclaredMadeForKids":False}}
 v=yt.videos().insert(part="snippet,status",body=body,media_body=MediaFileUpload(str(out),mimetype="video/mp4",resumable=True)).execute()
 print(f"{privacy.upper()} UPLOAD https://youtube.com/shorts/"+v["id"]);return v["id"],privacy
def main():
 drive,yt=services();folder=need("FANCAMVERSE_DRIVE_FOLDER_ID")
 h=json.loads(H.read_text(encoding="utf-8")) if H.exists() else {"processed":[]}
 done={x.get("drive_file_id") for x in h["processed"]}
 target=os.getenv("TARGET_ARTIST","").strip().casefold()
 candidates=[x for x in scan(drive,folder) if x["id"] not in done]
 if target:
  candidates=[x for x in candidates if target in x["name"].casefold() or (target=="eunha" and "은하" in x["name"])]
  if not candidates:raise RuntimeError(f"No new unprocessed Drive video found for requested artist: {target}")
 if not candidates:raise RuntimeError("No new unprocessed Drive video")
 item=candidates[0];print("Selected Drive source:",item["name"])
 src=download(drive,item);p=profile(item["name"]);out=render(src,p,h)
 vid,privacy=publish(yt,out,p,item["name"])
 pending=h.pop("pending_opening_frame",None)
 if pending:
  h.setdefault("opening_frames",{}).setdefault(pending["artist"],[]).append(pending["timestamp"])
 h["processed"].append({"drive_file_id":item["id"],"source_name":item["name"],"artist":p["artist"],"youtube_video_id":vid,"privacy":privacy,"published_at":datetime.now(timezone.utc).isoformat()})
 H.parent.mkdir(parents=True,exist_ok=True);H.write_text(json.dumps(h,ensure_ascii=False,indent=2),encoding="utf-8")
if __name__=="__main__":main()
