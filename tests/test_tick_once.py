import json
from dataclasses import replace

from hl_usdc_bot.state import LocalFileStateStore
from hl_usdc_bot.tick_once import main
from tests.test_decide import CONFIG, NOW, reading


def test_dry_run_prints_the_payload_it_would_have_sent(tmp_path, capsys):
    config = replace(CONFIG, dry_run=True, state_file=str(tmp_path / "state.json"))

    exit_code = main(
        config=config,
        store=LocalFileStateStore(tmp_path / "state.json"),
        now=NOW,
        fetch_reserve=lambda **_: reading("0.6410374822"),
    )

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert "64.10%" in printed["text"]


def test_a_suppressed_tick_says_so_instead_of_printing_a_payload(tmp_path, capsys):
    config = replace(CONFIG, dry_run=True, state_file=str(tmp_path / "state.json"))
    store = LocalFileStateStore(tmp_path / "state.json")

    main(config=config, store=store, now=NOW, fetch_reserve=lambda **_: reading("0.64"))
    capsys.readouterr()

    exit_code = main(config=config, store=store, now=NOW, fetch_reserve=lambda **_: reading("0.64"))

    assert exit_code == 0
    assert "SUPPRESSED" in capsys.readouterr().out
