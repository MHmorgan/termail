from .common import Error
from typing import Iterator
from configparser import ConfigParser
from pathlib import Path
from collections import namedtuple


# name
#     Name of the server configuration.
# host
#     Host domain for the IMAP server.
# user
#     User name for server login.
# password
#     Password for server login.
# folder
#     The remote folder name.
# primary
#     Primary configuration flag (boolean).
#
FolderConfig = namedtuple('FolderConfig', 'name host user password folder primary')


class Config(ConfigParser):
    '''Termail configuration.

    All the sections of the configuration are email folder configurations,
    except the app section.
    '''

    _file: Path
    app_section: str = 'APPLICATION'

    def __init__(self, file: Path):
        super().__init__()
        self._file = file

        #
        # Verify correct config file mode, or setup a new config.
        #
        if file.exists():
            mode = file.stat().st_mode
            if mode & 0o077:
                raise ConfigUnsafe(
                    f"config file {file} should only be readable by user (current mode: 0o{mode:04o}")
            self.read_string(file.read_text())
        else:
            file.touch(0o0600)
            #
            # Default folder config values
            #
            self.set(self.default_section, 'folder', 'Inbox')
            self.set(self.default_section, 'primary', True)
            #
            # Default scripts directory
            #
            sdir = file.with_name('scripts')
            self.set(self.app_section, 'Scripts', str(sdir))
            self.update()

    def update(self):
        '''Write the config to file.'''
        with self._file.open('w') as f:
            self.write(f)

    # Folders

    def folders(self) -> Iterator[FolderConfig]:
        '''Iterate over all server configurations.'''
        yield from (self.folder(s) for s in self.sections() if s != self.app_section)

    def folder(self, name: str) -> FolderConfig:
        '''Return the named folder config.'''
        if name not in self:
            raise FolderConfigNotFound(name)
        conf = {'name': name, **self[name]}
        conf['primary'] = conf['primary'] == 'True'
        return FolderConfig(**conf)

    def add_folder(self, name: str, host: str, user: str, password: str, **options):
        '''Add a new server config.
        
        `options` are optional options which have default values.
        '''
        self.add_section(name)
        self.set(name, 'host', host)
        self.set(name, 'user', user)
        self.set(name, 'password', password)
        for opt, val in options.items():
            self.set(name, opt, str(val))
        self.update()

    def update_folder(self, conf: FolderConfig):
        '''Update a folder config.'''
        old = self.folder(conf.name)._asdict()
        new = conf._asdict()
        del new['name']
        changed = [(opt, val) for opt, val in new.items() if val != old[opt]]
        for opt, val in changed:
            self.set(conf.name, opt, val)
        if changed:
            self.update()

    # Email

    @property
    def email(self) -> str:
        '''Default email used to send messages.'''
        from configparser import NoOptionError
        try:
            return self.get(self.app_section, 'Email')
        except NoOptionError:
            raise EmailNotConfigured()

    @email.setter
    def email(self, val: str):
        self.set(self.app_section, 'Email', val)
        self.update()

    # Scripts dir

    @property
    def scripts_dir(self) -> Path:
        '''Location of termail scripts.'''
        from configparser import NoOptionError
        try:
            dir = Path(self.get(self.app_section, 'Scripts'))
        except NoOptionError:
            raise ScriptDirNotConfigured()
        dir.mkdir(parents=True, exist_ok=True)
        return dir

    @scripts_dir.setter
    def scripts_dir(self, val: str | Path):
        self.set(self.app_section, 'Scripts', str(val))
        self.update()

    # Last message

    @property
    def last_message(self) -> int:
        '''Database entry id of last target message.'''
        from configparser import NoOptionError
        try:
            return int(self.get(self.app_section, 'Last message'))
        except NoOptionError:
            raise NoLastMessage()

    @last_message.setter
    def last_message(self, val: int):
        self.set(self.app_section, 'Last message', str(val))
        self.update()

    @last_message.deleter
    def last_message(self):
        self.remove_option(self.app_section, 'Last message')
        self.update()


class ConfigUnsafe(Error):
    pass


class EmailNotConfigured(Error):
    def __init__(self):
        super().__init__('Email is not configured')


class ScriptDirNotConfigured(Error):
    def __init__(self) -> None:
        super().__init__('Scripts directory is not configured')


class FolderConfigNotFound(Error):
    def __init__(self, name: str) -> None:
        super().__init__(f'Folder config not found for "{name}"')


class NoLastMessage(Error):
    def __init__(self) -> None:
        super().__init__('Last message not found.')
