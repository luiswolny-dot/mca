"""Ingestor entry point: ``python -m mca.ingest.runner --source <name>``.

Matches the invocations already declared in ``docker-compose.yml`` — one
process per source, so a crash or a rate-limit lockout in the Twitter client
cannot take down Solana pool detection. Sources that are not implemented yet
fail loudly and immediately with a clear message rather than starting and
doing nothing, which would look healthy to Docker while silently not ingesting
anything — a much worse failure mode than a container that exits non-zero and
gets flagged by ``docker compose ps``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

log = logging.getLogger(__name__)

IMPLEMENTED_SOURCES = {"solana-geyser", "solana-pools"}
KNOWN_UNIMPLEMENTED_SOURCES = {
    "evm-logs": "Base/BSC log listener — not yet built; see README.md roadmap phase 1",
    "dexscreener": "aggregator polling client — not yet built",
    "birdeye": "aggregator polling client — not yet built",
    "geckoterminal": "aggregator polling client — not yet built",
    "twitter": "social listener — not yet built",
    "telegram": "social listener — not yet built",
    "reddit": "social listener — not yet built",
    "trends": "social listener — not yet built",
}


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    )


async def _run_solana_pools(cfg) -> None:
    from mca.core.bus import Bus
    from mca.execution.solana_rpc import SolanaRpcClient, SolanaWebsocketClient
    from mca.ingest.solana_pools import DEX_PROGRAMS, SolanaPoolListener

    chain_cfg = cfg.chains.get("solana")
    if chain_cfg is None or not chain_cfg.enabled:
        raise SystemExit("chains.solana.enabled is false in config — nothing to listen to")

    bus = Bus(_redis_url())
    rpc = SolanaRpcClient(chain_cfg.rpc_http)
    ws = SolanaWebsocketClient(chain_cfg.rpc_ws)

    program_ids = [pid for pid in DEX_PROGRAMS if pid]
    if not program_ids:
        raise SystemExit(
            "no verified DEX program IDs configured — see "
            "mca.ingest.solana_pools module docstring: RAYDIUM_CPMM/CLMM and "
            "PUMPFUN_PROGRAM must be filled in and verified before this can run")

    listeners = [SolanaPoolListener(ws, rpc, bus, pid) for pid in program_ids]
    log.info("starting %d Solana pool listener(s): %s", len(listeners), program_ids)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    tasks = [asyncio.create_task(listener.run()) for listener in listeners]
    await stop.wait()
    log.info("shutdown signal received, cancelling %d listener task(s)", len(tasks))
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await rpc.close()


def _redis_url() -> str:
    import os
    return os.environ.get("REDIS_URL", "redis://redis:6379/0")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True,
                        help="comma-separated source name(s), e.g. solana-geyser")
    parser.add_argument("--chains", default="", help="comma-separated chain filter (evm sources)")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args(argv)

    _configure_logging("INFO")
    sources = [s.strip() for s in args.source.split(",") if s.strip()]

    unimplemented = [s for s in sources if s in KNOWN_UNIMPLEMENTED_SOURCES]
    if unimplemented:
        for s in unimplemented:
            log.error("--source %s: %s", s, KNOWN_UNIMPLEMENTED_SOURCES[s])
        unknown = [s for s in sources if s not in IMPLEMENTED_SOURCES
                  and s not in KNOWN_UNIMPLEMENTED_SOURCES]
        for s in unknown:
            log.error("--source %s: unrecognised source name", s)
        return 1

    from mca.core.config import load_config
    cfg = load_config(args.config)

    if any(s in ("solana-geyser", "solana-pools") for s in sources):
        asyncio.run(_run_solana_pools(cfg))
        return 0

    log.error("no recognised implemented source in %s", sources)
    return 1


if __name__ == "__main__":
    sys.exit(main())
