import typing as tp
import logging.config
import random
import time
from pinatapy import PinataPy
from connectivity.config.logging import LOGGING_CONFIG
from .pinning_gateway import (
    PinningGateway,
    PinArgs,
)


logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger("sensors-connectivity")

class PinataGateway(PinningGateway):

    def __init__(self, api_key: str, secret_key: str) -> None:
        self.api_key = api_key
        self.secret_key = secret_key

        self.max_attempts: int = 3
        self.base_backoff_s: float = 0.7    # sec
        self.max_backoff_s: float = 4.0     # sec


    def pin(self, args: PinArgs) -> None:
        """Pin file to Pinata for for better accessibility.
        Need to provide pinata credentials in the config file."""
        file_path: str = args.file_path

        log_prefix = f"PinataGateway: pin file={file_path}"

        for attempt in range(1, self.max_attempts + 1):
            try:
                logger.info(
                    "%s attempt=%d/%d",
                    log_prefix, attempt, self.max_attempts
                )

                pinata = PinataPy(self.api_key, self.secret_key)
                result = pinata.pin_file_to_ipfs(
                    path_to_file=file_path,
                    save_absolute_paths=False
                )

                ipfs_hash = result.get("IpfsHash")

                if ipfs_hash:
                    logger.info(
                        "PinataGateway: File sent to pinata. Hash is %s",
                        ipfs_hash
                    )
                else:
                    logger.warning(
                        "%s missing IpfsHash result=%r",
                        log_prefix, result
                    )

                return

            except Exception as e:
                cause = (
                    getattr(e, "__cause__", None)
                    or getattr(e, "__context__", None)
                )

                if attempt < self.max_attempts:
                    logger.warning(
                        "%s error %s: %r cause=%r (will retry)",
                        log_prefix,
                        type(e).__name__,
                        e,
                        cause,
                        exc_info=True,
                    )

                    self._sleep_backoff(attempt)
                    continue

                logger.warning(
                    "%s failed error %s: %r cause=%r",
                    log_prefix,
                    type(e).__name__,
                    e,
                    cause,
                    exc_info=True,
                )
                return

    def _sleep_backoff(self, attempt: int) -> None:
        """Pause before next attempt, growing exponentially with randomness"""
        exp = min(
            self.max_backoff_s, self.base_backoff_s * (2 ** (attempt - 1))
        )

        jitter = random.uniform(0, exp * 0.25)

        time.sleep(exp + jitter)
