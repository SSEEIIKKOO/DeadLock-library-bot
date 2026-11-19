"""Discord private text channel snapshots; SQLite is local index/cache."""
from __future__ import annotations
import gzip
import io
import json
import os
import discord

CHANNEL_ID_VAR='CR_DATA_STORAGE_CHANNEL_ID'

def channel_id():
    return int(os.getenv(CHANNEL_ID_VAR,'0') or '0')

async def get_channel(bot):
    cid=channel_id()
    if not cid:
        raise RuntimeError(f'.env に {CHANNEL_ID_VAR} を設定してください。')
    channel=bot.get_channel(cid) or await bot.fetch_channel(cid)
    if not isinstance(channel,discord.TextChannel):
        raise RuntimeError('CR保存先はテキストチャンネルにしてください。')
    return channel

async def snapshot(bot, row:dict, history:list):
    channel=await get_channel(bot)
    payload={key: row[key] for key in ('discord_id','steam32','a','b','c','confidence','badge','updated_at','version')}
    from utils import cr_store
    payload['b'] = cr_store.current_b(int(row['discord_id']))
    payload.update({'schema':2,'history':history,'custom_events':cr_store.events(int(row['discord_id']))})
    data=gzip.compress(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
    file=discord.File(io.BytesIO(data),filename=f"cr_{row['discord_id']}_v{row['version']}.json.gz")
    msg=await channel.send(content=f"CR SNAPSHOT | discord_id={row['discord_id']} | steam32={row['steam32']} | version={row['version']}",file=file)
    return msg.id

async def restore_all(bot):
    """Read most recent snapshots; never relies on expiring attachment URLs."""
    from utils.cr_store import restore_snapshot, set_remote
    channel=await get_channel(bot)
    best={}
    async for message in channel.history(limit=None,oldest_first=False):
        if message.author.id != bot.user.id or not message.attachments or not message.content.startswith('CR SNAPSHOT |'):
            continue
        try:
            parts=dict(x.split('=',1) for x in message.content.split(' | ')[1:])
            did=int(parts['discord_id']); version=int(parts['version'])
        except (ValueError, KeyError):
            continue
        if did not in best or version>best[did][0]:
            best[did]=(version,message)
    counts={'found':len(best),'restored':0,'failed':0}
    for did,(version,msg) in best.items():
        try:
            raw=await msg.attachments[0].read()
            doc=json.loads(gzip.decompress(raw))
            if int(doc['discord_id'])!=did or int(doc['version'])!=version or doc.get('schema') not in (1,2):
                raise ValueError('Invalid snapshot')
            if restore_snapshot(doc): counts['restored']+=1
            if doc.get('schema') == 2:
                from utils.cr_store import restore_events
                restore_events(did, doc.get('custom_events', []))
            set_remote(did,msg.id,None)
        except Exception:
            counts['failed']+=1
    return counts
