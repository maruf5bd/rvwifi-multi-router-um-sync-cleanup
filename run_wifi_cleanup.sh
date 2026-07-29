#!/bin/bash
# Run by cron: syncs lite data, then deletes expired users
cd /root || exit 1
python3 /root/sync_lite.py
python3 /root/delete_expired_codes.py
