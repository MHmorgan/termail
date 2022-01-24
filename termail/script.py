'''The `script` module implements everything related to termail scripts.

Script environment
==================

FOLDERS
    Dictionary of all the configured folders. The keys are folder names
    and values are lists of all the messages in the folder.

'''

from .context import Context

def execute(ctx: Context, code: str):
    '''Execute the given code as a termail script.'''
    from termail import cached_messages

    folders = {
        f['name']: cached_messages(ctx, [f['name']])
        for f in ctx.config.folders()
    }

    globals = {
        'FOLDERS': folders,
    }

    exec(code, globals)
