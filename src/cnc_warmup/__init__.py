"""Generate CNC machine warm-up programs.

Machine and warm-up definitions are read from TOML, turned into a
controller-neutral plan, and rendered as Heidenhain TNC 640 Klartext or
Fanuc 31i NC programs.
"""

from importlib.metadata import version

__version__ = version("cnc-warmup")
