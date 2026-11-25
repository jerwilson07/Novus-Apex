"""Novus Apex diary: read-only progression and existing MEE6 ticket preview.

Approved PB records are the evidence source. Eligibility remains provisional
until the verified WOM membership bridge and approval workflow are available.
No rank is granted by this module.
"""
import asyncio
import logging
import json
import hashlib
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

GUILD = 1532817291048124466
SYNC = 1544463223535833148
BRIDGE_PARENT = 1532825723511767246
BRIDGE_THREAD = "Apex verification • internal"
BRIDGE_MARKER = "Novus Sync • Apex membership verification v1"
BRIDGE_FILENAME = "apex-membership.json"
CATEGORY = 1560643971623944203
PANEL = "https://discord.com/channels/1532817291048124466/1533481323883401216/1565518684263293022"
STAFF = {1532829195657285843, 1532830356875513906, 1532830960062693396, 1533587986204069908}
RANKS = ("Challenger", "Vanguard", "Paragon")
COLORS = (0xC7CED8, 0x60C7F3, 0xFF626A)
# Exactly the ten published diary challenges; no HMT, ordinary CoX, or other raid sizes.
CHALLENGES = (
    ("ToB Duo","tob","Raids","Theatre of Blood","Duo",2,("25:00","24:00","23:00")),
    ("ToB Trio","tob","Raids","Theatre of Blood","Trio",3,("17:15","16:45","16:15")),
    ("ToB 4-man","tob","Raids","Theatre of Blood","4-Man",4,("14:45","14:30","14:15")),
    ("ToB 5-man","tob","Raids","Theatre of Blood","5-Man",5,("14:00","13:45","13:30")),
    ("CM Solo","cm","Raids","Chambers of Xeric","Challenge Mode · Solo",1,("35:00","32:00","29:00")),
    ("CM Trio","cm","Raids","Chambers of Xeric","Challenge Mode · Trio",3,("26:00","24:30","23:00")),
    ("CM 5-man","cm","Raids","Chambers of Xeric","Challenge Mode · 5-Man",5,("24:00","22:30","21:00")),
    ("ToA 300 Solo","toa","Raids","Tombs of Amascut","Expert · Solo",1,("20:45","20:15","19:45")),
    ("Inferno","solo","Bosses","Inferno","Standard",1,("61:30","59:00","57:30")),
    ("Colosseum","solo","Bosses","Fortis Colosseum","Standard",1,("21:30","20:00","18:30")),
)
RULES = ((1,1,1,1),(2,2,1,2),(4,3,1,2))
GROUPS = ("tob","cm","toa","solo")
GROUP_LABELS = ("ToB","CoX CM","ToA","Inferno / Colo")
logger = logging.getLogger("Novus Apex")

def threshold(value):
    minutes,seconds=map(int,value.split(":"))
    return (minutes*60+seconds)*1000

def participant_ids(row):
    values=[v.strip() for v in (row["member_ids"] or "").split(",") if v.strip()]
    if not values or any(not v.isdigit() or int(v)<=0 for v in values):
        return None
    result={int(v) for v in values}
    return result if len(result)==len(values) else None

def best_candidates(rows, member_id, today=None):
    """Read approved, dated, proof-bearing, complete-roster records only."""
    today=today or datetime.now(timezone.utc).date()
    result={}
    index={(c[2],c[3],c[4]):c for c in CHALLENGES}
    for row in rows:
        if row["status"]!="approved" or not row["proof"] or not row["reviewer"]:
            continue
        c=index.get((row["category"],row["activity"],row["variant"]))
        if c is None:
            continue
        ids=participant_ids(row)
        if not ids or member_id not in ids or len(ids)!=c[5]:
            continue
        try:
            run_date=date.fromisoformat(row["pb_date"])
            ms=int(row["ms"])
        except (TypeError,ValueError):
            continue
        if run_date>today or ms<=0:
            continue
        previous=result.get(c[0])
        if previous is None or (ms,row["id"])<(previous["ms"],previous["id"]):
            result[c[0]]=row
    return result

class DiaryEvidence(dict):
    def __init__(self,candidates,verified=None,membership=None,snapshot_available=False,award=None):
        super().__init__(candidates)
        self.verified=verified
        self.membership=membership
        self.snapshot_available=snapshot_available
        self.award=award

def verified_run(row,members):
    try:
        run=date.fromisoformat(row["pb_date"])
        ids=participant_ids(row)
        return bool(ids and all(str(i) in members and run>=date.fromisoformat(members[str(i)]["joined"]) for i in ids))
    except (TypeError,ValueError,KeyError):
        return False

def record_fingerprint(row):
    return hashlib.sha256(json.dumps([row[k] for k in ("id","guild","category","activity","variant","member_ids","ms","pb_date","proof","reviewer")],separators=(",",":")).encode()).hexdigest()

def eligible_record(row,member_id,snapshot,attestation=None):
    try:
        run=date.fromisoformat(row["pb_date"])
        member=snapshot.get(str(member_id))
        if not member or run<date.fromisoformat(member["joined"]):
            return False
        attestation=attestation or {}
        if not verified_run(row,snapshot) and not attestation.get("team_verified"):
            return False
        if row["activity"]=="Tombs of Amascut" and not attestation.get("invocation_300"):
            return False
        return True
    except (TypeError,ValueError,KeyError):
        return False

def rank_ready(best,tier):
    if not getattr(best,"snapshot_available",False) or not getattr(best,"membership",None):
        return False
    eligible=best.membership.get("tenure_eligible")
    if not eligible or datetime.now(ZoneInfo("America/New_York")).date()<date.fromisoformat(eligible):
        return False
    _,done,total=progress(best,tier)
    return done==total

def progress(best,tier):
    # When the bridge is unavailable, these are provisional time goals only.
    if getattr(best,"verified",None) is not None:
        best=best.verified
    counts={k:0 for k in GROUPS}
    for name,group,_,_,_,_,times in CHALLENGES:
        row=best.get(name)
        if row and row["ms"]<=threshold(times[tier]):
            counts[group]+=1
    caps=RULES[tier]
    return counts,sum(min(counts[g],cap) for g,cap in zip(GROUPS,caps)),sum(caps)

def member_role(member):
    names={role.name.strip().casefold() for role in getattr(member,"roles",())}
    return not member.bot and "member" in names and not names.intersection({"guest","clan friend"})

def staff(member):
    return isinstance(member,discord.Member) and (bool(STAFF.intersection(r.id for r in member.roles)) or member.guild_permissions.administrator)

def rank_emoji(guild,tier):
    name=RANKS[tier].casefold()
    candidates={name,"novus"+name,"novus_"+name,"pvm_"+name,"pvm"+name}
    matches=[e for e in guild.emojis if e.name.casefold() in candidates and e.is_usable()]
    if matches:
        return str(sorted(matches,key=lambda e:(e.name.casefold()!=name,e.id))[0])+" "
    return ""

class VerifyExceptionModal(ui.Modal,title="Verify Diary Evidence"):
    pb_id=ui.TextInput(label="Apex PB submission ID",placeholder="ID from the proof picker",max_length=19)
    invocation=ui.TextInput(label="ToA invocation (only if required)",placeholder="300",required=False,max_length=3)
    historical=ui.TextInput(label="Historical team membership evidence",placeholder="WOM dates / source link, only if automatic check fails",style=discord.TextStyle.paragraph,required=False,max_length=500)
    def __init__(self,view):
        super().__init__(timeout=300)
        self.view=view
    async def on_submit(self,i):
        await self.view.cog.verify_exception(i,self.view.member_id,self.view.tier,self.pb_id.value,self.invocation.value,self.historical.value)

class DiaryView(ui.View):
    def __init__(self,cog,member_id,best,tier=0,*,ticket=False):
        super().__init__(timeout=None if ticket else 900)
        self.cog,self.member_id,self.ticket=cog,member_id,ticket
        self.tier=tier
        self.tier_select=ui.Select(
            placeholder="Select Rank",custom_id=f"diary:tier:{member_id}",
            options=[discord.SelectOption(label=rank,value=str(i),default=i==tier) for i,rank in enumerate(RANKS)],
            row=0)
        self.tier_select.callback=self.select_tier
        self.add_item(self.tier_select)
        self.refresh=ui.Button(label="Refresh Diary",style=discord.ButtonStyle.secondary,custom_id=f"diary:refresh:{member_id}:{tier}",row=1)
        self.refresh.callback=self.refresh_diary
        self.add_item(self.refresh)
        if not ticket:
            self.add_item(ui.Button(label="Open Review Ticket",style=discord.ButtonStyle.link,url=PANEL,row=1))
        if ticket:
            self.verify=ui.Button(label="Verify Exceptions",style=discord.ButtonStyle.primary,custom_id=f"diary:verify:{member_id}:{tier}",row=1)
            self.verify.callback=self.verify_clicked
            self.add_item(self.verify)
            self.approve=ui.Button(label="Approve Diary",style=discord.ButtonStyle.success,custom_id=f"diary:approve:{member_id}:{tier}",row=1,disabled=not rank_ready(best,tier) or bool(best.award is not None and best.award>=tier))
            self.approve.callback=self.approve_clicked
            self.add_item(self.approve)
        # The proof picker is a durable fallback, even if a signed link expires.
        fallback=best
        if fallback:
            self.proof=ui.Select(placeholder="View Proof",custom_id=f"diary:proof:{member_id}:{tier}",row=2,
                options=[discord.SelectOption(label=name,description=f"PB #{record['id']}",value=str(record["id"])) for name,record in fallback.items()])
            self.proof.callback=self.view_proof
            self.add_item(self.proof)
    async def interaction_check(self,i):
        if i.guild_id==GUILD and ((i.user.id==self.member_id and member_role(i.user)) or staff(i.user)):
            if self.ticket and not await self.cog.ticket_applicant(i.channel)==self.member_id:
                await i.response.send_message("Ticket ownership could not be confirmed.",ephemeral=True)
                return False
            return True
        await i.response.send_message("This diary belongs to another member.",ephemeral=True)
        return False
    async def verify_clicked(self,i):
        if not staff(i.user):
            return await i.response.send_message("Staff only.",ephemeral=True)
        await i.response.send_modal(VerifyExceptionModal(self))
    async def approve_clicked(self,i):
        if not staff(i.user):
            return await i.response.send_message("Staff only.",ephemeral=True)
        await self.cog.approve_rank(i,self.member_id,self.tier)
    async def select_tier(self,i):
        await self.update(i,int(self.tier_select.values[0]))
    async def refresh_diary(self,i):
        await self.update(i,self.tier)
    async def update(self,i,tier):
        await i.response.defer()
        if self.ticket and i.message:
            async with self.cog.locks.setdefault(i.channel_id,asyncio.Lock()):
                if await self.cog.ticket_applicant(i.channel)!=self.member_id:
                    return await i.followup.send("Ticket ownership could not be confirmed.",ephemeral=True)
                best=await self.cog.best(self.member_id)
                if await self.cog.ticket_applicant(i.channel)!=self.member_id:
                    return await i.followup.send("Ticket ownership could not be confirmed.",ephemeral=True)
                await i.edit_original_response(embed=self.cog.embed(i.guild,self.member_id,best,tier),view=DiaryView(self.cog,self.member_id,best,tier,ticket=True),allowed_mentions=discord.AllowedMentions.none())
                await self.cog.save_ticket(i.channel_id,self.member_id,i.message.id,tier)
        else:
            best=await self.cog.best(self.member_id)
            await i.edit_original_response(embed=self.cog.embed(i.guild,self.member_id,best,tier),view=DiaryView(self.cog,self.member_id,best,tier),allowed_mentions=discord.AllowedMentions.none())
    async def view_proof(self,i):
        sid=int(self.proof.values[0])
        best=await self.cog.best(self.member_id,resolve_links=False)
        if not any(row["id"]==sid for row in best.values()):
            return await i.response.send_message("That record changed. Refresh the diary.",ephemeral=True)
        await self.cog.show_proof(i,sid)
    async def on_error(self,i,error,item):
        logger.error("Diary interaction failed",exc_info=(type(error),error,error.__traceback__))
        try:
            if i.response.is_done():
                await i.followup.send("Could not refresh the diary. Try again.",ephemeral=True)
            else:
                await i.response.send_message("Could not refresh the diary. Try again.",ephemeral=True)
        except discord.HTTPException:
            pass

class DiaryCog(commands.Cog):
    def __init__(self,bot,store,fmt,show_proof,proof_link,refresh_proofs):
        self.bot,self.store,self.fmt,self.show_proof=bot,store,fmt,show_proof
        self.proof_link,self.refresh_proofs=proof_link,refresh_proofs
        self.locks={}
        self.started=None
        self.recovered=False
        self.bridge_lock=asyncio.Lock()
        self.bridge_snapshot=None
        self.bridge_checked=0.0
        self.award_locks={}
        self.bridge_logged=False

    def cog_unload(self):
        self.refresh_ticket_cards.cancel()

    async def initialize(self):
        async with self.store.lock:
            await self.store.conn.execute("CREATE TABLE IF NOT EXISTS diary_ticket_previews (channel INTEGER PRIMARY KEY,member INTEGER NOT NULL,message INTEGER NOT NULL,tier INTEGER NOT NULL DEFAULT 0)")
            await self.store.conn.execute("CREATE TABLE IF NOT EXISTS diary_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            await self.store.conn.execute("CREATE TABLE IF NOT EXISTS diary_attestations (fingerprint TEXT PRIMARY KEY,pb INTEGER NOT NULL,team_verified INTEGER NOT NULL,invocation_300 INTEGER NOT NULL,evidence TEXT NOT NULL,reviewer INTEGER NOT NULL,reviewed_at TEXT NOT NULL)")
            await self.store.conn.execute("CREATE TABLE IF NOT EXISTS diary_verification_audit (id INTEGER PRIMARY KEY AUTOINCREMENT,pb INTEGER NOT NULL,fingerprint TEXT NOT NULL,team_verified INTEGER NOT NULL,invocation_300 INTEGER NOT NULL,evidence TEXT NOT NULL,reviewer INTEGER NOT NULL,reviewed_at TEXT NOT NULL)")
            await self.store.conn.execute("CREATE TABLE IF NOT EXISTS diary_rank_awards (member INTEGER NOT NULL,tier INTEGER NOT NULL,reviewer INTEGER NOT NULL,channel INTEGER NOT NULL,approved_at TEXT NOT NULL,proof_ids TEXT NOT NULL,status TEXT NOT NULL,PRIMARY KEY(member,tier))")
            await self.store.conn.execute("INSERT OR IGNORE INTO diary_meta VALUES ('preview_started',?)",(datetime.now(timezone.utc).isoformat(),))
            await self.store.conn.commit()
        row=await self.store.one("SELECT value FROM diary_meta WHERE key='preview_started'")
        self.started=datetime.fromisoformat(row["value"])
        for row in await self.store.all("SELECT * FROM diary_ticket_previews"):
            best=await self.best(row["member"],resolve_links=False)
            self.bot.add_view(DiaryView(self,row["member"],best,row["tier"],ticket=True),message_id=row["message"])

    async def membership_snapshot(self):
        async with self.bridge_lock:
            now=asyncio.get_running_loop().time()
            if now-self.bridge_checked<60:
                snapshot=self.bridge_snapshot
                if snapshot and (datetime.now(timezone.utc)-snapshot[0]).total_seconds()<1800:
                    return snapshot[1]
                return None
            self.bridge_checked=now
            self.bridge_snapshot=None
            guild=self.bot.get_guild(GUILD)
            if not guild:
                return None
            try:
                parent=guild.get_channel(BRIDGE_PARENT) or await self.bot.fetch_channel(BRIDGE_PARENT)
                if not isinstance(parent,discord.TextChannel) or parent.permissions_for(guild.default_role).view_channel:
                    return None
                threads=[t for t in await guild.active_threads() if t.parent_id==BRIDGE_PARENT and t.name==BRIDGE_THREAD and t.is_private() and t.owner_id==SYNC]
                if not threads:
                    async for t in parent.archived_threads(private=True,joined=True,limit=100):
                        if t.name==BRIDGE_THREAD and t.owner_id==SYNC:
                            threads.append(t)
                if len(threads)!=1:
                    return None
                async for msg in threads[0].history(limit=20):
                    if msg.author.id!=SYNC or msg.content!=BRIDGE_MARKER:
                        continue
                    file=next((a for a in msg.attachments if a.filename==BRIDGE_FILENAME and a.size<1000000),None)
                    if not file:
                        return None
                    data=json.loads(await file.read())
                    generated=datetime.fromisoformat(data["generated"])
                    age=(datetime.now(timezone.utc)-generated).total_seconds()
                    members=data["members"]
                    if data["version"]!=1 or data["guild"]!=GUILD or not isinstance(members,dict) or not 0<=age<1800:
                        return None
                    for key,value in members.items():
                        if not key.isdigit() or not isinstance(value,dict):
                            return None
                        date.fromisoformat(value["joined"])
                        if value.get("tenure_eligible"):
                            date.fromisoformat(value["tenure_eligible"])
                    self.bridge_snapshot=(generated,members)
                    if not self.bridge_logged:
                        logger.info("Diary WOM verification loaded members=%s",len(members))
                        self.bridge_logged=True
                    return members
            except (discord.HTTPException,ValueError,TypeError,KeyError,AttributeError):
                logger.warning("Diary WOM verification snapshot unavailable")
            return None

    async def best(self,member_id,*,resolve_links=True):
        rows=await self.store.approved_for_points(GUILD)
        candidates=best_candidates(rows,member_id)
        snapshot=await self.membership_snapshot() if self.bot.is_ready() else None
        if snapshot is None:
            best=DiaryEvidence(candidates)
        else:
            # Select the fastest eligible record, not simply the fastest PB.
            # ToA requires invocation evidence; generic Expert Solo is insufficient.
            attestations={r["fingerprint"]:dict(r) for r in await self.store.all("SELECT * FROM diary_attestations")}
            verified=best_candidates([r for r in rows if eligible_record(r,member_id,snapshot,attestations.get(record_fingerprint(r)))],member_id)
            display={**candidates,**verified}
            best=DiaryEvidence(display,verified,snapshot.get(str(member_id)),True)
        award=await self.store.one("SELECT MAX(tier) AS tier FROM diary_rank_awards WHERE member=? AND status='approved'",(member_id,))
        best.award=award["tier"] if award else None
        if resolve_links and best:
            await self.refresh_proofs(list(best.values()))
        return best

    async def verify_exception(self,i,member_id,tier,raw_id,invocation,historical):
        if not staff(i.user) or i.guild_id!=GUILD or await self.ticket_applicant(i.channel)!=member_id:
            return await i.response.send_message("Staff review ticket only.",ephemeral=True)
        if not raw_id.strip().isdigit() or not 0<int(raw_id)<2**63:
            return await i.response.send_message("Enter a valid Apex PB ID.",ephemeral=True)
        if invocation.strip() not in ("","300"):
            return await i.response.send_message("The diary requires a confirmed level 300 solo.",ephemeral=True)
        await i.response.defer(ephemeral=True,thinking=True)
        sid=int(raw_id)
        snapshot=await self.membership_snapshot()
        if snapshot is None or str(member_id) not in snapshot:
            return await i.followup.send("WOM membership must be available first. Refresh the diary after Sync updates.",ephemeral=True)
        record=await self.store.one("SELECT * FROM submissions WHERE guild=? AND id=?",(GUILD,sid))
        if not record or not best_candidates([record],member_id):
            return await i.followup.send("That PB is not an approved, dated diary submission for this applicant.",ephemeral=True)
        if date.fromisoformat(record["pb_date"])<date.fromisoformat(snapshot[str(member_id)]["joined"]):
            return await i.followup.send("This PB predates the applicant's WOM join date.",ephemeral=True)
        fp=record_fingerprint(record)
        existing=await self.store.one("SELECT * FROM diary_attestations WHERE fingerprint=?",(fp,))
        team_needed=not verified_run(record,snapshot) and not (existing and existing["team_verified"])
        toa_needed=record["activity"]=="Tombs of Amascut" and not (existing and existing["invocation_300"])
        if not team_needed and not toa_needed:
            return await i.followup.send("This PB already has the necessary verification.",ephemeral=True)
        if team_needed and len(historical.strip())<12:
            return await i.followup.send("Document the WOM evidence confirming every teammate was in Novus on the run date.",ephemeral=True)
        if toa_needed and invocation.strip()!="300":
            return await i.followup.send("Confirm the proof visibly shows a level 300 solo.",ephemeral=True)
        if await self.ticket_applicant(i.channel)!=member_id:
            return await i.followup.send("Ticket ownership changed.",ephemeral=True)
        when=datetime.now(timezone.utc).isoformat()
        team=int(bool(team_needed or (existing and existing["team_verified"])))
        toa=int(bool(toa_needed or (existing and existing["invocation_300"])))
        evidence=(historical.strip() if team_needed else "")+(" | Staff confirmed level 300 in original proof" if toa_needed else "")
        async with self.store.lock:
            current=await self.store.one("SELECT * FROM submissions WHERE guild=? AND id=?",(GUILD,sid))
            if not current or current["status"]!="approved" or record_fingerprint(current)!=fp:
                return await i.followup.send("PB changed during review. Reopen the verification form.",ephemeral=True)
            await self.store.conn.execute("INSERT OR REPLACE INTO diary_attestations VALUES (?,?,?,?,?,?,?)",(fp,sid,team,toa,evidence,i.user.id,when))
            await self.store.conn.execute("INSERT INTO diary_verification_audit(pb,fingerprint,team_verified,invocation_300,evidence,reviewer,reviewed_at) VALUES (?,?,?,?,?,?,?)",(sid,fp,team,toa,evidence,i.user.id,when))
            await self.store.conn.commit()
        await self.update_ticket(i.channel,member_id,tier)
        await i.followup.send(f"✅ PB #{sid} verified. Staff review recorded.",ephemeral=True)

    async def update_ticket(self,ch,member_id,tier):
        async with self.locks.setdefault(ch.id,asyncio.Lock()):
            row=await self.store.one("SELECT * FROM diary_ticket_previews WHERE channel=?",(ch.id,))
            if not row or row["member"]!=member_id or await self.ticket_applicant(ch)!=member_id:
                return
            msg=await ch.fetch_message(row["message"])
            if not self.is_diary_message(msg,member_id):
                return
            best=await self.best(member_id)
            if await self.ticket_applicant(ch)!=member_id:
                return
            await msg.edit(embed=self.embed(ch.guild,member_id,best,tier),view=DiaryView(self,member_id,best,tier,ticket=True),allowed_mentions=discord.AllowedMentions.none())
            await self.save_ticket(ch.id,member_id,msg.id,tier)

    async def approve_rank(self,i,member_id,tier):
        if not staff(i.user) or i.guild_id!=GUILD or await self.ticket_applicant(i.channel)!=member_id:
            return await i.response.send_message("Staff review ticket only.",ephemeral=True)
        if i.user.id==member_id:
            return await i.response.send_message("Another staff member must approve your diary.",ephemeral=True)
        await i.response.defer(ephemeral=True,thinking=True)
        async with self.award_locks.setdefault(member_id,asyncio.Lock()):
            best=await self.best(member_id,resolve_links=False)
            if not rank_ready(best,tier):
                return await i.followup.send("Rank requirements or WOM tenure are not verified. Refresh the diary.",ephemeral=True)
            member=await i.guild.fetch_member(member_id)
            if not member_role(member) or await self.ticket_applicant(i.channel)!=member_id:
                return await i.followup.send("Applicant is no longer a verified ticket owner.",ephemeral=True)
            roles=[r for r in i.guild.roles if r.name.casefold()==RANKS[tier].casefold() and not r.managed]
            if len(roles)!=1 or not roles[0].is_assignable():
                return await i.followup.send("Apex cannot assign that rank. Council must check role permissions and hierarchy.",ephemeral=True)
            role=roles[0]
            existing_names={r.name.casefold() for r in member.roles}
            if any(rank.casefold() in existing_names for rank in RANKS[tier+1:]):
                return await i.followup.send("Member already holds a higher PvM Diary rank.",ephemeral=True)
            previous=await self.store.one("SELECT status FROM diary_rank_awards WHERE member=? AND tier=?",(member_id,tier))
            if previous and previous["status"]=="approved":
                return await i.followup.send("This diary rank was already approved.",ephemeral=True)
            ids=json.dumps(sorted(r["id"] for r in best.verified.values()))
            when=datetime.now(timezone.utc).isoformat()
            async with self.store.lock:
                # Check that the evidence remains approved before recording intent.
                for record in best.verified.values():
                    current=await self.store.one("SELECT * FROM submissions WHERE guild=? AND id=?",(GUILD,record["id"]))
                    if not current or current["status"]!="approved" or record_fingerprint(current)!=record_fingerprint(record):
                        return await i.followup.send("PB evidence changed. Refresh the diary and review again.",ephemeral=True)
                await self.store.conn.execute("INSERT OR REPLACE INTO diary_rank_awards VALUES (?,?,?,?,?,?,?)",(member_id,tier,i.user.id,i.channel_id,when,ids,"pending"))
                await self.store.conn.commit()
            try:
                if role not in member.roles:
                    await member.add_roles(role,reason=f"Novus PvM Diary approved by {i.user.id}")
            except discord.HTTPException:
                return await i.followup.send("Discord did not allow the rank assignment. Council must check Apex permissions.",ephemeral=True)
            async with self.store.lock:
                await self.store.conn.execute("UPDATE diary_rank_awards SET status='approved' WHERE member=? AND tier=?",(member_id,tier))
                await self.store.conn.commit()
            logger.info("Diary rank approved member=%s tier=%s reviewer=%s",member_id,RANKS[tier],i.user.id)
        await self.update_ticket(i.channel,member_id,tier)
        await i.followup.send(f"✅ {RANKS[tier]} approved and assigned.",ephemeral=True)

    def embed(self,guild,member_id,best,tier):
        counts,done,total=progress(best,tier)
        e=discord.Embed(
            title="Novus PvM Diary",
            description=f"**Applicant:** <@{member_id}>\n**Selected Rank:** {rank_emoji(guild,tier)}{RANKS[tier]}",
            color=COLORS[tier],
        )
        if guild.icon:
            e.set_thumbnail(url=guild.icon.url)
        summary=[]
        for i,name in enumerate(RANKS):
            _,n,required=progress(best,i)
            summary.append(f"{rank_emoji(guild,i)}**{name}:** {n}/{required}")
        # The category headings carry the required counts, avoiding a second
        # dense summary. A zero-width spacer gives each section breathing room.
        e.add_field(name="Rank Progress",value="\n".join(summary)+"\n\u200b",inline=False)
        grouped={group:[] for group in GROUPS}
        link_budget=3500
        for name,group,cat,act,var,size,times in CHALLENGES:
            row=best.get(name)
            label=name.removeprefix("ToB ").removeprefix("CM ") if group in ("tob","cm") else ("300 Solo" if group=="toa" else name)
            if not row:
                line=f"▫️ {label}: — / {times[tier]}"
            else:
                verified=getattr(best,"verified",None)
                mark="⚠️" if verified is not None and name not in verified else ("✅" if row["ms"]<=threshold(times[tier]) else "❌")
                actual=self.fmt(row["ms"]).removesuffix(".00")
                evidence=self.proof_link(row) or f"Proof #{row['id']}"
                if len(evidence)>min(750,link_budget):
                    evidence=f"Proof #{row['id']}"
                else:
                    link_budget-=len(evidence)
                line=f"{mark} {label}: **{actual}**/{times[tier]} {evidence}"
            grouped[group].append(line)
        for group,label,cap in zip(GROUPS,("Theatre of Blood","CoX Challenge Mode","Tombs of Amascut","Inferno / Colosseum"),RULES[tier]):
            heading=f"{label}  •  {min(counts[group],cap)}/{cap}"
            batch=[]
            for line in grouped[group]:
                if batch and len("\n".join(batch+[line]))>1000:
                    e.add_field(name=heading,value="\n".join(batch),inline=False)
                    heading="\u200b"
                    batch=[]
                batch.append(line)
            if batch:
                e.add_field(name=heading,value="\n".join(batch)+"\n\u200b",inline=False)
        time_status="✅ **Time goals met**" if done==total else f"▫️ **Time goals:** {done}/{total}"
        membership=getattr(best,"membership",None)
        if not getattr(best,"snapshot_available",False):
            checks=[time_status,"WOM sync unavailable • provisional"]
        elif not membership:
            checks=[time_status,"⚠️ WOM membership not verified"]
        else:
            eligible=membership.get("tenure_eligible")
            today=datetime.now(ZoneInfo("America/New_York")).date()
            tenure_met=bool(eligible and today>=date.fromisoformat(eligible))
            checks=[time_status,f"✅ WOM joined: {membership['joined']}",
                    "✅ 3-month tenure met" if tenure_met else (f"▫️ Tenure eligible: {eligible}" if eligible else "⚠️ Tenure date unavailable")]
            if any(name not in best.verified for name in best if name!="ToA 300 Solo"):
                checks.append("⚠️ Some run dates / teams unverified")
        if "ToA 300 Solo" in best and (best.verified is None or "ToA 300 Solo" not in best.verified):
            checks.append("⚠️ ToA verification needed")
        if best.award is not None and best.award>=tier:
            checks.append(f"✅ **{RANKS[best.award]} approved**")
        elif rank_ready(best,tier):
            checks.append("✅ **Ready for staff approval**")
        e.add_field(name="Diary Status",value="\n".join(checks),inline=False)
        e.set_footer(text="Novus Apex • Diary Preview")
        return e

    @app_commands.command(name="diary",description="View your Novus Apex PvM Diary progress")
    @app_commands.describe(member="Staff only: inspect another member's diary")
    @app_commands.guild_only()
    async def diary(self,i:discord.Interaction,member:discord.Member=None):
        if i.guild_id!=GUILD:
            return await i.response.send_message("Novus only.",ephemeral=True)
        if member is not None and member.id!=i.user.id and not staff(i.user):
            return await i.response.send_message("Only staff can inspect another member's diary.",ephemeral=True)
        target=member or i.user
        if not member_role(target):
            return await i.response.send_message("The PvM Diary is for current Novus Members.",ephemeral=True)
        await i.response.defer(ephemeral=True,thinking=True)
        best=await self.best(target.id)
        await i.followup.send(embed=self.embed(i.guild,target.id,best,0),view=DiaryView(self,target.id,best),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    async def ticket_applicant(self,ch):
        if not isinstance(ch,discord.TextChannel) or ch.guild.id!=GUILD or ch.category_id!=CATEGORY:
            return None
        guild=ch.guild
        if ch.permissions_for(guild.default_role).view_channel:
            return None
        # Fail closed on broadly readable tickets.
        for role in guild.roles:
            if role.id not in STAFF and not role.is_bot_managed() and not role.permissions.administrator and ch.permissions_for(role).view_channel:
                return None
        ids=[]
        for target,overwrite in ch.overwrites.items():
            if overwrite.view_channel is not True or isinstance(target,discord.Role) or guild.get_role(target.id):
                continue
            try:
                m=target if isinstance(target,discord.Member) else await guild.fetch_member(target.id)
            except discord.HTTPException:
                return None
            if not m.bot:
                ids.append(m)
        if len(ids)!=1 or not member_role(ids[0]) or not ch.permissions_for(ids[0]).view_channel:
            return None
        return ids[0].id

    async def save_ticket(self,channel,member,message,tier):
        async with self.store.lock:
            await self.store.conn.execute("INSERT OR REPLACE INTO diary_ticket_previews VALUES (?,?,?,?)",(channel,member,message,tier))
            await self.store.conn.commit()

    def is_diary_message(self,msg,member):
        if msg.author.id!=self.bot.user.id:
            return False
        return any(
            e.footer.text==f"Novus Apex • Diary preview • {member}"
            or (e.footer.text=="Novus Apex • Diary Preview" and (e.description or "").split("\n",1)[0]==f"**Applicant:** <@{member}>")
            for e in msg.embeds
        )

    async def publish(self,ch):
        if not isinstance(ch,discord.TextChannel) or ch.guild.id!=GUILD or ch.category_id!=CATEGORY:
            return
        if self.started is None or ch.created_at<self.started:
            return
        async with self.locks.setdefault(ch.id,asyncio.Lock()):
            if await self.store.one("SELECT 1 FROM diary_ticket_previews WHERE channel=?",(ch.id,)):
                return
            member=await self.ticket_applicant(ch)
            if member is None:
                return
            async for msg in ch.history(limit=100):
                if self.is_diary_message(msg,member):
                    await self.save_ticket(ch.id,member,msg.id,0)
                    return
            best=await self.best(member)
            if await self.ticket_applicant(ch)!=member:
                return
            msg=await ch.send(embed=self.embed(ch.guild,member,best,0),view=DiaryView(self,member,best,ticket=True),allowed_mentions=discord.AllowedMentions.none())
            await self.save_ticket(ch.id,member,msg.id,0)
            logger.info("Diary preview posted ticket=%s message=%s",ch.id,msg.id)

    @commands.Cog.listener()
    async def on_guild_channel_create(self,ch):
        if isinstance(ch,discord.TextChannel) and ch.guild.id==GUILD and ch.category_id==CATEGORY:
            try:
                # MEE6 may finish applying overwrites shortly after creation.
                for delay in (2,5,15,30):
                    await asyncio.sleep(delay)
                    await self.publish(ch.guild.get_channel(ch.id) or ch)
                    if await self.store.one("SELECT 1 FROM diary_ticket_previews WHERE channel=?",(ch.id,)):
                        break
            except Exception:
                logger.exception("Diary ticket preview failed")

    @commands.Cog.listener()
    async def on_guild_channel_update(self,before,after):
        try:
            await self.publish(after)
        except Exception:
            logger.exception("Diary ticket update failed")

    @commands.Cog.listener()
    async def on_ready(self):
        if self.recovered:
            return
        self.recovered=True
        guild=self.bot.get_guild(GUILD)
        category=guild.get_channel(CATEGORY) if guild else None
        if isinstance(category,discord.CategoryChannel):
            for ch in category.text_channels:
                try:
                    await self.publish(ch)
                except Exception:
                    logger.exception("Diary ticket recovery failed")
        self.refresh_ticket_cards.start()
        logger.info("Novus Apex diary ready; WOM bridge and staff review controls enabled")

    @tasks.loop(hours=2)
    async def refresh_ticket_cards(self):
        # Re-render only saved private tickets. Never create a new message here.
        guild=self.bot.get_guild(GUILD)
        if not guild:
            return
        for row in await self.store.all("SELECT * FROM diary_ticket_previews"):
            ch=guild.get_channel(row["channel"])
            if ch is None:
                continue
            try:
                async with self.locks.setdefault(ch.id,asyncio.Lock()):
                    current=await self.store.one("SELECT * FROM diary_ticket_previews WHERE channel=?",(ch.id,))
                    if not current or await self.ticket_applicant(ch)!=current["member"]:
                        continue
                    msg=await ch.fetch_message(current["message"])
                    if not self.is_diary_message(msg,current["member"]):
                        continue
                    best=await self.best(current["member"])
                    if await self.ticket_applicant(ch)!=current["member"]:
                        continue
                    await msg.edit(embed=self.embed(guild,current["member"],best,current["tier"]),view=DiaryView(self,current["member"],best,current["tier"],ticket=True),allowed_mentions=discord.AllowedMentions.none())
                await asyncio.sleep(1)
            except discord.NotFound:
                continue
            except Exception:
                logger.exception("Diary ticket card refresh failed channel=%s",row["channel"])

async def install(bot,store,fmt,show_proof,proof_link,refresh_proofs):
    cog=DiaryCog(bot,store,fmt,show_proof,proof_link,refresh_proofs)
    await cog.initialize()
    await bot.add_cog(cog)
