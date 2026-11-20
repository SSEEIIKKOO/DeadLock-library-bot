#!/usr/bin/env python3
"""Update ONLY main-community ID keys in existing .env, preserving library IDs and secrets."""
from pathlib import Path
from datetime import datetime
import re, shutil, sys

ENV=Path(__file__).resolve().parent / '.env'
UPDATES={
    'MAIN_GUILD_ID':'1459954118142464206',
    'PROFILE_ADDRESS_CHANNEL_ID':'1461251105139851264',
    'CUSTOM_PANEL_CHANNEL_ID':'1573335992755294208',
    'CUSTOM_WAITING_VC_ID':'1462421006659358740',
    'CUSTOM_TEAM_A_VC_ID':'1462421097193668618',
    'CUSTOM_TEAM_B_VC_ID':'1462421266517590314',
    'TEMP_VC_ENJOY_CREATE_ID':'1573379742428823623',
    'TEMP_VC_SERIOUS_CREATE_ID':'1573379962872795236',
    'TEMP_VC_OTHER_CREATE_ID':'1485675570271617054',
    'TEMP_VC_LOG_CHANNEL_ID':'1573337653741494313',
}

def main():
    if not ENV.is_file():
        print('エラー: このスクリプトは既存の .env と同じフォルダに置いて実行してください。')
        return 1
    original=ENV.read_text(encoding='utf-8-sig')
    updates=UPDATES
    seen=set(); result=[]
    for line in original.splitlines(keepends=True):
        m=re.match(r'^(\s*)([A-Z][A-Z0-9_]*)\s*=',line)
        if not m or m.group(2) not in updates:
            result.append(line);continue
        key=m.group(2)
        if key in seen: continue
        seen.add(key)
        result.append(f'{key}={updates[key]}\n')
    for key,value in updates.items():
        if key not in seen:
            if result and not result[-1].endswith(('\n','\r')): result.append('\n')
            result.append(f'{key}={value}\n')
    # Deduplicate existing CR channel key without changing its first value.
    lines=result;result=[];seen_cr=False
    for line in lines:
        if re.match(r'^\s*CR_DATA_STORAGE_CHANNEL_ID\s*=',line):
            if seen_cr:continue
            seen_cr=True
        result.append(line)
    updated=''.join(result)
    backup=ENV.with_name('.env.backup-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(ENV,backup)
    try:
        ENV.write_text(updated,encoding='utf-8')
    except Exception:
        shutil.copy2(backup,ENV)
        raise
    print('完了: メインサーバー側IDを更新しました。')
    print('既存の図書館サーバーID・認証情報・保存データは変更していません。')
    print('バックアップ:',backup.name)
    print('本番メインサーバーの使い捨てVCトリガー3件とログチャンネルを設定しました。')
    print('注意: バックアップと.envには機密情報が含まれます。公開しないでください。')
    return 0

if __name__=='__main__':sys.exit(main())
