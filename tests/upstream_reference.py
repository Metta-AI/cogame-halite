"""Drive upstream's own ``make("halite")`` env over a recorded order stream.

Run as a SCRIPT, in a subprocess with a clean ``sys.path``: the assembled
vendor tree is a package literally named ``kaggle_environments``, so importing
the real distribution in the same interpreter is impossible. The production sim
must never see the installed package and this script must never see the
assembled tree — the process boundary is what guarantees both.

    python tests/upstream_reference.py <input.json> <output.json>

Input:  {"configuration": {...}, "orders": [[{...} x seats] x turns]}
Output: [{"step", "halite", "players", "status", "reward"} x (turns + 1)]
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    from kaggle_environments import make  # noqa: PLC0415 - the real distribution

    payload = json.load(open(argv[1]))
    env = make("halite", configuration=payload["configuration"], debug=False)
    env.reset(payload.get("num_agents", 4))

    def snapshot() -> dict:
        obs = env.state[0].observation
        return {
            "step": obs.step,
            "halite": list(obs.halite),
            "players": [
                [p[0], dict(p[1]), {k: list(v) for k, v in p[2].items()}]
                for p in obs.players
            ],
            "status": [agent.status for agent in env.state],
            "reward": [agent.reward for agent in env.state],
        }

    out = [snapshot()]
    for orders in payload["orders"]:
        if env.done:
            break
        env.step(orders)
        out.append(snapshot())
    json.dump(out, open(argv[2], "w"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
