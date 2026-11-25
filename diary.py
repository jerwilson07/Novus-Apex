"""Novus Apex diary: read-only progression and existing MEE6 ticket preview.

Approved PB records are the evidence source. Eligibility remains provisional
until the verified WOM membership bridge and approval workflow are available.
No rank is granted by this module.
"""
import asyncio
import logging
from datetime import date, datetime, timezone

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

GUILD = 1532817291048124466
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

def progress(best,tier):
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
        # The proof picker is a durable fallback, even if a signed link expires.
        fallback=best
        if fallback:
            self.proof=ui.Select(placeholder="View Proof",custom_id=f"diary:proof:{member_id}:{tier}",row=2,
                options=[discord.SelectOption(label=name,value=str(record["id"])) for name,record in fallback.items()])
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

    def cog_unload(self):
        self.refresh_ticket_cards.cancel()

    async def initialize(self):
        async with self.store.lock:
            await self.store.conn.execute("CREATE TABLE IF NOT EXISTS diary_ticket_previews (channel INTEGER PRIMARY KEY,member INTEGER NOT NULL,message INTEGER NOT NULL,tier INTEGER NOT NULL DEFAULT 0)")
            await self.store.conn.execute("CREATE TABLE IF NOT EXISTS diary_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            await self.store.conn.execute("INSERT OR IGNORE INTO diary_meta VALUES ('preview_started',?)",(datetime.now(timezone.utc).isoformat(),))
            await self.store.conn.commit()
        row=await self.store.one("SELECT value FROM diary_meta WHERE key='preview_started'")
        self.started=datetime.fromisoformat(row["value"])
        for row in await self.store.all("SELECT * FROM diary_ticket_previews"):
            best=await self.best(row["member"],resolve_links=False)
            self.bot.add_view(DiaryView(self,row["member"],best,row["tier"],ticket=True),message_id=row["message"])

    async def best(self,member_id,*,resolve_links=True):
        rows=await self.store.approved_for_points(GUILD)
        best=best_candidates(rows,member_id)
        if resolve_links and best:
            await self.refresh_proofs(list(best.values()))
        return best

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
        e.add_field(name="Rank Progress",value="\n".join(summary),inline=False)
        status=[]
        for label,g,cap in zip(GROUP_LABELS,GROUPS,RULES[tier]):
            n=min(counts[g],cap)
            status.append(f"{'✅' if n==cap else '▫️'} {label} **{n}/{cap}**")
        e.add_field(name="Required Challenges",value=" • ".join(status[:2])+"\n"+" • ".join(status[2:]),inline=False)
        grouped={group:[] for group in GROUPS}
        link_budget=3500
        for name,group,cat,act,var,size,times in CHALLENGES:
            row=best.get(name)
            label=name.removeprefix("ToB ").removeprefix("CM ") if group in ("tob","cm") else name
            if not row:
                line=f"▫️ {label} · — / {times[tier]}"
            else:
                mark="✅" if row["ms"]<=threshold(times[tier]) else "❌"
                actual=self.fmt(row["ms"]).removesuffix(".00")
                evidence=self.proof_link(row) or f"Proof #{row['id']}"
                if len(evidence)>min(750,link_budget):
                    evidence=f"Proof #{row['id']}"
                else:
                    link_budget-=len(evidence)
                line=f"{mark} {label} · **{actual}** / {times[tier]} · {evidence}"
            grouped[group].append(line)
        for group,label in zip(GROUPS,("Theatre of Blood","CoX Challenge Mode","Tombs of Amascut","Inferno / Colosseum")):
            batch=[]
            for line in grouped[group]:
                if batch and len("\n".join(batch+[line]))>1000:
                    e.add_field(name=label,value="\n".join(batch),inline=False)
                    batch=[]
                batch.append(line)
            if batch:
                e.add_field(name=label,value="\n".join(batch),inline=False)
        time_status="✅ **Time goals met**" if done==total else f"▫️ **Time goals:** {done}/{total}"
        e.add_field(name="Diary Status",value=f"{time_status}\nWOM join date, team membership & 3-month tenure: pending\nToA invocation: staff confirmation\nStaff approval pending",inline=False)
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
        logger.info("Novus Apex diary preview ready; WOM verification and role awards disabled")

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
