#!/usr/bin/env python3
"""
drive_setup.py — one-time: create the Drive inbox for bank-export CSVs.

Creates (idempotently) a "Relay Imports" folder + a "Done" subfolder owned by the
service account, shares the inbox with the owner so it appears in their Drive, and
writes the folder IDs to the Drive config for import_relay.py to use.

Drop any property's bank export into the inbox; routing is by the #account number in
the filename, so one folder serves all properties. Owner email / paths come from config.py.
"""
import json
import sys
import warnings

warnings.filterwarnings("ignore")

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import config

CREDS_FILE = config.CREDS_FILE
CONFIG_FILE = config.DRIVE_CONFIG
OWNER_EMAIL = config.OWNER_EMAIL
INBOX_NAME = "Relay Imports"
DONE_NAME = "Done"
FOLDER_MIME = "application/vnd.google-apps.folder"
SCOPES = ["https://www.googleapis.com/auth/drive"]


def drive():
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_folder(svc, name, parent=None):
    q = [f"name = '{name}'", f"mimeType = '{FOLDER_MIME}'", "trashed = false"]
    if parent:
        q.append(f"'{parent}' in parents")
    res = svc.files().list(q=" and ".join(q), fields="files(id, name)",
                           spaces="drive").execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def create_folder(svc, name, parent=None):
    meta = {"name": name, "mimeType": FOLDER_MIME}
    if parent:
        meta["parents"] = [parent]
    return svc.files().create(body=meta, fields="id").execute()["id"]


def ensure_shared(svc, folder_id, email):
    perms = svc.permissions().list(fileId=folder_id,
                                   fields="permissions(emailAddress, role)").execute()
    for p in perms.get("permissions", []):
        if p.get("emailAddress", "").lower() == email.lower():
            return False
    svc.permissions().create(
        fileId=folder_id,
        body={"type": "user", "role": "writer", "emailAddress": email},
        sendNotificationEmail=False,
    ).execute()
    return True


def main():
    svc = drive()

    inbox_id = find_folder(svc, INBOX_NAME)
    created_inbox = inbox_id is None
    if created_inbox:
        inbox_id = create_folder(svc, INBOX_NAME)

    done_id = find_folder(svc, DONE_NAME, parent=inbox_id)
    created_done = done_id is None
    if created_done:
        done_id = create_folder(svc, DONE_NAME, parent=inbox_id)

    shared = ensure_shared(svc, inbox_id, OWNER_EMAIL)

    with open(CONFIG_FILE, "w") as f:
        json.dump({"inbox_folder_id": inbox_id, "done_folder_id": done_id}, f, indent=2)

    print(f"Inbox '{INBOX_NAME}':  {inbox_id}   ({'created' if created_inbox else 'existing'})")
    print(f"  '{DONE_NAME}/':        {done_id}   ({'created' if created_done else 'existing'})")
    print(f"  shared with {OWNER_EMAIL}: {'newly shared' if shared else 'already had access'}")
    print(f"  link: https://drive.google.com/drive/folders/{inbox_id}")
    print(f"  wrote {CONFIG_FILE}")


if __name__ == "__main__":
    main()
