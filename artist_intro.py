import asyncio,json,os,re,subprocess
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
def run(a): subprocess.run(a,check=True)
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
 prompt=f"""Source filename: {Path(filename).stem}
Return JSON only with artist, group, narration, title, tags.
Identify the artist only from clear filename clues, never face recognition. If unclear return artist "Unknown Artist".
Narration 55-70 natural English words for a 30-36 second artist introduction. Only stable widely established facts; do not invent dates, awards or positions. If unsure, keep intro general and describe the artist's performance/appeal without unsupported claims.
Title max 85 chars. tags array 4-8 strings."""
 r=requests.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",params={"key":need("GEMINI_API_KEY")},json={"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"responseMimeType":"application/json","temperature":0.2}},timeout=60);r.raise_for_status()
 p=json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
 if p.get("artist")=="Unknown Artist" or len(p.get("narration","").split())<45:raise RuntimeError("Could not confidently identify artist from Drive filename: "+filename)
 return p
def render(src,p):
 length=36.; intro=1.5; footage=length-intro; source_len=dur(src)
 if source_len<footage+1:raise RuntimeError(f"Source too short ({source_len:.1f}s); need at least 36.5s")
 audio=W/"voice.mp3";vtt=W/"voice.vtt"
 asyncio.run(edge_tts.Communicate(p["narration"],"en-US-JennyNeural",rate="+2%").save(str(audio),str(vtt)))
 still=W/"opening.jpg";t=min(5.,max(.5,source_len*.12))
 run(["ffmpeg","-y","-ss",f"{t:.2f}","-i",str(src),"-frames:v","1","-q:v","2",str(still)])
 start=max(0,(source_len-footage)/2);name=p["artist"].replace("'","").replace(":","");group=p["group"].replace("'","").replace(":","")
 out=W/(re.sub(r"[^A-Za-z0-9_-]+","_",name).lower()+"_artist_intro.mp4")
 font="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf";bold="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
 sub=str(vtt).replace("'","\\'")
 fc=("[0:v]scale=1080:1800:force_original_aspect_ratio=increase,crop=1080:1800,setsar=1,trim=duration=1.5,setpts=PTS-STARTPTS,"
 f"drawtext=fontfile={bold}:text='{name}':fontcolor=white:fontsize=76:borderw=3:bordercolor=black@0.8:x=(w-text_w)/2:y=h-390,"
 f"drawtext=fontfile={font}:text='{group}':fontcolor=white:fontsize=38:borderw=2:bordercolor=black@0.8:x=(w-text_w)/2:y=h-300[v0];"
 f"[1:v]trim=start={start:.3f}:duration={footage:.3f},setpts=PTS-STARTPTS,scale=1080:1800:force_original_aspect_ratio=increase,crop=1080:1800,setsar=1,"
 f"subtitles='{sub}':force_style='FontName=DejaVu Sans,FontSize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BackColour=&H50000000,BorderStyle=3,Outline=1,Alignment=2,MarginL=90,MarginR=90,MarginV=250'[v1];"
 "[v0][v1]concat=n=2:v=1:a=0,format=yuv420p[v];[2:a]adelay=1500|1500,apad,atrim=duration=36[a]")
 run(["ffmpeg","-y","-loop","1","-t","1.5","-i",str(still),"-i",str(src),"-i",str(audio),"-filter_complex",fc,"-map","[v]","-map","[a]","-t","36","-r","30","-c:v","libx264","-preset","medium","-crf","20","-c:a","aac","-b:a","192k","-movflags","+faststart",str(out)])
 check=json.loads(subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height","-of","json",str(out)],check=True,capture_output=True,text=True).stdout)["streams"][0]
 if (int(check["width"]),int(check["height"]))!=(1080,1800) or not 30<=dur(out)<=40:raise RuntimeError("Output validation failed")
 return out
def publish(yt,out,p,source):
 ch=yt.channels().list(part="id,snippet",mine=True).execute().get("items",[])
 if len(ch)!=1:raise RuntimeError("Cannot verify YouTube channel")
 c=ch[0];title=c.get("snippet",{}).get("title","");expected=os.getenv("YOUTUBE_CHANNEL_ID","").strip()
 if expected and c["id"]!=expected:raise RuntimeError("YouTube channel ID mismatch")
 if not expected and not any(x in title.casefold() for x in ("curioloop","dailyneedle")):raise RuntimeError("Unexpected YouTube channel "+title)
 body={"snippet":{"title":p["title"][:100],"description":f"Meet {p['artist']} from {p['group']}.\nArtist introduction and performance highlight.\nSource: {source}\n#Kpop #ArtistSpotlight #Shorts","tags":p.get("tags",[])+["K-pop","artist spotlight","Shorts"],"categoryId":"10"},"status":{"privacyStatus":"public","selfDeclaredMadeForKids":False}}
 v=yt.videos().insert(part="snippet,status",body=body,media_body=MediaFileUpload(str(out),mimetype="video/mp4",resumable=True)).execute()
 print("PUBLIC UPLOAD https://youtube.com/shorts/"+v["id"]);return v["id"]
def main():
 drive,yt=services();folder=need("FANCAMVERSE_DRIVE_FOLDER_ID")
 h=json.loads(H.read_text(encoding="utf-8")) if H.exists() else {"processed":[]}
 done={x.get("drive_file_id") for x in h["processed"]}
 candidates=[x for x in scan(drive,folder) if x["id"] not in done]
 if not candidates:raise RuntimeError("No new unprocessed Drive video")
 item=candidates[0];print("Selected Drive source:",item["name"])
 src=download(drive,item);p=profile(item["name"]);out=render(src,p)
 if os.getenv("INTRO_OUTPUT_ONLY","false").lower() in {"1","true","yes"}:print("PREVIEW ONLY",out);return
 vid=publish(yt,out,p,item["name"])
 h["processed"].append({"drive_file_id":item["id"],"source_name":item["name"],"artist":p["artist"],"youtube_video_id":vid,"published_at":datetime.now(timezone.utc).isoformat()})
 H.parent.mkdir(parents=True,exist_ok=True);H.write_text(json.dumps(h,ensure_ascii=False,indent=2),encoding="utf-8")
if __name__=="__main__":main()
