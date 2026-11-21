"""Fetch two specified Deadlock match metadata JSON files, cache/S3 only.
Run: py check_street_brawl_matches.py
No Steam fallback (disable_steam=true), no bot token, no database mutations.
"""
import json
import pathlib
import urllib.error
import urllib.request

MATCH_IDS = (140699914, 137827214)
OUTPUT = pathlib.Path('street_brawl_samples')

def describe(data):
    if not isinstance(data, dict):
        print('  JSON root type:', type(data).__name__)
        return
    print('  top level keys:', ', '.join(sorted(data.keys())))
    for key in ('game_mode', 'match_mode', 'match_id', 'winning_team', 'winning_team_id', 'team_score', 'street_brawl_rounds', 'start_time'):
        if key in data:
            value = data[key]
            if isinstance(value, (dict, list)):
                print(' ', key, ':', type(value).__name__, 'length=', len(value))
            else:
                print(' ', key, ':', value)
    # Metadata is often nested, so list keys there without assuming fields.
    for key in ('match_info', 'match', 'metadata'):
        obj = data.get(key)
        if isinstance(obj, dict):
            print(' ', key, 'keys:', ', '.join(sorted(obj.keys())))
            for k in ('game_mode', 'winning_team', 'team_score', 'street_brawl_rounds', 'players'):
                if k in obj:
                    val = obj[k]
                    if isinstance(val, (list, dict)):
                        print('   ', k, type(val).__name__, 'length=', len(val))
                    else:
                        print('   ', k, val)

def main():
    OUTPUT.mkdir(exist_ok=True)
    for match_id in MATCH_IDS:
        url = f'https://api.deadlock-api.com/v1/matches/{match_id}/metadata?disable_steam=true'
        print('\nMatch ID:', match_id)
        request = urllib.request.Request(url, headers={'Accept':'application/json','User-Agent':'INDEX-OF-PAiGEs-StreetBrawl-Validation/1.0'})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                payload = response.read()
            obj = json.loads(payload)
            outpath = OUTPUT / f'{match_id}.json'
            outpath.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
            print('  saved:', outpath)
            describe(obj)
        except urllib.error.HTTPError as exc:
            print(f'  HTTP {exc.code}; cache/S3 にない場合、Steam取得は行いません。')
        except Exception as exc:
            print('  failed:', type(exc).__name__, str(exc))
    print('\nJSONが保存された場合、その2ファイルをチャットへアップロードしてください。')
    print('注意: 生の試合データにはプレイヤーのアカウントID等が含まれます。')

if __name__ == '__main__':
    main()
