import io
import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SCOPES = ["".join(["https", "://", "www.", "googleapis", ".com/", "auth/", "drive.file"])]
CLIENT_SECRET_FILE = "client_secret.json"
TOKEN_FILE = "token.json"
DEFAULT_FOLDER_NAME = "Deadlock_Replays"


def get_drive_service():
    """サブ垢のOAuth認証を行い、Google Drive APIサービスを返す"""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET_FILE):
                raise FileNotFoundError("client_secret.json が見つかりません。フォルダ直下に配置してください。")
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w", encoding="utf-8") as token:
            token.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def get_or_create_replay_folder(service, folder_name: str = DEFAULT_FOLDER_NAME) -> str:
    """Botがアクセス可能な専用フォルダを検索し、なければ新規作成して公開設定にする"""
    query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    results = service.files().list(q=query, spaces="drive", fields="files(id, name)").execute()
    files = results.get("files", [])

    if files:
        return files[0]["id"]

    # フォルダがなければBot自身の手で新規作成する（これでdrive.file権限でも確実に書き込める）
    folder_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    folder = service.files().create(body=folder_metadata, fields="id").execute()
    folder_id = folder["id"]

    # リンクを知っている全員が閲覧（ダウンロード）できるように共有権限を自動付与
    service.permissions().create(
        fileId=folder_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    return folder_id


def upload_replay_bytes(file_bytes: bytes, filename: str, folder_id: str | None = None) -> str:
    """同一マッチの保存済みファイルがあれば再アップロードせずリンクを返す。"""
    match = _REPLAY_NAME_RE.search(filename)
    if not match:
        raise ValueError("リプレイのファイル名が想定外です")

    with _UPLOAD_LOCK:
        service = get_drive_service()
        target_folder_id = get_or_create_replay_folder(service)
        existing = _find_saved_replays_in_folder(service, target_folder_id, match.group(1))
        if existing:
            found = existing[0]
            return found.get("webViewLink") or f"https://drive.google.com/file/d/{found['id']}/view"

        file_metadata = {"name": filename, "parents": [target_folder_id]}
        mimetype = "application/zstd" if filename.endswith(".dem.zst") else "application/x-bzip2"
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mimetype, resumable=True)
        uploaded = service.files().create(
            body=file_metadata, media_body=media, fields="id,webViewLink"
        ).execute()
        return uploaded.get("webViewLink") or f"https://drive.google.com/file/d/{uploaded['id']}/view"


# 以下は保存済みリプレイの検索と重複防止。
# /match の既存API（upload_replay_bytes）は維持する。
import re
import threading

_UPLOAD_LOCK = threading.RLock()
_REPLAY_NAME_RE = re.compile(r"(?:^|_)Match([0-9]+)\.dem\.(?:zst|bz2)$", re.IGNORECASE)


def _find_replay_folder(service, folder_name: str = DEFAULT_FOLDER_NAME):
    """検索時に新規フォルダを勝手に作らない。"""
    safe_name = folder_name.replace("'", "\\'")
    response = service.files().list(
        q=(f"name = '{safe_name}' and "
           "mimeType = 'application/vnd.google-apps.folder' and trashed = false"),
        spaces="drive",
        fields="nextPageToken,files(id,name)",
        pageSize=100,
    ).execute()
    folders = response.get("files", [])
    return folders[0]["id"] if folders else None


def _find_saved_replays_in_folder(service, folder_id: str, match_id: str):
    """Google Driveのページを走査し、マッチIDが完全一致するファイルのみ返す。"""
    result = []
    token = None
    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            spaces="drive",
            fields="nextPageToken,files(id,name,mimeType,size,createdTime,webViewLink)",
            pageSize=1000,
            pageToken=token,
        ).execute()
        for file in response.get("files", []):
            match = _REPLAY_NAME_RE.search(file.get("name", ""))
            if match and match.group(1) == match_id and file.get("mimeType") != "application/vnd.google-apps.folder":
                result.append(file)
        token = response.get("nextPageToken")
        if not token:
            break
    return sorted(result, key=lambda f: f.get("createdTime", ""), reverse=True)


def find_saved_replays(match_id: str):
    """保存済みの圧縮リプレイを検索。見つからない場合は空リスト。"""
    if not isinstance(match_id, str) or not match_id.isascii() or not match_id.isdecimal():
        raise ValueError("マッチIDは半角数字で指定してください")
    service = get_drive_service()
    folder_id = _find_replay_folder(service)
    if not folder_id:
        return []
    return _find_saved_replays_in_folder(service, folder_id, match_id)


def append_replay_caption(match_id: str, caption: str, thread_url: str) -> bool:
    """Append a short caption to the existing Drive file description.

    SQLite stores the complete, searchable history. Drive description is a secondary
    human-readable copy, trimmed to a safe length rather than growing indefinitely.
    """
    if not match_id.isascii() or not match_id.isdecimal():
        raise ValueError('invalid match ID')
    with _UPLOAD_LOCK:
        service = get_drive_service()
        folder = _find_replay_folder(service)
        if not folder:
            return False
        entries = _find_saved_replays_in_folder(service, folder, match_id)
        if not entries:
            return False
        file_id = entries[0]['id']
        metadata = service.files().get(fileId=file_id, fields='id,description').execute()
        original = metadata.get('description') or ''
        addition = f'\n[{match_id}] {caption[:400]} | {thread_url}'
        # Description is a convenience, not the source of truth.
        updated = (original + addition)[-15000:]
        service.files().update(fileId=file_id, body={'description': updated}, fields='id').execute()
        return True
