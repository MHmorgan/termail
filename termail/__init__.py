

from pathlib import Path
from email.message import EmailMessage
from typing import Any, TypeAlias

import click
from click import echo, secho
from imap_tools import MailBox
from sqlalchemy.orm import Session

from .message import Message
from .common import header, info, warn, err, bail
from .config import Config, FolderConfig
from .context import Context

version = '1.0.0'


def run():
    from .common import Error
    try:
        cli()  # pylint: disable=no-value-for-parameter
    except Error as e:
        bail(e)
    except KeyboardInterrupt:
        echo('Keyboard interrupt. Stopping.')


def fmt(obj: Any, **kwargs):
    '''Format an object for pretty-printing.'''
    import arrow
    from pprint import pformat
    from re import sub
    #
    # Strip away the parent for one-item collections
    #
    try:
        while len(obj) == 1 and isinstance(obj, (list, tuple)):
           obj = obj[0]
    except TypeError:
        pass
    #
    # Empty objects should be empty without any indication about their type
    #
    if not obj and not isinstance(obj, bool):
        return ''
    #
    # Clean up strings
    #
    if isinstance(obj, str):
        obj = sub(r'(^\s*|\s*$)', '', obj)
        return sub('(\s*\n){2,}', '\n\n', obj)
    #
    # Print dates on local time
    #
    if isinstance(obj, arrow.Arrow):
        return obj.to('local').format('DD MMM YYYY HH:mm:ss Z')
    #
    # Generic pretty-printing for other types
    #
    kwargs.setdefault('indent', 4)
    kwargs.setdefault('compact', True)
    kwargs.setdefault('underscore_numbers', True)
    return pformat(obj, **kwargs)


def remote_messages(
    ctx: Context,
    folders: list[FolderConfig],
    limit: int | slice | None = None,
    show_bar: bool = False,
) -> list[Message]:
    '''Fetch messages from the folders.
    
    Number of messages fetched from each folder may be limited
    with `limit`.
    '''
    from contextlib import nullcontext
    #
    # Setup progress bar
    #
    progressbar = click.progressbar(
        folders,
        length=len(folders),
        item_show_func=lambda s: s,
        update_min_steps=0,
        show_eta=False,
    )
    progressbar = progressbar if show_bar else nullcontext(folders)

    with progressbar as bar:
        update = bar.update if show_bar else lambda *_: None
        messages = []
        #
        # Fetch messages from folders
        #
        for i, f in enumerate(folders, start=1):
            update(1 if i > 1 else 0, f'{f.name} ({f.host})')
            with MailBox(f.host).login(f.user, f.password, f.folder) as mb:
                tmp = mb.fetch(limit=limit, mark_seen=False, reverse=True, bulk=True)
                messages.extend(Message.from_mail_message(f, mail) for mail in tmp)

    if ctx.debug:
        info(f'Fetched {len(messages)} messages from {i} folder(s)')
    return messages


def cached_messages(
    ctx: Context,
    folders: list[str] | list[FolderConfig] | None = None,
    limit: int | str | None = None,
    sort: str = 'date',
    reverse: bool = False,
) -> list[Message]:
    '''Fetch messages from the cache.'''
    from sqlalchemy import select, desc
    
    if folders and isinstance(folders[0], FolderConfig):
        folders = [f.name for f in folders]
    #
    # Fetch messages from cache
    #
    with Session(ctx.engine) as s:
        stmt = select(Message).order_by(desc(Message.timestamp))
        if folders:
            stmt = stmt.where(Message.config_name.in_(folders))
        if limit:
            stmt = stmt.limit(limit)
        messages = [row[0] for row in s.execute(stmt)]
    if ctx.debug:
        info(f'Fetched {len(messages)} messages from cache')
    #
    # Sort messages
    #
    match sort:
        case 'subject':
            def sorter(m): return m.subject
        case 'from':
            def sorter(m): return m.from_
        case 'config':
            def sorter(m): return m.config_name
        case _:
            def sorter(m): return m.date
    messages = sorted(messages, key=sorter)
    if reverse:
        messages = reversed(messages)
    return list(messages)


def update_cache(ctx: Context, show_bar: bool = False):
    '''Update the cache with new messages from the servers.'''
    from sqlalchemy import delete
    #
    # Fetch messages from all configured folders
    #
    folders = list(ctx.config.folders())
    messages = remote_messages(ctx, folders, show_bar=show_bar)
    if ctx.verbose:
        info(f'Got {len(messages)} messages from {len(folders)} folders')
    if not messages:
        return
    #
    # Update cache
    #
    with Session(ctx.engine) as s:
        # Could probably only remove outdated messages and just add the new ones
        # pretty easily with the power of sets, instead of deleting the entire
        # database content on each update. But... Nah. This is easy.
        s.execute(delete(Message))
        s.add_all(messages)
        # TODO: Gracefully handle database constraint exceptions
        s.commit()
    # Last message must be deleted here since we cannot guarantee that the
    # stored message id still is valid after the database updates.
    del ctx.config.last_message


def send_mail(ctx: Context, email: EmailMessage):
    from smtplib import SMTP
    to = email['To']
    frm = email['From']
    sub = email['Subject']
    info(f'Sending "{sub}" to {to}')
    with SMTP('localhost') as smtp:
        smtp.send_message(email, frm, to)


def set_flag(ctx: Context, msg: Message, flag: str, val: bool):
    '''Set a message flag.'''
    from sqlalchemy import update
    #
    # Don't do anything if the flag already has correct value
    #
    if not (val ^ (flag in msg.flags)):
        return
    #
    # Set flag on server
    #
    f = ctx.config.folder(msg.config_name)
    with MailBox(f.host).login(f.user, f.password, f.folder) as mb:
        mb.flag(str(msg.uid), flag, val)
    #
    # Update flag for cached message
    #
    if val:
        new_flags = (flag, *msg.flags)
    else:
        new_flags = tuple(f for f in msg.flags if f != flag)
    if ctx.debug:
        info(f'set_flag: New flags in cache entry: {new_flags}')
    with Session(ctx.engine) as s:
        s.execute(
            update(Message).
            where(Message.id == msg.id).
            values(flags=new_flags)
        )
        s.commit()
    msg.flags = new_flags


def delete_msg(ctx: Context, msg: Message):
    '''Delete a message both remote and from cache.'''
    from sqlalchemy import delete
    #
    # Delete message on the server
    #
    assert msg.uid is not None
    f = ctx.config.folder(msg.config_name)
    with MailBox(f.host).login(f.user, f.password, f.folder) as mb:
        mb.delete(str(msg.uid))
    #
    # Delete message from cache
    #
    with Session(ctx.engine) as s:
        s.execute(
            delete(Message).
            where(Message.id == msg.id)
        )
        s.commit()


Pattern: TypeAlias = 'str | list[str] | tuple[str]'


def select_msg(ctx: Context, pattern: Pattern, interactive: bool) -> Message:
    '''Select a message from cache with `val` as identifying pattern.
    
    If `val` is empty the last targeted message is returned.
    '''
    from sqlalchemy import or_, select
    from prompt_toolkit.shortcuts import radiolist_dialog
    from .common import Error
    #
    # Database select filter
    #
    if not pattern:
        exp = Message.id == ctx.config.last_message
    else:
        if isinstance(pattern, str):
            pattern = (pattern,)
        exp = or_(*[Message.subject.contains(ptn) for ptn in pattern])
    #
    # Fetch matching messages from cache
    #
    with Session(ctx.engine) as s:
        rows = s.execute(
            select(Message)
            .where(exp)
        ).all()
        messages = [row[0] for row in rows]
    #
    # Determine the correct target message
    #
    match messages:
        case [msg]:
            pass
        case [*messages] if messages:
            if not interactive:
                raise Error('Multiple messages matched search pattern.')
            msg = radiolist_dialog(
                title='Messages',
                text='Select the message to read',
                values=[(msg, msg.subject) for msg in messages]
            ).run() or exit(1)
        case _:
            raise Error('No message matched search pattern.')
    ctx.config.last_message = msg.id
    return msg


def select_folders(ctx: Context, pattern: Pattern, all_folders: bool) -> list[FolderConfig]:
    '''Select folders configurations by matching pattern against folder names.'''
    from .common import Error
    #
    # Return all folders if there's no pattern
    #
    if not pattern:
        folders = [f for f in ctx.config.folders() if all_folders or f.primary]
        if not folders:
            primary = '' if all_folders else 'primary '
            raise Error(f'No {primary}folders to list messages from.')
        return folders

    if isinstance(pattern, str):
        pattern = (pattern,)

    def match(name):
        return all(p in name for p in pattern)

    #
    # Find matching folders
    #
    folders = [f for f in ctx.config.folders() if match(f.name)]
    if not folders:
        raise Error('No folders matching pattern.')
    if all_folders or not any(f.primary for f in folders):
        return folders
    return [f for f in folders if f.primary]


def pretty_print_messages(ctx: Context, messages: list[Message]):
    '''Print a pretty list of messages.'''
    from textwrap import shorten
    from shutil import get_terminal_size

    if not messages:
        return

    def fit(text, width):
        if not width:
            return ''
        text = shorten(text, width, break_long_words=True)
        return f'{text:<{width}}'

    #
    # Width calculations
    #
    cols, _ = get_terminal_size()
    date_width = 12
    from_width = max(len(m.from_) for m in messages) + 2
    conf_width = max(len(m.config_name) for m in messages) + 2
    min_sub_width = 80
    sub_width = cols - date_width - from_width - conf_width
    # Remove sender address if line's too long
    if sub_width < min_sub_width:
        sub_width += from_width
        from_width = 0
    # Remove folder field if line's still too long
    if sub_width < min_sub_width:
        sub_width += conf_width
        conf_width = 0
    # Finally, remove date if line's still too long
    if sub_width < min_sub_width:
        sub_width += date_width
        date_width = 0
    #
    # Print messages
    #
    for msg in messages:
        sub = fit(msg.subject, sub_width).strip()
        frm = fit(msg.from_, from_width)
        conf = fit(msg.config_name, conf_width)
        date = fit(msg.date.strftime('%Y-%m-%d'), date_width)
        echo(f'{conf}{date}{frm}{sub}')

################################################################################
#                                                                              #
# Main CLI
#                                                                              #
################################################################################
# {{{

#
# Argument for single message commands.
#
single_message = click.argument(
    'msg',
    metavar='[PATTERN]...',
    nargs=-1,
    callback=lambda ctx, _, val: select_msg(ctx.obj, val, True)
)


@click.group()
@click.version_option(version)
@click.option('-v', '--verbose', is_flag=True, help='Be verbose.')
@click.option('-q', '--quiet', is_flag=True, help='Be quiet.')
@click.option('--debug', is_flag=True, help='Run with debugging information.')
@click.option('--echo-db', is_flag=True, help='Echo database queries.')
@click.pass_context
def cli(ctx, verbose, quiet, debug, echo_db):
    '''Read, send and manage mail from the terminal.

    \b
    Single message commands:
        read, details, flag, unflag, delete

    These commands target a single mail message. This mail may be selected by
    supplying one or more arguments which are matched agains mail subject. If no
    arguments are given the last message targeted in one of these single message
    commands are used.

    \b
    Listing commands:
        list, flagged, unread

    Listing commands lists mail messages with different filters.
    '''
    from sqlalchemy import create_engine
    from termail import Config
    from termail.message import Base
    from termail.context import Context
    #
    # Application config setup
    #
    app_dir = Path(click.get_app_dir('termail'))
    app_dir.mkdir(parents=True, exist_ok=True)
    conf_file = app_dir / 'termail.conf'
    conf = Config(conf_file)
    #
    # Database setup
    #
    engine = create_engine('sqlite:///db.sqlite', echo=echo_db)
    Base.metadata.create_all(engine)
    #
    # Application context setup
    #
    ctx.obj = Context(
        config=conf,
        engine=engine,
        verbose=verbose,
        quiet=quiet,
        debug=debug,
    )


@cli.command()
@single_message
@click.option('--html', is_flag=True, help='Dump the message HTML instead of human friendly text.')
@click.pass_obj
def read(ctx: Context, msg: Message, html: bool):
    '''Read a mail message.
    
    PATTERN may be one or more strings used to identify the target message for
    this command. If no arguments are given the last message targeted in a
    single message command is used.
    '''
    set_flag(ctx, msg, r'\Seen', True)
    set_flag(ctx, msg, 'TERMAIL', True)
    if html:
        echo(msg.html)
        return
    #
    # Pretty print message
    #
    def print_section(name: str, val: Any):
        secho(f'{name.capitalize()}: ', nl=False, bold=True)
        echo(fmt(val))
    print_section('from', msg.from_)
    print_section('to', msg.to)
    print_section('cc', msg.cc)
    print_section('bcc', msg.bcc)
    print_section('date', msg.date)
    print_section('reply', msg.reply_to)
    header(msg.subject)
    echo(msg.pretty_text or '<no plain text>')
    

@cli.command(name='list')
@click.argument('pattern', nargs=-1)
@click.option('-u', '--update', is_flag=True, help='Update the cache before listing messages.')
@click.option('-s', '--sort',
              type=click.Choice(['date', 'subject', 'from', 'config']),
              help='Specify how to sort the messages.')
@click.option('-d', '--desc', is_flag=True, help='Sort in descending order.')
@click.option('-a', '--all', 'all_folders', is_flag=True, help='Display mail messages from all folders.')
@click.option('-l', '--limit', type=int, default=20, show_default=True, help='Message limit for each folder.')
@click.pass_obj
def list_(
    ctx: Context,
    pattern: tuple[str],
    update: bool,
    sort: bool,
    desc: bool,
    all_folders: bool,
    limit: int,
):
    '''List mail messages.

    Without NAME, messages from the primary folders are listed. With --all
    messages from all folders are listed.

    With NAME, messages from folders with a matching name is listed. If NAME
    matches both primary and non-primary folders only message from primary
    folders are listed, unless --all is given. If NAME matches only non-primary
    folders, messages from those folders are listed even without --all.
    '''
    if update:
        update_cache(ctx, show_bar=not ctx.quiet)
    folders = select_folders(ctx, pattern, all_folders)
    messages = cached_messages(ctx, folders, limit, sort, desc)
    fstr = ', '.join(f.name for f in folders)
    if not messages:
        bail(f'No messages found in folders: {fstr} (try running `termail update`)')
    if ctx.verbose:
        info(f'Listing messages from folders: {fstr}')
    pretty_print_messages(ctx, messages)


@cli.command()
@click.argument('pattern', nargs=-1)
@click.option('-u', '--update', is_flag=True, help='Update the cache before listing messages.')
@click.option('-s', '--sort',
              type=click.Choice(['date', 'subject', 'from', 'config']),
              help='Specify how to sort the messages.')
@click.option('-d', '--desc', is_flag=True, help='Sort in descending order.')
@click.option('-a', '--all', 'all_folders', is_flag=True, help='Display unread mail messages from all folders.')
@click.option('-l', '--limit', type=int, default=20, show_default=True, help='Message limit for each folder.')
@click.pass_obj
def unread(
    ctx: Context,
    pattern: tuple[str],
    update: bool,
    sort: bool,
    desc: bool,
    all_folders: bool,
    limit: int,
):
    '''List unread mail messages.'''
    if update:
        update_cache(ctx, show_bar=not ctx.quiet)
    folders = select_folders(ctx, pattern, all_folders)
    messages = cached_messages(ctx, folders, limit, sort, desc)
    fstr = ', '.join(f.name for f in folders)
    if not messages:
        bail(f'No messages found in folders: {fstr}')
    messages = [m for m in messages if not m.seen]
    if not messages and ctx.verbose:
        info(f'No unread messages in folders: {fstr}')
    elif ctx.verbose:
        info(f'Listing unread messages from folders: {fstr}')
    pretty_print_messages(ctx, messages)


@cli.command()
@click.argument('recipent')
@click.option('-s', '--subject', prompt=True, help='Subject of email.')
@click.option('-m', '--message',
              default='-',
              type=click.File(),
              help='File to read message text from. Reads from stdin by default.')
@click.pass_obj
def send(ctx, recipent, subject, message):
    '''Send a new mail to RECIPENT.'''
    # TODO: Open new messages to send in an editor, instead of reading from stdin
    # TODO: Support drafts - after editing a message may choose to send or save
    # TODO: New message templates - ex: use an HTML template with some nice CSS boilerplate
    msg = EmailMessage()
    msg['To'] = recipent
    msg['From'] = ctx.config.email
    msg['Subject'] = subject
    msg.set_content(message.read())
    send_mail(ctx, msg)
    if ctx.verbose:
        info(f'Mail sent to {recipent}')


@cli.command(name='update')
@click.pass_context
def update_(ctx):
    '''Update configured mail folders.'''
    update_cache(ctx.obj, show_bar=True)
    if ctx.obj.verbose:
        info(f'Updated all folders')


@cli.command()
@single_message
@click.option('-h', '--headers', is_flag=True, help='Print message headers. Not enabled by default since this makes the output noisy.')
@click.option('-S', '--no-shorten', is_flag=True, help='Do not shorten very long header values (only usefull with --headers).')
@click.pass_obj
def details(ctx, msg: Message, headers: bool, no_shorten: bool):
    '''Show details of a mail message.
    
    PATTERN may be one or more strings used to identify the target message for
    this command. If no arguments are given the last message targeted in a
    single message command is used.
    '''
    from textwrap import shorten
    header(shorten(msg.subject, 30, break_long_words=True))
    if not msg.text:
        echo('No plain text content.')
    echo(f'From: {msg.from_}')
    echo(f'To: {fmt(msg.to)}')
    echo(f'CC: {fmt(msg.cc)}')
    echo(f'BCC: {fmt(msg.bcc)}')
    echo(f'Reply to: {fmt(msg.reply_to)}')
    echo(f'Date: {msg.date_str}')
    echo(f'Size: {msg.size}')
    if msg.flags:
        flags = '\n  '.join(msg.flags)
        echo(f'Flags:\n  {flags}')
    else:
        echo('No flags.')
    if headers and msg.headers:
        echo('Headers:')
        for name, val in msg.headers.items():
            val = fmt(val)
            if not no_shorten:
                val = shorten(val, 30, break_long_words=True)
            echo(f'  {name}: {val}')
    elif headers:
        echo('No headers.')
    else:
        echo(f'{len(msg.headers)} headers.')


@cli.command()
@single_message
@click.pass_obj
def delete(ctx: Context, msg: Message):
    '''Delete a mail message.

    PATTERN may be one or more strings used to identify the target message for
    this command. If no arguments are given the last message targeted in a
    single message command is used.
    '''
    delete_msg(ctx, msg)
    del ctx.config.last_message
    if ctx.verbose:
        info(f'Deleted message "{msg.subject}"')


@cli.command()
@single_message
@click.pass_obj
def flag(ctx: Context, msg: Message):
    '''Flag a mail message.
    
    PATTERN may be one or more strings used to identify the target message for
    this command. If no arguments are given the last message targeted in a
    single message command is used.
    '''
    set_flag(ctx, msg, r'\Flagged', True)
    if ctx.verbose:
        info(f'Flagged message "{msg.subject}"')


@cli.command()
@single_message
@click.pass_obj
def unflag(ctx: Context, msg: Message):
    '''Unflag a mail message.
    
    PATTERN may be one or more strings used to identify the target message for
    this command. If no arguments are given the last message targeted in a
    single message command is used.
    '''
    set_flag(ctx, msg, r'\Flagged', False)
    if ctx.verbose:
        info(f'Unflaggde message "{msg.subject}"')


@cli.command()
@click.argument('pattern', nargs=-1)
@click.option('-u', '--update', is_flag=True, help='Update the cache before listing messages.')
@click.option('-s', '--sort',
              type=click.Choice(['date', 'subject', 'from', 'config']),
              help='Specify how to sort the messages.')
@click.option('-d', '--desc', is_flag=True, help='Sort in descending order.')
@click.option('-a', '--all', 'all_folders', is_flag=True, help='Display flagged mail messages from all folders.')
@click.option('-l', '--limit', type=int, default=20, show_default=True, help='Message limit for each folder.')
@click.pass_obj
def flagged(
    ctx: Context,
    pattern: tuple[str],
    update: bool,
    sort: bool,
    desc: bool,
    all_folders: bool,
    limit: int,
):
    '''List flagged mail messages.'''
    if update:
        update_cache(ctx, show_bar=not ctx.quiet)
    folders = select_folders(ctx, pattern, all_folders)
    messages = cached_messages(ctx, folders, limit, sort, desc)
    fstr = ', '.join(f.name for f in folders)
    if not messages:
        bail(f'No messages found in folders: {fstr}')
    messages = [m for m in messages if m.flagged]
    if not messages and ctx.verbose:
        info(f'No flagged messages in folders: {fstr}')
    elif ctx.verbose:
        info(f'Listing flagged messages from folders: {fstr}')
    pretty_print_messages(ctx, messages)
# }}}


################################################################################
#                                                                              #
# Script CLI
#                                                                              #
################################################################################
# {{{

@cli.group()
def script():
    '''Handle mail scripts.'''


@script.command(name='run')
@click.argument('name')
@click.pass_context
def run_(ctx, name):
    '''Run a termail script.'''
    from .script import execute
    execute(ctx.obj, 'print(FOLDERS.keys())')


@script.command()
@click.argument('name')
@click.pass_context
def edit(ctx, name):
    '''Edit a termail script.'''
#}}}


################################################################################
#                                                                              #
# Folder configs CLI
#                                                                              #
################################################################################
# {{{

@cli.group()
def folder():
    '''Manage and configure email folders. An email folder configuration
    contains the server login information needed to access a folder.

    All the main actions of termail rely on one or more folders being
    configured.
    '''


@folder.command()
@click.argument('name')
@click.argument('host')
@click.argument('folder')
@click.option('-u', '--user', prompt=True, help='User name for server login.')
@click.password_option(help='Password for server login.')
@click.option('--not-primary', is_flag=True, help='Do not mark this as a primary folder.')
@click.pass_context
def add(ctx, name, host, user, password, folder, not_primary):
    '''Add a new email folder. Called NAME, which references FOLDER on HOST.

    The folder is by default marked as a primary folder, meaning that its
    messages will be displayed by the `list` command without the --all flag.
    '''
    ctx.obj.config.add_server(
        name=name,
        host=host,
        user=user,
        password=password,
        folder=folder,
        primary=not not_primary
    )
    if ctx.obj.verbose:
        info(f'Added folder {name}')


@folder.command()
@click.argument('name')
@click.pass_context
def remove(ctx, name):
    '''Remove the folder with the given NAME.'''
    conf = ctx.obj.config
    try:
        del conf[name]
        conf.update()
    except KeyError:
        bail(f'Folder "{name}" not found (try `folder list` to see all folders)')
    if ctx.obj.verbose:
        info(f'Removed folder {name}')


@folder.command(name='list')
@click.pass_context
def list_(ctx):
    '''List all email folder names.'''
    folders = list(ctx.obj.config.folders())
    if not folders:
        return
    width = max(len(f.name) for f in folders)
    for f in folders:
        echo(f'{f.name:<{width}} ({f.host})')


@folder.command()
@click.argument('name')
@click.option('-p', '--password', is_flag=True, help='Show the password in plaintext.')
@click.pass_context
def show(ctx, name, password):
    '''Show details about a folder config.'''
    ctx: Context = ctx.obj
    config: Config = ctx.config
    if name not in config:
        bail(f'Folder "{name}" not found (try `folder list` to see all folders)')

    folder = config.folder(name)._asdict()
    folder['cached msg'] = len(cached_messages(ctx, [folder['name']]))

    width = max(len(k) for k in folder)
    for opt, val in folder.items():
        if opt == 'password' and not password:
            val = '*' * len(val)
        echo(f'{opt.capitalize():<{width}} : {val}')
#}}}


################################################################################
#                                                                              #
# Server helpers CLI
#                                                                              #
################################################################################
# {{{

@cli.group()
def server():
    '''Helper commands for inspecting and interacting with imap servers.
    
    Inteded to help when setting up new inboxes.
    '''


@server.command()
@click.argument('host')
@click.argument('user')
@click.password_option(help='Password for server login.')
@click.pass_context
def folders(ctx, host, user, password):
    '''List all folders for the given USER on the server.'''
    from imap_tools import MailBox, MailBoxFolderManager
    ctx = ctx.obj

    if ctx.verbose:
        info(f'Logging into {host} with user {user}')

    with MailBox(host).login(user, password) as mb:
        fm = MailBoxFolderManager(mb)
        folders = [f.name for f in fm.list()]
    echo('\n'.join(folders))


@server.command()
@click.argument('host')
@click.argument('user')
@click.argument('folder')
@click.password_option(help='Password for server login.')
@click.option('-l', '--limit', type=int, default=10, help='Limit of messages fetched from the server.')
@click.pass_context
def messages(ctx, host, user, folder, password, limit):
    '''List messages from a folder on an imap server.
    
    Usefull for exploring server content, but an email folder should be configured
    for regular use.
    '''
    from textwrap import shorten
    from imap_tools import MailBox, MailboxFolderSelectError
    ctx = ctx.obj

    if ctx.verbose:
        info(f'Logging into {host} with user {user}')

    #
    # Fetch messages from the imap server
    #
    try:
        with MailBox(host).login(user, password, folder) as mb:
            if ctx.verbose:
                info(f'Fetching {limit} messages from {folder}')
            messages = list(mb.fetch(limit=limit, mark_seen=False, reverse=True))
    except MailboxFolderSelectError:
        bail(f'Unknown mailbox: "{folder}"')

    #
    # Print the messages with minimal information
    #
    width = 80
    for msg in messages:
        sub = shorten(msg.subject, width)
        echo(f'{sub:<{width}} {msg.from_}')
#}}}


################################################################################
#                                                                              #
# Config CLI
#                                                                              #
################################################################################
# {{{

@cli.group()
def config():
    '''Manage application configurations.'''


@config.command()
@click.argument('email', required=False)
@click.pass_context
def email(ctx, email):
    '''Set your email address. Without an argument it prints your
    configured email.
    '''
    from configparser import Error
    config: Config = ctx.obj.config
    if email:
        config.email = email
    else:
        echo(config.email)
# }}}


# with MailBox(host).login(user, pwd) as mb:
#     s = '\n'.join(msg.subject for msg in mb.fetch())
#     echo(s)
#     msg = list(mb.fetch(mark_seen=False))[-1]
#     echo(f'{msg.uid=}')          # str | None: '123'
#     echo(f'{msg.subject=}')      # str: 'some subject 你 привет'
#     echo(f'{msg.from_=}')        # str: 'Bartölke@ya.ru'
#     echo(f'{msg.to=}')           # tuple: ('iam@goo.ru', 'friend@ya.ru', )
#     echo(f'{msg.cc=}')           # tuple: ('cc@mail.ru', )
#     echo(f'{msg.bcc=}')          # tuple: ('bcc@mail.ru', )
#     echo(f'{msg.reply_to=}')     # tuple: ('reply_to@mail.ru', )
#     echo(f'{msg.date=}')         # datetime.datetime: 1900-1-1 for unparsed, may be naive or with tzinfo
#     echo(f'{msg.date_str=}')     # str: original date - 'Tue, 03 Jan 2017 22:26:59 +0500'
#     echo(f'{msg.text=:40}')         # str: 'Hello 你 Привет'
#     echo(f'{msg.html=}')         # str: '<b>Hello 你 Привет</b>'
#     echo(f'{msg.flags=}')        # tuple: ('\\Seen', '\\Flagged', 'ENCRYPTED')
#     echo(f'{msg.headers.keys()=}')      # dict: {'received': ('from 1.m.ru', 'from 2.m.ru'), 'anti-virus': ('Clean',)}
#     echo(f'{msg.size_rfc822=}')  # int: 20664 bytes - size info from server (*useful with headers_only arg)
#     echo(f'{msg.size=}')         # int: 20377 bytes - size of received message
#     for att in msg.attachments:  # list: imap_tools.MailAttachment
#         echo(f'{att.filename=}')             # str: 'cat.jpg'
#         echo(f'{att.payload=}')              # bytes: b'\xff\xd8\xff\xe0\'
#         echo(f'{att.content_id=}')           # str: 'part45.06020801.00060008@mail.ru'
#         echo(f'{att.content_type=}')         # str: 'image/jpeg'
#         echo(f'{att.content_disposition=}')  # str: 'inline'
#         echo(f'{att.part=}')                 # email.message.Message: original object
#         echo(f'{att.size=}')                 # int: 17361 bytes
#     echo(f'{msg.obj=}')              # email.message.Message: original object
#     echo(f'{msg.from_values=}')      # imap_tools.EmailAddress | None
#     echo(f'{msg.to_values=}')        # tuple: (imap_tools.EmailAddress,)
#     echo(f'{msg.cc_values=}')        # tuple: (imap_tools.EmailAddress,)
#     echo(f'{msg.bcc_values=}')       # tuple: (imap_tools.EmailAddress,)
#     echo(f'{msg.reply_to_values=}')  # tuple: (imap_tools.EmailAddress,)
#     mb.flag(msg.uid, '\Seen', True)
#     mb.flag(msg.uid, '\Recent', True)
