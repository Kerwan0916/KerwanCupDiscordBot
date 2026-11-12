import logging

from .bot import KerwanCupBot
from .config import load_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    settings = load_settings()
    KerwanCupBot(settings).run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
