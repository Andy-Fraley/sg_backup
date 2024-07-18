#!/usr/bin/env python

#######################################################################################################################
# Utility for renaming plurality of backups in ./backups directory from --rename-from to --rename-to
#######################################################################################################################

import typer
from typing_extensions import Annotated, List, Optional
from ansible_vault import Vault
import paramiko
import os
import datetime
import re
from enum import Enum
import locale
import logging
import logging.handlers
import pathlib
import json
from pathlib import Path
import shutil
import subprocess
import getpass
import keyring
import sys
import io
import smtplib


app = typer.Typer()


TIMESTAMP_FORMAT = '%Y%m%d%H%M%S'

# Intervals are slightly less than stated backup interval to allow for jitter of cron kickoff timing.
# These are 'Live' backup intervals below.  Comment these out and use artificially short test intervals below
# in test mode.
BACKUP_INTERVALS = {
    'yearly': datetime.timedelta(days=364) + datetime.timedelta(hours=12),
    'monthly': datetime.timedelta(days=30),
    'weekly': datetime.timedelta(days=6) + datetime.timedelta(hours=12),
    'daily': datetime.timedelta(hours=23),
    'hourly': datetime.timedelta(minutes=55)
}

# These are VERY SHORT 'Test' mode intervals used to test backup timing and mechanics (rsync-in-place, archiving,
# deletion).  Comment these out and uncomment backup intervals above to operate in 'Live' mode.
#BACKUP_INTERVALS = {
#    'yearly': datetime.timedelta(seconds=3645),  # 3650
#    'monthly': datetime.timedelta(seconds = 295),  # 300
#    'weekly': datetime.timedelta(seconds=65),  # 70
#    'daily': datetime.timedelta(seconds=5),  # 10
#    'hourly': datetime.timedelta(seconds=1)  # 2
#}


class LoggingLevel(str, Enum):
    debug = 'DEBUG'
    info = 'INFO'
    warning = 'WARNING'
    error = 'ERROR'
    critical = 'CRITICAL'


# Global variables
class g:
    datetime_now = None
    datetime_stamp = None
    string_stream = None
    gmail_user = None
    gmail_password = None
    notification_target_email = None
    did_a_backup = None
    ssh_test_failure = None


@app.command()
def process(
        rename_from: Annotated[str, typer.Option("--rename-from", help="Date stamp (YYYYMMDDHHMMSS) of " \
            "current backup to be renamed.")],
        rename_to: Annotated[str, typer.Option("--rename-to", help="Date stamp (YYYYMMDDHHMMSS) to " \
            "rename to.  Often this is an earlier date stamp than --rename from in order to trigger interval " \
            "updates.")],
        vault_file: Annotated[Optional[Path], typer.Option("--vault-file", exists=True, file_okay=True, dir_okay=False,
            readable=True, resolve_path=True, help="Credentials and settings vault file which is an encrypted " \
            "ansible vault file. If not specified, defaults to ./vault.yml file in the same directory as this " \
            "backups_siteground.py utility.")] = None,
        use_keyring: Annotated[bool, typer.Option("--use-keyring", help="Using machine keyring to store the vault " \
            "credentials file password is useful for running this utility using cron without leaking the " \
            "credentials file password by storing it in a script or in a file. If this option is specified, then " \
            "the keyring value backup_siteground:vault_password is used as a password for the credentials vault " \
            "file (defaults to vault.yml). In addition, two simple utilities are are provided to set and clear " \
            "the vault password in the keyring on this machine. Use specify_vault_password.py to " \
            "store vault.yml password in keyring on this machine. And use clear_vault_password.py to clear " \
            "any stored vault password on this machine.  If you do not use this option, then you will be " \
            "prompted for the password on command line.")] = False,
        logging_level: Annotated[
            LoggingLevel, typer.Option(case_sensitive=False)
            ] = LoggingLevel.warning.value):

    # Init
    global g

    # Make sure timezone info is correct and capture starting timestamp (in datetime and string formats)
    locale.setlocale(locale.LC_ALL, '')
    g.datetime_start = datetime.datetime.now()
    g.datetime_start_string = g.datetime_start.strftime(TIMESTAMP_FORMAT)

    # Grab directory path to backups
    g.backups_dir_path = os.path.dirname(os.path.abspath(__file__)) + '/backups'

    # Set up base logging
    root_logger = logging.getLogger() # Grab root logger
    root_logger.setLevel(logging.NOTSET) # Ensure EVERYTHING is logged thru root logger (don't block anything there)
    logging_formatter = logging.Formatter('%(asctime)s %(levelname)s\t%(message)s', '%Y-%m-%d %H:%M:%S')

    # Echo messages to console (stdout)
    console_handler = logging.StreamHandler()
    logging_level_numeric = getattr(logging, logging_level, None) # Allow user to specify level of logging on console
    console_handler.setFormatter(logging_formatter)
    console_handler.setLevel(logging_level_numeric)
    root_logger.addHandler(console_handler)

    # Log into central messages files in the backups directory
    if not os.path.isdir(g.backups_dir_path):
        os.mkdir(g.backups_dir_path)
    if not os.path.isfile(g.backups_dir_path + '/messages.log'):
        Path(g.backups_dir_path + '/messages.log').touch()
    file_handler = logging.handlers.RotatingFileHandler(g.backups_dir_path + '/messages.log', maxBytes=10000000, \
        backupCount=1)
    file_handler.setLevel(logging.DEBUG) # Into log files, write everything including DEBUG messages
    file_handler.setFormatter(logging_formatter)
    root_logger.addHandler(file_handler)

    # Create a hook to funnel all unhandled exceptions into errors
    sys.excepthook = except_hook

    # Test rename_from and rename_to
    try:
        stripped = strip_zip(rename_from)
        assert(len(stripped) == 14)
        x = datetime.datetime.strptime(stripped, TIMESTAMP_FORMAT)
    except:
        logging.error(f"--rename-from argument '{rename_from} must be in YYYYMMDDHHMMSS format with optional " \
            '.zip ending.')
        exit(1)
    try:
        stripped = strip_zip(rename_to)
        assert(len(stripped) == 14)
        x = datetime.datetime.strptime(stripped, TIMESTAMP_FORMAT)
    except:
        logging.error(f"--rename-to argument '{rename_to} must be in YYYYMMDDHHMMSS format with optional " \
            '.zip ending.')
        exit(1)

    # Ensure if rename_from ends in '.zip' that rename_to also ends in '.zip'
    if len(rename_from) > 4 and rename_from[-4:] == '.zip':
        if len(rename_to) < 5 or rename_to[-4:] != '.zip':
            logging.error(f"If --rename-from argument '{rename_from}' ends in '.zip' then --rename-to argument " \
                f"'{rename_to}' must also end in '.zip'.")
            exit(1)
    elif len(rename_to) > 4 and rename_to[-4:] == '.zip':
        if len(rename_from) < 5 or rename_from[-4:] != '.zip':
            logging.error(f"If --rename-to argument '{rename_to}' ends in '.zip' then --rename-from argument " \
                f"'{rename_from}' must also end in '.zip'.")
            exit(1)

    # Open vault file with credentials and settings
    program_path = os.path.dirname(os.path.abspath(__file__))
    if vault_file:
        vault_file_path = vault_file
    else:
        vault_file_path = program_path + '/vault.yml'

    if use_keyring:
        vault_password = keyring.get_password('backup_siteground', 'default')
        if not vault_password:
            err_string = 'Use ./specify_vault_password.py to set password if you intend to use keyring'
            logging.error(err_string)
            raise Exception(err_string)
    elif os.path.isfile(program_path + '/.y4zwCKnyBvoPevYX'):
        with open(program_path + '/.y4zwCKnyBvoPevYX', 'r') as f:
            vault_password = f.readline().strip()
    else:
        vault_password = getpass.getpass('Password for vault.yml: ')

    vault = Vault(vault_password)
    vault_data = vault.load(open(vault_file_path).read())
    sites_data = vault_data['sites']

    for site_name in sites_data:
        if 'backup_intervals' not in sites_data[site_name]:
            continue
        existing_backups = get_existing_backups(site_name)
        backups_tracker_current = get_current_backups_tracker(site_name)
        print(f'Site: {site_name}')
        for existing_backup in existing_backups:
            if existing_backup[1] == rename_from:
                os.rename(f'{g.backups_dir_path}/{site_name}/{existing_backup[1]}',
                    f'{g.backups_dir_path}/{site_name}/{rename_to}')
                logging.info(f"Renamed '{g.backups_dir_path}/{site_name}/{existing_backup[1]}' " \
                    f"'{g.backups_dir_path}/" f"{site_name}/{rename_to}'")
                for backup_interval in backups_tracker_current:
                    for backup in backups_tracker_current[backup_interval]:
                        if backup == rename_from:
                            logging.info(f"Renaming '{backup_interval}' interval '{rename_from}' to '{rename_to}' " \
                                "in JSON tracker")
                            backups_tracker_current[backup_interval].remove(rename_from)
                            backups_tracker_current[backup_interval].append(rename_to)
        json.dump(backups_tracker_current, open(g.backups_dir_path + '/' + site_name + '/backups_tracker.json', 'w'))
        logging.info(f"Rewrote '{g.backups_dir_path}/{site_name}/backups_tracker.json' file")
    print('Done!')


def get_existing_backups(site_name):
    global g

    backup_path = g.backups_dir_path + '/' + site_name
    if not os.path.isdir(backup_path):
        return []

    backups_for_site = {}
    for subfolder in [ f.path for f in os.scandir(backup_path) if f.is_dir() or ( f.is_file() and \
        is_zip_file(f.path) ) ]:
        subfolder_leaf = os.path.basename(subfolder)
        subfolder_leaf_wo_ext = pathlib.Path(subfolder_leaf).stem
        try:
            backup_datetime = datetime.datetime.strptime(subfolder_leaf_wo_ext, TIMESTAMP_FORMAT)
        except:
            logging.warning(f'Found subfolder in {backup_path} without proper YYYYmmddHHMMSS ' \
                                        f'format: {subfolder_leaf}. Ignoring.')
            continue
        backups_for_site[backup_datetime] = subfolder_leaf
    return [ ( x, backups_for_site[x] ) for x in sorted(backups_for_site) ]


def is_zip_file(file_name):
    return os.path.splitext(file_name)[1] == '.zip'


def strip_zip(file_name):
    if len(file_name) > 4:
        if file_name[-4:] == '.zip':
            return file_name[:-4]
    return file_name


def get_current_backups_tracker(site_name):
    backups_tracker_filename = g.backups_dir_path + '/' + site_name + '/backups_tracker.json'
    if os.path.isfile(backups_tracker_filename):
        with open(backups_tracker_filename) as backups_tracker_file:
            current_backups_tracker = json.load(backups_tracker_file)
    else:
        current_backups_tracker = {}
    return current_backups_tracker
    

def except_hook(type,value,traceback):
    logging.error("Unhandled exception occured",exc_info=(type,value,traceback))


if __name__ == "__main__":
    app()
