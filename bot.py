import os, re, asyncio, logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs, urlparse
import aiohttp, discord, aiosqlite
from discord import app_commands, ui
from discord.ext import commands
from dotenv import load_dotenv
from novus_logging import (LoggedCommandTree, LoggedModal, LoggedView, configure_logging,
                           report_interaction_error, start_logged_task)
from proof_archive import (
 ProofUnavailable, archive_discord_proof, archive_path,
 attachment_candidates, discord_attachment_key, is_private_proof,
)

load_dotenv()
TOKEN=os.getenv("DISCORD_TOKEN","")
GUILD_ID=int(os.getenv("GUILD_ID","0"))
NOVUS_GUILD_ID=1532817291048124466
REVIEW_ID=int(os.getenv("REVIEW_CHANNEL_ID","0"))
GENERAL_CHAT_ID=int(os.getenv("PB_GENERAL_CHAT_CHANNEL_ID","1532817292205883514"))
DBPATH=os.getenv("DATABASE_PATH","novus_pb.sqlite3")
STAFF_ROLES={int(s) for s in os.getenv("STAFF_ROLE_IDS","").split(",") if s.strip().isdigit()}
GOLD=0xEAC17B
BLUE=0x57B7FF
RED=0xD94A4A
# Use the uploaded animation when present in the deployed repository.
# PB_BANNER_URL can override this with another public GIF URL.
PB_BANNER_GIF=Path(__file__).parent / "assets" / "novus_pb_banner.gif"
PB_BANNER_STATIC="https://raw.githubusercontent.com/jerwilson07/Novus-PB-Board/e56d115997dde7d1696ef4c535ce366da87ba84b/assets/novus_pb_banner.jpg"
PB_BANNER_ANIMATED="https://raw.githubusercontent.com/jerwilson07/Novus-PB-Board/main/assets/novus_pb_banner.gif"
PB_BANNER_URL=os.getenv("PB_BANNER_URL") or (PB_BANNER_ANIMATED if PB_BANNER_GIF.is_file() else PB_BANNER_STATIC)
REGULAR_RAID_SIZES=("Solo","Duo","Trio","4-Man","5-Man")
SHARED_SIZE_RAIDS=("The Fractured Archive","Theatre of Blood","Chambers of Xeric")
RAIDS={
 "The Fractured Archive":[*REGULAR_RAID_SIZES],
 "Theatre of Blood":[*REGULAR_RAID_SIZES[1:],"Hard Mode · Trio","Hard Mode · 4-Man","Hard Mode · 5-Man"],
 "Chambers of Xeric":[*(size for size in REGULAR_RAID_SIZES if size!="4-Man"),"Challenge Mode · Solo","Challenge Mode · Duo","Challenge Mode · Trio","Challenge Mode · 5-Man"],
 "Tombs of Amascut":["Expert · Solo","Expert · 8-Man"]}
RAID_LABELS={
 "The Fractured Archive":"TFA",
 "Theatre of Blood":"ToB",
 "Chambers of Xeric":"CoX",
 "Tombs of Amascut":"ToA",
}
BOSSES={
 "Inferno":["Standard"],
 "Fortis Colosseum":["Standard"],
 "Fight Caves":["Standard"],
 "Alchemical Hydra":["Standard"],
 "Araxxor":["Standard"],
 "Grotesque Guardians":["Standard"],
 "Gauntlet":["Standard"],
 "Corrupted Gauntlet":["Standard"],
 "Doom of Mokhaiotl":["Standard"],
 "Demonic Brutus":["Standard"],
 "Mad Angel":["Standard"],
 "Maggot King":["Standard"],
 "Phantom Muspah":["Standard"],
 "Phosani's Nightmare":["Standard"],
 "Royal Titans":["Standard"],
 "Vorkath":["Standard"],
 "Yama":["Standard"],
 "Zulrah":["Standard"],
 "Duke Sucellus":["Normal","Awakened"],
 "The Leviathan":["Normal","Awakened"],
 "Vardorvis":["Normal","Awakened"],
 "The Whisperer":["Normal","Awakened"]}
CATS={"Raids":RAIDS,"Bosses":BOSSES}
NON_SOLO_BOSSES={"Yama","Royal Titans"}
ROYAL_TITANS_35_PARTNER="What A Piety"
royal_titans_35_partner_id=None

def solo_boss_variant(activity,variant):
 return activity not in NON_SOLO_BOSSES
PB_EARLIEST_DATE=datetime(2026,11,7).date()

def pb_date_eligible(value):
 return value>=PB_EARLIEST_DATE

def stored_member_ids(r):
 try: raw=r["member_ids"]
 except (KeyError,IndexError): raw=None
 return list(dict.fromkeys(x.strip() for x in (raw or "").split(",") if x.strip().isdigit()))

def is_royal_titans_35(r):
 return (
  r["guild"]==NOVUS_GUILD_ID and r["id"]==35
  and r["category"]=="Bosses" and r["activity"]=="Royal Titans"
  and r["variant"]=="Standard" and r["ms"]==46800
  and r["pb_date"]=="2026-11-11"
 )

def public_members(r):
 """Render every saved teammate without assuming that the submitter played."""
 ids=stored_member_ids(r)
 names=[name.strip() for name in (r["members"] or "").split(",") if name.strip()]
 if is_royal_titans_35(r) and len(ids)==1 and ids[0]!=str(r["submitter"]):
  # Staff confirmed this older entry's missing teammate. Keep the correction
  # display-only and never infer other teammates from who submitted a PB.
  partner=(f"<@{royal_titans_35_partner_id}>" if royal_titans_35_partner_id
           else clean(ROYAL_TITANS_35_PARTNER))
  return f"<@{ids[0]}> · {partner}"
 expected=team_size(r["category"],r["variant"],r["activity"])
 if ids and (
  len(ids)>=len(names) or (expected is not None and len(ids)==expected)
  or (r["activity"] in NON_SOLO_BOSSES and len(ids)==2)
 ):
  return " · ".join(f"<@{x}>" for x in ids)
 # Older data can contain the full roster as text but only some Discord IDs.
 # Show the full saved names rather than guessing which IDs go with them.
 return " · ".join(clean(name) for name in names)

def unique_solo_results(results,limit=5):
 unique=[]
 seen=set()
 for r in results:
  raw_ids=r["member_ids"] or ""
  ids=[value.strip() for value in raw_ids.split(",") if value.strip().isdigit()]
  member_key=ids[0] if ids else clean(r["members"]).casefold()
  if member_key in seen:
   continue
  seen.add(member_key)
  unique.append(r)
  if len(unique)>=limit:
   break
 return unique

def unique_team_results(results,limit=5):
 unique=[]
 seen=set()
 for r in results:
  raw_ids=r["member_ids"] or ""
  ids=sorted(
   value.strip()
   for value in raw_ids.split(",")
   if value.strip().isdigit()
  )
  if ids:
   team_key=tuple(ids)
  else:
   team_key=tuple(
    sorted(
     name.strip().casefold()
     for name in str(r["members"]).split(",")
     if name.strip()
    )
   )
  if team_key in seen:
   continue
  seen.add(team_key)
  unique.append(r)
  if len(unique)>=limit:
   break
 return unique

PUBLIC_PROOF_URLS={}
PUBLIC_PROOF_LOCK=asyncio.Lock()
PROOF_THREAD_LOCK=asyncio.Lock()
PROOF_THREAD_NAME="PB Proof Archive"
PROOF_THREAD_RETRY_AT={}
PROOF_LINK_REFRESH_SECONDS=3*60*60

def proof_text(r,label="Proof",*,omit=()):
 if r["id"] in omit:
  return ""
 proof=r["proof"]
 if is_private_proof(proof):
  proof=PUBLIC_PROOF_URLS.get(proof,"")
 return f"[{label}]({proof})" if proof and valid_url(proof) else ""

def players_with_proof(r,*,omit=()):
 evidence=proof_text(r,omit=omit)
 return public_members(r)+(f" · {evidence}" if evidence else "")

def cache_public_attachment(ref,attachment):
 if discord_attachment_key(attachment.url):
  PUBLIC_PROOF_URLS[ref]=attachment.url
  return True
 return False

def proof_link_needs_refresh(url,at):
 if not url:
  return True
 try:
  return int(parse_qs(urlparse(url).query)["ex"][0],16)<=at+2*PROOF_LINK_REFRESH_SECONDS
 except (KeyError,ValueError,IndexError):
  return True

def short_record(r,rank,include_variant=False,*,omit=()):
 icon=card_rank_icon(rank+1,r["guild"])
 lines=[f"{icon} **{fmt(r['ms'])}**"]
 if include_variant:
  lines.append(f"**{clean(r['variant'])}**")
 players=players_with_proof(r,omit=omit)
 if r["id"] in omit:
  players+=f" · PB #{r['id']}"
 lines.append(players)
 if rank==0:
  lines.append(f"-# ✔️ {pb_date_label(r['pb_date'])}")
 return "\n".join(lines)

def discord_updated(timestamp):
 if not timestamp:return None
 try:
  parsed=datetime.fromisoformat(timestamp.replace("Z","+00:00"))
  if parsed.tzinfo is None:
   parsed=parsed.replace(tzinfo=timezone.utc)
  return f"<t:{int(parsed.timestamp())}:R>"
 except (TypeError,ValueError,OverflowError):
  return None


def pb_date_label(value):
 """Show the exact stored PB date without timezone conversion."""
 if not value:return "Date unavailable"
 try:
  parsed=datetime.strptime(str(value),"%Y-%m-%d").date()
  return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
 except ValueError:
  return "Date unavailable"

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
# Server emoji names correspond to the PNG files in the Novus Discord emoji pack.
# No hardcoded emoji IDs: existing server emojis are resolved at render time.
ACTIVITY_EMOJIS={
 "The Fractured Archive":"tfa",
 "Theatre of Blood":"verzik",
 "Tombs of Amascut":"warden",
 "Chambers of Xeric":"COX",
 "Inferno":"zuk",
 "Fortis Colosseum":"sol_heredit",
 "Fight Caves":"jad",
 "Alchemical Hydra":"hydra",
 "Araxxor":"araxxor",
 "Grotesque Guardians":"dawn",
 "Gauntlet":"crystalline_hunllef",
 "Corrupted Gauntlet":"corrupted_hunllef",
 "Doom of Mokhaiotl":"doom",
 "Demonic Brutus":"demonic_brutus",
 "Mad Angel":"mad_angel",
 "Maggot King":"maggot_king",
 "Phantom Muspah":"muspah",
 "Phosani's Nightmare":"nightmare",
 "Royal Titans":"eldric",
 "Vorkath":"vorkath",
 "Yama":"yama",
 "Zulrah":"zulrah",
 "Duke Sucellus":"duke_sucellus",
 "The Leviathan":"leviathan",
 "Vardorvis":"vardorvis",
 "The Whisperer":"whisperer",
}

ACTIVITY_EMOJI_CACHE={}
PB_PODIUM_FALLBACK={1:"#1",2:"#2",3:"#3"}
NOVUS_PB_TROPHY_IDS={
 1:1572157972702953563,
 2:1572155458423361637,
 3:1572155471606054982,
}
PB_FEATURED_HEADING="# "
PB_TOP10_TITLE="## ✦ NOVUS TOP 10"
PB_TOP10_PODIUM_HEADING="### "
PB_PLACEMENT_POINTS=(5,3,1)


def podium_icon(rank,guild_id):
 if rank not in PB_PODIUM_FALLBACK:
  return f"**{rank}.**"
 name=f"novuspb{rank}"
 if guild_id==NOVUS_GUILD_ID:
  return f"<:{name}:{NOVUS_PB_TROPHY_IDS[rank]}>"
 return ACTIVITY_EMOJI_CACHE.get(guild_id,{}).get(name) or PB_PODIUM_FALLBACK[rank]

def card_rank_icon(rank,guild_id):
 return podium_icon(rank,guild_id) if rank in PB_PODIUM_FALLBACK else f"{rank}."


# Preserve a name-width inset in Discord subtext without indenting the legend.
PB_TROPHY_COUNT_INDENTS=(
 "\u200b\u2003\u2003\u2002",  # Podium headings use the wider trophy emoji.
 "\u200b\u2003\u2002\u2005",  # Single-digit numbered rows.
 "\u200b\u2003\u2003\u2005",  # Two-digit numbered rows.
)


def top10_trophy_subtext(guild_id,values,*,scoring=False,rank=4):
 prefix="Scoring · " if scoring else ""
 parts=(
  f"{podium_icon(rank,guild_id)} "
  + (f"+{value} {'pt' if value==1 else 'pts'}" if scoring else f"× {value}")
  for rank,value in enumerate(values,1)
 )
 indent="" if scoring else PB_TROPHY_COUNT_INDENTS[0 if rank<=3 else 2 if rank>=10 else 1]
 return "-# "+indent+"*"+prefix+" · ".join(parts)+"*"


async def fetch_activity_emojis(guild):
 try:
  server_emojis=await guild.fetch_emojis()
 except discord.HTTPException:
  logging.warning("Could not fetch server emojis for %s; using cache",guild.id)
  cached=dict(ACTIVITY_EMOJI_CACHE.get(guild.id,{}))
  cached.update({emoji.name.casefold():str(emoji) for emoji in guild.emojis})
  ACTIVITY_EMOJI_CACHE[guild.id]=cached
 else:
  ACTIVITY_EMOJI_CACHE[guild.id]={
   emoji.name.casefold():str(emoji) for emoji in server_emojis
  }
 return ACTIVITY_EMOJI_CACHE[guild.id]

def activity_icon(activity):
 name=ACTIVITY_EMOJIS.get(activity)
 if not name:return ""
 guild=bot.get_guild(GUILD_ID) if GUILD_ID else (bot.guilds[0] if bot.guilds else None)
 if not guild:return ""
 resolved=ACTIVITY_EMOJI_CACHE.get(guild.id,{}).get(name.casefold())
 if not resolved:
  emoji=next((e for e in guild.emojis if e.name.casefold()==name.casefold()),None)
  resolved=str(emoji) if emoji else ""
 return f"{resolved} " if resolved else ""

def clean(v): return discord.utils.escape_mentions(discord.utils.escape_markdown(str(v)))
def valid_url(v):
 try:
  x=urlparse(v); return x.scheme in ("http","https") and bool(x.netloc)
 except ValueError: return False
def parse_ms(s):
 raw=s.strip()
 # Under one minute, seconds may be supplied without a 00: prefix.
 if re.fullmatch(r"\d{1,2}(?:\.\d{1,3})?",raw):
  parts=["0",raw]
 else:
  parts=raw.split(":")
 if len(parts) not in (2,3) or any(not x.isdecimal() for x in parts[:-1]):
  raise ValueError("Use SS.ss, MM:SS.ss, or HH:MM:SS.ss.")
 if not re.fullmatch(r"\d{1,2}(?:\.\d{1,3})?",parts[-1]):
  raise ValueError("Invalid seconds.")
 secs,*fraction=parts[-1].split(".")
 seconds=int(secs)
 ms=int(fraction[0].ljust(3,"0")) if fraction else 0
 minutes=int(parts[-2])
 hours=int(parts[0]) if len(parts)==3 else 0
 if seconds>59 or (len(parts)==3 and minutes>59):
  raise ValueError("Invalid seconds/minutes.")
 total=((hours*60+minutes)*60+seconds)*1000+ms
 if not 0<total<=86400000:
  raise ValueError("Time must be positive and within 24h.")
 return total
def fmt(ms):
 s,m=divmod(ms,1000); minute,sec=divmod(s,60); hour,minute=divmod(minute,60)
 cs=m//10
 return f"{hour}:{minute:02}:{sec:02}.{cs:02}" if hour else f"{minute}:{sec:02}.{cs:02}"

def proof_looks_like_media(url):
 try:
  path=urlparse(url).path.lower()
  return path.endswith((".png",".jpg",".jpeg",".gif",".webp",".mp4",".mov",".webm"))
 except ValueError:
  return False
def canonical_raid_variant(category,activity,variant):
 if category=="Raids" and activity in SHARED_SIZE_RAIDS and variant.startswith("Normal · "):
  size=variant.removeprefix("Normal · ")
  if size in REGULAR_RAID_SIZES and size in RAIDS[activity]:
   return size
 return variant

def team_size(cat,variant,activity=None):
 if cat=="Bosses":
  return None if activity in NON_SOLO_BOSSES else 1
 if "Solo" in variant:return 1
 if "Duo" in variant:return 2
 if "Trio" in variant:return 3
 m=re.search(r"(\d+)-Man",variant);return int(m.group(1)) if m else None

def review_members(r):
 return public_members(r)

class Store:
 def __init__(self):self.conn=None;self.lock=asyncio.Lock()
 async def open(self):
  os.makedirs(os.path.dirname(os.path.abspath(DBPATH)),exist_ok=True)
  self.conn=await aiosqlite.connect(DBPATH)
  self.conn.row_factory=aiosqlite.Row
  await self.conn.execute("PRAGMA journal_mode=WAL")
  await self.conn.executescript("""CREATE TABLE IF NOT EXISTS submissions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER NOT NULL,submitter INTEGER NOT NULL,
  category TEXT NOT NULL,activity TEXT NOT NULL,variant TEXT NOT NULL,
  members TEXT NOT NULL,member_ids TEXT,ms INTEGER NOT NULL,pb_date TEXT,proof TEXT,status TEXT NOT NULL DEFAULT 'pending',
  reviewer INTEGER,review_channel INTEGER,review_message INTEGER,created TEXT,updated TEXT);
  CREATE INDEX IF NOT EXISTS pb_rank ON submissions(guild,category,activity,variant,status,ms);
  CREATE TABLE IF NOT EXISTS panels(guild INTEGER PRIMARY KEY,channel INTEGER,message INTEGER);
  CREATE TABLE IF NOT EXISTS panel_banners(guild INTEGER PRIMARY KEY,channel INTEGER,message INTEGER);
  CREATE TABLE IF NOT EXISTS panel_bosses(guild INTEGER PRIMARY KEY,channel INTEGER,message INTEGER);
  CREATE TABLE IF NOT EXISTS boss_board_messages(guild INTEGER NOT NULL,activity TEXT NOT NULL,channel INTEGER NOT NULL,message INTEGER NOT NULL,PRIMARY KEY(guild,activity));
  CREATE TABLE IF NOT EXISTS raid_board_pages(guild INTEGER NOT NULL,activity TEXT NOT NULL,page INTEGER NOT NULL,channel INTEGER NOT NULL,message INTEGER NOT NULL,PRIMARY KEY(guild,activity,page));
  CREATE TABLE IF NOT EXISTS panel_snapshot(guild INTEGER PRIMARY KEY,channel INTEGER,message INTEGER);
  CREATE TABLE IF NOT EXISTS panel_footer(guild INTEGER PRIMARY KEY,channel INTEGER,message INTEGER);
  CREATE TABLE IF NOT EXISTS proof_threads(guild INTEGER PRIMARY KEY,parent INTEGER NOT NULL,thread INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS proof_button_cards(guild INTEGER NOT NULL,message INTEGER NOT NULL,PRIMARY KEY(guild,message));
  CREATE TABLE IF NOT EXISTS proof_thread_messages(
    guild INTEGER NOT NULL,proof TEXT NOT NULL,thread INTEGER NOT NULL,message INTEGER NOT NULL,
    PRIMARY KEY(guild,proof)
  );""")
  async with self.conn.execute("PRAGMA table_info(submissions)") as c:
   columns={row[1] async for row in c}
  if "member_ids" not in columns:
   await self.conn.execute("ALTER TABLE submissions ADD COLUMN member_ids TEXT")
  if "pb_date" not in columns:
   await self.conn.execute("ALTER TABLE submissions ADD COLUMN pb_date TEXT")
  # Relabel existing PBs in place so all readers see one regular bracket per size.
  migration=await self.conn.executemany(
   """UPDATE submissions SET variant=? WHERE category='Raids'
      AND activity=? AND variant=?""",
   [(size,activity,f"Normal · {size}") for activity in SHARED_SIZE_RAIDS
    for size in REGULAR_RAID_SIZES if size in RAIDS[activity]]
  )
  await self.conn.commit()
  if migration.rowcount:
   logging.info("Relabelled %s existing regular raid PBs by team size",migration.rowcount)
 async def one(self,q,args=()):
  async with self.conn.execute(q,args) as c:return await c.fetchone()
 async def all(self,q,args=()):
  async with self.conn.execute(q,args) as c:return await c.fetchall()
 async def save_proof_thread(self,guild,parent,thread):
  async with self.lock:
   await self.conn.execute("""INSERT INTO proof_threads VALUES(?,?,?) ON CONFLICT(guild)
                    DO UPDATE SET parent=excluded.parent,thread=excluded.thread""",(guild,parent,thread))
   await self.conn.commit()
 async def save_proof_message(self,guild,proof,thread,message):
  async with self.lock:
   await self.conn.execute("""INSERT INTO proof_thread_messages VALUES(?,?,?,?) ON CONFLICT(guild,proof)
                    DO UPDATE SET thread=excluded.thread,message=excluded.message""",(guild,proof,thread,message))
   await self.conn.commit()
 async def unmark_proof_button_card(self,guild,message):
  async with self.lock:
   await self.conn.execute("DELETE FROM proof_button_cards WHERE guild=? AND message=?",(guild,message))
   await self.conn.commit()
 async def mark_proof_button_card(self,guild,message):
  async with self.lock:
   await self.conn.execute("INSERT OR IGNORE INTO proof_button_cards VALUES(?,?)",(guild,message))
   await self.conn.commit()
 async def add(self,g,u,cat,act,v,m,member_ids,ms,pb_date,p):
  v=canonical_raid_variant(cat,act,v)
  async with self.lock:
   c=await self.conn.execute("""INSERT INTO submissions(guild,submitter,category,activity,variant,members,member_ids,ms,pb_date,proof,created,updated)
   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(g,u,cat,act,v,m,member_ids,ms,pb_date,p,now(),now()))
   await self.conn.commit();return c.lastrowid
 async def change(self,sid,prev,target,reviewer=None,proof=None):
  async with self.lock:
   if proof is None:
    c=await self.conn.execute("UPDATE submissions SET status=?,reviewer=?,updated=? WHERE id=? AND status=?",
      (target,reviewer,now(),sid,prev))
   else:
    c=await self.conn.execute("UPDATE submissions SET status=?,proof=?,updated=? WHERE id=? AND status=?",
      (target,proof,now(),sid,prev))
   await self.conn.commit();return c.rowcount==1
 async def attach_review(self,sid,ch,msg):
  async with self.lock:
   await self.conn.execute("UPDATE submissions SET review_channel=?,review_message=? WHERE id=?",(ch,msg,sid))
   await self.conn.commit()
 async def replace_proof(self,sid,guild,old,new,*,status=None,reopen=False):
  async with self.lock:
   values="proof=?,status='pending',updated=?" if reopen else "proof=?"
   query=f"UPDATE submissions SET {values} WHERE id=? AND guild=? AND proof IS ?"
   args=(new,now(),sid,guild,old) if reopen else (new,sid,guild,old)
   if status is not None:
    query+=" AND status=?"
    args+=(status,)
   c=await self.conn.execute(query,args)
   await self.conn.commit()
   return c.rowcount==1
 async def panel(self,g):return await self.one("SELECT * FROM panels WHERE guild=?",(g,))
 async def set_panel(self,g,ch,msg):
  async with self.lock:
   await self.conn.execute("""INSERT INTO panels VALUES(?,?,?) ON CONFLICT(guild) DO UPDATE
   SET channel=excluded.channel,message=excluded.message""",(g,ch,msg))
   await self.conn.commit()
 async def banner(self,g):return await self.one("SELECT * FROM panel_banners WHERE guild=?",(g,))
 async def set_banner(self,g,ch,msg):
  async with self.lock:
   await self.conn.execute("""INSERT INTO panel_banners VALUES(?,?,?) ON CONFLICT(guild) DO UPDATE
   SET channel=excluded.channel,message=excluded.message""",(g,ch,msg))
   await self.conn.commit()
 async def boss_panel(self,g):return await self.one("SELECT * FROM panel_bosses WHERE guild=?",(g,))
 async def set_boss_panel(self,g,ch,msg):
  async with self.lock:
   await self.conn.execute("""INSERT INTO panel_bosses VALUES(?,?,?) ON CONFLICT(guild) DO UPDATE
   SET channel=excluded.channel,message=excluded.message""",(g,ch,msg))
   await self.conn.commit()
 async def boss_board(self,g,activity):
  return await self.one(
   "SELECT * FROM boss_board_messages WHERE guild=? AND activity=?",
   (g,activity)
  )
 async def boss_boards(self,g):
  return await self.all(
   "SELECT * FROM boss_board_messages WHERE guild=?",
   (g,)
  )
 async def set_boss_board(self,g,activity,ch,msg):
  async with self.lock:
   await self.conn.execute(
    """INSERT INTO boss_board_messages(guild,activity,channel,message)
    VALUES(?,?,?,?) ON CONFLICT(guild,activity) DO UPDATE SET
    channel=excluded.channel,message=excluded.message""",
    (g,activity,ch,msg)
   )
   await self.conn.commit()
 async def clear_boss_boards(self,g):
  async with self.lock:
   await self.conn.execute(
    "DELETE FROM boss_board_messages WHERE guild=?",
    (g,)
   )
   await self.conn.execute("DELETE FROM raid_board_pages WHERE guild=?",(g,))
   await self.conn.commit()
 async def raid_pages(self,g,activity=None):
  if activity is None:
   return await self.all("SELECT * FROM raid_board_pages WHERE guild=? ORDER BY activity,page",(g,))
  return await self.all("SELECT * FROM raid_board_pages WHERE guild=? AND activity=? ORDER BY page",(g,activity))
 async def set_raid_page(self,g,activity,index,ch,msg):
  async with self.lock:
   await self.conn.execute("""INSERT INTO raid_board_pages VALUES(?,?,?,?,?)
    ON CONFLICT(guild,activity,page) DO UPDATE SET channel=excluded.channel,message=excluded.message""",
    (g,activity,index,ch,msg))
   await self.conn.commit()
 async def remove_raid_page(self,g,activity,index):
  async with self.lock:
   await self.conn.execute("DELETE FROM raid_board_pages WHERE guild=? AND activity=? AND page=?",(g,activity,index))
   await self.conn.commit()
 async def snapshot_panel(self,g):return await self.one("SELECT * FROM panel_snapshot WHERE guild=?",(g,))
 async def set_snapshot_panel(self,g,ch,msg):
  async with self.lock:
   await self.conn.execute("""INSERT INTO panel_snapshot VALUES(?,?,?) ON CONFLICT(guild) DO UPDATE
   SET channel=excluded.channel,message=excluded.message""",(g,ch,msg))
   await self.conn.commit()
 async def footer(self,g):return await self.one("SELECT * FROM panel_footer WHERE guild=?",(g,))
 async def set_footer(self,g,ch,msg):
  async with self.lock:
   await self.conn.execute("""INSERT INTO panel_footer VALUES(?,?,?) ON CONFLICT(guild) DO UPDATE
   SET channel=excluded.channel,message=excluded.message""",(g,ch,msg))
   await self.conn.commit()
 async def ranking(self,g,cat,a,v):
  rows=await self.all("""SELECT * FROM submissions WHERE guild=? AND category=? AND activity=? AND variant=?
  AND status='approved' ORDER BY ms,id""",(g,cat,a,v))
  if cat=="Bosses" and solo_boss_variant(a,v):
   return unique_solo_results(rows,5)
  return unique_team_results(rows,5)
 async def best_approved(self,g,cat,activity,variant):
  return await self.one(
   """SELECT * FROM submissions
   WHERE guild=? AND category=? AND activity=? AND variant=?
   AND status='approved' ORDER BY ms,id LIMIT 1""",
   (g,cat,activity,variant)
  )
 async def recent_approved(self,g,limit=5):
  return await self.all("""SELECT * FROM submissions
  WHERE guild=? AND status='approved'
  ORDER BY updated DESC,id DESC LIMIT ?""",(g,limit))
 async def approved_for_points(self,g):
  return await self.all("""SELECT * FROM submissions
  WHERE guild=? AND status='approved'
  ORDER BY category,activity,variant,ms,id""",(g,))
 async def raid_top3(self,g,activity):
  rows=await self.all("""SELECT * FROM submissions
  WHERE guild=? AND category='Raids' AND activity=? AND status='approved'
  ORDER BY ms,id""",(g,activity))
  return unique_team_results(rows,3)
 async def clear_guild(self,g):
  async with self.lock:
   await self.conn.execute("DELETE FROM submissions WHERE guild=?",(g,))
   remaining=await self.one("SELECT COUNT(*) AS n FROM submissions")
   if remaining and remaining["n"]==0:
    await self.conn.execute("DELETE FROM sqlite_sequence WHERE name='submissions'")
   await self.conn.commit()
store=Store()
intents=discord.Intents.default()
class PB(commands.Bot):
 async def setup_hook(self):
  await store.open()
  self.add_dynamic_items(BoardProofSelect,BoardProofButton)
  self.add_view(Home())
  self.add_view(BossDirectory())
  for r in await store.all("SELECT * FROM submissions WHERE status IN ('pending','awaiting_proof') AND review_message IS NOT NULL"):
   self.add_view(await Review.make(r),message_id=r["review_message"])
  if GUILD_ID:
   guild=discord.Object(id=GUILD_ID)
   self.tree.copy_global_to(guild=guild)
   await self.tree.sync(guild=guild)
  else:await self.tree.sync()

 async def close(self):
  task=getattr(self,"_proof_links_task",None)
  if task:
   task.cancel()
   await asyncio.gather(task,return_exceptions=True)
  await super().close()
bot=PB(command_prefix=commands.when_mentioned,intents=intents,tree_cls=LoggedCommandTree)


class PBLayoutView(ui.LayoutView):
 async def on_error(self,interaction,error,item):
  report_interaction_error(interaction,error,f"{type(self).__name__}.{type(item).__name__}")

def pb_points_order(entry):
 return (-entry["points"],-entry["firsts"],-entry["seconds"],-entry["thirds"])


def rank_pb_points(entries,limit=10):
 # IDs only stabilize display order within a tie; they never affect rank.
 ordered=sorted(entries,key=lambda entry:(*pb_points_order(entry),entry["member_id"]))
 ranked=[]
 previous=None
 rank=0
 for index,entry in enumerate(ordered,1):
  key=pb_points_order(entry)
  if key!=previous:
   rank=index
   previous=key
  if rank>limit:
   break
  ranked.append({**entry,"rank":rank})
 return ranked


async def calculate_pb_points(g):
 rows=await store.approved_for_points(g)
 grouped={}
 scores={}

 for r in rows:
  key=(
   r["category"],
   r["activity"],
   r["variant"]
  )
  grouped.setdefault(key,[]).append(r)

 for key,results in grouped.items():
  category,activity,variant=key
  if category=="Bosses" and solo_boss_variant(activity,variant):
   results=unique_solo_results(results,5)
  else:
   results=unique_team_results(results,5)

  for rank,r in enumerate(results[:3]):
   raw_ids=r["member_ids"] or ""
   member_ids={
    int(value.strip())
    for value in raw_ids.split(",")
    if value.strip().isdigit()
   }

   for member_id in member_ids:
    entry=scores.setdefault(
     member_id,
     {
      "member_id":member_id,
      "points":0,
      "firsts":0,
      "seconds":0,
      "thirds":0,
     }
    )
    entry["points"]+=PB_PLACEMENT_POINTS[rank]
    if rank==0:
     entry["firsts"]+=1
    elif rank==1:
     entry["seconds"]+=1
    else:
     entry["thirds"]+=1

 return rank_pb_points(scores.values())


def member_ids_for_record(record):
 return {
  int(value.strip()) for value in (record["member_ids"] or "").split(",")
  if value.strip().isdigit()
 }

def build_member_profile(rows,member_id):
 """Compute member standings from exactly the public Top 5 ranking rules."""
 groups={}
 history=[]
 for r in rows:
  groups.setdefault((r["category"],r["activity"],r["variant"]),[]).append(r)
  if member_id in member_ids_for_record(r):
   history.append(r)
 placements=[]
 for (category,activity,variant),results in groups.items():
  results=sorted(results,key=lambda r:(r["ms"],r["id"]))
  if category=="Bosses" and solo_boss_variant(activity,variant):
   ranked=unique_solo_results(results,5)
  else:
   ranked=unique_team_results(results,5)
  for pos,r in enumerate(ranked,1):
   if member_id in member_ids_for_record(r):
    placements.append((pos,r))
 placements.sort(key=lambda p:(p[0],p[1]["category"],p[1]["activity"],p[1]["variant"]))
 history.sort(key=lambda r:(r["updated"] or "",r["id"]),reverse=True)
 return {
  "points":sum({1:5,2:3,3:1}.get(pos,0) for pos,_ in placements),
  "firsts":sum(pos==1 for pos,_ in placements),
  "seconds":sum(pos==2 for pos,_ in placements),
  "thirds":sum(pos==3 for pos,_ in placements),
  "approved_count":len(history),
  "placements":placements,
  "history":history
 }


class PersonalPBProfile(PBLayoutView):
 """Private Discord-only view of the member's standings and approved history."""
 def __init__(self,member_id,summary,*,guild_id=None):
  super().__init__(timeout=900)
  panel=ui.Container(accent_colour=GOLD)
  panel.add_item(ui.TextDisplay(f"## ✦ NOVUS · MY PBs\n<@{member_id}>"))
  panel.add_item(ui.Separator())
  if not summary["approved_count"]:
   panel.add_item(ui.TextDisplay(
    "-# No approved PBs yet. Submit your first record with /pbsubmit."
   ))
  else:
   panel.add_item(ui.TextDisplay(
    f"**{summary['points']} pts** · {podium_icon(1,guild_id)} {summary['firsts']} first · "
    f"{podium_icon(2,guild_id)} {summary['seconds']} second · "
     f"{podium_icon(3,guild_id)} {summary['thirds']} third\n"
    f"-# {summary['approved_count']} approved PB submissions"
   ))
   panel.add_item(ui.Separator())
   if summary["placements"]:
    lines=[]
    for rank,r in summary["placements"][:10]:
     icon=card_rank_icon(rank,r["guild"])
     lines.append(
      f"{icon} **{clean(r['activity'])}** · "
      f"{clean(r['variant'])} · **{fmt(r['ms'])}**"
     )
    panel.add_item(ui.TextDisplay(
     "### CURRENT TOP 5 PLACEMENTS\n"+"\n".join(lines) +
     ("\n-# Showing the first 10 placements." if len(summary["placements"])>10 else "")
    ))
   else:
    panel.add_item(ui.TextDisplay(
     "-# No current Top 5 placements yet."
    ))
  self.add_item(panel)


async def show_my_pbs(i):
 if not i.guild_id:
  return await i.response.send_message(
   "Use this inside the Novus server.",ephemeral=True
  )
 rows=await store.approved_for_points(i.guild_id)
 await i.response.send_message(
  view=PersonalPBProfile(i.user.id,build_member_profile(rows,i.user.id),guild_id=i.guild_id),
  ephemeral=True,allowed_mentions=discord.AllowedMentions.none()
 )

def pb_scroll_to_top_text(url):
 return f"## [↑ JUMP TO THE TOP ↑]({url})"


async def paced_startup_edit(message, **kwargs):
 # Channel-wide edit bursts receive 429s. Leave headroom for other bots.
 await message.edit(**kwargs)
 await asyncio.sleep(1.4)

async def resolve_royal_titans_35_partner(guild):
 """Verify the historical teammate's Discord mention when possible."""
 global royal_titans_35_partner_id
 if guild.id!=NOVUS_GUILD_ID:
  return
 record=await store.one("SELECT * FROM submissions WHERE guild=? AND id=35",(guild.id,))
 if not record or not is_royal_titans_35(record):
  return
 ids=stored_member_ids(record)
 if len(ids)!=1 or ids[0]==str(record["submitter"]):
  return
 try:
  member=guild.get_member(record["submitter"]) or await guild.fetch_member(record["submitter"])
 except discord.HTTPException:
  return  # The verified teammate's plain display name is still available.
 if any(
  value and " ".join(value.casefold().split())==ROYAL_TITANS_35_PARTNER.casefold()
  for value in (member.name,member.global_name,member.display_name)
 ):
  royal_titans_35_partner_id=member.id

@bot.event
async def on_ready():
 # Refresh persisted views only once per process, not after every reconnect.
 if getattr(bot, "_startup_panels_refreshed", False):return
 bot._startup_panels_refreshed=True
 logging.info("Novus PB bot ready as %s (%s)",bot.user,bot.user.id)
 try:
  await recover_existing_proofs()
 except Exception:
  logging.exception("Proof recovery failed; continuing the leaderboard refresh")
 # One-time update of the two previously posted #1 cards, without reposting.
 if not getattr(bot,"_first_card_retrofit_started",False):
  bot._first_card_retrofit_started=True
  start_logged_task(retrofit_existing_first_announcements(),name="pb-announcement-retrofit")
 # Remove proof previews from two existing #1 general-chat announcements in place.
 if not getattr(bot,"_first_no_gallery_retrofit_started",False):
  bot._first_no_gallery_retrofit_started=True
  start_logged_task(remove_existing_first_announcement_galleries(),name="pb-announcement-gallery-cleanup")
 # Retrieve actual emoji IDs, then update existing leaderboard messages in place.
 for guild in bot.guilds:
  cached=await fetch_activity_emojis(guild)
  if guild.id==NOVUS_GUILD_ID:
   matched=[n for n in NOVUS_PB_TROPHY_IDS if cached.get(f"novuspb{n}")==podium_icon(n,guild.id)]
   logging.info("Pinned Novus PB trophy IDs: %s; matched server emoji ranks=%s",NOVUS_PB_TROPHY_IDS,matched)
   if len(matched)!=len(NOVUS_PB_TROPHY_IDS):
    logging.warning("Could not match all pinned Novus PB trophy IDs in the fetched guild emojis")
  loaded=[n for n in range(1,4) if podium_icon(n,guild.id)!=PB_PODIUM_FALLBACK[n]]
  logging.info("Resolved Novus PB podium emojis for guild %s: %s",guild.id,loaded)
  if 1 in loaded and (guild.id==NOVUS_GUILD_ID or guild.id==GUILD_ID):
   start_logged_task(refresh_first_announcement_trophy_icons(guild.id),name="pb-announcement-trophies")
  await resolve_royal_titans_35_partner(guild)
  if guild.id==NOVUS_GUILD_ID:
   start_logged_task(refresh_royal_titans_35_review_once(),name="pb-royal-titans-review")
  records=await store.boss_boards(guild.id)
  jump_links={r["activity"]:f"https://discord.com/channels/{guild.id}/{r['channel']}/{r['message']}" for r in records}
  directory=await store.boss_panel(guild.id)
  if directory:
   try:
    ch=bot.get_channel(directory["channel"]) or await bot.fetch_channel(directory["channel"])
    message=await ch.fetch_message(directory["message"])
    await paced_startup_edit(message, view=BossDirectory(jump_links),allowed_mentions=discord.AllowedMentions.none())
   except discord.HTTPException:
    logging.exception("Failed to refresh PB directory emojis")
  # Update the existing home panel with the latest heading and points.
  home=await store.panel(guild.id)
  if home:
   try:
    ch=bot.get_channel(home["channel"]) or await bot.fetch_channel(home["channel"])
    message=await ch.fetch_message(home["message"])
    points=await calculate_pb_points(guild.id)
    view=Home(points,guild_id=guild.id)
    await paced_startup_edit(message, view=view,allowed_mentions=discord.AllowedMentions.none())
    confirmed=await ch.fetch_message(message.id)
    body=old_announcement_text(confirmed)
    standings=body.splitlines()
    live=[n for n in loaded if any(
     re.fullmatch(rf"{re.escape(PB_TOP10_PODIUM_HEADING+podium_icon(n,guild.id))} <@[0-9]+> · \*\*[0-9]+ pts\*\*",line)
     for line in standings
    )]
    numbered_rows=[int(match[1]) for line in standings
                   if (match:=re.match(r"\*\*([0-9]+)\.\*\* <@",line))]
    count_indent_pattern="(?:"+"|".join(map(re.escape,PB_TROPHY_COUNT_INDENTS))+")"
    count_pattern="-# "+count_indent_pattern+r"\*"+" · ".join(
     rf"{re.escape(podium_icon(rank,guild.id))} × [0-9]+"
     for rank in range(1,4)
    )+r"\*"
    muted_breakdowns=sum(re.fullmatch(count_pattern,line) is not None for line in standings)
    scoring_footer=bool(standings and standings[-1]==top10_trophy_subtext(guild.id,PB_PLACEMENT_POINTS,scoring=True))
    single_heading=standings.count(PB_TOP10_TITLE)==1 and "### TOP 10" not in standings
    ranks=[entry["rank"] for entry in points]
    matches_ranked_rows=body==PB_TOP10_TITLE+"\n"+top10_standings_text(points,guild.id)
    dividers=sum(isinstance(item,ui.Separator) for item in ui.LayoutView.from_message(confirmed).walk_children())
    podium_divider=dividers==3
    logging.info("Verified shared PB Top 10 ranks on message %s: ranks=%s ranked Discord content matches=%s podium divider=%s",
                 message.id,ranks,matches_ranked_rows,podium_divider)
    logging.info("Verified overall PB Top 10 on message %s: inline trophies in small podium headings=%s numbered ranks=%s indented trophy counts with multiplication signs=%s labeled trophy scoring footer with regular points=%s single heading=%s",
                 message.id,live,numbered_rows,muted_breakdowns,scoring_footer,single_heading)
   except discord.HTTPException:
    logging.exception("Failed to refresh PB home panel heading")
  # Refresh the existing footer in place so no /pbpanel rerun is needed.
  footer=await store.footer(guild.id)
  panel=await store.panel(guild.id)
  if footer and panel:
   try:
    ch=bot.get_channel(footer["channel"]) or await bot.fetch_channel(footer["channel"])
    message=await ch.fetch_message(footer["message"])
    url=f"https://discord.com/channels/{guild.id}/{panel['channel']}/{panel['message']}"
    expected=pb_scroll_to_top_text(url)
    await paced_startup_edit(message, content=expected,allowed_mentions=discord.AllowedMentions.none())
    confirmed=await ch.fetch_message(message.id)
    if confirmed.content==expected:
     logging.info("Verified PB scroll-to-top footer message %s points to %s",message.id,url)
    else:
     logging.warning("PB scroll-to-top footer readback did not match for message %s",message.id)
   except discord.HTTPException:
    logging.exception("Failed to refresh PB scroll-to-top footer")
  proof_boards_ok=True
  boss_trophies=0
  verified_first_slots=0
  first_slot_issues=[]
  verified_runner_up_slots=[0,0]
  runner_up_slot_issues=[]
  priority={"Demonic Brutus":-1,"Inferno":0,"Araxxor":1,"Royal Titans":2}
  for record in sorted(records,key=lambda record:priority.get(record["activity"],2)):
   activity=record["activity"]
   if activity not in BOSSES and activity not in RAIDS:continue
   try:
    ch=bot.get_channel(record["channel"]) or await bot.fetch_channel(record["channel"])
    message=await ch.fetch_message(record["message"])
    published=[]
    saved=await write_linked_board(ch,guild.id,activity,message=message,published=published)
    factory=BossLeaderboard if activity in BOSSES else RaidLeaderboard
    blocks=await factory.blocks(guild.id,activity)
    count=sum(bool(results) for _,results in blocks)
    expected_icon=podium_icon(1,guild.id)
    matched=sum(count_first_place_slots(page,expected_icon) for page in published)
    verified_first_slots+=matched
    runner_ups=tuple(map(sum,zip(*(count_runner_up_trophies(page,guild.id) for page in published))))
    if activity in RAIDS:
     board_text="\n".join(old_announcement_text(page) for page in published)
     logging.info("Verified public proof links for %s: messages=%s clickable=%s available=%s compact_refs=%s raid_titles=%s",
                  activity,len(published),board_text.count("[Proof]("),
                  sum(bool(proof_text(r)) for _,rows in blocks for r in rows),
                  len(re.findall(r" · PB #\d+",board_text)),
                  sum(line.startswith("## ") and line.endswith(clean(activity)) for line in board_text.splitlines()))
    for i,total in enumerate(runner_ups):
     verified_runner_up_slots[i]+=total
    if runner_ups!=(count,count) or (count and not {2,3}.issubset(loaded)):
     runner_up_slot_issues.append(f"{activity} ({runner_ups[0]}/{count}, {runner_ups[1]}/{count})")
    if activity in BOSSES and 1 in loaded:
     boss_trophies+=matched
    if matched!=count or (count and 1 not in loaded):
     first_slot_issues.append(f"{activity} ({matched}/{count})")
    if count:
     logging.info("Verified NOVUS #1 slots on %s board %s: %s/%s",
                  activity,message.id,matched,count)
    await asyncio.sleep(1.4)
    if activity in ("Demonic Brutus","Inferno","Araxxor"):
     body=old_announcement_text(saved)
     view=ui.LayoutView.from_message(saved)
     controls=sum((getattr(item,"custom_id",None) or "").startswith("pb:proof:")
                  for item in view.walk_children())
     thumbnails=sum(isinstance(item,ui.Thumbnail) for item in view.walk_children())
     urls=[url for url in re.findall(r"\[Proof\]\((https://[^\s)]+)\)",body)
           if discord_attachment_key(url)]
     reachable=await count_reachable_proof_images(urls)
     logging.info("Verified %s PB board %s: inline proof links=%s proof controls=%s thumbnails=%s reachable images=%s/%s",
                  activity,message.id,body.count("[Proof]("),controls,thumbnails,reachable,len(urls))
    if activity in SHARED_SIZE_RAIDS:
     logging.info("Refreshed raid team-size brackets for %s on Discord message %s",activity,message.id)
    if guild.id==NOVUS_GUILD_ID and activity=="Royal Titans":
     await verify_royal_titans_35_public(guild,ch,message.id)
   except (discord.HTTPException,aiosqlite.Error):
    proof_boards_ok=False
    first_slot_issues.append(f"{activity} (refresh failed)")
    runner_up_slot_issues.append(f"{activity} (refresh failed)")
    logging.exception("Failed to refresh PB icon for %s",activity)
  logging.info("NOVUS #1 board verification: %s occupied slots with largest trophy headings and green badges; issues=%s",
               verified_first_slots,first_slot_issues)
  logging.info("Verified custom runner-up trophies on PB boards: second=%s third=%s issues=%s",
               verified_runner_up_slots[0],verified_runner_up_slots[1],runner_up_slot_issues)
  snapshot_messages=[]
  snapshot_ok=await refresh_proof_snapshot(guild.id,published=snapshot_messages)
  if snapshot_ok and 1 in loaded:
   snapshot=await store.snapshot_panel(guild.id)
   if snapshot:
    try:
     ch=bot.get_channel(snapshot["channel"]) or await bot.fetch_channel(snapshot["channel"])
     message=await ch.fetch_message(snapshot["message"])
     snapshot_rows=[await store.raid_top3(guild.id,raid) for raid in RAIDS]
     expected=tuple(sum(len(results)>=rank for results in snapshot_rows) for rank in range(1,4))
     all_snapshot_messages=[message,*snapshot_messages[1:]]
     badges=sum(count_first_place_slots(page,podium_icon(1,guild.id)) for page in all_snapshot_messages)
     runner_ups=tuple(map(sum,zip(*(count_runner_up_trophies(page,guild.id) for page in all_snapshot_messages))))
     logging.info("Verified raid snapshot custom trophies and green badges: %s/%s; runner-up trophies=%s expected=%s",
                  badges,expected[0],runner_ups,expected[1:])
     snapshot_text="\n".join(old_announcement_text(page) for page in all_snapshot_messages)
     snapshot_lines=snapshot_text.splitlines()
     logging.info("Verified PB date styling on raid snapshot %s: heavy_check_dates=%s compact_note_present=%s",
                  message.id,sum(line.startswith("-# ✔️ ") for line in snapshot_lines),
                  "For compact entries" in snapshot_text)
     logging.info("Verified level-2 raid name headings on snapshot %s: %s/%s",
                  message.id,sum(snapshot_raid_heading(raid) in snapshot_lines for raid in RAIDS),len(RAIDS))
     logging.info("Verified snapshot proof links on message %s: clickable=%s compact_refs=%s",
                  message.id,snapshot_text.count("[Proof]("),len(re.findall(r" · PB #\d+",snapshot_text)))
    except (discord.HTTPException,aiosqlite.Error):
     logging.exception("Unable to verify the updated raid snapshot")
  logging.info("Verified %s custom first-place boss trophy headings for guild %s",boss_trophies,guild.id)
  logging.info("Finished refreshing inline proof links for guild %s",guild.id)
  if proof_boards_ok and snapshot_ok:
   try:
    await restore_staff_proof_cards(guild.id)
   except aiosqlite.Error:
    logging.exception("Could not read PB staff cards for migration")

 if not getattr(bot,"_proof_links_task",None):
  bot._proof_links_task=start_logged_task(refresh_live_proof_links(),name="pb-proof-refresh")

 # Update the tracked message in place after the animated asset deploys.
 # Do not delete/repost the board or alter its other panels.
 if PB_BANNER_URL != PB_BANNER_STATIC:
  for tracked in await store.all("SELECT guild,channel,message FROM panels"):
   try:
    channel=bot.get_channel(tracked["channel"]) or await bot.fetch_channel(tracked["channel"])
    message=await channel.fetch_message(tracked["message"])
    await paced_startup_edit(message, 
     view=await Home.make(tracked["guild"]),
     allowed_mentions=discord.AllowedMentions.none()
    )
   except discord.HTTPException:
    logging.exception("Could not refresh PB banner in channel %s",tracked["channel"])

def is_staff(i):
 if not i.guild or not isinstance(i.user,discord.Member):return False
 m=i.user
 return m.guild_permissions.manage_guild or m.guild_permissions.administrator or bool(STAFF_ROLES.intersection(x.id for x in m.roles))

def is_council_or_founder(i):
 if not i.guild or not isinstance(i.user,discord.Member):return False
 allowed={"council","founder"}
 return any(role.name.casefold() in allowed for role in i.user.roles)

class RulesModal(LoggedModal,title="NOVUS | PB Rules"):
 def __init__(self):
  super().__init__(timeout=300)
  self.add_item(ui.TextDisplay(
   "**1. Novus-only records.** Your PB must have been achieved while you were "
   "a member of Novus. Older PBs are not eligible.\n\n"
   "**2. Group PBs.** Every player involved must have been a Novus member "
   "on the date of the run. List every participant accurately.\n\n"
   "**3. Record details.** Include the PB date, completion time, activity, "
   "mode/team size, and all involved players.\n\n"
   "**4. Proof.** Provide a dated screenshot, clip, or evidence URL. "
   "Use **/pbproof** after submitting if proof was not attached yet. "
   "Staff verifies eligibility and proof before approval.\n\n"
   "-# **Submit below only acknowledges these rules and closes this window. "
   "It does not submit a PB.**"
  ))

 async def on_submit(self,i):
  await i.response.defer(ephemeral=True)


def top10_standings_parts(points,guild_id):
 footer=top10_trophy_subtext(guild_id,PB_PLACEMENT_POINTS,scoring=True)

 def row(entry,names):
  rank=entry["rank"]
  icon=podium_icon(rank,guild_id)
  heading=PB_TOP10_PODIUM_HEADING if rank<=3 else ""
  breakdown=top10_trophy_subtext(guild_id,(entry["firsts"],entry["seconds"],entry["thirds"]),rank=rank)
  return f"{heading}{icon} {names} · **{entry['points']} pts**\n{breakdown}"

 def render(rows,ranks):
  split=next((index for index,rank in enumerate(ranks) if rank>3),0)
  rest="\n".join([*rows[split:],"",footer])
  return ("\n".join(rows[:split]),rest) if split else (rest,)

 def fits(parts):
  return len((PB_TOP10_TITLE+"".join(parts)).encode("utf-16-le"))//2<=4000

 rows=[row(entry,f"<@{entry['member_id']}>") for entry in points]
 parts=render(rows or ["-# No approved PB points yet."],[entry["rank"] for entry in points])
 if fits(parts):
  return parts

 # Combine mentions on oversized boards without splitting tied players by ID.
 groups={}
 for entry in points:
  groups.setdefault(entry["rank"],[]).append(entry)
 grouped=list(groups.values())
 rows=[row(group[0]," · ".join(f"<@{entry['member_id']}>" for entry in group)) for group in grouped]
 ranks=[group[0]["rank"] for group in grouped]
 parts=render(rows,ranks)
 for index in sorted(range(len(rows)),key=lambda i:len(rows[i]),reverse=True):
  if fits(parts):
   break
  group=grouped[index]
  if len(group)>1:
   rows[index]=row(group[0],f"{len(group)} players tied")
   parts=render(rows,ranks)
 return parts


def top10_standings_text(points,guild_id):
 return "\n".join(top10_standings_parts(points,guild_id))


class Home(PBLayoutView):
 def __init__(self,points=None,*,guild_id=None):
  super().__init__(timeout=None)
  points=points or []

  hero=ui.Container(accent_colour=GOLD)
  banner=ui.MediaGallery()
  banner.add_item(media=PB_BANNER_URL)
  hero.add_item(banner)
  hero.add_item(ui.TextDisplay(PB_TOP10_TITLE))
  hero.add_item(ui.Separator())
  for index,part in enumerate(top10_standings_parts(points,guild_id)):
   if index:
    hero.add_item(ui.Separator())
   hero.add_item(ui.TextDisplay(part))
  hero.add_item(ui.Separator())

  actions=ui.ActionRow()
  submit=ui.Button(
   label="Submit PB",
   style=discord.ButtonStyle.success,
   custom_id="pb:home:submit"
  )
  rules=ui.Button(
   label="PB Rules",
   style=discord.ButtonStyle.danger,
   custom_id="pb:home:rules"
  )
  async def open_submit(i):
   await i.response.send_modal(SubmitModal())
  async def open_rules(i):
   await i.response.send_modal(RulesModal())
  mine=ui.Button(
   label="My PBs",
   style=discord.ButtonStyle.secondary,
   custom_id="pb:home:mine"
  )
  submit.callback=open_submit
  rules.callback=open_rules
  mine.callback=show_my_pbs
  actions.add_item(submit)
  actions.add_item(rules)
  actions.add_item(mine)
  hero.add_item(actions)

  self.add_item(hero)

 @classmethod
 async def make(cls,g):
  return cls(await calculate_pb_points(g),guild_id=g)


def snapshot_raid_heading(activity):
 return f"## {activity_icon(activity)}{clean(activity)}"


class SnapshotPanel(PBLayoutView):
 def __init__(self,raid_leaders=None,recent=None,*,omit=(),activities=None,show_title=True,include_recent=True):
  super().__init__(timeout=None)
  raid_leaders=raid_leaders or {}
  recent=recent or []

  records=ui.Container(accent_colour=BLUE)
  if show_title:
   records.add_item(ui.TextDisplay("### RAID SNAPSHOT"))
  for activity in RAIDS if activities is None else activities:
   results=raid_leaders.get(activity,[])
   heading=snapshot_raid_heading(activity)
   if results:
    champion=results[0]
    records.add_item(first_place_section(
     champion,heading+"\n"+PB_FEATURED_HEADING+short_record(champion,0,include_variant=True,omit=omit),
    ))
    if results[1:]:
     records.add_item(ui.TextDisplay("\n".join(
      short_record(r,k,include_variant=True,omit=omit) for k,r in enumerate(results[1:],1)
     )))
   else:
    records.add_item(ui.TextDisplay(heading+"\n-# Be the first to claim this record!"))

  recent_panel=ui.Container(accent_colour=BLUE)
  updated=discord_updated(recent[0]["updated"]) if recent else None
  recent_panel.add_item(ui.TextDisplay(
   "### RECENT PBs"
   + (f"\n-# UPDATED {updated}" if updated else "")
  ))
  if not recent:
   recent_panel.add_item(ui.TextDisplay(
    "-# No recent PBs."
   ))
  else:
   for idx,r in enumerate(recent):
    activity=RAID_LABELS.get(r["activity"],r["activity"])
    recent_text=(
     f"**{clean(activity)} · {clean(r['variant'])}**\n"
     f"**{fmt(r['ms'])}**\n"
     f"{players_with_proof(r,omit=omit)}"
     + (f" · PB #{r['id']}" if r["id"] in omit else "")
     + f"\n-# ✔️ {pb_date_label(r['pb_date'])}"
    )
    recent_panel.add_item(ui.TextDisplay(recent_text))
    if idx!=len(recent)-1:
     recent_panel.add_item(ui.Separator())

  if records.children:
   self.add_item(records)
  if include_recent:
   self.add_item(recent_panel)

 @classmethod
 async def make(cls,g):
  leaders={
   activity:await store.raid_top3(g,activity)
   for activity in RAIDS
  }
  return fit_snapshot_panel(leaders,await store.recent_approved(g,3))


PB_PUBLIC_TEXT_LIMIT=4000


def pb_view_text_units(view):
 return sum(len(item.content.encode("utf-16-le"))//2 for item in view.walk_children()
            if isinstance(item,ui.TextDisplay))


def fit_public_proof_links(render,priority_groups,context,*,text_limit=PB_PUBLIC_TEXT_LIMIT):
 full=render(())
 full_size=pb_view_text_units(full)
 if full_size<=text_limit:
  return full

 # Fit the most links within each priority tier. Measuring the rendered
 # cost accounts for repeated PBs and the space used by fallback labels.
 seen=set()
 tiers=[]
 for group in priority_groups:
  tier=[]
  for r in group:
   sid=r["id"]
   if sid not in seen and proof_text(r):
    seen.add(sid)
    tier.append(sid)
  tiers.append(tier)

 no_links=frozenset(seen)
 base_size=pb_view_text_units(render(no_links))
 costs={sid:pb_view_text_units(render(no_links-{sid}))-base_size for sid in seen}
 budget=text_limit-base_size
 omit=set(no_links)
 for sid,cost in costs.items():
  if cost<=0:
   omit.remove(sid)
   budget-=cost
 for tier in tiers:
  for sid in sorted(tier,key=costs.__getitem__):
   if sid in omit and costs[sid]<=budget:
    omit.remove(sid)
    budget-=costs[sid]
 fitted=render(omit)
 used=pb_view_text_units(fitted)
 logging.info("PB proof link fit for %s: kept=%s omitted=%s text=%s/%s remaining=%s smallest_omitted_cost=%s",
              context,len(seen)-len(omit),len(omit),used,text_limit,
              text_limit-used,min((costs[sid] for sid in omit),default=None))
 return fitted


def fit_snapshot_panel(leaders,recent):
 return fit_public_proof_links(
  lambda omit:SnapshotPanel(leaders,recent,omit=omit),
  ([rows[0] for rows in leaders.values() if rows],recent,
   [r for rows in leaders.values() for r in rows[1:]]),
  "Raid Snapshot",
 )


class BossDirectory(PBLayoutView):
 def __init__(self,jump_links=None):
  super().__init__(timeout=None)
  jump_links=jump_links or {}

  panel=ui.Container(accent_colour=BLUE)
  panel.add_item(ui.TextDisplay(
   "## Boss Directory\n"
   "-# Select an activity to jump to its leaderboard."
  ))

  lines=[
   "### Minigames",
  ]

  for name in ("Inferno","Fortis Colosseum","Fight Caves"):
   url=jump_links.get(name)
   lines.append(
    f"{activity_icon(name)}[{name}]({url})"
    if url
    else f"{activity_icon(name)}**{name}**"
   )

  lines.append("### Raids")
  for activity in RAIDS:
   label=RAID_LABELS[activity]
   url=jump_links.get(activity)
   lines.append(
    f"{activity_icon(activity)}[{label}]({url})"
    if url
    else f"{activity_icon(activity)}**{label}**"
   )

  remaining_groups=[
   ("Slayer Bosses",[
    "Alchemical Hydra","Araxxor","Grotesque Guardians"
   ]),
   ("Solo & Duo Bosses",[
    "Gauntlet","Corrupted Gauntlet","Doom of Mokhaiotl","Demonic Brutus",
    "Mad Angel","Maggot King","Phantom Muspah","Phosani's Nightmare",
    "Royal Titans","Vorkath","Yama","Zulrah"
   ]),
   ("DT2 Bosses",[
    "Duke Sucellus","The Leviathan","Vardorvis","The Whisperer"
   ])
  ]

  for heading,names in remaining_groups:
   lines.append(f"### {heading}")
   for name in names:
    url=jump_links.get(name)
    lines.append(
     f"{activity_icon(name)}[{name}]({url})"
     if url
     else f"{activity_icon(name)}**{name}**"
    )

  panel.add_item(
   ui.TextDisplay("\n".join(lines))
  )
  self.add_item(panel)



def first_place_section(record,content):
 return ui.Section(
  ui.TextDisplay(content),
  accessory=ui.Button(
   label="NOVUS #1",
   style=discord.ButtonStyle.success,
   disabled=True,
   custom_id=f"pb:leader:first:{record['id']}",
  ),
 )


def add_featured_ranking(panel, results, *, compact=False, omit=(), heading=""):
 # Display one featured champion and keep the remaining places easy to scan.
 # No custom fonts or image cards: everything is native Discord Components V2.
 if not results:
  panel.add_item(ui.TextDisplay(
   heading+"-# Be the first to claim this record!"
  ))
  return

 champion=results[0]
 champion_icon=podium_icon(1,champion["guild"])
 champion_text=(
  f"{heading}{PB_FEATURED_HEADING}{champion_icon} {fmt(champion['ms'])}\n"
  f"{players_with_proof(champion,omit=omit)}"
  + (f" · PB #{champion['id']}" if champion["id"] in omit else "")
  + f"\n-# ✔️ {pb_date_label(champion['pb_date'])}"
 )
 panel.add_item(first_place_section(champion,champion_text))

 remaining=[]
 for rank in range(1,5):
  icon=card_rank_icon(rank+1,champion["guild"])
  if rank>=len(results):
   remaining.append(f"{icon} Empty")
   continue
  r=results[rank]
  evidence=proof_text(r,omit=omit)
  proof=f" · {evidence}" if evidence else (f" · PB #{r['id']}" if r["id"] in omit else "")
  remaining.append(
   f"{icon} **{fmt(r['ms'])}** · {public_members(r)}{proof}"
  )

 if not compact:
  panel.add_item(ui.Separator())
 panel.add_item(ui.TextDisplay("\n".join(remaining)))


def board_proof_rows(blocks,*,force_menu=False):
 options={}
 for variant,results in blocks:
  for rank,r in enumerate(results,1):
   if is_private_proof(r["proof"]) and not proof_text(r):
    title=r["activity"] if variant=="Recent" else variant
    options.setdefault(r["id"],(title,rank,fmt(r["ms"])))
 if not options:
  return []
 if not force_menu and len(blocks)==1 and len(options)<=5:
  return [ui.ActionRow(*(ProofButton(sid,label=f"Proof #{rank} · {time}")
                         for sid,(_,rank,time) in options.items()))]
 entries=list(options.items())
 chunks=[entries[offset:offset+25] for offset in range(0,len(entries),25)]
 return [ui.ActionRow(BoardProofSelect(index,[
  discord.SelectOption(label=f"{title} · #{rank} · {time}"[:100],value=str(sid))
  for sid,(title,rank,time) in chunk
 ],len(chunks))) for index,chunk in enumerate(chunks)]


class BossLeaderboard(PBLayoutView):
 def __init__(self,activity,blocks):
  super().__init__(timeout=None)

  accent=RED if activity in ("Inferno","Fortis Colosseum","Fight Caves") else BLUE
  # Keep the boss title and PBs in one native Discord container.
  # Up to two variants fit the Discord 10-child limit.
  panel=ui.Container(accent_colour=accent)
  panel.add_item(ui.TextDisplay(
   f"## {activity_icon(activity)}{clean(activity)}"
  ))

  for index,(variant,results) in enumerate(blocks):
   if index:
    panel.add_item(ui.Separator())
   if len(blocks)>1:
    panel.add_item(ui.TextDisplay(f"### {clean(variant)}"))
   add_featured_ranking(panel,results)

  self.add_item(panel)

 @classmethod
 async def blocks(cls,g,activity):
  return [
    (
     variant,
     await store.ranking(
      g,
      "Bosses",
      activity,
      variant
     )
    )
    for variant in BOSSES[activity]
   ]

 @classmethod
 async def make(cls,g,activity):
  return cls(activity,await cls.blocks(g,activity))


class RaidLeaderboard(PBLayoutView):
 def __init__(self,activity,blocks,*,omit=(),show_title=True):
  super().__init__(timeout=None)

  title=f"## {activity_icon(activity)}{clean(activity)}"
  # A full bracket takes seven components. Compact brackets take five,
  # including the green badge. Eight populated brackets use all 40, so
  # merge the title and variant headings into existing displays in that case.
  title_components=2 if show_title else 0
  compact=title_components+sum(7 if results else 3 for _,results in blocks)>40
  merge_title=show_title and compact and 2+sum(5 if results else 2 for _,results in blocks)>40
  if show_title and not merge_title:
   heading=ui.Container(accent_colour=GOLD)
   heading.add_item(ui.TextDisplay(title))
   self.add_item(heading)

  for index,(variant,results) in enumerate(blocks):
   panel=ui.Container(accent_colour=GOLD)
   subtitle=f"### {clean(variant)}"
   if not compact:
    panel.add_item(ui.TextDisplay(subtitle))
   prefix=((title+"\n" if merge_title and index==0 else "")+subtitle+"\n"
           if compact else "")
   add_featured_ranking(panel,results,compact=compact,omit=omit,heading=prefix)
   self.add_item(panel)

 @classmethod
 async def blocks(cls,g,activity):
  return [
    (
     variant,
     await store.ranking(
      g,
      "Raids",
      activity,
      variant
     )
    )
    for variant in RAIDS[activity]
   ]

 @classmethod
 async def make(cls,g,activity):
  return cls(activity,await cls.blocks(g,activity))


def linked_board_view(factory,activity,blocks,*,show_title=True,text_limit=PB_PUBLIC_TEXT_LIMIT):
 if factory is not RaidLeaderboard:
  return factory(activity,blocks)
 return fit_public_proof_links(
  lambda omit:factory(activity,blocks,omit=omit,show_title=show_title),
  ([rows[rank] for _,rows in blocks if rank<len(rows)] for rank in range(5)),
  activity,text_limit=text_limit,
 )


def raid_board_page_plan(activity,blocks):
 full=RaidLeaderboard(activity,blocks)
 if pb_view_text_units(full)<=PB_PUBLIC_TEXT_LIMIT:
  return [(blocks[0][0],full)] if blocks else [("",full)]

 # Every continuation starts at a scale boundary and reuses the same gold
 # cards. Leave room on each nonfinal message for its next-scale jump link.
 groups=[]
 current=[]
 for block in blocks:
  show_title=not groups
  candidate=RaidLeaderboard(activity,current+[block],show_title=show_title)
  if (pb_view_text_units(candidate)<=PB_PUBLIC_TEXT_LIMIT-180
      and sum(1 for _ in candidate.walk_children())<=39):
   current.append(block)
   continue
  if current or show_title:
   groups.append(current)
  current=[block]
 if current:
  groups.append(current)
 return [
  (group[0][0] if group else "",linked_board_view(
   RaidLeaderboard,activity,group,show_title=index==0,
   text_limit=PB_PUBLIC_TEXT_LIMIT-(180 if index<len(groups)-1 else 0),
  ))
  for index,group in enumerate(groups)
 ]


SNAPSHOT_CONTINUATION_KEY="__raid_snapshot__"


def snapshot_page_plan(leaders,recent):
 full=SnapshotPanel(leaders,recent)
 if pb_view_text_units(full)<=PB_PUBLIC_TEXT_LIMIT:
  return [("",full)]
 # Keep entire raid cards or the Recent PBs card together where possible.
 remain=[([raid],False) for raid in RAIDS]+[([],True)]
 groups=[]
 raids=[]
 recent_card=False
 for extra,with_recent in remain:
  candidate=SnapshotPanel(leaders,recent,activities=raids+extra,
                          show_title=not groups,include_recent=with_recent)
  if (raids or recent_card) and pb_view_text_units(candidate)>PB_PUBLIC_TEXT_LIMIT-180:
   groups.append((raids,recent_card))
   raids=[]
  raids=raids+extra
  recent_card=with_recent
 groups.append((raids,recent_card))
 plan=[]
 for index,(activities,with_recent) in enumerate(groups):
  selected=[leaders[raid] for raid in activities if leaders.get(raid)]
  def render(omit):
   return SnapshotPanel(leaders,recent,omit=omit,activities=activities,
                        show_title=index==0,include_recent=with_recent)
  view=fit_public_proof_links(
   render,([rows[0] for rows in selected],recent if with_recent else [],
           [r for rows in selected for r in rows[1:]]),"Raid Snapshot",
   text_limit=PB_PUBLIC_TEXT_LIMIT-(180 if index<len(groups)-1 else 0),
  )
  plan.append((activities[0] if activities else "Recent PBs",view))
 return plan


def raid_page_link(label,url):
 return ui.TextDisplay(f"### [{clean(label)} ↓]({url})")


async def publish_raid_board_pages(channel,g,activity,message,blocks,mentions):
 return await publish_board_pages(channel,g,activity,message,raid_board_page_plan(activity,blocks),mentions)


async def publish_board_pages(channel,g,activity,message,plan,mentions):
 tracked={r["page"]:r for r in await store.raid_pages(g,activity)}
 saved=[message]
 if message is None:
  saved[0]=await channel.send(view=plan[0][1],allowed_mentions=mentions)
 # Send new pages in scale order. On an existing board, edit only the
 # tracked posts and keep its original directory destination.
 for i,(_,view) in enumerate(plan[1:],1):
  page=tracked.get(i)
  existing=None
  if page and page["channel"]==channel.id:
   try:
    existing=await channel.fetch_message(page["message"])
   except discord.NotFound:
    pass
  if existing is None:
   existing=await channel.send(view=view,allowed_mentions=mentions)
   await store.set_raid_page(g,activity,i,channel.id,existing.id)
  saved.append(existing)
 for i in reversed(range(len(plan))):
  _,view=plan[i]
  if i<len(plan)-1:
   view.add_item(raid_page_link(plan[i+1][0],saved[i+1].jump_url))
  if i or message is not None or len(plan)>1:
   saved[i]=await saved[i].edit(view=view,allowed_mentions=mentions,attachments=[])
 for i,page in tracked.items():
  if i<len(plan):
   continue
  if page["channel"]==channel.id:
   try:
    await channel.get_partial_message(page["message"]).delete()
   except discord.NotFound:
    pass
  await store.remove_raid_page(g,activity,i)
 return saved


def proof_attachment_name(r,path):
 return f"pb-{r['id']}-{path.name}"


def review_proof_media(message,filename):
 for component in message.components:
  for child in getattr(component,"children",()):
   file=getattr(child,"media",None) if getattr(child,"type",None)==discord.ComponentType.file else None
   url=getattr(file,"url",None)
   key=discord_attachment_key(url)
   if key and key[-1]==filename:
    return file
 return None


def is_proof_thread(thread,parent_id,guild_id):
 return (
  getattr(thread,"type",None)==discord.ChannelType.private_thread
  and getattr(thread,"parent_id",None)==parent_id
  and getattr(getattr(thread,"guild",None),"id",None)==guild_id
 )


async def proof_thread_for(guild_id):
 parent=bot.get_channel(REVIEW_ID) or await bot.fetch_channel(REVIEW_ID)
 if getattr(getattr(parent,"guild",None),"id",None)!=guild_id:
  raise ProofUnavailable("PB review channel belongs to another server")
 row=await store.one("SELECT * FROM proof_threads WHERE guild=?",(guild_id,))
 if row and row["parent"]==parent.id:
  try:
   thread=bot.get_channel(row["thread"]) or await bot.fetch_channel(row["thread"])
  except discord.NotFound:
   thread=None
  if thread is not None:
   if not is_proof_thread(thread,parent.id,guild_id):
    raise ProofUnavailable("Stored proof archive is not a private review thread")
   return thread

 def owned_archive(thread):
  return (is_proof_thread(thread,parent.id,guild_id) and thread.name==PROOF_THREAD_NAME
          and thread.owner_id==bot.user.id)

 thread=next((t for t in parent.threads if owned_archive(t)),None)
 if thread is None:
  # Reuse a bot-owned thread even if a restart interrupted saving its ID.
  async for candidate in parent.archived_threads(private=True,joined=True,limit=100):
   if owned_archive(candidate):
    thread=candidate
    break
 if thread is None:
  thread=await parent.create_thread(name=PROOF_THREAD_NAME,type=discord.ChannelType.private_thread,
                                     invitable=False,auto_archive_duration=60,
                                     reason="Keep approved PB proof files off the staff cards")
  logging.info("Created private PB Proof Archive thread %s under staff channel %s",thread.id,parent.id)
 if not is_proof_thread(thread,parent.id,guild_id):
  raise ProofUnavailable("Discord did not return the requested private thread")
 await store.save_proof_thread(guild_id,parent.id,thread.id)
 return thread


async def cache_proof_from_thread(r,path):
 if r["status"]!="approved" or r["review_channel"]!=REVIEW_ID or not REVIEW_ID:
  return False
 async with PROOF_THREAD_LOCK:
  if PROOF_THREAD_RETRY_AT.get(r["guild"],0)>asyncio.get_running_loop().time():
   return False
  try:
   thread=await proof_thread_for(r["guild"])
   filename=proof_attachment_name(r,path)
   row=await store.one("SELECT * FROM proof_thread_messages WHERE guild=? AND proof=?",
                       (r["guild"],r["proof"]))
   existing=None
   if row and row["thread"]==thread.id:
    try:
     existing=await thread.fetch_message(row["message"])
    except discord.NotFound:
     pass
   if existing and existing.author.id!=bot.user.id:
    raise ProofUnavailable("Proof archive message is not owned by the bot")
   attachment=next((a for a in existing.attachments if a.filename==filename),None) if existing else None
   is_new=attachment is None
   if is_new:
    file=discord.File(path,filename=filename)
    try:
     existing=await thread.send(f"Approved PB #{r['id']}",file=file,silent=True,
                                 allowed_mentions=discord.AllowedMentions.none())
    finally:
     file.close()
    attachment=next((a for a in existing.attachments if a.filename==filename),None)
    if attachment:
     # Keep the reference even if the CDN has a transient error right after upload.
     await store.save_proof_message(r["guild"],r["proof"],thread.id,existing.id)
   key=discord_attachment_key(attachment.url) if attachment else None
   if not key or key[0]!="attachments" or int(key[1])!=thread.id:
    return False
   if (is_new or not discord_attachment_key(PUBLIC_PROOF_URLS.get(r["proof"]))) and not await count_reachable_proof_images([attachment.url],allow_video=True):
    logging.warning("Thread proof for PB #%s did not pass the CDN check",r["id"])
    return False
   return cache_public_attachment(r["proof"],attachment)
  except (discord.Forbidden,ProofUnavailable) as exc:
   PROOF_THREAD_RETRY_AT[r["guild"]]=asyncio.get_running_loop().time()+300
   logging.warning("PB archive thread unavailable for guild %s (%s). Keeping the existing proof layout.",
                   r["guild"],type(exc).__name__)
  except (discord.HTTPException,OSError,aiosqlite.Error):
   logging.exception("Could not refresh PB #%s from the archive thread",r["id"])
  return False


async def cache_proof_from_review(r,path):
 if await cache_proof_from_thread(r,path):
  return
 await cache_legacy_review_proof(r,path)


async def cache_legacy_review_proof(r,path):
 # Back public image links with the approved record's existing staff message.
 # The clickable link is to the signed image, not to the restricted channel.
 if r["status"]!="approved" or not r["review_channel"] or not r["review_message"]:
  return
 filename=proof_attachment_name(r,path)
 try:
  channel=bot.get_channel(r["review_channel"]) or await bot.fetch_channel(r["review_channel"])
  message=await channel.fetch_message(r["review_message"])
  if message.author.id!=bot.user.id:
   return
  existing=next((a for a in message.attachments if a.filename==filename),None)
  if existing is None:
   existing=review_proof_media(message,filename)
  if existing is None:
   if len(message.attachments)>=10:
    return
   await store.unmark_proof_button_card(r["guild"],r["review_message"])
   file=discord.File(path,filename=filename)
   try:
    message=await message.edit(view=Review(r,archived_filename=filename),
                               attachments=[*message.attachments,file],
                               allowed_mentions=discord.AllowedMentions.none())
   finally:
    file.close()
   existing=next((a for a in message.attachments if a.filename==filename),None)
   if existing is None:
    existing=review_proof_media(message,filename)
  if existing:
   cache_public_attachment(r["proof"],existing)
  else:
   logging.warning("Discord did not return an attached image for PB #%s",r["id"])
 except (discord.HTTPException,OSError):
  logging.exception("Unable to refresh proof image for PB #%s",r["id"])


async def restore_staff_proof_cards(guild_id):
 # Remove a legacy tile only after the leaderboards and snapshot have been
 # updated to point to the new thread copy. Files on disk are never removed.
 rows=await store.all("""SELECT s.*,t.thread AS archive_thread FROM submissions s
    LEFT JOIN proof_thread_messages t ON t.guild=s.guild AND t.proof=s.proof
    LEFT JOIN proof_button_cards b ON b.guild=s.guild AND b.message=s.review_message
    WHERE b.message IS NULL AND s.guild=? AND s.review_channel=? AND s.review_message IS NOT NULL
    AND s.proof IS NOT NULL AND s.proof<>''
    ORDER BY CASE WHEN s.id=71 THEN 0 ELSE 1 END,s.id""",(guild_id,REVIEW_ID))
 if not rows:
  return
 try:
  channel=bot.get_channel(REVIEW_ID) or await bot.fetch_channel(REVIEW_ID)
 except discord.HTTPException:
  logging.exception("Unable to read PB staff review channel")
  return

 updated=0
 for r in rows:
  path=archive_path(DBPATH,r["proof"])
  external=not is_private_proof(r["proof"]) and valid_url(r["proof"])
  if not path and not external:
   continue
  if path and not path.is_file():
   continue
  try:
   msg=await channel.fetch_message(r["review_message"])
   if msg.author.id!=bot.user.id:
    continue
   view=ui.LayoutView.from_message(msg)
   if any(getattr(item,"custom_id",None)==f"pb:proof:view:{r['id']}" for item in view.walk_children()):
    # An approved card can temporarily fall back to an attachment if the
    # archive thread is unavailable. Keep it eligible for a later cleanup.
    if not path or r["archive_thread"]:
     await store.mark_proof_button_card(guild_id,msg.id)
    continue
   if path:
    if not review_proof_media(msg,proof_attachment_name(r,path)):
     continue
    url=PUBLIC_PROOF_URLS.get(r["proof"])
    key=discord_attachment_key(url)
    if not key or int(key[1])!=r["archive_thread"] or proof_link_needs_refresh(
        url,datetime.now(timezone.utc).timestamp()):
     # Older tiles no longer on the leaderboard still get a thread copy.
     # Defer removal to the next successful board/snapshot refresh.
     if await cache_proof_from_thread(r,path):
      logging.info("Staged an older PB #%s proof for the next staff card refresh",r["id"])
     continue
   fresh=await Review.make(r)
   result=await msg.edit(view=fresh,allowed_mentions=discord.AllowedMentions.none(),
                         **({"attachments":[]} if path else {}))
   rendered=ui.LayoutView.from_message(result)
   if (any(getattr(item,"custom_id",None)==f"pb:proof:view:{r['id']}" for item in rendered.walk_children())
       and not any(isinstance(item,ui.File) for item in rendered.walk_children())):
    await store.mark_proof_button_card(guild_id,msg.id)
    updated+=1
    if r["id"]==71:
     logging.info("Verified PB #71 approved staff card: View Proof button present, file tile absent")
  except discord.NotFound:
   continue
  except discord.HTTPException:
   logging.exception("Could not restore View Proof on staff PB #%s",r["id"])
 logging.info("PB staff proof card refresh complete for guild %s: restored=%s",guild_id,updated)


async def write_linked_board(channel,g,activity,message=None,*,archived_only=False,published=None):
 factory=BossLeaderboard if activity in BOSSES else RaidLeaderboard
 async with PUBLIC_PROOF_LOCK:
  blocks=await factory.blocks(g,activity)
  proofs={}
  for _,results in blocks:
   for r in results:
    if r["status"]!="approved":
     continue
    path=archive_path(DBPATH,r["proof"])
    if path and path.is_file():
     proofs.setdefault(r["proof"],(r,path))
  if archived_only and not proofs:
   return
  if isinstance(message,int):
   message=await channel.fetch_message(message)
  at=datetime.now(timezone.utc).timestamp()
  for ref,(r,path) in proofs.items():
   if proof_link_needs_refresh(PUBLIC_PROOF_URLS.get(ref),at):
    await cache_proof_from_review(r,path)
  mentions=discord.AllowedMentions.none()
  if factory is RaidLeaderboard:
   messages=await publish_raid_board_pages(channel,g,activity,message,blocks,mentions)
  else:
   view=linked_board_view(factory,activity,blocks)
   saved=(await channel.send(view=view,allowed_mentions=mentions) if message is None
          else await message.edit(view=view,allowed_mentions=mentions,attachments=[]))
   messages=[saved]
  if published is not None:
   published.extend(messages)
  return messages[0]


async def count_reachable_proof_images(urls,*,allow_video=False):
 async def probe(session,url):
  try:
   async with session.get(url,headers={"Range":"bytes=0-31"},allow_redirects=False) as response:
    kinds=("image/","video/") if allow_video else ("image/",)
    return response.status in (200,206) and response.content_type.startswith(kinds)
  except (aiohttp.ClientError,asyncio.TimeoutError):
   return False

 async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
  return sum(await asyncio.gather(*(probe(session,url) for url in urls)))


async def refresh_live_proof_links():
 """Rotate signed Discord links before expiration using the original attached files."""
 while not bot.is_closed():
  await asyncio.sleep(PROOF_LINK_REFRESH_SECONDS)
  for guild in bot.guilds:
   try:
    boards=await store.boss_boards(guild.id)
   except aiosqlite.Error:
    logging.exception("Unable to read PB boards for %s",guild.id)
    continue
   proof_boards_ok=True
   for board in boards:
    activity=board["activity"]
    if activity not in BOSSES and activity not in RAIDS:
     continue
    try:
     channel=bot.get_channel(board["channel"]) or await bot.fetch_channel(board["channel"])
     await write_linked_board(channel,guild.id,activity,board["message"],archived_only=True)
    except (discord.HTTPException,OSError,aiosqlite.Error):
     proof_boards_ok=False
     logging.exception("Unable to rotate proof links for %s",activity)
   try:
    if await refresh_proof_snapshot(guild.id) and proof_boards_ok:
     await restore_staff_proof_cards(guild.id)
   except aiosqlite.Error:
    logging.exception("Unable to read PB snapshot or staff cards for %s",guild.id)


class RaidBrowser(PBLayoutView):
 def __init__(self,g,raid_index,variant_index,results):
  super().__init__(timeout=900)
  self.g=g
  self.raid_index=raid_index
  self.variant_index=variant_index

  activities=list(RAIDS)
  activity=activities[raid_index]
  variants=RAIDS[activity]
  variant=variants[variant_index]

  panel=ui.Container(accent_colour=GOLD)
  panel.add_item(ui.TextDisplay(
   f"## ✦ NOVUS · {activity_icon(activity)}{clean(activity)}"
  ))

  raid_row=ui.ActionRow()
  for idx,name in enumerate(activities):
   button=ui.Button(
    label=("✓ " if idx==raid_index else "")+RAID_LABELS[name],
    style=(
     discord.ButtonStyle.primary if idx==raid_index
     else discord.ButtonStyle.secondary
    )
   )
   async def switch_raid(i,target=idx):
    await i.response.edit_message(
     view=await RaidBrowser.make(self.g,target,0)
    )
   button.callback=switch_raid
   raid_row.add_item(button)
  panel.add_item(raid_row)

  panel.add_item(ui.Separator())
  variant_select=ui.Select(
   placeholder="Filter by mode / team size",
   min_values=1,
   max_values=1,
   options=[
    discord.SelectOption(
     label=name,
     value=str(idx),
     default=idx==variant_index
    )
    for idx,name in enumerate(variants)
   ]
  )
  async def choose_variant(i):
   target=int(variant_select.values[0])
   await i.response.edit_message(
    view=await RaidBrowser.make(self.g,self.raid_index,target)
   )
  variant_select.callback=choose_variant
  variant_row=ui.ActionRow()
  variant_row.add_item(variant_select)
  panel.add_item(variant_row)

  rankings=ui.Container(accent_colour=BLUE)
  rankings.add_item(ui.TextDisplay(
   f"### {clean(variant)}"
  ))
  rankings.add_item(ui.Separator())

  add_featured_ranking(rankings,results)

  shortcuts=ui.ActionRow()
  submit=ui.Button(label="Submit PB",style=discord.ButtonStyle.success)
  rules=ui.Button(label="PB Rules",style=discord.ButtonStyle.danger)
  bosses=ui.Button(label="Bosses",style=discord.ButtonStyle.secondary)

  async def open_submit(i):
   await i.response.send_modal(SubmitModal())
  async def open_rules(i):
   await i.response.send_modal(RulesModal())
  async def open_bosses(i):
   await i.response.edit_message(view=await Browser.make(self.g,"Bosses",0))

  submit.callback=open_submit
  rules.callback=open_rules
  bosses.callback=open_bosses
  shortcuts.add_item(submit)
  shortcuts.add_item(rules)
  shortcuts.add_item(bosses)
  rankings.add_item(shortcuts)

  self.add_item(panel)
  self.add_item(rankings)
  for row in board_proof_rows([(variant,results)]):
   self.add_item(row)

 @classmethod
 async def make(cls,g,raid_index,variant_index):
  activities=list(RAIDS)
  raid_index%=len(activities)
  activity=activities[raid_index]
  variants=RAIDS[activity]
  variant_index%=len(variants)
  variant=variants[variant_index]
  results=await store.ranking(g,"Raids",activity,variants[variant_index])
  return cls(g,raid_index,variant_index,results)


class Browser(PBLayoutView):
 def __init__(self,g,cat,index,blocks):
  super().__init__(timeout=900)
  self.g=g
  self.cat=cat
  self.index=index
  activities=CATS[cat]
  name=list(activities)[index]

  panel=ui.Container(accent_colour=GOLD)
  panel.add_item(ui.TextDisplay(
   f"## ✦ NOVUS · {clean(name)}"
  ))

  tabs=ui.ActionRow()
  raids=ui.Button(label="Raids",style=discord.ButtonStyle.secondary)
  bosses=ui.Button(label="Bosses",style=discord.ButtonStyle.primary)
  async def show_raids(i):
   await i.response.edit_message(view=await RaidBrowser.make(self.g,0,0))
  async def show_bosses(i):
   await i.response.edit_message(view=await Browser.make(self.g,"Bosses",0))
  raids.callback=show_raids
  bosses.callback=show_bosses
  tabs.add_item(raids)
  tabs.add_item(bosses)
  panel.add_item(tabs)
  panel.add_item(ui.Separator())

  rankings=ui.Container(accent_colour=BLUE)
  for pos,(variant,results) in enumerate(blocks):
   if pos:
    rankings.add_item(ui.Separator())
   if variant!="Standard":
    rankings.add_item(ui.TextDisplay(f"### {clean(variant)}"))
   add_featured_ranking(rankings,results)

  shortcuts=ui.ActionRow()
  submit=ui.Button(label="Submit PB",style=discord.ButtonStyle.success)
  rules=ui.Button(label="PB Rules",style=discord.ButtonStyle.danger)
  async def open_submit(i):
   await i.response.send_modal(SubmitModal())
  async def open_rules(i):
   await i.response.send_modal(RulesModal())
  submit.callback=open_submit
  rules.callback=open_rules
  shortcuts.add_item(submit)
  shortcuts.add_item(rules)
  rankings.add_item(shortcuts)

  self.add_item(panel)
  self.add_item(rankings)
  for row in board_proof_rows(blocks):
   self.add_item(row)

 @classmethod
 async def make(cls,g,cat,index):
  names=list(CATS[cat])
  index%=len(names)
  activity=names[index]
  return cls(
   g,cat,index,[
    (variant,await store.ranking(g,cat,activity,variant))
    for variant in CATS[cat][activity]
   ]
  )


def submitted_review_members(r):
 return public_members(r)


async def verify_royal_titans_35_public(guild,channel,message_id):
 record=await store.one("SELECT * FROM submissions WHERE guild=? AND id=35",(guild.id,))
 if not record or not is_royal_titans_35(record) or record["status"]!="approved":
  return
 message=await channel.fetch_message(message_id)
 expected=public_members(record)
 if any(expected in line and fmt(record["ms"]) in line
        for line in old_announcement_text(message).splitlines()):
  logging.info(
   "Verified Royal Titans #35 public roster in Discord: message=%s time=%s roster=%s",
   message_id,fmt(record["ms"]),expected,
  )
 else:
  logging.warning("Royal Titans #35 public roster not present on message=%s",message_id)


def royal_titans_35_ready_to_refresh(r):
 """Match only the existing review; never change roster or approval state."""
 if not r:return False
 return (
  is_royal_titans_35(r)
  and r["status"] in ("pending","awaiting_proof","approved")
  and r["review_message"]==1570207527088431165
 )


async def refresh_royal_titans_35_review_once():
 """Update the existing Royal Titans #35 review, no team DB or rank changes."""
 await asyncio.sleep(5)
 key="royal_titans_35_refresh_shared_roster_v4"
 try:
  async with store.lock:
   await store.conn.execute(
    "CREATE TABLE IF NOT EXISTS one_time_pb_migrations "
    "(name TEXT PRIMARY KEY,applied TEXT NOT NULL)"
   )
   await store.conn.commit()
  done=await store.one(
   "SELECT name FROM one_time_pb_migrations WHERE name=?",(key,)
  )
  if done:return
  record=await store.one("SELECT * FROM submissions WHERE id=?",(35,))
  if not royal_titans_35_ready_to_refresh(record):
   logging.warning(
    "Skipped Royal Titans #35 review refresh: record status or identity "
    "changed (status=%s id=%s category=%s activity=%s variant=%s time=%s date=%s review=%s)",
    record["status"] if record else None,
    record["id"] if record else None,
    record["category"] if record else None,
    record["activity"] if record else None,
    record["variant"] if record else None,
    record["ms"] if record else None,
    record["pb_date"] if record else None,
    record["review_message"] if record else None
   )
   return
  if GUILD_ID and record["guild"]!=GUILD_ID:
   logging.warning("Skipped Royal Titans #35 review refresh: guild mismatch")
   return
  channel=bot.get_channel(record["review_channel"]) or await bot.fetch_channel(
   record["review_channel"]
  )
  message=await channel.fetch_message(record["review_message"])
  if message.author.id!=bot.user.id:
   logging.warning("Skipped Royal Titans #35: review message belongs to another user")
   return
  await message.edit(
   view=await Review.make(record),
   allowed_mentions=discord.AllowedMentions.none()
  )
  async with store.lock:
   await store.conn.execute(
    "INSERT OR IGNORE INTO one_time_pb_migrations(name,applied) VALUES(?,?)",
    (key,now())
   )
   await store.conn.commit()
  logging.info(
   "Royal Titans #35 review roster updated in place (message=%s)",
   message.id
  )
 except discord.HTTPException:
  logging.exception("Failed to update Royal Titans #35 existing review message")
 except Exception:
  logging.exception("Unexpected Royal Titans #35 review refresh error")


class Review(PBLayoutView):
 def __init__(self,r,leader=None,placement=None,*,archived_filename=None):
  active=r["status"] in ("pending","awaiting_proof")
  super().__init__(timeout=None if active else 60)
  self.sid=r["id"]
  status=r["status"].replace("_"," ").title()
  status_style={
   "pending":discord.ButtonStyle.secondary,
   "awaiting_proof":discord.ButtonStyle.primary,
   "approved":discord.ButtonStyle.success,
   "denied":discord.ButtonStyle.danger,
  }.get(r["status"],discord.ButtonStyle.secondary)

  panel=ui.Container(accent_colour=GOLD)
  status_badge=ui.Button(
   label=status,
   style=status_style,
   disabled=True,
   custom_id=f"pb:status:{self.sid}"
  )
  panel.add_item(
   ui.Section(
    ui.TextDisplay(
     f"## ✦ NOVUS | PB REVIEW\n"
     f"**{clean(r['activity'])}** · {clean(r['variant'])}\n"
     f"-# Submission #{r['id']} · Submitted by <@{r['submitter']}>"
    ),
    accessory=status_badge
   )
  )

  panel.add_item(ui.Separator())
  panel.add_item(ui.TextDisplay("-# PB DATE\n" f"**{clean(r['pb_date'] or 'Not recorded')}**"))
  if active:
   if not leader:
    difference="First record for this leaderboard"
   elif r["ms"]<leader["ms"]:
    difference=f"{fmt(leader['ms']-r['ms'])} faster than current #1"
   elif r["ms"]>leader["ms"]:
    difference=f"{fmt(r['ms']-leader['ms'])} behind current #1"
   else:
    difference="Tied with current #1"
   label=(f"NEW #{placement}" if isinstance(placement,int) else
          "NO RANK CHANGE" if placement=="unchanged" else "OUTSIDE TOP 5")
   panel.add_item(ui.Section(
    ui.TextDisplay("-# SUBMITTED PB\n"
     f"**{submitted_review_members(r)}** · **{fmt(r['ms'])}**\n"
     f"-# {difference}"),
    accessory=ui.Button(label=label,disabled=True,
     style=discord.ButtonStyle.success if placement==1 else discord.ButtonStyle.secondary,
     custom_id=f"pb:placement:{self.sid}")
   ))
   panel.add_item(ui.Separator())
   panel.add_item(ui.TextDisplay(
    "-# CURRENT #1\n" +
    (f"**{review_members(leader)}** · **{fmt(leader['ms'])}**" if leader else "No approved record yet.")
   ))
  else:
   panel.add_item(ui.TextDisplay(
    "-# SUBMITTED PB\n" f"**{submitted_review_members(r)}** · **{fmt(r['ms'])}**"
   ))

  p=r["proof"]
  if archived_filename:
   panel.add_item(ui.File(f"attachment://{archived_filename}"))
  elif is_private_proof(p):
   panel.add_item(ui.ActionRow(ProofButton(r["id"])))
  elif p and valid_url(p):
   panel.add_item(ui.ActionRow(ProofButton(r["id"])))
  else:
   panel.add_item(
    ui.TextDisplay(
     "### PROOF\n"
     "-# Awaiting proof."
    )
   )

  if active:
   panel.add_item(ui.Separator())
   decision_row=ui.ActionRow()
   approve=ui.Button(
    label="Approve",
    style=discord.ButtonStyle.success,
    custom_id=f"pb:review:approve:{self.sid}"
   )
   deny=ui.Button(
    label="Deny",
    style=discord.ButtonStyle.danger,
    custom_id=f"pb:review:deny:{self.sid}"
   )
   async def approve_clicked(i):await decide(i,self.sid,"approve")
   async def deny_clicked(i):await decide(i,self.sid,"deny")
   approve.callback=approve_clicked
   deny.callback=deny_clicked
   request=ui.Button(
    label="Request Proof",
    style=discord.ButtonStyle.primary,
    custom_id=f"pb:review:request:{self.sid}"
   )
   async def request_clicked(i):await decide(i,self.sid,"request")
   request.callback=request_clicked
   decision_row.add_item(approve)
   decision_row.add_item(deny)
   decision_row.add_item(request)
   panel.add_item(decision_row)

  self.add_item(panel)

 @classmethod
 async def make(cls,r):
  leader=None
  placement=None
  if r["status"] in ("pending","awaiting_proof"):
   rows=await store.all(
    """SELECT * FROM submissions WHERE guild=? AND category=? AND activity=? AND variant=?
    AND status='approved' ORDER BY ms,id""",
    (r["guild"],r["category"],r["activity"],r["variant"])
   )
   leader=rows[0] if rows else None
   candidates=sorted([*rows,r],key=lambda entry:(entry["ms"],entry["id"]))
   unique_fn=(unique_solo_results if r["category"]=="Bosses"
              and solo_boss_variant(r["activity"],r["variant"]) else unique_team_results)
   projected=unique_fn(candidates,5)
   placement=next((i for i,item in enumerate(projected,1) if item["id"]==r["id"]),None)
   if placement is None:
    all_unique=unique_fn(candidates,len(candidates))
    placement="unchanged" if not any(item["id"]==r["id"] for item in all_unique) else "outside"
  return cls(r,leader,placement)


async def refresh_panel(g,activity=None):
 home=await store.panel(g)
 if home:
  try:
   ch=bot.get_channel(home["channel"]) or await bot.fetch_channel(home["channel"])
   msg=await ch.fetch_message(home["message"])
   await msg.edit(
    view=await Home.make(g),
    allowed_mentions=discord.AllowedMentions.none()
   )
  except discord.HTTPException:
   logging.exception("Unable to refresh PB points panel")

 if activity in BOSSES:
  public_activities=[("Bosses",activity)]
 elif activity in RAIDS:
  public_activities=[("Raids",activity)]
 else:
  public_activities=(
   [("Bosses",name) for name in BOSSES]
   + [("Raids",name) for name in RAIDS]
  )

 for category,name in public_activities:
  tracked=await store.boss_board(g,name)
  if not tracked:
   continue
  try:
   ch=bot.get_channel(tracked["channel"]) or await bot.fetch_channel(tracked["channel"])
   msg=await ch.fetch_message(tracked["message"])
   await write_linked_board(ch,g,name,msg)
  except discord.HTTPException:
   logging.exception(
    "Unable to refresh public PB board for %s",
    name
   )

 await refresh_proof_snapshot(g)


async def update_review(r):
 if not r["review_message"]:return
 try:
  ch=bot.get_channel(r["review_channel"]) or await bot.fetch_channel(r["review_channel"])
  msg=await ch.fetch_message(r["review_message"])
  await msg.edit(view=await Review.make(r),allowed_mentions=discord.AllowedMentions.none())
 except discord.HTTPException:
  logging.exception("Unable to update review | guild_id=%s submission_id=%s message_id=%s",
                    r["guild"],r["id"],r["review_message"])


async def recover_discord_proof(record,session):
 original=record["proof"]
 if not discord_attachment_key(original):
  return None
 candidates=[]
 if record["review_channel"] and record["review_message"]:
  try:
   channel=bot.get_channel(record["review_channel"]) or await bot.fetch_channel(record["review_channel"])
   message=await channel.fetch_message(record["review_message"])
   candidates.extend(attachment_candidates(message,original))
  except discord.NotFound:
   pass
  except discord.HTTPException as exc:
   logging.warning("Could not read review for proof recovery | submission_id=%s status=%s code=%s",
                   record["id"],exc.status,exc.code)
 for url in dict.fromkeys([*candidates,original]):
  try:
   return await archive_discord_proof(DBPATH,session,url)
  except (ProofUnavailable,aiohttp.ClientError,TimeoutError,EOFError):
   continue
 return None


async def restore_record_proof(record,session):
 ref=await recover_discord_proof(record,session)
 if not ref:
  return None
 try:
  if await store.replace_proof(record["id"],record["guild"],record["proof"],ref):
   return ref
 except Exception:
  archive_path(DBPATH,ref).unlink(missing_ok=True)
  raise
 archive_path(DBPATH,ref).unlink(missing_ok=True)
 return None


async def recover_existing_proofs():
 for guild in bot.guilds:
  records=await store.all(
   """SELECT * FROM submissions WHERE guild=? AND status IN
      ('approved','pending','awaiting_proof') AND proof IS NOT NULL""",(guild.id,)
  )
  candidates=[r for r in records if discord_attachment_key(r["proof"])]
  archived=set()
  gate=asyncio.Semaphore(3)
  async with aiohttp.ClientSession() as session:
   async def recover(record):
    async with gate:
     try:
      if await restore_record_proof(record,session):
       archived.add(record["id"])
     except (OSError,aiosqlite.Error):
      logging.exception("Unable to archive stored PB proof #%s",record["id"])
   try:
    async with asyncio.timeout(60):
     await asyncio.gather(*(recover(r) for r in candidates))
   except TimeoutError:
    logging.warning("PB proof backfill reached startup budget; other proofs can recover on demand")
  unresolved=[r["id"] for r in candidates if r["id"] not in archived]
  logging.info("PB proof backfill: guild=%s archived=%s unresolved=%s ids=%s",
               guild.id,len(archived),len(unresolved),unresolved)
  # The persistent board refresh will pick up the new proof references.
  # Re-render only affected live staff reviews, without sending new messages.
  for r in candidates:
   if r["id"] not in archived or r["status"] not in ("pending","awaiting_proof"):
    continue
   updated=await store.one("SELECT * FROM submissions WHERE guild=? AND id=?",(guild.id,r["id"]))
   if updated and updated["status"] in ("pending","awaiting_proof"):
    await update_review(updated)
    await asyncio.sleep(1.4)


async def refresh_proof_snapshot(guild_id,*,published=None):
 tracked=await store.snapshot_panel(guild_id)
 if not tracked:
  return True
 try:
  channel=bot.get_channel(tracked["channel"]) or await bot.fetch_channel(tracked["channel"])
  message=await channel.fetch_message(tracked["message"])
  leaders={activity:await store.raid_top3(guild_id,activity) for activity in RAIDS}
  recent=await store.recent_approved(guild_id,3)
  at=datetime.now(timezone.utc).timestamp()
  async with PUBLIC_PROOF_LOCK:
   for r in [*recent,*(r for rows in leaders.values() for r in rows)]:
    path=archive_path(DBPATH,r["proof"])
    if path and path.is_file() and proof_link_needs_refresh(PUBLIC_PROOF_URLS.get(r["proof"]),at):
     await cache_proof_from_review(r,path)
   messages=await publish_board_pages(channel,guild_id,SNAPSHOT_CONTINUATION_KEY,message,
                                      snapshot_page_plan(leaders,recent),discord.AllowedMentions.none())
  if published is not None:
   published.extend(messages)
  return True
 except discord.HTTPException:
  logging.exception("Unable to refresh PB proof links on the snapshot")
  return False


async def show_record_proof(i,sid):
 if i.guild_id is None or not 0<sid<2**63:
  return await i.response.send_message("PB not found.",ephemeral=True)
 # Acknowledge before waiting on SQLite or fetching an old attachment.
 await i.response.defer(ephemeral=True,thinking=True)
 try:
  record=await store.one("SELECT * FROM submissions WHERE guild=? AND id=?",(i.guild_id,sid))
 except aiosqlite.Error:
  logging.exception("Unable to look up proof for PB #%s",sid)
  return await i.followup.send("Proof is temporarily unavailable. Try again in a moment.",ephemeral=True)
 if not record or (record["status"]!="approved"
                   and record["submitter"]!=i.user.id and not is_staff(i)):
  return await i.followup.send("PB not found or not available to you.",ephemeral=True)
 proof=record["proof"]
 if not is_private_proof(proof):
  if proof and valid_url(proof):
   return await i.followup.send(f"Proof for PB #{sid}: <{proof}>",ephemeral=True)
  return await i.followup.send("No proof was saved for this PB.",ephemeral=True)
 if discord_attachment_key(proof):
  try:
   async with aiohttp.ClientSession() as session:
    proof=await restore_record_proof(record,session)
  except (OSError,aiosqlite.Error):
   logging.exception("On-demand recovery failed for PB #%s",sid)
   proof=None
  if not proof:
   current=await store.one("SELECT * FROM submissions WHERE guild=? AND id=?",(i.guild_id,sid))
   proof=current["proof"] if current else None
 file=archive_path(DBPATH,proof)
 if not file or not file.is_file():
  return await i.followup.send(
   "That original Discord upload could not be recovered. Use `/pbproof` to reattach it, or ask staff if it’s already approved.",
   ephemeral=True,
  )
 try:
  if file.stat().st_size>i.filesize_limit:
   return await i.followup.send("The archived proof is larger than this Discord server can send.",ephemeral=True)
  upload=discord.File(file,filename=f"PB-{sid}{file.suffix}")
  try:
   await i.followup.send(f"Proof for PB #{sid}",file=upload,ephemeral=True)
  finally:
   upload.close()
 except (discord.HTTPException,OSError):
  logging.exception("Unable to deliver proof for PB #%s",sid)
  await i.followup.send("Discord couldn’t deliver this proof. Try again in a moment.",ephemeral=True)


PROOF_BUTTON_ID=re.compile(r"pb:proof:view:([0-9]{1,19})")


class ProofButton(ui.Button):
 def __init__(self,sid,label="View Proof"):
  super().__init__(label=label,style=discord.ButtonStyle.secondary,
                   custom_id=f"pb:proof:view:{sid}")

 def is_dispatchable(self):
  # Discord sends the click normally. Route it in on_interaction rather
  # than registering it with the temporary view that rendered the button.
  return False


@bot.event
async def on_interaction(i):
 if i.type!=discord.InteractionType.component or not isinstance(i.data,dict):
  return
 if i.data.get("component_type")!=discord.ComponentType.button.value:
  return
 match=PROOF_BUTTON_ID.fullmatch(i.data.get("custom_id") or "")
 if not match or i.message is None or bot.user is None or i.message.author.id!=bot.user.id:
  return
 await show_record_proof(i,int(match[1]))

class BoardProofSelect(ui.DynamicItem[ui.Select],template=r"pb:proof:pick:(?P<part>[0-9]+)"):
 def __init__(self,index,options,pages=1):
  placeholder="View Proof · Select a PB"
  if pages>1:
   placeholder+=f" ({index+1}/{pages})"
  super().__init__(ui.Select(custom_id=f"pb:proof:pick:{index}",placeholder=placeholder,
                             options=options,min_values=1,max_values=1))

 @classmethod
 async def from_custom_id(cls,interaction,item,match):
  return cls(int(match["part"]),item.options)

 async def callback(self,i):
  values=self.item.values
  if len(values)!=1 or values[0] not in {option.value for option in self.item.options}:
   return await i.response.send_message("Select a PB from this list.",ephemeral=True)
  await show_record_proof(i,int(values[0]))


class BoardProofModal(LoggedModal,title="View PB Proof"):
 def __init__(self):
  super().__init__(timeout=300)
  self.number=ui.TextInput(label="PB submission number",max_length=19,placeholder="Number shown next to the PB")
  self.add_item(self.number)

 async def on_submit(self,i):
  number=self.number.value.strip()
  if not re.fullmatch(r"[0-9]{1,19}",number):
   return await i.response.send_message("Enter the PB number shown on the leaderboard.",ephemeral=True)
  await show_record_proof(i,int(number))


class BoardProofButton(ui.DynamicItem[ui.Button],template=r"pb:proof:board"):
 def __init__(self):
  super().__init__(ui.Button(label="View Proof",style=discord.ButtonStyle.secondary,
                            custom_id="pb:proof:board"))

 @classmethod
 async def from_custom_id(cls,interaction,item,match):
  return cls()

 async def callback(self,i):
  await i.response.send_modal(BoardProofModal())


@bot.tree.command(name="pbviewproof",description="Open the archived proof for a PB")
@app_commands.guild_only()
@app_commands.describe(submission_id="PB number shown next to the leaderboard record")
async def pbviewproof(i:discord.Interaction,submission_id:int):
 await show_record_proof(i,submission_id)


async def save_interaction_proof(i,record,proof):
 await i.response.defer(ephemeral=True,thinking=True)
 saved=proof
 if discord_attachment_key(proof):
  try:
   async with aiohttp.ClientSession() as session:
    saved=await archive_discord_proof(DBPATH,session,proof)
  except (ProofUnavailable,aiohttp.ClientError,TimeoutError,EOFError,OSError):
   return await i.followup.send(
    "Couldn’t archive that upload. Reattach a PNG, JPG, GIF, WebP, MP4, MOV or WebM "
    "file up to 20 MiB, or use a lasting video link.",ephemeral=True,
   )
 try:
  changed=await store.replace_proof(
   record["id"],record["guild"],record["proof"],saved,status=record["status"],
   reopen=record["status"]!="approved",
  )
 except aiosqlite.Error:
  if saved!=proof:
   archive_path(DBPATH,saved).unlink(missing_ok=True)
  logging.exception("Could not save proof for PB #%s",record["id"])
  return await i.followup.send("Couldn’t save that proof. Try again in a moment.",ephemeral=True)
 if not changed:
  if saved!=proof:
   archive_path(DBPATH,saved).unlink(missing_ok=True)
  return await i.followup.send("This PB changed while proof was uploading. Try again.",ephemeral=True)
 updated=await store.one("SELECT * FROM submissions WHERE id=? AND guild=?",(record["id"],record["guild"]))
 await i.followup.send(
  f"Proof saved for PB #{record['id']}."
  + (" The approval and ranking are unchanged." if record["status"]=="approved"
     else " Staff can review it now."),ephemeral=True,
 )
 if updated:
  await update_review(updated)
  if updated["status"]=="approved":
   await refresh_panel(i.guild_id,updated["activity"])


def qualifies_for_first_place(new_record, previous_leader, current_ranking):
 # Only announce a genuinely faster approved record, never a tie or a
 # position change caused solely by another submission being superseded.
 if not current_ranking or current_ranking[0]["id"]!=new_record["id"]:
  return False
 return previous_leader is None or new_record["ms"]<previous_leader["ms"]


def first_place_announcement(r, previous_leader, leaderboard_url=None):
 title=r["activity"]
 names=public_members(r)
 if previous_leader is None:
  comparison="-# First approved PB for this leaderboard."
 else:
  improvement=fmt(previous_leader["ms"]-r["ms"])
  comparison=f"-# Beat the previous #1 by **{improvement}**."
 lines=[
  f"## {podium_icon(1,r['guild'])} NEW NOVUS #1",
  f"**{clean(title)} · {clean(r['variant'])}**",
  f"## {fmt(r['ms'])}",
  names,
  comparison,
  f"-# PB achieved {pb_date_label(r['pb_date'])}",
 ]
 links=[]
 if evidence:=proof_text(r):
  links.append(evidence)
 if leaderboard_url:
  links.append(f"[View leaderboard]({leaderboard_url})")
 if links:
  lines.append(" · ".join(links))
 return "\n".join(lines)



class FirstPlaceAnnouncementCard(PBLayoutView):
 """Public #1 milestone, styled after the staff review card."""
 def __init__(self,record,previous_leader=None,leaderboard_url=None):
  super().__init__(timeout=900)
  panel=ui.Container(accent_colour=GOLD)

  badge=ui.Button(
   label="New #1",
   style=discord.ButtonStyle.success,
   disabled=True,
   custom_id=f"pb:newfirst:badge:{record['id']}"
  )
  panel.add_item(ui.Section(
   ui.TextDisplay(
    f"## {podium_icon(1,record['guild'])} NEW NOVUS #1\n"
    f"**{clean(record['activity'])} · {clean(record['variant'])}**\n"
    f"-# Achieved by {public_members(record)}"
   ),
   accessory=badge
  ))
  panel.add_item(ui.Separator())
  panel.add_item(ui.TextDisplay(
   "-# PB TIME\n"
   f"## {fmt(record['ms'])}\n"
   "-# PB DATE\n"
   f"**{clean(record['pb_date'] or 'Not recorded')}**"
  ))

  if previous_leader is None:
   comparison="-# First approved PB for this leaderboard."
  else:
   comparison=(
    f"-# Beat the previous #1 by **{fmt(previous_leader['ms']-record['ms'])}**."
   )
  panel.add_item(ui.TextDisplay(comparison))

  proof=record["proof"]
  # Avoid Discord unfurling direct proof images inside the public post.
  # Only the explicit View Proof link button should expose the evidence URL.
  links=ui.ActionRow()
  if is_private_proof(proof):
   links.add_item(ProofButton(record["id"]))
  elif proof and valid_url(proof):
   links.add_item(ui.Button(
    label="View Proof",
    style=discord.ButtonStyle.link,
    url=proof
   ))
  if leaderboard_url and valid_url(leaderboard_url):
   links.add_item(ui.Button(
    label="View Leaderboard",
    style=discord.ButtonStyle.link,
    url=leaderboard_url
   ))
  if links.children:
   panel.add_item(links)
  self.add_item(panel)


def count_first_place_slots(message,icon):
 view=ui.LayoutView.from_message(message)
 return sum(
  isinstance(item,ui.Section)
  and isinstance(item.accessory,ui.Button)
  and item.accessory.label=="NOVUS #1"
  and item.accessory.style==discord.ButtonStyle.success
  and any(line.startswith(f"{PB_FEATURED_HEADING}{icon} ") for child in item.children
          for line in child.content.splitlines())
  for item in view.walk_children()
 )


def count_runner_up_trophies(message,guild_id):
 lines=old_announcement_text(message).splitlines()
 return tuple(
  sum(line.startswith(podium_icon(rank,guild_id)+" ") for line in lines)
  for rank in (2,3)
 )


def old_announcement_text(message):
 """Extract text from a Discord Components V2 announcement, including containers."""
 chunks=[]
 def walk(item):
  content=getattr(item,"content",None)
  if isinstance(content,str):
   chunks.append(content)
  for child in getattr(item,"children",()) or ():
   walk(child)
 for component in getattr(message,"components",()) or ():
  walk(component)
 return "\n".join(chunks)


FIRST_TROPHY_HEADING=re.compile(r"(?m)^(## )(?:🏆|#1|:novuspb1:|<a?:novuspb1:[0-9]+>)(?= NEW NOVUS #1[ \t]*$)")


def replace_first_trophy_heading(content,icon):
 return FIRST_TROPHY_HEADING.sub(lambda match:match.group(1)+icon,content)


async def refresh_first_announcement_trophy_icons(guild_id):
 """Replace only the old trophy in bot-authored #1 posts, without reposting."""
 icon=podium_icon(1,guild_id)
 if not GENERAL_CHAT_ID or not bot.user or icon==PB_PODIUM_FALLBACK[1]:
  return
 op=f"first_announcements_novuspb1_v3_{guild_id}_{icon}"
 try:
  channel=bot.get_channel(GENERAL_CHAT_ID) or await bot.fetch_channel(GENERAL_CHAT_ID)
  if getattr(getattr(channel,"guild",None),"id",None)!=guild_id:
   return
  async with store.lock:
   await store.conn.execute("CREATE TABLE IF NOT EXISTS one_time_pb_migrations "
                            "(name TEXT PRIMARY KEY, applied TEXT NOT NULL)")
   await store.conn.commit()
  if await store.one("SELECT name FROM one_time_pb_migrations WHERE name=?",(op,)):
   return
  changed=0
  failed=0
  after=datetime(2026,11,6,tzinfo=timezone.utc)
  async for message in channel.history(limit=None,after=after):
   if message.author.id!=bot.user.id:
    continue
   try:
    old_view_text=old_announcement_text(message)
    if replace_first_trophy_heading(old_view_text,icon)!=old_view_text:
     view=ui.LayoutView.from_message(message,timeout=None)
     for item in view.walk_children():
      if isinstance(item,ui.TextDisplay):
       item.content=replace_first_trophy_heading(item.content,icon)
     saved=await message.edit(view=view,allowed_mentions=discord.AllowedMentions.none())
     verified=icon in old_announcement_text(saved)
    elif (updated:=replace_first_trophy_heading(message.content,icon))!=message.content:
     saved=await message.edit(content=updated,
                              allowed_mentions=discord.AllowedMentions.none())
     verified=icon in saved.content
    else:
     continue
    if verified:
     changed+=1
    else:
     failed+=1
    await asyncio.sleep(1.4)
   except (discord.HTTPException,ValueError):
    failed+=1
    logging.exception("Could not edit trophy on #1 announcement %s",message.id)
  if not failed:
   async with store.lock:
    await store.conn.execute("INSERT OR IGNORE INTO one_time_pb_migrations(name,applied) VALUES(?,?)",(op,now()))
    await store.conn.commit()
  logging.info("Novus #1 announcement trophy refresh complete: changed=%s failed=%s",changed,failed)
 except (discord.HTTPException,aiosqlite.Error):
  logging.exception("Could not refresh existing #1 announcement trophy icons")


def match_legacy_first_post(message,approved):
 """Only accept an unambiguously matched, old-style Novus #1 card."""
 text=old_announcement_text(message)
 if "NEW NOVUS #1" not in text or "PB TIME" in text:
  return None
 found=[]
 for record in approved:
  if (
   f"**{clean(record['activity'])} · {clean(record['variant'])}**" in text
   and f"## {fmt(record['ms'])}" in text
   and public_members(record) in text
   and f"PB achieved {pb_date_label(record['pb_date'])}" in text
  ):
   found.append(record)
 return found[0] if len(found)==1 else None


def previous_approved_leader(record,approved):
 """Recover the fastest earlier approval for the new card's comparison."""
 older=[
  candidate for candidate in approved
  if candidate["id"]!=record["id"]
  and candidate["category"]==record["category"]
  and candidate["activity"]==record["activity"]
  and candidate["variant"]==record["variant"]
  and candidate["updated"] < record["updated"]
 ]
 return min(older,key=lambda row:(row["ms"],row["id"])) if older else None


async def retrofit_existing_first_announcements():
 """One-off, targeted edit of the two latest legacy #1 posts in general-chat.

 The DB flag prevents future deployments from editing unrelated older posts.
 Skip any message that isn't bot-authored or cannot match a unique approved PB.
 """
 await asyncio.sleep(12)
 if not GENERAL_CHAT_ID or not bot.user:
  return
 op="review_style_first_announcements_20261111_two"
 try:
  async with store.lock:
   await store.conn.execute(
    "CREATE TABLE IF NOT EXISTS one_time_pb_migrations "
    "(name TEXT PRIMARY KEY, applied TEXT NOT NULL)"
   )
   await store.conn.commit()
  if await store.one(
   "SELECT name FROM one_time_pb_migrations WHERE name=?",(op,)
  ):
   return

  channel=bot.get_channel(GENERAL_CHAT_ID) or await bot.fetch_channel(GENERAL_CHAT_ID)
  guild=getattr(channel,"guild",None)
  if not guild or (GUILD_ID and guild.id!=GUILD_ID):
   logging.warning("Legacy #1 retrofit skipped: general-chat guild mismatch")
   return
  approved=await store.all(
   "SELECT * FROM submissions WHERE guild=? AND status='approved'",
   (guild.id,)
  )
  since=datetime.now(timezone.utc)-timedelta(hours=18)
  changed=0
  async for message in channel.history(limit=500,after=since):
   if message.author.id!=bot.user.id:
    continue
   record=match_legacy_first_post(message,approved)
   if record is None:
    continue
   previous=previous_approved_leader(record,approved)
   board=await store.boss_board(record["guild"],record["activity"])
   board_url=(
    f"https://discord.com/channels/{record['guild']}/{board['channel']}/{board['message']}"
    if board else None
   )
   await message.edit(
    view=FirstPlaceAnnouncementCard(record,previous,board_url),
    allowed_mentions=discord.AllowedMentions(users=True,roles=False,everyone=False)
   )
   changed+=1
   logging.info(
    "Updated existing Novus #1 announcement message=%s record=%s",
    message.id,record["id"]
   )
   if changed>=2:
    break

  async with store.lock:
   await store.conn.execute(
    "INSERT OR IGNORE INTO one_time_pb_migrations(name,applied) VALUES(?,?)",
    (op,now())
   )
   await store.conn.commit()
  logging.info("One-time Novus #1 announcement retrofit completed: %s/2",changed)
 except (discord.Forbidden,discord.HTTPException):
  logging.exception("Could not edit existing Novus #1 announcements")
 except Exception:
  logging.exception("Unexpected error editing existing Novus #1 announcements")



# These two message/record pairs were confirmed by earlier successful edit logs.
# No channel search, repost or broad historical edits.
EXISTING_FIRST_ANNOUNCEMENTS=(
 (1570203003233902657,34),
 (1570203078253093076,33),
)


def matches_first_announcement(message,record):
 """Safety check before replacing a Components V2 announcement."""
 if not record or record["status"]!="approved":
  return False
 body=old_announcement_text(message)
 return all((
  "NEW NOVUS #1" in body,
  f"**{clean(record['activity'])} · {clean(record['variant'])}**" in body,
  f"## {fmt(record['ms'])}" in body,
  public_members(record) in body,
 ))


async def remove_existing_first_announcement_galleries():
 """One-time, targeted in-place edit of exactly two existing general-chat posts.

 Keeps proof and leaderboard links, and never publishes new messages or
 changes any submitted PB data. Per-message markers allow safe retries.
 """
 await asyncio.sleep(8)
 if not GENERAL_CHAT_ID or not bot.user:
  return
 try:
  channel=bot.get_channel(GENERAL_CHAT_ID) or await bot.fetch_channel(GENERAL_CHAT_ID)
  guild=getattr(channel,"guild",None)
  if not guild or (GUILD_ID and guild.id!=GUILD_ID):
   logging.warning("Skipped #1 proof gallery cleanup: general-chat guild mismatch")
   return
  approved=await store.all(
   "SELECT * FROM submissions WHERE guild=? AND status='approved'",(guild.id,)
  )
  by_id={r["id"]:r for r in approved}
  async with store.lock:
   await store.conn.execute(
    "CREATE TABLE IF NOT EXISTS one_time_pb_migrations "
    "(name TEXT PRIMARY KEY, applied TEXT NOT NULL)"
   )
   await store.conn.commit()
  for message_id,record_id in EXISTING_FIRST_ANNOUNCEMENTS:
   op=f"no_inline_proof_preview_first_announcement_v2_{message_id}"
   if await store.one(
    "SELECT name FROM one_time_pb_migrations WHERE name=?",(op,)
   ):
    continue
   record=by_id.get(record_id)
   if record is None:
    logging.warning("Skipped existing #1 message %s: PB %s missing",message_id,record_id)
    continue
   try:
    message=await channel.fetch_message(message_id)
    if message.author.id!=bot.user.id or not matches_first_announcement(message,record):
     logging.warning(
      "Skipped existing #1 message %s: author/content didn't match approved PB %s",
      message_id,record_id
     )
     continue
    previous=previous_approved_leader(record,approved)
    board=await store.boss_board(record["guild"],record["activity"])
    url=(
     f"https://discord.com/channels/{record['guild']}/{board['channel']}/{board['message']}"
     if board else None
    )
    logging.info(
     "Before #1 preview cleanup message=%s: components=%s attachments=%s embeds=%s",
     message_id,
     [str(getattr(c,"type",type(c).__name__)) for c in message.components],
     len(message.attachments),len(message.embeds)
    )
    await message.edit(
     view=FirstPlaceAnnouncementCard(record,previous,url),
     content=None,
     embeds=[],
     attachments=[],
     suppress=True,
     allowed_mentions=discord.AllowedMentions(users=True,roles=False,everyone=False)
    )
    # Check Discord's actual stored message, not just the edit response.
    updated=await channel.fetch_message(message_id)
    def has_gallery(items):
     for item in items:
      if getattr(item,"type",None)==discord.ComponentType.media_gallery:
       return True
      if has_gallery(getattr(item,"children",()) or ()):
       return True
     return False
    if updated.attachments or updated.embeds or has_gallery(updated.components):
     logging.warning(
      "Proof preview not fully cleared message=%s: attachments=%s embeds=%s media_gallery=%s",
      message_id,len(updated.attachments),len(updated.embeds),has_gallery(updated.components)
     )
     # Leave the one-time operation eligible to run again if Discord retained media.
     continue
    logging.info(
     "Confirmed #1 announcement is preview-free message=%s: attachments=0 embeds=0 gallery=False",
     message_id
    )
    async with store.lock:
     await store.conn.execute(
      "INSERT OR IGNORE INTO one_time_pb_migrations(name,applied) VALUES(?,?)",
      (op,now())
     )
     await store.conn.commit()
    logging.info(
     "Removed inline proof preview from existing #1 announcement message=%s record=%s",
     message_id,record_id
    )
   except discord.HTTPException:
    logging.exception(
     "Failed to remove proof gallery from existing #1 message %s",message_id
    )
 except Exception:
  logging.exception("Unexpected error removing proof galleries from existing #1 posts")


async def announce_first_place(r, previous_leader):
 # Read the final approved standings, not just the review-card projection.
 standings=await store.ranking(r["guild"],r["category"],r["activity"],r["variant"])
 if not qualifies_for_first_place(r,previous_leader,standings):
  return False
 if not GENERAL_CHAT_ID or (GUILD_ID and r["guild"]!=GUILD_ID):
  return False
 try:
  channel=bot.get_channel(GENERAL_CHAT_ID) or await bot.fetch_channel(GENERAL_CHAT_ID)
  if getattr(getattr(channel,"guild",None),"id",None)!=r["guild"]:
   logging.error("PB general-chat channel does not belong to submission guild")
   return False
  board=await store.boss_board(r["guild"],r["activity"])
  url=(f"https://discord.com/channels/{r['guild']}/{board['channel']}/{board['message']}"
       if board else None)
  await channel.send(
   view=FirstPlaceAnnouncementCard(r,previous_leader,url),
   suppress_embeds=True,
   allowed_mentions=discord.AllowedMentions(users=True,roles=False,everyone=False)
  )
  logging.info("Announced new Novus #1 submission %s in general chat",r["id"])
  return True
 except (discord.Forbidden,discord.HTTPException):
  logging.exception("Unable to post new Novus #1 PB %s to general chat",r["id"])
  return False


async def decide(i,sid,choice):
 if not is_staff(i):return await i.response.send_message("Staff only.",ephemeral=True)
 r=await store.one("SELECT * FROM submissions WHERE id=?",(sid,))
 if not r or r["guild"]!=i.guild_id:return await i.response.send_message("Record not found.",ephemeral=True)
 if r["status"] not in ("pending","awaiting_proof"):return await i.response.send_message("Already resolved.",ephemeral=True)
 if choice=="approve":
  saved=archive_path(DBPATH,r["proof"])
  if (not r["proof"] or discord_attachment_key(r["proof"])
      or (saved is not None and not saved.is_file())):
   return await i.response.send_message(
    f"Proof is missing or expired. Try `/pbviewproof {sid}` to recover it, or use Request Proof for a new upload.",
    ephemeral=True,
   )
 if choice=="request" and r["status"]=="awaiting_proof":
  return await i.response.send_message("Already awaiting proof.",ephemeral=True)
 previous_leader=(await store.best_approved(i.guild_id,r["category"],r["activity"],r["variant"])) if choice=="approve" else None
 target={"approve":"approved","deny":"denied","request":"awaiting_proof"}[choice]
 if not await store.change(sid,r["status"],target,i.user.id):
  return await i.response.send_message("Another reviewer updated this record.",ephemeral=True)
 updated=await store.one("SELECT * FROM submissions WHERE id=?",(sid,))
 await i.response.edit_message(view=await Review.make(updated),allowed_mentions=discord.AllowedMentions.none())
 if choice=="approve":
  await refresh_panel(i.guild_id,updated["activity"])
  try:
   await announce_first_place(updated,previous_leader)
  except Exception:
   logging.exception("Unexpected failure announcing Novus PB #%s",sid)
 if choice=="request":
  try:
   user=await bot.fetch_user(r["submitter"])
   await user.send(f"Your Novus PB #{sid} needs proof. Use /pbproof with submission_id {sid} in the Novus server.")
  except discord.HTTPException:logging.warning("Unable to DM submission owner")

class ProofUploadModal(LoggedModal,title="NOVUS | PB Proof"):
 def __init__(self,sid):
  super().__init__(timeout=300)
  self.sid=sid
  self.proof_upload=ui.FileUpload(
   custom_id="pb:proof:upload",
   required=True,
   min_values=1,
   max_values=1
  )
  self.add_item(ui.Label(
   text="Proof",
   description="Upload a screenshot or video clip of the PB.",
   component=self.proof_upload
  ))

 async def on_submit(self,i):
  r=await store.one(
   "SELECT * FROM submissions WHERE id=? AND guild=?",
   (self.sid,i.guild_id)
  )
  if not r or (r["submitter"]!=i.user.id and not is_staff(i)):
   return await i.response.send_message("PB not found or not yours.",ephemeral=True)
  if r["status"] not in ("pending","awaiting_proof"):
   return await i.response.send_message("PB is already resolved.",ephemeral=True)

  attachment=self.proof_upload.values[0]
  if not (attachment.content_type or "").startswith(("image/","video/")):
   return await i.response.send_message("Only image/video files accepted.",ephemeral=True)

  if not discord_attachment_key(attachment.url):
   return await i.response.send_message("Discord did not return a valid proof attachment URL.",ephemeral=True)

  await save_interaction_proof(i,r,attachment.url)


class ProofUploadView(LoggedView):
 def __init__(self,sid):
  super().__init__(timeout=900)
  self.sid=sid
  attach=ui.Button(
   label="Attach Proof",
   style=discord.ButtonStyle.primary,
   custom_id=f"pb:proof:button:{sid}"
  )
  async def attach_clicked(i):
   await i.response.send_modal(ProofUploadModal(self.sid))
  attach.callback=attach_clicked
  self.add_item(attach)


async def submit(i,cat,act,variant,t,pb_date,members,proof=""):
 try:ms=parse_ms(t)
 except ValueError as e:return await i.response.send_message(str(e),ephemeral=True)

 raw_date=pb_date.strip()
 parsed_date=None
 for date_format in ("%m-%d-%Y","%Y-%m-%d","%m/%d/%Y"):
  try:
   parsed_date=datetime.strptime(raw_date,date_format).date()
   break
  except ValueError:
   pass
 if parsed_date is None:
  return await i.response.send_message(
   "Enter the PB date as MM/DD/YYYY or MM-DD-YYYY.",
   ephemeral=True
  )
 if not pb_date_eligible(parsed_date):
  return await i.response.send_message(
   "Only PBs achieved on or after **November 7, 2026** are eligible.",
   ephemeral=True
  )
 if parsed_date>datetime.now(timezone.utc).date():
  return await i.response.send_message(
   "The PB date cannot be in the future.",
   ephemeral=True
  )
 normalized_date=parsed_date.isoformat()

 required=team_size(cat,variant,act)
 if not members:
  return await i.response.send_message("Select at least one party member.",ephemeral=True)
 if cat=="Bosses" and act in NON_SOLO_BOSSES and len(members)>2:
  return await i.response.send_message(
   f"Select no more than two Discord members for **{act}**.",
   ephemeral=True
  )
 if required is not None and len(members)!=required:
  return await i.response.send_message(f"Expected {required} Discord member(s) for this category.",ephemeral=True)
 if len({m.id for m in members})!=len(members):
  return await i.response.send_message("Each party member can only be selected once.",ephemeral=True)
 if proof and not valid_url(proof):
  return await i.response.send_message("Provide an HTTP(S) proof link, or add proof later with /pbproof.",ephemeral=True)

 try:ch=bot.get_channel(REVIEW_ID) or await bot.fetch_channel(REVIEW_ID)
 except discord.HTTPException:return await i.response.send_message("Staff review channel not available.",ephemeral=True)

 await i.response.defer(thinking=True,ephemeral=True)
 if discord_attachment_key(proof):
  try:
   async with aiohttp.ClientSession() as session:
    proof=await archive_discord_proof(DBPATH,session,proof)
  except (ProofUnavailable,aiohttp.ClientError,TimeoutError,EOFError,OSError):
   return await i.followup.send("Couldn’t archive that upload. Reattach the original proof.",ephemeral=True)
 names=", ".join(m.display_name for m in members)
 member_ids=",".join(str(m.id) for m in members)
 sid=await store.add(i.guild_id,i.user.id,cat,act,variant,names,member_ids,ms,normalized_date,proof)
 r=await store.one("SELECT * FROM submissions WHERE id=?",(sid,))
 try:
  message=await ch.send(view=await Review.make(r),allowed_mentions=discord.AllowedMentions.none())
  await store.attach_review(sid,ch.id,message.id)
 except discord.HTTPException:
  logging.exception("Staff review message failed")
  return await i.followup.send(f"Submission #{sid} saved, but staff delivery failed. Notify staff.",ephemeral=True)
 await i.followup.send(
  f"PB #{sid} submitted for staff review for **{normalized_date}**. "
  "The PB must have been achieved after you joined Novus; for group PBs, every listed player "
  "must also have been a Novus member on that date. "
  + ("Proof received." if proof else "Attach proof below, or use /pbproof later."),
  ephemeral=True,
  view=None if proof else ProofUploadView(sid)
 )

def submission_target_options():
 options=[
  discord.SelectOption(
   label=activity,
   value=f"raid|||{activity}"
  )
  for activity in RAIDS
 ]
 options.append(
  discord.SelectOption(
   label="Boss PB",
   value="boss"
  )
 )
 return options

def submission_format_options():
 seen=set()
 options=[
  discord.SelectOption(
   label="Select if non-DT2 boss",
   value="__none__"
  )
 ]
 for variants in RAIDS.values():
  for variant in variants:
   if variant in seen:
    continue
   seen.add(variant)
   options.append(
    discord.SelectOption(label=variant,value=variant)
   )
 for mode in ("Normal","Awakened"):
  if mode not in seen:
   options.append(
    discord.SelectOption(label=mode,value=mode)
   )
 return options

def boss_submission_options():
 return [
  discord.SelectOption(label="Select if Raid PB",value="__none__"),
  *[
   discord.SelectOption(label=activity,value=activity)
   for activity in BOSSES
  ]
 ]

def parse_submission_details(raw):
 match=re.fullmatch(
  r"\s*(.+?)\s*[|,]\s*(\d{1,2}-\d{1,2}-\d{4}|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{4})\s*",
  raw.strip()
 )
 if not match:
  return None
 time_value,pb_date=match.groups()
 return pb_date,time_value

class SubmitModal(LoggedModal,title="NOVUS | PB Submission"):
 def __init__(self):
  super().__init__(timeout=300)

  self.target_select=ui.Select(
   placeholder="Choose PB type",
   options=submission_target_options(),
   min_values=1,
   max_values=1,
   required=True,
   custom_id="pb:submit:target"
  )
  self.format_select=ui.Select(
   placeholder="Choose raid format / DT2 mode",
   options=submission_format_options(),
   min_values=1,
   max_values=1,
   required=True,
   custom_id="pb:submit:format"
  )
  self.boss_select=ui.Select(
   placeholder="Choose boss if submitting a boss PB",
   options=boss_submission_options(),
   min_values=1,
   max_values=1,
   required=True,
   custom_id="pb:submit:boss"
  )
  self.details_input=ui.TextInput(
   placeholder="30.60 | 11/08/2026",
   max_length=40,
   custom_id="pb:submit:details"
  )
  self.members_select=ui.UserSelect(
   placeholder="Search Discord members",
   min_values=1,
   max_values=8,
   custom_id="pb:submit:members"
  )

  self.add_item(ui.Label(
   text="What are you submitting?",
   description="Choose one of the four raids or Boss PB.",
   component=self.target_select
  ))
  self.add_item(ui.Label(
   text="Raid format / boss mode",
   description="Pick a raid format, Normal/Awakened for DT2, or the first option for any other boss.",
   component=self.format_select
  ))
  self.add_item(ui.Label(
   text="Boss",
   description="Only used when Boss PB is selected above.",
   component=self.boss_select
  ))
  self.add_item(ui.Label(
   text="PB time & date",
   description="Use SS.ss or MM:SS.ss | MM/DD/YYYY or MM-DD-YYYY.",
   component=self.details_input
  ))
  self.add_item(ui.Label(
   text="Party members",
   description="Select every Novus member in the run.",
   component=self.members_select
  ))

 async def on_submit(self,i):
  target=self.target_select.values[0]
  selected_format=self.format_select.values[0]
  selected_boss=self.boss_select.values[0]

  if target.startswith("raid|||"):
   category="Raids"
   _,activity=target.split("|||",1)
   selected_format=canonical_raid_variant(category,activity,selected_format)
   if selected_format not in RAIDS[activity]:
    valid=" · ".join(RAIDS[activity])
    return await i.response.send_message(
     f"Choose a valid format for **{activity}**: {valid}.",
     ephemeral=True
    )
   variant=selected_format
  else:
   category="Bosses"
   if selected_boss=="__none__":
    return await i.response.send_message(
     "Choose which boss the PB is for.",
     ephemeral=True
    )
   activity=selected_boss
   valid_modes=BOSSES[activity]
   if len(valid_modes)==1:
    variant=valid_modes[0]
   elif selected_format not in valid_modes:
    return await i.response.send_message(
     f"Choose **Normal** or **Awakened** for **{activity}**.",
     ephemeral=True
    )
   else:
    variant=selected_format

  parsed=parse_submission_details(self.details_input.value)
  if not parsed:
   return await i.response.send_message(
    "Use **SS.ss or MM:SS.ss | MM/DD/YYYY or MM-DD-YYYY**.",
    ephemeral=True
   )

  pb_date,time_value=parsed
  await submit(
   i,
   category,
   activity,
   variant,
   time_value,
   pb_date,
   self.members_select.values,
  )


@bot.tree.command(name="pbsubmit",description="Submit an eligible Novus PB for staff review")
@app_commands.guild_only()
async def pbsubmit(i:discord.Interaction):
 await i.response.send_modal(SubmitModal())

@bot.tree.command(name="mypbs",description="View your Novus PB points, placements and approved records")
@app_commands.guild_only()
async def mypbs(i:discord.Interaction):
 await show_my_pbs(i)

@bot.tree.command(name="pbproof",description="Attach PB proof or let staff replace proof on an approved PB")
@app_commands.guild_only()
@app_commands.describe(submission_id="Your PB submission number",url="Proof URL (optional)",attachment="Screenshot or video clip (optional)")
async def pbproof(i:discord.Interaction,submission_id:int,url:str="",attachment:discord.Attachment=None):
 r=await store.one("SELECT * FROM submissions WHERE id=? AND guild=?",(submission_id,i.guild_id))
 if not r or (r["submitter"]!=i.user.id and not is_staff(i)):
  return await i.response.send_message("PB not found or not yours.",ephemeral=True)
 if r["status"]=="approved" and not is_staff(i):
  return await i.response.send_message("Ask staff to replace proof for an approved PB.",ephemeral=True)
 if r["status"] not in ("pending","awaiting_proof","approved"):
  return await i.response.send_message("PB is already resolved.",ephemeral=True)
 proof=attachment.url if attachment else url.strip()
 if not valid_url(proof):return await i.response.send_message("Provide a valid URL or attach an image/video.",ephemeral=True)
 if attachment and not (attachment.content_type or "").startswith(("image/","video/")):
  return await i.response.send_message("Only image/video files accepted.",ephemeral=True)
 if attachment and not discord_attachment_key(proof):
  return await i.response.send_message("Discord did not return a valid proof attachment URL.",ephemeral=True)
 await save_interaction_proof(i,r,proof)

@bot.tree.command(name="pbclear",description="Clear all Novus PB submissions and leaderboard records")
@app_commands.guild_only()
async def pbclear(i:discord.Interaction):
 if not is_council_or_founder(i):
  return await i.response.send_message("Council or Founder only.",ephemeral=True)

 await i.response.defer(ephemeral=True,thinking=True)
 rows=await store.all(
  "SELECT review_channel,review_message FROM submissions WHERE guild=?",
  (i.guild_id,)
 )
 deleted_cards=0
 for r in rows:
  if not r["review_channel"] or not r["review_message"]:
   continue
  try:
   ch=bot.get_channel(r["review_channel"]) or await bot.fetch_channel(r["review_channel"])
   msg=await ch.fetch_message(r["review_message"])
   await msg.delete()
   deleted_cards+=1
  except discord.NotFound:
   pass
  except discord.HTTPException as exc:
   logging.warning("Could not delete an old PB card | guild_id=%s message_id=%s status=%s code=%s",
                   i.guild_id,r["review_message"],exc.status,exc.code)

 await store.clear_guild(i.guild_id)
 await refresh_panel(i.guild_id)
 await i.followup.send(
  f"Cleared **{len(rows)}** PB submission(s). "
  f"Removed **{deleted_cards}** staff review card(s). "
  "The PB database is back to **0 submissions**.",
  ephemeral=True
 )

async def post_current_pb_board(channel,guild_id):
 """Post exactly the same public Home panel shown at the top of #pb-leaderboard.

 Reuse Home.make so banner, points layout, and action buttons remain identical.
 This is a one-message snapshot and does not alter persistent panel tracking.
 """
 message=await channel.send(
  view=await Home.make(guild_id),
  allowed_mentions=discord.AllowedMentions.none()
 )
 return message,1


@bot.tree.command(name="pbpost",description="Post the Novus PB banner and Top 10 panel in this channel")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def pbpost(i:discord.Interaction):
 if not is_staff(i):
  return await i.response.send_message("Staff only.",ephemeral=True)
 if not i.channel or not hasattr(i.channel,"send"):
  return await i.response.send_message(
   "Use this in a text channel where I can post the board.",ephemeral=True
  )
 me=i.guild.me
 perms=i.channel.permissions_for(me) if me else None
 if not perms or not perms.view_channel or not perms.send_messages or not perms.embed_links:
  return await i.response.send_message(
   "Novus PB needs View Channel, Send Messages, and Embed Links here.",
   ephemeral=True
  )
 await i.response.defer(ephemeral=True)
 try:
  main,_=await post_current_pb_board(i.channel,i.guild_id)
  await i.followup.send(
   f"Posted the Novus PB Top 10: {main.jump_url}\n"
   "-# This is a snapshot; the main PB leaderboard remains unchanged.",
   ephemeral=True
  )
 except (discord.Forbidden,discord.HTTPException):
  logging.exception("Could not post PB board copy in channel %s",i.channel_id)
  await i.followup.send(
   "Discord could not post the PB board here. Check Novus PB permissions and try again.",
   ephemeral=True
  )
 except Exception:
  logging.exception("PB board copy failed in channel %s",i.channel_id)
  await i.followup.send(
   "Could not post the PB board copy. Check the PB bot logs.",
   ephemeral=True
  )


@bot.tree.command(name="pbpanel",description="Create or refresh the persistent PB panel in this channel")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def pbpanel(i:discord.Interaction):
 if not is_staff(i):return await i.response.send_message("Staff only.",ephemeral=True)
 await i.response.defer(ephemeral=True)
 await fetch_activity_emojis(i.guild)
 me=i.guild.me
 perms=i.channel.permissions_for(me) if me else None
 if not perms or not perms.view_channel or not perms.send_messages:
  return await i.followup.send(
   "I need **View Channel** and **Send Messages** in this channel before I can create the PB panel.",
   ephemeral=True
  )

 # Remove the currently tracked PB board messages before recreating them.
 tracked_records=[
  await store.banner(i.guild_id),
  await store.panel(i.guild_id),
  await store.boss_panel(i.guild_id),
  await store.snapshot_panel(i.guild_id),
  await store.footer(i.guild_id),
 ]
 tracked_records.extend(
  await store.boss_boards(i.guild_id)
 )
 tracked_records.extend(await store.raid_pages(i.guild_id))

 seen_messages=set()
 for record in tracked_records:
  if not record:
   continue
  key=(record["channel"],record["message"])
  if key in seen_messages:
   continue
  seen_messages.add(key)
  try:
   ch=bot.get_channel(record["channel"]) or await bot.fetch_channel(record["channel"])
   msg=await ch.fetch_message(record["message"])
   await msg.delete()
  except discord.NotFound:
   pass
  except discord.HTTPException as exc:
   logging.warning("Could not delete an old PB card | guild_id=%s message_id=%s status=%s code=%s",
                   i.guild_id,record["message"],exc.status,exc.code)

 await store.clear_boss_boards(i.guild_id)

 try:
  panel_msg=await i.channel.send(view=await Home.make(i.guild_id))

  # Post a temporary directory first so it stays above the leaderboards.
  boss_msg=await i.channel.send(view=BossDirectory())

  jump_links={}

  # Minigames first.
  for activity in ("Inferno","Fortis Colosseum","Fight Caves"):
   board_msg=await write_linked_board(i.channel,i.guild_id,activity)
   await store.set_boss_board(
    i.guild_id,
    activity,
    i.channel_id,
    board_msg.id
   )
   jump_links[activity]=board_msg.jump_url

  # Then the four raid boards, each showing Top 1-5 per format.
  for activity in RAIDS:
   board_msg=await write_linked_board(i.channel,i.guild_id,activity)
   await store.set_boss_board(
    i.guild_id,
    activity,
    i.channel_id,
    board_msg.id
   )
   jump_links[activity]=board_msg.jump_url

  # Then the remaining bosses beginning with Alchemical Hydra.
  for activity in BOSSES:
   if activity in ("Inferno","Fortis Colosseum","Fight Caves"):
    continue
   board_msg=await write_linked_board(i.channel,i.guild_id,activity)
   await store.set_boss_board(
    i.guild_id,
    activity,
    i.channel_id,
    board_msg.id
   )
   jump_links[activity]=board_msg.jump_url

  # Turn each boss name into a native Discord jump link.
  await boss_msg.edit(
   view=BossDirectory(jump_links),
   allowed_mentions=discord.AllowedMentions.none()
  )

  async with PUBLIC_PROOF_LOCK:
   leaders={raid:await store.raid_top3(i.guild_id,raid) for raid in RAIDS}
   recent=await store.recent_approved(i.guild_id,3)
   snapshot_msg=(await publish_board_pages(
    i.channel,i.guild_id,SNAPSHOT_CONTINUATION_KEY,None,
    snapshot_page_plan(leaders,recent),discord.AllowedMentions.none()
   ))[0]
  footer_msg=await i.channel.send(
   pb_scroll_to_top_text(panel_msg.jump_url),
   allowed_mentions=discord.AllowedMentions.none()
  )

  await store.set_panel(i.guild_id,i.channel_id,panel_msg.id)
  await store.set_boss_panel(i.guild_id,i.channel_id,boss_msg.id)
  await store.set_snapshot_panel(i.guild_id,i.channel_id,snapshot_msg.id)
  await store.set_footer(i.guild_id,i.channel_id,footer_msg.id)
  async with store.lock:
   await store.conn.execute("DELETE FROM panel_banners WHERE guild=?",(i.guild_id,))
   await store.conn.commit()
  await i.followup.send(
   "Refreshed the Novus PB board with jump-linked raid and boss leaderboards.",
   ephemeral=True
  )
 except discord.Forbidden:
  logging.exception("PB panel send forbidden in channel %s",i.channel_id)
  await i.followup.send(
   "Discord blocked the PB board post. Give Novus PB **View Channel**, **Send Messages**, and **Read Message History** here, then run /pbpanel again.",
   ephemeral=True
  )
 except Exception as e:
  logging.exception("PB panel creation failed")
  await i.followup.send(
   f"PB panel creation failed: `{type(e).__name__}`. Check the Railway log for the matching traceback.",
   ephemeral=True
  )

if __name__=="__main__":
 configure_logging("Novus PB Board")
 if not TOKEN:raise RuntimeError("DISCORD_TOKEN environment variable is required")
 if not REVIEW_ID:raise RuntimeError("REVIEW_CHANNEL_ID environment variable is required")
 bot.run(TOKEN,log_handler=None)
