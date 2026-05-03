import os

import machine

OTA_MAIN_FILE = "main.py"
OTA_BACKUP_FILE = "main.py.bak"
OTA_PENDING_FILE = "ota_pending.json"
OTA_BOOTING_FILE = "ota_booting"
OTA_WDT_TIMEOUT_MS = 8000

OTA_WDT = None


def file_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def remove_file(path):
    try:
        os.remove(path)
    except OSError:
        pass


def sync_filesystem():
    try:
        os.sync()
    except Exception:
        pass


def restore_backup():
    if not file_exists(OTA_BACKUP_FILE):
        print("OTA rollback requested, but no backup exists")
        return False

    remove_file(OTA_MAIN_FILE)
    os.rename(OTA_BACKUP_FILE, OTA_MAIN_FILE)
    print("OTA rollback restored {}".format(OTA_BACKUP_FILE))
    return True


def mark_booting():
    with open(OTA_BOOTING_FILE, "w") as f:
        f.write("1")
    sync_filesystem()


if file_exists(OTA_PENDING_FILE) and file_exists(OTA_BOOTING_FILE):
    print("OTA previous boot did not become healthy; rolling back")
    try:
        restore_backup()
    except Exception as exc:
        print("OTA rollback failed: {}".format(exc))
    remove_file(OTA_PENDING_FILE)
    remove_file(OTA_BOOTING_FILE)
    sync_filesystem()
elif file_exists(OTA_PENDING_FILE):
    try:
        mark_booting()
        OTA_WDT = machine.WDT(timeout=OTA_WDT_TIMEOUT_MS)
        OTA_WDT.feed()
        print("OTA watchdog armed")
    except Exception as exc:
        print("OTA watchdog unavailable: {}".format(exc))
