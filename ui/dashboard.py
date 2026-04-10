"""Rich terminal dashboard for the hybrid sniper."""

import time
from datetime import datetime, timezone

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.config import CFG
from core.database import Database
from feeds.prices import PriceFeeds
from feeds.markets import MarketDiscovery
from engine.risk import RiskManager
from execution.executor import Executor


class Dashboard:
    def __init__(self, db: Database, feeds: PriceFeeds, markets: MarketDiscovery,
                 risk: RiskManager, executor: Executor, is_live: bool):
        self.db = db
        self.feeds = feeds
        self.markets = markets
        self.risk = risk
        self.executor = executor
        self.is_live = is_live
        self.signals_seen = 0
        self.signals_fired = 0
        self.console = Console()

    def render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1),
        )
        layout["left"].split_column(
            Layout(name="prices", size=9),
            Layout(name="positions"),
        )
        layout["right"].split_column(
            Layout(name="stats", size=12),
            Layout(name="trades"),
        )

        # Header
        mode = "[bold red]LIVE MODE[/]" if self.is_live else "[bold green]PAPER MODE[/]"
        kill = "  [bold red]KILL SWITCH[/]" if self.risk.kill_switch else ""
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        layout["header"].update(Panel(Text.from_markup(
            f"  Hybrid Oracle Sniper  |  {mode}{kill}  |  {ts}"
        ), style="bold"))

        # Prices
        pt = Table(title="Oracle Prices", expand=True)
        pt.add_column("Asset")
        pt.add_column("Chainlink", justify="right")
        pt.add_column("Binance", justify="right")
        pt.add_column("CL age", justify="right")
        for a in CFG.assets:
            cl = f"${self.feeds.chainlink[a]:,.2f}" if self.feeds.chainlink[a] > 0 else "---"
            bn = f"${self.feeds.binance[a]:,.2f}" if self.feeds.binance[a] > 0 else "---"
            age = f"{self.feeds.chainlink_staleness(a):.0f}s"
            pt.add_row(a, cl, bn, age)
        layout["prices"].update(Panel(pt))

        # Stats
        st = self.db.lifetime_stats()
        daily = self.db.daily_pnl()
        dc = "green" if daily >= 0 else "red"
        tc = "green" if st["pnl"] >= 0 else "red"
        stats = Table(title="Performance", expand=True)
        stats.add_column("Metric", style="bold")
        stats.add_column("Value", justify="right")
        stats.add_row("Portfolio", f"${self.risk.portfolio:,.2f}")
        stats.add_row("Daily P&L", f"[{dc}]${daily:+,.4f}[/]")
        stats.add_row("Total P&L", f"[{tc}]${st['pnl']:+,.4f}[/]")
        stats.add_row("Win Rate", f"{st['wr']:.1f}% ({st['wins']}/{st['total']})")
        stats.add_row("Expectancy", f"${st['expectancy']:+,.4f}/trade")
        stats.add_row("Avg Win", f"${st['avg_win']:+,.4f}")
        stats.add_row("Avg Loss", f"${st['avg_loss']:+,.4f}")
        stats.add_row("Signals", f"{self.signals_fired}/{self.signals_seen}")
        stats.add_row("Markets", str(len(self.markets.tokens)))
        layout["stats"].update(Panel(stats))

        # Open positions
        ot = Table(title=f"Open ({self.executor.open_count})", expand=True)
        ot.add_column("Asset"); ot.add_column("Dir"); ot.add_column("Entry", justify="right")
        ot.add_column("Delta", justify="right"); ot.add_column("TTL", justify="right")
        for _, pos in list(self.executor.open_positions.items()):
            ttl = max(0, pos.window_ts + 300 - time.time())
            ot.add_row(pos.asset, pos.direction, f"${pos.entry_price:.3f}",
                      f"{pos.oracle_delta:.4f}%", f"{ttl:.0f}s")
        layout["positions"].update(Panel(ot))

        # Recent trades
        recent = self.db.recent(10)
        rt = Table(title="Recent Trades", expand=True)
        rt.add_column("ID", max_width=12); rt.add_column("Asset")
        rt.add_column("Dir"); rt.add_column("Entry", justify="right")
        rt.add_column("Delta", justify="right")
        rt.add_column("P&L", justify="right"); rt.add_column("St")
        for t in recent:
            pc = "green" if t["pnl"] > 0 else ("red" if t["pnl"] < 0 else "dim")
            rt.add_row(
                t["id"][:12], t["asset"], t["direction"],
                f"${t['entry_price']:.3f}", f"{t['oracle_delta']:.3f}%",
                f"[{pc}]${t['pnl']:+.4f}[/]", t["status"][:4])
        layout["trades"].update(Panel(rt))

        # Footer
        layout["footer"].update(Panel(Text.from_markup(
            f"  [dim]Ctrl+C to stop  |  "
            f"Entry: T-{CFG.snipe_entry_sec:.0f}s to T-{CFG.snipe_exit_sec:.0f}s  |  "
            f"Price: ${CFG.min_token_price}-${CFG.max_token_price}  |  "
            f"Delta: >={CFG.min_delta_pct}%  |  "
            f"Conf: >={CFG.min_confidence}[/]"
        )))

        return layout
