"""NHL agent configuration — re-exports shared config with NHL-specific defaults."""

from __future__ import annotations

from shared.config import SharedConfig


class NHLConfig(SharedConfig):
    """NHL-specific configuration."""

    # NHL scan interval (minutes)
    NHL_SCAN_INTERVAL: int = 12

    # Edge thresholds
    MIN_GAME_EDGE: float = 0.04  # 4% minimum edge

    # Bet sizing
    MAX_BET_PCT: float = 0.08  # 8% max per bet (half-Kelly capped)
    MAX_GAME_EXPOSURE_PCT: float = 0.12  # 12% max per game
    MAX_TOTAL_EXPOSURE_PCT: float = 0.50  # 50% total (NBA + NHL)

    # NHL data
    NHL_SEASON: str = "2025-26"

    # Data file paths
    @property
    def nhl_positions_path(self):
        return self.DATA_DIR / "nhl_positions.json"

    @property
    def nhl_trades_path(self):
        return self.DATA_DIR / "nhl_trades.json"

    @property
    def nhl_calibration_path(self):
        return self.DATA_DIR / "nhl_calibration.json"
