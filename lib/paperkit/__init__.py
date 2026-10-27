"""paperkit: builds a Typst paper whose numbers, tables and figures come from data sources, and checks it
(README.md). A data source imports what it needs from here:

    from paperkit import Ledger, source
"""

from paperkit.data import source
from paperkit.ledger import Ledger

__all__ = ["Ledger", "source"]
