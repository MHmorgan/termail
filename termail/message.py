
import pickle
from datetime import datetime

from arrow import Arrow
from imap_tools.message import MailMessage
from sqlalchemy import Column, Date, ForeignKey, Integer, LargeBinary, String, Float
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.types import BLOB, TypeDecorator

from .config import FolderConfig

Base = declarative_base()


class Pickled(TypeDecorator):
    '''SQLAlchemy column type which will pickle it's data'''
    impl = BLOB
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            value = pickle.dumps(value)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            value = pickle.loads(value)
        return value


class Message(Base):
    '''Message objects contain all the message data received from an IMAP server.
    
    These objects represent received emails, and can be stored in a database.
    For sending emails see `email.message.EmailMessage` from the standard library.
    '''

    __tablename__ = 'message'
    
    id: int = Column(Integer, primary_key=True)

    config_name: str = Column(String)
    server: str      = Column(String)
    folder: str      = Column(String)

    # Unique Identifier, are used to access a message on the server.
    uid: int | None = Column(Integer)     # str | None: '123'

    subject: str            = Column(String)      # str: 'some subject 你 привет'
    from_: str              = Column(String)      # str: 'Bartölke@ya.ru'
    to: tuple[str]          = Column(Pickled)     # tuple: ('iam@goo.ru', 'friend@ya.ru', )
    cc: tuple[str]          = Column(Pickled)     # tuple: ('cc@mail.ru', )
    bcc: tuple[str]         = Column(Pickled)     # tuple: ('bcc@mail.ru', )
    reply_to: tuple[str]    = Column(Pickled)     # tuple: ('reply_to@mail.ru', )
    date_str: str           = Column(String)      # str: original date - 'Tue, 03 Jan 2017 22:26:59 +0500'
    text: str               = Column(String)      # str: 'Hello 你 Привет'
    html: str               = Column(String)      # str: '<b>Hello 你 Привет</b>'
    flags: tuple[str]       = Column(Pickled)     # tuple: ('\\Seen', '\\Flagged', 'ENCRYPTED')
    headers: dict[str, str] = Column(Pickled)     # dict: {'received': ('from 1.m.ru', 'from 2.m.ru'), 'anti-virus': ('Clean',)}
    size_rfc822: int        = Column(Integer)     # int: 20664 bytes - size info from server (*useful with headers_only arg)
    size: int               = Column(Integer)     # int: 20377 bytes - size of received message

    # Not a part of the original imap message object and doesn't contain the
    # timezone information from `Message.date_str`.
    # The timestamp is only inteded to be user for ordering messages in
    # database queries.
    timestamp: float = Column(Float)

    attachments = relationship('Attachment', back_populates='message')

    @classmethod
    def from_mail_message(cls, folder: FolderConfig, mail: MailMessage):
        uid = int(mail.uid) if mail.uid else None
        return cls(
            config_name=folder.name,
            server=folder.host,
            folder=folder.folder,
            uid=uid,
            subject=mail.subject,
            from_=mail.from_,
            to=mail.to,
            cc=mail.cc,
            bcc=mail.bcc,
            reply_to=mail.reply_to,
            date_str=mail.date_str,
            text=mail.text,
            html=mail.html,
            flags=mail.flags,
            headers=mail.headers,
            size_rfc822=mail.size_rfc822,
            size=mail.size,
            timestamp=mail.date.timestamp(),
        )

    def __repr__(self) -> str:
        return f'<Message "{self.subject}" "{self.from_}"'

    def __hash__(self) -> int:
        # Use date_str since the datetime object loses some information
        # when stored in the database.
        return hash((self.uid, self.subject, self.date_str))

    def __eq__(self, other) -> bool:
        return hash(self) == hash(other)

    @property
    def date(self) -> Arrow:
        '''Message date parsed to an `arrow.Arrow` object.'''
        import arrow
        from arrow.parser import ParserError
        try:
            date = arrow.get(self.date_str, [
                arrow.FORMAT_RFC2822,
                arrow.FORMAT_RFC3339,
                arrow.FORMAT_W3C,
                arrow.FORMAT_COOKIE,
                'D MMM YYYY HH:mm:ss Z',
            ])
        except ParserError:
            date = arrow.get(self.timestamp)
        return date

    @property
    def pretty_text(self) -> str:
        '''Pretty formatted message text.'''
        # TODO: Format pretty message text from html
        from re import sub
        from click import style
        txt = sub(r'(^\s*|\s*$)', '', self.text)
        txt = sub(r'https://\S+', style('LINK', dim=True), txt)
        return sub('(\s*\n){2,}', '\n\n', txt)

    @property
    def seen(self) -> bool:
        return r'\Seen' in self.flags

    @property
    def flagged(self) -> bool:
        return r'\Flagged' in self.flags

    @property
    def answered(self) -> bool:
        return r'\Answered' in self.flags


class Attachment(Base):
    __tablename__ = 'attachment'

    id = Column(Integer, primary_key=True)

    message_id = Column(Integer, ForeignKey('message.id'))

    filename = Column(String)             # str: 'cat.jpg'
    payload = Column(LargeBinary)         # bytes: b'\xff\xd8\xff\xe0\'
    content_id = Column(String)           # str: 'part45.06020801.00060008@mail.ru'
    content_type = Column(String)         # str: 'image/jpeg'
    content_disposition = Column(String)  # str: 'inline'
    size = Column(Integer)                # int: 17361 bytes

    message = relationship('Message', back_populates='attachments')
