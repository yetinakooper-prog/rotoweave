from backend.app import network


def test_current_client_network_accepts_only_loopback_origins() -> None:
    assert network.is_loopback_host("127.0.0.1")
    assert network.is_loopback_host("::1")
    assert network.origin_is_allowed("http://127.0.0.1:8766", 8766)
    assert network.origin_is_allowed("http://localhost:3000", 8766)
    assert not network.origin_is_allowed("http://192.168.1.8:8766", 8766)
    assert not network.origin_is_allowed("https://example.com", 8766)
