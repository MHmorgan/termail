from .config import Config
from dataclasses import dataclass
from sqlalchemy.engine import Engine

@dataclass
class Context:
    '''Application-wide context.'''

    config: Config
    engine: Engine

    verbose: bool = False
    quiet: bool = False
    debug: bool = False
