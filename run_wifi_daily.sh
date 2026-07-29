#!/bin/bash
python3 /root/sync_lite.py --full >> /root/cron_full.log 2>&1
python3 /root/delete_expired_codes.py >> /root/cron_cleanup.log 2>&1
