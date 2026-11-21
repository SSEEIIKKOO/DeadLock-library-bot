from pathlib import Path
import shutil,sys
root=Path(__file__).resolve().parent
for name,old,new in [
 ('cogs/custom_match.py','await self.refresh_panel(channel.guild, reinstall=True)','await self.refresh_panel(channel.guild, reinstall=False)'),
 ('cogs/voice_manager.py','await self.refresh(channel.id, reinstall=True)','await self.refresh(channel.id, reinstall=False)'),
]:
 p=root/name
 if not p.exists():sys.exit(f'ERROR: {name} not found')
 src=p.read_text(encoding='utf8')
 if old not in src and new not in src:sys.exit(f'ERROR: unexpected version of {name}; nothing changed')
 if old in src:
  backup=p.with_suffix(p.suffix+'.before_panel_fix.bak')
  if not backup.exists():shutil.copy2(p,backup)
  p.write_text(src.replace(old,new),encoding='utf8')
  print('Updated',name)
 else:print('Already updated',name)
