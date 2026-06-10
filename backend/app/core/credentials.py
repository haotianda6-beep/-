import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

from app.core.credential_utils import (
    CONFIG_DIR,
    CREDENTIALS_KEY_PATH,
    CREDENTIALS_PATH,
    ENV_PATH,
    EXCHANGE_ENV_PREFIX,
    EXCHANGE_FIELDS,
    env_bool,
    mask_secret,
    parse_datetime,
)
from app.core.models import ExchangeCredentialInput, ExchangeCredentialStatus, ExchangeName


@dataclass(frozen=True)
class EffectiveExchangeCredentials:
    exchange: ExchangeName
    values: dict[str, str]
    source: str
    missing_fields: list[str]
    use_testnet: bool
    use_demo: bool


class CredentialStore:
    _lock = threading.RLock()

    def __init__(self, path: Path = CREDENTIALS_PATH) -> None:
        self.path = path

    def exchange_statuses(
        self,
        live_data_enabled: bool,
        trading_enabled: bool,
        order_execution_enabled: bool,
        read_only_mode: bool,
    ) -> list[ExchangeCredentialStatus]:
        doc = self._load()
        return [
            self._exchange_status(
                exchange,
                doc,
                live_data_enabled,
                trading_enabled,
                order_execution_enabled,
                read_only_mode,
            )
            for exchange in ExchangeName
        ]

    def effective_exchange(self, exchange: ExchangeName) -> EffectiveExchangeCredentials:
        doc = self._load()
        item = self._exchange_item(doc, exchange)
        values: dict[str, str] = {}
        vault_fields = 0
        env_fields = 0
        missing: list[str] = []
        for field, env_name in EXCHANGE_FIELDS[exchange]:
            vault_value = str(item.get(field) or "").strip()
            env_value = os.getenv(env_name, "").strip()
            value = vault_value or env_value
            if vault_value:
                vault_fields += 1
            elif env_value:
                env_fields += 1
            else:
                missing.append(env_name)
            values[field] = value
        source = self._source(vault_fields, env_fields)
        prefix = EXCHANGE_ENV_PREFIX[exchange]
        return EffectiveExchangeCredentials(
            exchange=exchange,
            values=values,
            source=source,
            missing_fields=missing,
            use_testnet=self._flag(item, "use_testnet", f"{prefix}_USE_TESTNET"),
            use_demo=self._flag(item, "use_demo", f"{prefix}_USE_DEMO"),
        )

    def save_exchange(self, exchange: ExchangeName, payload: ExchangeCredentialInput) -> ExchangeCredentialStatus:
        with self._lock:
            doc = self._load()
            item = self._exchange_item(doc, exchange)
            for source_key, target_key in (("api_key", "api_key"), ("api_secret", "api_secret"), ("passphrase", "passphrase")):
                value = getattr(payload, source_key)
                if value and value.strip():
                    item[target_key] = value.strip()
            if payload.use_testnet is not None:
                item["use_testnet"] = bool(payload.use_testnet)
            if payload.use_demo is not None:
                item["use_demo"] = bool(payload.use_demo)
            item["updated_at"] = self._now()
            doc.setdefault("exchanges", {})[exchange.value] = item
            self._save(doc)
        return self.exchange_statuses(False, False, False, True)[list(ExchangeName).index(exchange)]

    def delete_exchange(self, exchange: ExchangeName) -> None:
        with self._lock:
            doc = self._load()
            doc.setdefault("exchanges", {}).pop(exchange.value, None)
            self._save(doc)

    def save_exchange_test(self, exchange: ExchangeName, ok: bool, message: str) -> None:
        with self._lock:
            doc = self._load()
            item = self._exchange_item(doc, exchange)
            item["last_test"] = {"ok": ok, "message": message[:220], "tested_at": self._now()}
            doc.setdefault("exchanges", {})[exchange.value] = item
            self._save(doc)

    def deepseek_credentials(self) -> dict[str, str]:
        doc = self._load()
        item = doc.setdefault("ai", {}).get("deepseek", {})
        return {
            "api_key": str(item.get("api_key") or os.getenv("DEEPSEEK_API_KEY", "")).strip(),
            "base_url": str(item.get("base_url") or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).strip(),
            "model": str(item.get("model") or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")).strip(),
        }

    def save_deepseek(self, api_key: str | None, base_url: str | None, model: str | None) -> dict[str, Any]:
        with self._lock:
            doc = self._load()
            item = dict(doc.setdefault("ai", {}).get("deepseek", {}))
            if api_key and api_key.strip():
                item["api_key"] = api_key.strip()
            if base_url and base_url.strip():
                item["base_url"] = base_url.strip().rstrip("/")
            if model and model.strip():
                item["model"] = model.strip()
            item["updated_at"] = self._now()
            doc.setdefault("ai", {})["deepseek"] = item
            self._save(doc)
        return self.deepseek_status()

    def delete_deepseek(self) -> None:
        with self._lock:
            doc = self._load()
            doc.setdefault("ai", {}).pop("deepseek", None)
            self._save(doc)

    def deepseek_status(self) -> dict[str, Any]:
        doc = self._load()
        item = doc.setdefault("ai", {}).get("deepseek", {})
        creds = self.deepseek_credentials()
        source = "vault" if item.get("api_key") else ("env" if os.getenv("DEEPSEEK_API_KEY") else "missing")
        return {
            "provider": "deepseek",
            "configured": bool(creds["api_key"]),
            "source": source,
            "masked_api_key": mask_secret(creds["api_key"]),
            "base_url": creds["base_url"],
            "model": creds["model"],
            "updated_at": item.get("updated_at"),
        }

    def mt4_token(self) -> str:
        doc = self._load()
        item = doc.setdefault("mt4", {})
        return str(item.get("bridge_token") or os.getenv("MT4_BRIDGE_TOKEN", "")).strip()

    def save_mt4_token(self, bridge_token: str | None) -> dict[str, Any]:
        with self._lock:
            doc = self._load()
            item = dict(doc.setdefault("mt4", {}))
            if bridge_token and bridge_token.strip():
                item["bridge_token"] = bridge_token.strip()
            item["updated_at"] = self._now()
            doc["mt4"] = item
            self._save(doc)
        return self.mt4_status()

    def delete_mt4_token(self) -> None:
        with self._lock:
            doc = self._load()
            doc["mt4"] = {}
            self._save(doc)

    def mt4_status(self) -> dict[str, Any]:
        doc = self._load()
        item = doc.setdefault("mt4", {})
        token = self.mt4_token()
        source = "vault" if item.get("bridge_token") else ("env" if os.getenv("MT4_BRIDGE_TOKEN") else "missing")
        return {"configured": bool(token), "source": source, "masked_token": mask_secret(token), "updated_at": item.get("updated_at")}

    def secret_values(self) -> list[str]:
        doc = self._load()
        values: list[str] = []
        for exchange in ExchangeName:
            item = self._exchange_item(doc, exchange)
            for field, env_name in EXCHANGE_FIELDS[exchange]:
                values.extend([str(item.get(field) or ""), os.getenv(env_name, "")])
        deepseek = self.deepseek_credentials()
        values.extend([deepseek["api_key"], self.mt4_token()])
        return [value for value in values if len(value) > 4]

    def _exchange_status(
        self,
        exchange: ExchangeName,
        doc: dict[str, Any],
        live_data_enabled: bool,
        trading_enabled: bool,
        order_execution_enabled: bool,
        read_only_mode: bool,
    ) -> ExchangeCredentialStatus:
        creds = self.effective_exchange(exchange)
        item = self._exchange_item(doc, exchange)
        test = item.get("last_test") if isinstance(item.get("last_test"), dict) else {}
        return ExchangeCredentialStatus(
            exchange=exchange,
            configured=not creds.missing_fields,
            missing_fields=creds.missing_fields,
            live_data_enabled=live_data_enabled,
            trading_enabled=trading_enabled,
            order_execution_enabled=order_execution_enabled,
            read_only_mode=read_only_mode,
            source=creds.source,
            masked_api_key=mask_secret(creds.values.get("api_key", "")),
            updated_at=parse_datetime(item.get("updated_at")),
            last_test_ok=test.get("ok"),
            last_test_message=test.get("message"),
            last_test_at=parse_datetime(test.get("tested_at")),
            use_testnet=creds.use_testnet,
            use_demo=creds.use_demo,
        )

    def _load(self) -> dict[str, Any]:
        load_dotenv(ENV_PATH, override=False)
        if not self.path.exists() or self.path.stat().st_size == 0:
            return self._blank()
        try:
            raw = self._fernet().decrypt(self.path.read_bytes())
            doc = json.loads(raw.decode("utf-8"))
            return doc if isinstance(doc, dict) else self._blank()
        except (OSError, json.JSONDecodeError, InvalidToken) as exc:
            raise RuntimeError("credentials storage cannot be decrypted; check CREDENTIALS_MASTER_KEY") from exc

    def _save(self, doc: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        token = self._fernet().encrypt(json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(token)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def _fernet(self) -> Fernet:
        key = os.getenv("CREDENTIALS_MASTER_KEY", "").strip()
        if not key:
            key = self._file_key()
        return Fernet(key.encode("utf-8"))

    def _file_key(self) -> str:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if CREDENTIALS_KEY_PATH.exists():
            return CREDENTIALS_KEY_PATH.read_text(encoding="utf-8").strip()
        key = Fernet.generate_key().decode("utf-8")
        CREDENTIALS_KEY_PATH.write_text(key, encoding="utf-8")
        os.chmod(CREDENTIALS_KEY_PATH, 0o600)
        return key

    def _exchange_item(self, doc: dict[str, Any], exchange: ExchangeName) -> dict[str, Any]:
        return dict(doc.setdefault("exchanges", {}).get(exchange.value, {}))

    def _flag(self, item: dict[str, Any], field: str, env_name: str) -> bool:
        if field in item:
            return bool(item[field])
        return env_bool(env_name)

    def _source(self, vault_fields: int, env_fields: int) -> str:
        if vault_fields and env_fields:
            return "mixed"
        if vault_fields:
            return "vault"
        if env_fields:
            return "env"
        return "missing"

    def _blank(self) -> dict[str, Any]:
        return {"version": 1, "exchanges": {}, "ai": {}, "mt4": {}}

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
