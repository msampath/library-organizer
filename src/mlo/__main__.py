"""Enable `python -m mlo` as an alias for the `mlo` console script."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
