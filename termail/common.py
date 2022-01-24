
from pyexpat.errors import messages
import sys
from click import echo, secho

def info(message):
    echo(f'[*] {message}')


def warn(message):
    secho(f'[!] {message}', err=True, fg='yellow')


def err(message):
    secho(f'[!!] {message}', err=True, fg='red')


def bail(*args, **kwargs):
    err(*args, **kwargs)
    sys.exit(1)


def header(message):
    '''Print a header with a border and padding.'''
    message = str(message)
    border = '=' * len(message)
    secho(f'\n{message}\n{border}\n', bold=True)


class Error(Exception):
    '''Base class for termail application exceptions.
    
    Errors of this kind should not require a stack trace - the error message
    itself should provide all necessary information to the user.
    '''
