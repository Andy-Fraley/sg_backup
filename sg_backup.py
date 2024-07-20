#!/usr/bin/env python

#######################################################################################################################
# See README.txt for details on using this utility to pull backups out of SiteGround website hosting.
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
        backup_site: Annotated[List[str], typer.Option("--backup-site", help="The default (not using this option) " \
            "is to backup all sites listed in the vault credentials file. But by specifying one more more sites " \
            "with this option, you can backup just the specified subset of sites.  To backup site1_org and " \
            "site2_com, you would specify --backup-site site1_org --backup-site site2_com")] = None,
        backups_dir: Annotated[Optional[Path], typer.Option("--backups-dir", exists=True, dir_okay=True,
            file_okay=False, writable=True, resolve_path=True, help="Target directory for backups. If not " \
            "specified, defaults to ./backups directory in the same folder as this backups_siteground.py " \
            "utility.")] = None,
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
        no_email: Annotated[bool, typer.Option("--no-email", help="If specified, then notification emails are " \
            "not sent.")] = False,
        backup_now: Annotated[bool, typer.Option("--backup-now", help="If specified, then a target backups_dir " \
            "outside of default ./backups must also be specified.  Some (e.g. --backup-site site1 --backup-site " \
            "site2) or all (default) websites listed in vault.yml are backed up to the --backups_dir target " \
            "directory now independent of specified backup intervals in vault.yml.  In fact, websites without "
            "a backup schedule can only be backed up using this --backup-now flag.")] = False,
        test_ssh: Annotated[bool, typer.Option("--ssh-test", help="Cycle through all sites in vault.yml and test " \
            "SSH connectivity to each site.  Specifying subset of sites to test using --backup-site " \
            "parameters is fine.")] = False,
        logging_level: Annotated[
            LoggingLevel, typer.Option(case_sensitive=False)
            ] = LoggingLevel.warning.value):

    # Init
    global g

    # Make sure timezone info is correct and capture starting timestamp (in datetime and string formats)
    locale.setlocale(locale.LC_ALL, '')
    g.datetime_start = datetime.datetime.now()
    g.datetime_start_string = g.datetime_start.strftime(TIMESTAMP_FORMAT)

    # Grab directoy path to backups
    if backups_dir:
        g.backups_dir_path = str(backups_dir)
    else:
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

    # Gather up ERROR messages into a string to email to someone
    g.string_stream = io.StringIO()
    string_handler = logging.StreamHandler(g.string_stream)
    string_handler.setFormatter(logging_formatter)
    string_handler.setLevel(logging.NOTSET)
    string_handler.addFilter(EmailFilter())
    root_logger.addHandler(string_handler)

    # Create a hook to funnel all unhandled exceptions into errors
    sys.excepthook = except_hook

    # Before we start logging into backups directory, make sure its legit backups directory
    if backup_now:
        if not backups_dir:
            err_string = 'When --backup-now is specified, --backups-dir must also be provided to specify a ' \
                'location outside of normal backups directory for backup files.'
            logging.error(err_string)
            raise Exception(err_string)
        normal_backups_dir = Path(os.path.dirname(os.path.abspath(__file__)) + '/backups')
        specified_backups_dir = Path(backups_dir)
        if normal_backups_dir in specified_backups_dir.parents:
            err_string = 'When --backup-now is specified, provided --backups-dir cannot be within normal backups ' \
                f'location.  However {str(specified_backups_dir)} exists within {str(normal_backups_dir)} directory.'
            logging.error(err_string)
            raise Exception(err_string)

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
    if not no_email:
        if not 'gmail' in vault_data:
            err_string = f"'gmail' is a required section in {vault_file_path} file"
            logging.error(err_string)
            raise Exception(err_string)
        vault_data_gmail = vault_data['gmail']
        if not 'user' in vault_data_gmail or not 'password' in vault_data_gmail \
            or not 'notify_target' in vault_data_gmail:
            err_string = f"'user', 'password', and 'notify_target' are all required parameters in " \
                f"the 'gmail' section of {vault_file_path} file"
            logging.error(err_string)
            raise Exception(err_string)
        g.gmail_user = vault_data_gmail['user']
        g.gmail_password = vault_data_gmail['password']
        g.notification_target_email = vault_data_gmail['notify_target']

    # If user specified specific list of sites to backup, using --backup-site site1_com --backup-site site2_org, etc.,
    # then reduce sites being backed up to that subset of sites
    sites_data = vault_data['sites']
    abort = False
    if backup_site:
        for site in backup_site:
            if site not in sites_data:
                logging.error(f'Cannot find credentials in vault file for site {site}')
                abort = True
        if abort:
            err_string = 'Cannot find credentials in vault file for site(s).  Aborting...'
            logging.error(err_string)
            raise Exception(err_string)
        sites_data = dict((x, sites_data[x]) for x in backup_site)

    # Confirm required credentials provided for each site needed for backup access
    for site_name in sites_data:
        sites_data[site_name]['do_mysql_backup'] = confirm_keys(vault_file, sites_data, site_name, \
            ['ssh_hostname', 'ssh_username', 'ssh_port', 'mysql_user', 'mysql_password', 'mysql_db'])

    # Do simple SSH establishment to all sites if user specifies --test-ssh
    if test_ssh:
        # Force logging level up to at least INFO level to see results of an SSH test
        if logging_level_numeric > getattr(logging, 'INFO'):
            console_handler.setLevel(getattr(logging, 'INFO'))
            # Tamp down Paramiko INFO level logging if we're at INFO level globally, else it's too chatty
            # with logging INFO
            logging.getLogger("paramiko").setLevel(logging.WARNING)
        g.ssh_test_failure = False
        for site_name in sites_data:
            site_data = sites_data[site_name]
            ssh_test(site_name, site_data)
        if g.ssh_test_failure and logging_level_numeric >= getattr(logging, 'INFO'):
            print('SSH failure occurred.  Suggestion:  re-run with --logging-level DEBUG to debug failed ' \
                'SSH connection(s)')
        exit(0)

    # Allow for a triggered --backup-now which backs up all sites independent of backup schedule
    elif backup_now:
        for site_name in sites_data:
            site_data = sites_data[site_name]
            do_backup(site_name, site_data)

    # It not a "now" (--backup-now) backup, then do the backups if enough time has transpired that a new backup
    # is required per site backup schedules
    else:
        g.did_a_backup = False
        for site_name in sites_data:
            site_data = sites_data[site_name]
            backup_schedule = get_backup_schedule(site_data)
            do_backup_if_time(site_name, site_data, backup_schedule)

        if not g.did_a_backup:
            exit(0)

    du_string = '/usr/bin/du -d 2 -h ' + g.backups_dir_path
    logging.debug(f'Executing: {du_string}')
    try:
        exec_output = subprocess.check_output(du_string, stderr=subprocess.STDOUT, shell=True). \
            decode(sys.stdout.encoding)
        logging.info(f'Size of backups:\n{exec_output}')
    except subprocess.CalledProcessError as e:
        logging.error('du exited with error status ' + str(e.returncode) + ' and error: ' + e.output)

    error_string = g.string_stream.getvalue()
    if error_string:
        send_admin_email(error_string)


class EmailFilter(logging.Filter):
    def filter(self, record):
        if 'Completed backup' in record.msg or 'Size of backups' in record.msg or 'No backups to do' in record.msg \
           or record.levelname == 'ERROR' or record.levelname == 'CRITICAL':
            return True
        else:
            return False


def get_backup_schedule(site_data):
    if not 'backup_intervals' in site_data:
        return None
    backup_schedule = {}
    for backup_interval in site_data['backup_intervals']:
        if backup_interval not in BACKUP_INTERVALS.keys():
            err_string = f"Specified backup interval, '{backup_interval}', must be one of: " \
                f"{', '.join(BACKUP_INTERVALS.keys())}. Aborting..."
            logging.error(err_string)
            raise Exception(err_string)
        backup_schedule[backup_interval] = int(site_data['backup_intervals'][backup_interval])
        assert(backup_schedule[backup_interval] >= 0)
    return backup_schedule


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


def get_current_tracker_and_backups(site_name):
    global g

    existing_backups = get_existing_backups(site_name)
    backups_tracker_current = get_current_backups_tracker(site_name)

    # To ensure backup set and corresponding backups tracker JSON file are in sync, we need to compare set of
    # existing_backups -> backups_tracker_current, checking for missing. And have to do vice versa,
    # comparing backup_tracker_current -> existing_backups, checking for missing
    set_of_existing = sorted({ x[1] for x in existing_backups })
    set_of_tracked = sorted({ x for list_values in backups_tracker_current.values() for x in list_values })
    missing_from_tracked = []
    missing_from_existing = []
    if set_of_existing != set_of_tracked:
        logging.debug(f'existing_backups: {existing_backups}')
        logging.debug(f'set_of_exising: {set_of_existing}')
        logging.debug(f'backups_tracker_current: {backups_tracker_current}')
        logging.debug(f'set_of_tracked: {set_of_tracked}')
        for existing_element in set_of_existing:
            if existing_element not in set_of_tracked:
                missing_from_tracked.append(existing_element)
        missing_from_tracked.sort()
        for tracked_element in set_of_tracked:
            if tracked_element not in set_of_existing:
                missing_from_existing.append(tracked_element)
        missing_from_existing.sort()
        error_string = ''
        if len(missing_from_existing) > 0:
            error_string = f"The following elements in {site_name} backups_tracker.json file do not have " \
                f"corresponding backups: {', '.join(missing_from_existing)}. "
        if len(missing_from_tracked) > 0:
            error_string += f"The following elements in {site_name} set of backups do not have " \
                f"corresponding entry in backups_tracker.json file: {', '.join(missing_from_tracked)}."
        raise Exception(error_string)
    return (backups_tracker_current, existing_backups)


def do_backup_if_time(site_name, site_data, backup_schedule):
    # Never do backups for websites with no backup schedule in vault.yml
    if backup_schedule is None:
        logging.info(f'Site {site_name} does not have backup schedule.  Skipping timed backup for this site')
        return
    
    # Get current backup tracker info from JSON file and scan existing backups on disk *and* ensure they
    # are in sync (else Exception is thrown)
    (backups_tracker_current, existing_backups) = get_current_tracker_and_backups(site_name)

    backups_tracker_new = {}
    do_new_backup = False

    ###################################################################################################################
    # FIRST question: Do we need to do a new backup, and if so, on what backup interval (note: a single backup
    # can apply to multiple intervals, like daily, weekly, monthly, simultaneously)
    ###################################################################################################################
    if len(existing_backups) == 0:
        # Do first backup for this site
        do_new_backup = True
        for backup_interval in backup_schedule.keys():
            backups_tracker_new[backup_interval] = [g.datetime_start_string]

    else:
        for backup_interval in backup_schedule.keys():
            if backup_interval in backups_tracker_current:
                # Figure if we have to do backup for any of the backup intervals
                most_recent_backup_stamp = backups_tracker_current[backup_interval][-1]
                most_recent_backup_datetime = datetime.datetime.strptime(strip_zip(most_recent_backup_stamp),
                    TIMESTAMP_FORMAT)
                logging.debug(f'Most recent for {backup_interval} interval was {most_recent_backup_stamp}')
                logging.debug(f'Time diff in secs for {backup_interval} is '\
                    f'{g.datetime_start - most_recent_backup_datetime}')
                if g.datetime_start - most_recent_backup_datetime > BACKUP_INTERVALS[backup_interval]:
                    logging.info(f'Doing new backup for site {site_name} on {backup_interval} schedule')
                    backups_tracker_new[backup_interval] = [g.datetime_start_string]
                    do_new_backup = True
            else:
                backups_tracker_new[backup_interval] = [g.datetime_start_string]
                do_new_backup = True

    # Actually do the backup, retrieving WordPress DB and set of HTML files
    if do_new_backup:
        do_backup(site_name, site_data, existing_backups)
        g.did_a_backup = True
    else:
        logging.info(f'No backups to do for site {site_name}')

    # Now update the JSON tracker file to associate the new backup set with backup interval(s)
    backups_tracker_updated = merge_backups_trackers(site_name, backups_tracker_current, backups_tracker_new)
    json.dump(backups_tracker_updated, open(g.backups_dir_path + '/' + site_name + '/backups_tracker.json', 'w'))

    # Now again after updating tracker, get current backup tracker info from JSON file and scan existing backups
    # on disk *and* ensure they are in sync (else Exception is thrown)
    (backups_tracker_current, existing_backups) = get_current_tracker_and_backups(site_name)

    ###################################################################################################################
    # SECOND question: What old backups should be deleted?
    # Those backups no longer needed (aged out) across *all* backup intervals.  For example, if 'daily' backups
    # dereferences a backup but 'weekly' backups still refers to it, then the underlying backup should remain.  But,
    # if all backup_intervals ('daily', 'weekly', 'monthly', 'yearly') no longer reference a backup, then it should
    # be deleted.
    ###################################################################################################################
    set_of_backups_to_keep = set()
    backups_tracker_new = {}
    for backup_interval in backups_tracker_current:
        num_backups_to_keep = site_data['backup_intervals'][backup_interval]
        backups_for_interval = backups_tracker_current[backup_interval]
        keep_list = backups_for_interval
        if num_backups_to_keep != 0 and len(backups_for_interval) > num_backups_to_keep:
            num_to_drop = len(backups_for_interval) - num_backups_to_keep
            keep_list = backups_for_interval[-num_backups_to_keep:]
        for backup_to_keep in keep_list:
            set_of_backups_to_keep.add(backup_to_keep)
        backups_tracker_new[backup_interval] = sorted(keep_list)
    set_of_all_backups = { x for list_values in backups_tracker_current.values() for x in list_values }
    to_be_deleted = set_of_all_backups - set_of_backups_to_keep
    delete_backups(site_name, to_be_deleted)
    json.dump(backups_tracker_new, open(g.backups_dir_path + '/' + site_name + '/backups_tracker.json', 'w'))

    # Now again after updating tracker, get current backup tracker info from JSON file and scan existing backups
    # on disk *and* ensure they are in sync (else Exception is thrown)
    (backups_tracker_current, existing_backups) = get_current_tracker_and_backups(site_name)

    ###################################################################################################################
    # THIRD question: what old backups should be compressed?
    # Compress all but the most recent backup across all existing backups for this site.
    ###################################################################################################################

    set_based_backups_tracker = {}
    for backup_interval in backups_tracker_current:
        set_based_backups_tracker[backup_interval] = set(backups_tracker_current[backup_interval])
    if len(existing_backups) > 1:
        for backup in existing_backups[:-1]:
            if not is_zip_file(backup[1]):
                compress_backup(site_name, backup[1])
                for backup_interval in set_based_backups_tracker:
                    if backup[1] in set_based_backups_tracker[backup_interval]:
                        set_based_backups_tracker[backup_interval].remove(backup[1])
                        set_based_backups_tracker[backup_interval].add(backup[1] + '.zip')

    backups_tracker_new = {}
    for backup_interval in set_based_backups_tracker:
        backups_tracker_new[backup_interval] = sorted(list(set_based_backups_tracker[backup_interval]))
    json.dump(backups_tracker_new, open(g.backups_dir_path + '/' + site_name + '/backups_tracker.json', 'w'))

    # One last time after all work done, get current backup tracker info from JSON file and scan existing backups
    # on disk *and* ensure they are in sync (else Exception is thrown)
    (backups_tracker_current, existing_backups) = get_current_tracker_and_backups(site_name)


def strip_zip(file_name):
    if len(file_name) > 4:
        if file_name[-4:] == '.zip':
            return file_name[:-4]
    return file_name


def compress_backup(site_name, backup_directory_name):
    global g
    backup_directory_path = g.backups_dir_path + '/' + site_name + '/' + backup_directory_name
    backup_directory_zip = backup_directory_path + '.zip'
    assert(os.path.isdir(backup_directory_path))
    assert(not(os.path.isfile(backup_directory_zip)))
    logging.info(f'Compressing backup {backup_directory_path} into {backup_directory_zip} ' \
                  '(can take a while for large sites)')
    shutil.make_archive(backup_directory_path, 'zip', backup_directory_path)
    shutil.rmtree(backup_directory_path)
    logging.info(f'Compressed backup {backup_directory_path} into {backup_directory_zip}')
    return


def delete_backups(site_name, to_be_deleted):
    global g
    for delete_backup in to_be_deleted:
        delete_file_path = g.backups_dir_path + '/' + site_name + '/' + delete_backup
        if is_zip_file(delete_file_path):
            assert(os.path.isfile(delete_file_path))
            os.remove(delete_file_path)
            logging.info(f"Backup zip file '{delete_file_path}' deleted")
        else:
            assert(os.path.isdir(delete_file_path))
            shutil.rmtree(delete_file_path)
            logging.info(f"Backup file directory '{delete_file_path}' deleted")


def get_current_backups_tracker(site_name):
    backups_tracker_filename = g.backups_dir_path + '/' + site_name + '/backups_tracker.json'
    if os.path.isfile(backups_tracker_filename):
        with open(backups_tracker_filename) as backups_tracker_file:
            current_backups_tracker = json.load(backups_tracker_file)
    else:
        current_backups_tracker = {}
    return current_backups_tracker
    

def ssh_test(site_name, site_data):
    logging.info(f'Testing SSH connection to site {site_name}')
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    try:
        client.connect(site_data['ssh_hostname'], username=site_data['ssh_username'], port=int(site_data['ssh_port']))
        msg = [stdin, stdout, stderr] = client.exec_command('ls')
    except:
        logging.info(f'SSH test *** FAILED *** for site {site_name}!')
        g.ssh_test_failure = True
        return
    logging.info(f'SSH test succeeded for site {site_name}!')


def do_backup(site_name, site_data, existing_backups=None):
    global g

    # Make sure backup directories exist to backup into
    if not os.path.isdir(g.backups_dir_path):
        os.mkdir(g.backups_dir_path)
    if not os.path.isdir(g.backups_dir_path + '/' + site_name):
        os.mkdir(g.backups_dir_path + '/' + site_name)
    if not os.path.isdir(g.backups_dir_path + '/' + site_name + '/' + g.datetime_start_string):
        os.mkdir(g.backups_dir_path + '/' + site_name + '/' + g.datetime_start_string)
    if site_data['do_mysql_backup']:
        if not os.path.isdir(g.backups_dir_path + '/' + site_name + '/' + g.datetime_start_string + '/db'):
            os.mkdir(g.backups_dir_path + '/' + site_name + '/' + g.datetime_start_string + '/db')
    if not os.path.isdir(g.backups_dir_path + '/' + site_name + '/' + g.datetime_start_string + '/files'):
        os.mkdir(g.backups_dir_path + '/' + site_name + '/' + g.datetime_start_string + '/files')

    # Hook up logging output stream into target backup directory
    root_logger = logging.getLogger() # Grab root logger
    logging_formatter = logging.Formatter('%(asctime)s %(levelname)s\t%(message)s', '%Y-%m-%d %H:%M:%S')
    file_handler = logging.FileHandler(g.backups_dir_path + '/' + site_name + '/' + g.datetime_start_string + \
        '/messages.log')
    file_handler.setLevel(logging.DEBUG) # Into log files, write everything including DEBUG messages
    file_handler.setFormatter(logging_formatter)
    root_logger.addHandler(file_handler)
    
    logging.info(f'Starting backup for {site_name}')
    if site_data['do_mysql_backup']:
        dump_db(site_name, site_data)
    retrieve_html_files(site_name, site_data, existing_backups)

    logging.info(f'Completed backup for {site_name} in ' \
                  f"{g.backups_dir_path + '/' + site_name + '/' + g.datetime_start_string}")

    # Shutdown and remove per-backup logging handler
    file_handler.close()
    logging.getLogger().removeHandler(file_handler)


def do_work(work_to_do, site_name, site_data):
    if work_to_do.do_new_backup:
        do_backup(site_name, site_data)
        merge_tracked_backups(site_name, work_to_do.backups_tracker)


def merge_backups_trackers(site_name, backups_tracker_starting, backups_tracker_new):
    if backups_tracker_starting is None:
        return backups_tracker_new
    for backup_interval in backups_tracker_new:
        if backup_interval not in backups_tracker_starting:
            backups_tracker_starting[backup_interval] = []
            logging.info(f'Adding new backup interval into tracker, {backup_interval}, for site {site_name}')
        assert(len(backups_tracker_new[backup_interval]) == 1)
        backups_tracker_starting[backup_interval].append(backups_tracker_new[backup_interval][0])
    return backups_tracker_starting


# Returns True if keys include mysql keys for dumping a MySQL database, else returns False
def confirm_keys(vault_file, sites, site_name, full_key_list):
    key_list_wo_mysql = { x for x in full_key_list if 'mysql' not in x }
    mysql_keys = { x for x in full_key_list if 'mysql' in x }
    abort = False
    for key in key_list_wo_mysql:
        if not key in sites[site_name]:
            logging.error(f'Cannot find {key} for site {site_name} in {vault_file}')
            abort = True
    if abort:
        raise Exception(f'Missing required parameters for {site_name} in {vault_file}. Aborting...')
    mysql_parameters = { x for x in sites[site_name] if 'mysql' in x }
    if len(mysql_parameters) > 0:
        abort = False
        for key in mysql_keys:
            if not key in sites[site_name]:
                logging.error(f'Cannot find {key} for site {site_name} in {vault_file}')
                abort = True
        if abort:
            raise Exception(f'If any mysql parameters are provided, all must be provided for ' \
                f'{site_name} in {vault_file}. Aborting...')
    return len(mysql_parameters) > 0


def retrieve_html_files(site_name, site_data, existing_backups):
    html_files_dir = g.backups_dir_path + '/' + site_name + '/' + g.datetime_start_string + '/files'
    assert(os.path.isdir(html_files_dir))
    # Seed the target /files directory with files from the last backup to drastically reduce rsync
    # retrieval data and time
    first_rsync = True
    if existing_backups is not None and len(existing_backups) > 1:
        last_backup = existing_backups[-1][1]
        if not is_zip_file(last_backup):
            last_backup_path = g.backups_dir_path + '/' + site_name + '/' + last_backup + '/files'
            copy_string = 'cp -r ' + last_backup_path + '/* ' + html_files_dir
            logging.info(f'Copying files from {last_backup_path} to {html_files_dir} to accelerate rsync ' \
                          '(can take a while for large sites)')
            try:
                exec_output = subprocess.check_output(copy_string, stderr=subprocess.STDOUT, shell=True)
            except subprocess.CalledProcessError as e:
                err_string = 'cp exited with error status ' + str(e.returncode) + ' and error: ' + e.output
                logging.error(err_string)
                raise Exception(err_string)
            first_rsync = False
    log_string = f'Starting HTML file retrieval using rsync for site {site_name} to {html_files_dir}'
    if first_rsync:
        log_string += ' (can take a while since this is first rsync retrieval)'
    logging.info(log_string)
    rsync_string = '/usr/bin/rsync --append --delete -aviz -e "ssh -p ' + str(site_data['ssh_port']) + '" "' + \
        str(site_data['ssh_username']) + '@' + site_data['ssh_hostname'] + ':/home/' + site_data['ssh_username'] + \
        '/www/' + site_data['site_hostname'] + '/public_html/*" "' + html_files_dir + '"'
    logging.debug(f'Executing: {rsync_string}')
    try:
        exec_output = subprocess.check_output(rsync_string, stderr=subprocess.STDOUT, shell=True)
    except subprocess.CalledProcessError as e:
        rsync_err_string = 'rsync exited with error status ' + str(e.returncode) + ' and error: ' + str(e.output)
        logging.error(rsync_err_string)
        raise Exception(rsync_err_string)
    logging.info(f'Completed HTML file using rsync retrieval for site {site_name} to {html_files_dir}')


def dump_db(site_name, site_data):
    global g

    logging.info(f'Starting database dump for site {site_name}')
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.connect(site_data['ssh_hostname'], username=site_data['ssh_username'], port=int(site_data['ssh_port']))
    mysqldump_string = f"mysqldump -u {site_data['mysql_user']} -p{site_data['mysql_password']} " \
        f"{site_data['mysql_db']} >/home/{site_data['ssh_username']}/tmp/database.sql"
    mysqldump_string_star = f"mysqldump -u {site_data['mysql_user']} -p***** " \
        f"{site_data['mysql_db']} >/home/{site_data['ssh_username']}/tmp/database.sql"
    logging.debug(f"Executing this command over SSH: '{mysqldump_string_star}'")
    msg = [stdin, stdout, stderr] = client.exec_command(mysqldump_string)
    logging.debug(f'DB dump now under /tmp on server. Will do rsync to retrieve it')
    db_dump_filename = g.backups_dir_path + '/' + site_name + '/' + g.datetime_start_string + '/db/database.sql'
    rsync_string = '/usr/bin/rsync --append --delete -aviz -e "ssh -p ' + str(site_data['ssh_port']) + '" "' + \
        str(site_data['ssh_username']) + '@' + site_data['ssh_hostname'] + ':/home/' + site_data['ssh_username'] + \
        '/tmp/database.sql' + '" "' + db_dump_filename + '"'
    logging.debug(f'Executing: {rsync_string}')
    try:
        exec_output = subprocess.check_output(rsync_string, stderr=subprocess.STDOUT, shell=True)
    except subprocess.CalledProcessError as e:
        rsync_err_string = 'rsync exited with error status ' + str(e.returncode) + ' and error: ' + str(e.output)
        logging.error(rsync_err_string)
        raise Exception(rsync_err_string)
    logging.debug(f'DB dump retrieved. Now delete on server.')
    rm_string = f"rm /home/{site_data['ssh_username']}/tmp/database.sql"
    logging.debug(f"Executing this command over SSH: '{rm_string}'")
    msg = [stdin, stdout, stderr] = client.exec_command(rm_string)
    logging.debug(f'DB dump deleted on server.')
    db_dump_filename = g.backups_dir_path + '/' + site_name + '/' + g.datetime_start_string + '/db/database.sql'
    logging.info(f'Completed database dump for site {site_name} to {db_dump_filename}')


def ssh(client, cmd):
    out = []
    msg = [stdin, stdout, stderr] = client.exec_command(cmd)
    for item in msg:
        try:
            for line in item:
                out.append(line)
                logging.debug(f'Received line: {line}')
        except: pass
    return(list(out))


def except_hook(type,value,traceback):
    logging.error("Unhandled exception occured",exc_info=(type,value,traceback))
    # And because flow was interrupted, handled ERROR log entries will not be emailed to admin, so email
    # the thrown unhandled exception right here
    error_string = g.string_stream.getvalue()
    send_admin_email(f'backup_siteground encountered errors:\n{error_string}')


def send_email(recipient, subject, body):

    if not (g.gmail_user and g.gmail_password and recipient):
        return

    FROM = recipient
    TO = recipient if type(recipient) is list else [recipient]
    SUBJECT = subject
    TEXT = body

    # Prepare actual message
    message = """From: %s\nTo: %s\nSubject: %s\n\n%s
    """ % (FROM, ", ".join(TO), SUBJECT, TEXT)
    server_ssl = smtplib.SMTP_SSL("smtp.gmail.com", 465)
    server_ssl.ehlo() # optional, called by login()
    server_ssl.login(g.gmail_user, g.gmail_password)
    server_ssl.sendmail(FROM, TO, message)
    server_ssl.close()

    logging.info(f'Sent notification email to {recipient}')


def send_admin_email(body):
    if 'ERROR' in body:
        subject = 'backup_siteground.py encountered errors'
    else:
        subject = 'backup_siteground.py completed without errors'
    send_email(g.notification_target_email, subject, body)


if __name__ == "__main__":
    app()
