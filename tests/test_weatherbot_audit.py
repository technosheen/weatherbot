import weatherbot_audit


def test_classifies_resolved_winning_closed_held_as_claimable():
    local = {
        "file": "tokyo_2026-04-27.json",
        "market_status": "resolved",
        "position_status": "closed",
        "close_reason": "resolved",
        "exit_status": None,
    }
    live = {"currentValue": 19.7955, "curPrice": 1, "size": 19.7955}

    row = weatherbot_audit.classify_wallet_match(local, live)

    assert row["bucket"] == "claimable_or_resolved"
    assert row["claimable_value"] == 19.7955
    assert row["active_exposure_value"] == 0.0


def test_classifies_closed_losing_held_as_resolved_not_active():
    local = {
        "file": "denver_2026-04-26.json",
        "market_status": "resolved",
        "position_status": "closed",
        "close_reason": "resolved",
        "exit_status": "resolved",
    }
    live = {"currentValue": 0, "curPrice": 0, "size": 42.8625}

    row = weatherbot_audit.classify_wallet_match(local, live)

    assert row["bucket"] == "claimable_or_resolved"
    assert row["claimable_value"] == 0.0
    assert row["active_exposure_value"] == 0.0


def test_classifies_open_wallet_held_as_active_exposure():
    local = {
        "file": "london_2026-04-28.json",
        "market_status": "open",
        "position_status": "open",
        "close_reason": None,
        "exit_status": None,
    }
    live = {"currentValue": 1.966, "curPrice": 0.61, "size": 3.2231}

    row = weatherbot_audit.classify_wallet_match(local, live)

    assert row["bucket"] == "active_open"
    assert row["active_exposure_value"] == 1.966
    assert row["claimable_value"] == 0.0


def test_classifies_auxiliary_duplicate_as_auxiliary():
    row = weatherbot_audit.classify_wallet_match(None, {"currentValue": 0.5, "curPrice": 0.145, "size": 3.4493}, auxiliary_file="toronto_2026-04-28.json")

    assert row["bucket"] == "auxiliary_duplicate"
    assert row["active_exposure_value"] == 0.5
    assert row["claimable_value"] == 0.0
