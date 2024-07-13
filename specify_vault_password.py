#!/usr/bin/env python

import typer
import keyring
import getpass


app = typer.Typer()


@app.command()
def process():
    vault_password = getpass.getpass('Password for vault.yml: ')
    keyring.set_password('backup_siteground', 'default', vault_password)
    print('Vault password has been configured in the keyring.  backup_siteground.py utility can now be used with ' \
          '--use-keyring flag to pull password from the keyring.')


if __name__ == "__main__":
    app()
