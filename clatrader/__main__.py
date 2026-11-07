import logging

from .bot import ClaTrader
from .config import Config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ClaTrader(Config.from_env()).run()


if __name__ == "__main__":
    main()
