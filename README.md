# backup_siteground
Utility to backup websites created on SiteGround.com

This backup utility pulls from SiteGround by running an ssh mysqldump into a local file to collect WordPress database
and by running an rsync against the next-oldest backup (if one exists, else use rsync to populate new) for website
files.  It leaves the newest backup unzipped (as rsync target for next backup to significantly speed up rsync file
transfers) and zips all non-newest backups to save on local disk space.

Operation of this utility is controlled by the (encrypted) vault.yml configuration file.  Settings in the file are as
follows.

```
gmail:
    user: <gmail_username>
    password: <gmail_password>
    notify_target <email_address>
sites:
    site1:
        site_hostname: <domain.ext>
        ssh_key_file: <path_to_ssh_keyfile>
        ssh_key_passphrase: <passphrase_for_ssh_key>
        ssh_hostname: <ssh_host_for_ssh_traffic_for_website>
        ssh_username: <ssh_username>
        ssh_port: 18765
        mysql_user: <mysql_username> (optional, only for pulling MySQL DB for WordPress site)
        mysql_password: <mysql_password) (optional, only for pulling MySQL DB for WordPress site)
        mysql_db: <mysql_dbname> (optional, only for pulling MySQL DB for WordPress site)
        backup_intervals:
            hourly: <n>
            daily: <n>
            weekly: <n>
            monthly: <n>
            yearly: <n>
    site2:
        ...
    siteN:
        ...
```

<table><tr><td>
NOTE:  A sample vault file, sample_vault.yml, is provided as a template.  Its temporary password is 'password'.  To
change its password, use ansible-vault rekey sample_vault.yml.  And to use it, rename it vault.yml (which is the
default name for the credentials and settings config file for the sg_backup.py utility).
</td></tr></table>

SSH credentials for SiteGround can be found on site Devs | SSH Keys Manager page.  MySQL credentials are best found
in SiteGround under Site | File Manager and viewing wp-config.php.

SSH is used to backup the site using mysqldump over an SSH connection (established by Python-Paramiko) as well as rsync
over SSH.  To facilitate this, you *must* establish an SSH connection and set up SSH key file *and* establish an SSH
connection manually to the target host *before* trying to run this backup utility against the target host.  If you do
not establish and prove out a manual SSH connection from backup machine to the target SiteGround host, then trying to
use it as a backup target will almost certainly fail as sg_backup.py tries to establish that connection (and
hits an untrusted host or encounters some other sort of SSH key error).

To establish an SSH connection, create and download an SSH key from SiteGround and store in ~/.ssh (recommended file
name format domain_com__ssh_key) and chmod it to 600.  Then do ssh-add on the new key (you will be prompted for the
key's passphrase).  Then test it by doing 'ssh -p 18765 <ssh_username>@<ssh_hostname> -i
~/.ssh/domain_ext__ssh_key' to open a test connection.  If asked prompted about new your key like below, type
'yes' to continue.

    This key is not known by any other names.
    Are you sure you want to continue connecting (yes/no/[fingerprint])?

Once you establish a successful SSH connection from the host to your SiteGround SSH target site, then you may proceed
to run a backup against this new target using a site entry in the vault.yml file.

If the three mysql parameters are omitted for a site, then no WordPress database backup happens for that site, but HTML
files for the site are still retrieved.  This would be common for a static HTML site, to omit the mysql backup
parameters and only backup files from the site.

In the backup_intervals setting, <n> for backup_intervals is the number of backups to keep for that time interval.  If
a time interval, like hourly, is not specified, then no backups are kept for that time interval.  If <n> is specified
as "0" (zero), then backups at that time interval are never deleted.  It would be common to keep yearly backups
forever, for example.  So a typical backup with minimum backup granularity at weekly interval might look like: weekly:
5, monthly: 12, yearly: 0.  This basically says keep last 5 weekly backups (basically covering a month), then keep
monthly backups for a year, and then keep yearly backups forever.

Backups by default are stored in local ./backups directory.  There is one subfolder per site in the backups directory.
And under each site subfolder, there are date-stamped (YYYYmmddHHMMSS) subfolders or zip files, each with subfolders
'db' with one database.sql database backup file and 'files' with recursive content of the website's public HTML folder.
And all log messages emitted during the backup can be found in the 'messages.log' file that is also stored in the
date-stamped backup directory alongside the 'db' and 'files' subfolders.

Restoration from these backups is manual.  Will need to rename the database (in database.sql) to match target site.
And will need to reconfigure wp-config.php to match new database name as well as database user:password settings in the
target website.  Also, the target WordPress website will need to have all https:://xxx.yyy website references (except
GUIDs) search/replaced using wp command-line tool to match new domain website is reinstantiated on.

List of backed-up sites and credentials to access files and database on those sites is stored in vault.yml which is an
Ansible vault (view/edit using ansible-vault utility which can be installed using pip install ansible-vault).  Password
for this vault should be stored in a local keyring if running on a server like in a cron job.  To specify the vault
password in local machine's keyring, use utility specify_vault_password.py and to clear the vault password from local
machine's keyring, use utility clear_vault_password.py.

If minimum granularity of backup is weekly, then this utility should be run weekly using cron at exact same time each
week at a time when traffic against SiteGround won't be noticed in such a way as to interfere with website(s)
operation.  To tolerate some clock/execution skew, backup intervals have some flex.  For example, when seeing if next
weekly backup is due, it checks gap against next-oldest backup for >6.5 (not 7) days.  This is to ensure that a weekly
backup isn't skipped due to clock or execution time skew.

Since backups are typically run in a cron job on a server, to support notification emails, a Gmail account can be used
to send out notification emails when backups complete successfully or encounter errors.  To set up email notifications,
set up the gmail parameters.  The user:password parameters specify the Gmail account to be used to send notification
emails.  And the notify_target parameter is the email address you want notification emails sent to.  That Google
account must have an app password configured (requires establishment of 2-factor authentication on the account you want
to use for emails) and used as the password here.

If you fail part-way through a backup, it may leave around fragments of a backup.  So if you see an error message
like this:

    Exception: The following elements in domain_com set of backups do not have corresponding entry in
    backups_tracker.json file: 20240712230707.

Then you'll need to remove the partial backup files before you can continue.  For the example above, it's as simple as

    rm -rf ./backups/domain_com/20240712230707

Then fix whatever caused the backup to fail and rerun sg_backup.py.

For more usage details, run ./sg_backup.py --help
