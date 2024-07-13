#!/usr/bin/env python

import typer
import keyring
import getpass


app = typer.Typer()


@app.command()
def process():
    keyring.delete_password('backup_siteground', 'default')
    print('Vault password has been deleted from the keyring.')


if __name__ == "__main__":
    app()
