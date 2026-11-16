"""Generic core: nothing in here knows about any particular application."""

# ui is the import point of its parts (walk, find, ...): loading it here means
# a part is never imported before it, whichever module is imported first.
from . import ui as ui
