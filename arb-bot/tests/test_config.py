from app.config import update_local_config_file


def test_update_local_config_file_only_writes_safe_parameters(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("BINANCE_API_KEY=secret-key\nOPEN_MIN_EDGE=1.50\n", encoding="utf-8")

    update_local_config_file(
        {
            "open_min_edge": "2.10",
            "mt4_bridge_token": "should-not-be-written",
        },
        path=env_path,
    )

    content = env_path.read_text(encoding="utf-8")
    assert "BINANCE_API_KEY=secret-key" in content
    assert "OPEN_MIN_EDGE=2.10" in content
    assert "MT4_BRIDGE_TOKEN" not in content
    assert "should-not-be-written" not in content
