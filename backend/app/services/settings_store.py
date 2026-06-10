import json
from pathlib import Path

from app.core.models import BotSettings


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        root = Path(__file__).resolve().parents[3]
        self.path = path or root / "config" / "settings.json"

    def load(self) -> BotSettings:
        if not self.path.exists():
            return BotSettings()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return BotSettings.model_validate(raw)

    def save(self, settings: BotSettings) -> BotSettings:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = settings.model_dump(mode="json")
        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return settings

