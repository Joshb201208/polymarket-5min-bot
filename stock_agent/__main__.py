import asyncio
import fcntl
import signal

from stock_agent.config import Config, Settings, SettingsError, load_settings
from stock_agent.logging_setup import configure_logging
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
    try:
        settings = load_settings(load_dotenv_file=True, require_runtime_secrets=True)
    except SettingsError as exc:
        raise SystemExit(str(exc)) from exc
    configure_logging(settings)
    runtime_config = Config(**{name: getattr(settings, name) for name in Settings.model_fields})
    asyncio.run(_serve(runtime_config))


if __name__ == "__main__":
    main()
