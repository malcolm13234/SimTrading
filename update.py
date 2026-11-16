"""Headless daily update, run by GitHub Actions after the US close.

Loads the saved state and market data from data/, runs the same refresh the app runs
(fills limit orders that traded through, posts and reinvests dividends, records new daily
bars), and writes the files back only if something changed. It never creates a state:
the app does that, so the two cannot disagree about the start.
"""
import json, os, sys
import sandbox_core as core

STATE = "data/investing_sandbox_state.json"
MARKET = "data/market_data.json"


def load(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def main():
    S = load(STATE)
    if S is None:
        print(f"No {STATE} yet. Open the app once so it saves a state, then rerun.")
        return 0
    M = load(MARKET) or core.new_market()
    before = core.signature(S)
    hist, r, bench = core.refresh(S)
    new_rows = core.merge_market(M, hist)
    state_changed = core.signature(S) != before
    if state_changed:
        with open(STATE, "w") as f:
            json.dump(S, f, indent=1)
    if new_rows:
        with open(MARKET, "w") as f:
            json.dump(M, f, separators=(",", ":"))
    print(f"Latest price date {r.index[-1].date()}. New market rows: {new_rows}. "
          f"State changed: {state_changed}. Net value ${r['net'].iloc[-1]:,.2f}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
