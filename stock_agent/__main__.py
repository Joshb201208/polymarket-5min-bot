import asyncio
import logging
import signal

from stock_agent.scheduler import StockAgentScheduler


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    scheduler = StockAgentScheduler()

    def handle_signal(sig, frame):
        scheduler.shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    asyncio.run(scheduler.run())


if __name__ == "__main__":
    main()
