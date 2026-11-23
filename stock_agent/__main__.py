import asyncio
import fcntl
import logging
import signal

from stock_agent.config import Config
from stock_agent.scheduler import StockAgentScheduler


async def _serve(config: Config) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (config.DATA_DIR / "agent.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("Another stock-agent process holds the state directory lock") from exc
        scheduler = StockAgentScheduler(config)
        loop = asyncio.get_running_loop()
        signals = (signal.SIGTERM, signal.SIGINT)
        for signum in signals:
            loop.add_signal_handler(signum, scheduler.shutdown)
        try:
            await scheduler.run()
        finally:
            for signum in signals:
                loop.remove_signal_handler(signum)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(_serve(Config()))


if __name__ == "__main__":
    main()
