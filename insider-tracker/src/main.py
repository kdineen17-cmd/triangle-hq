"""Insider cluster-buy tracker with a mandatory human-in-the-loop trade guardrail.

Every cluster signal is intercepted here. Nothing reaches Robinhood's
place_equity_order unless a human, at an interactive terminal, answers:

    Insiders bought [Ticker]. Execute order on Robinhood? (Y/N)

Usage:
    python -m src.main --link             # one-time Robinhood OAuth handshake
    python -m src.main --backfill         # load the last 30 days of Form 4s
    python -m src.main --once             # scan once, prompt for each new signal
    python -m src.main                    # poll continuously
    python -m src.main --alerts-only      # never connect to Robinhood
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from src.tracker import ClusterSignal, InsiderTracker, TrackerConfig
from src.trader import (GuardrailViolation, HumanApproval, RobinhoodAgenticTrader, TraderConfig,
                        link_account)

log = logging.getLogger("insider")
PROMPT = "Insiders bought {ticker}. Execute order on Robinhood? (Y/N) "


# --------------------------------------------------------------------- config
def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def load_configs() -> tuple[TrackerConfig, TraderConfig]:
    load_dotenv()
    tcfg = TrackerConfig(
        user_agent=os.getenv("SEC_USER_AGENT", ""),
        db_path=Path(os.getenv("DB_PATH", "data/insider.db")),
        min_insiders=int(os.getenv("MIN_INSIDERS", "2")),
        cluster_window_days=int(os.getenv("CLUSTER_WINDOW_DAYS", "30")),
        max_market_cap_usd=float(os.getenv("MAX_MARKET_CAP_USD", "2000000000")),
        min_cluster_value_usd=float(os.getenv("MIN_CLUSTER_VALUE_USD", "0")),
        skip_unknown_market_cap=_bool("SKIP_UNKNOWN_MARKET_CAP", True),
    )
    rcfg = TraderConfig(
        account_number=os.getenv("ROBINHOOD_ACCOUNT_NUMBER", ""),
        mcp_url=os.getenv("ROBINHOOD_MCP_URL", "https://agent.robinhood.com/mcp/trading"),
        token_path=Path(os.getenv("ROBINHOOD_TOKEN_PATH",
                                  "~/.config/insider-tracker/robinhood_oauth.json")).expanduser(),
        redirect_port=int(os.getenv("OAUTH_REDIRECT_PORT", "8765")),
        dry_run=_bool("DRY_RUN", True),
        order_type=os.getenv("ORDER_TYPE", "limit"),
        order_dollars=float(os.getenv("ORDER_DOLLARS", "100")),
        max_order_dollars=float(os.getenv("MAX_ORDER_DOLLARS", "250")),
        max_orders_per_day=int(os.getenv("MAX_ORDERS_PER_DAY", "3")),
        limit_buffer_bps=float(os.getenv("LIMIT_BUFFER_BPS", "25")),
    )
    return tcfg, rcfg


# --------------------------------------------------------------------- guardrail
class TerminalGuardrail:
    """Forces an explicit, interactive Y/N decision for every signal.

    * Refuses to run if stdin/stdout is not a real terminal, so answers can't be
      piped in (e.g. `yes | python -m src.main`) and it can't run unattended.
    * Only a literal 'Y' or 'y' approves. Anything else re-prompts; EOF, Ctrl-C
      or 'N' declines. There is no default and no "approve all".
    * Pending input typed before the prompt appears is flushed first.
    """

    def __init__(self):
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise SystemExit("Refusing to trade: the guardrail requires an interactive terminal. "
                             "Use --alerts-only for non-interactive runs.")

    @staticmethod
    def _flush_stdin() -> None:
        try:
            import termios
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except Exception:
            pass

    def ask(self, ticker: str) -> bool:
        self._flush_stdin()
        while True:
            try:
                ans = input(PROMPT.format(ticker=ticker)).strip()
            except (EOFError, KeyboardInterrupt):
                print("\n-> declined")
                return False
            if ans in ("Y", "y"):
                return True
            if ans in ("N", "n"):
                return False
            print("Please answer Y or N.")


def _audit(event: str, **kw) -> None:
    Path("data").mkdir(exist_ok=True)
    with open("data/decisions.jsonl", "a") as f:
        f.write(json.dumps(dict(ts=time.strftime("%Y-%m-%dT%H:%M:%S"), event=event, **kw), default=str) + "\n")


async def handle_signal(sig: ClusterSignal, trader: RobinhoodAgenticTrader | None,
                        guard: TerminalGuardrail | None) -> None:
    print("\n" + "=" * 72)
    print("CLUSTER BUY\n" + sig.summary())
    _audit("signal", ticker=sig.ticker, key=sig.key, total=sig.total_value_usd)
    if trader is None or guard is None:
        return

    try:
        order = await trader.build_buy(sig.ticker)
        review = await trader.review(order)       # simulation only; nothing is placed
    except GuardrailViolation as e:
        print(f"Blocked before prompting: {e}")
        _audit("blocked", ticker=sig.ticker, reason=str(e))
        return
    except Exception as e:  # network/API failure: report and move on, never prompt on a bad review
        print(f"Could not review order for {sig.ticker}: {e}")
        _audit("error", ticker=sig.ticker, reason=str(e))
        return

    print(f"\nProposed order: {order.describe()}")
    print("Robinhood review:")
    print(json.dumps(review.raw.get("data", review.raw), indent=2, default=str)[:3000])
    if review.alerts:
        print(f"\n!! Robinhood pre-trade alerts: {review.alerts}")
    if trader.cfg.dry_run:
        print("(DRY_RUN=true: approving will only be logged, no order will be sent)")

    if not guard.ask(sig.ticker):
        print(f"-> skipped {sig.ticker}")
        _audit("declined", ticker=sig.ticker, order=order.describe())
        return

    approval = HumanApproval(ref_id=order.ref_id, symbol=order.symbol,
                             approved_at=time.time(), nonce=review.nonce)
    _audit("approved", ticker=sig.ticker, order=order.describe())
    try:
        res = await trader.place(review, approval)
        print(f"-> submitted: {json.dumps(res, default=str)[:1500]}")
        _audit("submitted", ticker=sig.ticker, response=res)
    except GuardrailViolation as e:
        print(f"-> blocked: {e}")
        _audit("blocked", ticker=sig.ticker, reason=str(e))


# --------------------------------------------------------------------- run loop
async def run(args) -> None:
    tcfg, rcfg = load_configs()
    tracker = InsiderTracker(tcfg)

    if args.backfill:
        n = await asyncio.to_thread(tracker.backfill)
        print(f"backfill complete: {n} qualifying purchase rows stored")

    trader = guard = None
    if not args.alerts_only:
        guard = TerminalGuardrail()
        trader = RobinhoodAgenticTrader(rcfg)
        await trader.__aenter__()
        mode = "DRY RUN" if rcfg.dry_run else "LIVE"
        print(f"Robinhood agentic account ****{rcfg.account_number[-4:]} linked ({mode}).")

    try:
        while True:
            added = await asyncio.to_thread(tracker.poll)
            log.info("poll: %d new qualifying purchases", added)
            for sig in await asyncio.to_thread(tracker.clusters):
                await handle_signal(sig, trader, guard)
                tracker.mark_alerted(sig)
            if args.once:
                break
            await asyncio.sleep(args.interval)
    finally:
        if trader:
            await trader.__aexit__(None, None, None)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--link", action="store_true", help="run the Robinhood OAuth handshake and exit")
    p.add_argument("--backfill", action="store_true", help="ingest the last CLUSTER_WINDOW_DAYS of filings first")
    p.add_argument("--once", action="store_true", help="scan once then exit")
    p.add_argument("--alerts-only", action="store_true", help="print signals; never connect to Robinhood")
    p.add_argument("--interval", type=int, default=int(os.getenv("POLL_SECONDS", "300")))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.link:
        _, rcfg = load_configs()
        acct = asyncio.run(link_account(rcfg))
        print(f"Linked. Account ****{acct['account_number'][-4:]} "
              f"({acct.get('nickname') or acct.get('brokerage_account_type')}) is enabled for agentic trading.")
        return
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
