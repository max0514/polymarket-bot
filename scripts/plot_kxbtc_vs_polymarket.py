"""Plot Kalshi vs Polymarket BTC 15m price differences.

This reads the comparison CSV produced for the BTC 15-minute market and saves a
two-panel chart:

1. Executable prices: Kalshi YES ask vs Polymarket UP best bid.
2. Cross-venue edge: Polymarket UP bid minus Kalshi YES ask.

The chart is written next to the source CSV in data/btc_updown_15m/.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


def build_chart(input_csv: Path, output_png: Path) -> None:
    df = pd.read_csv(input_csv)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    df = df.sort_values("timestamp_utc")

    positive = df["edge_yes_buy_up_sell_poly"] > 0

    plt.style.use("dark_background")
    fig, (ax_price, ax_edge) = plt.subplots(
        2,
        1,
        figsize=(16, 9),
        sharex=True,
        gridspec_kw={"height_ratios": [2.2, 1]},
    )

    kalshi_color = "#7dd3fc"
    poly_color = "#fca5a5"
    edge_color = "#86efac"
    neg_color = "#fb7185"

    # Price panel.
    ax_price.plot(
        df["timestamp_utc"],
        df["yes_ask"],
        label="Kalshi YES ask",
        color=kalshi_color,
        linewidth=2.2,
    )
    ax_price.plot(
        df["timestamp_utc"],
        df["poly_up_best_bid"],
        label="Polymarket UP best bid",
        color=poly_color,
        linewidth=2.2,
    )
    ax_price.plot(
        df["timestamp_utc"],
        df["yes_bid"],
        label="Kalshi YES bid",
        color=kalshi_color,
        linewidth=1.2,
        linestyle="--",
        alpha=0.65,
    )
    ax_price.plot(
        df["timestamp_utc"],
        df["poly_up_best_ask"],
        label="Polymarket UP best ask",
        color=poly_color,
        linewidth=1.2,
        linestyle="--",
        alpha=0.65,
    )
    ax_price.fill_between(
        df["timestamp_utc"],
        df["yes_ask"],
        df["poly_up_best_bid"],
        where=positive,
        interpolate=True,
        color=edge_color,
        alpha=0.18,
        label="Positive cross-venue edge",
    )
    ax_price.set_ylabel("Price")
    ax_price.set_title("Kalshi vs Polymarket BTC 15m: executable price comparison")
    ax_price.grid(True, alpha=0.18)
    ax_price.legend(loc="upper left", ncol=2, frameon=False)
    ax_price.set_ylim(0, 1)

    # Edge panel.
    ax_edge.axhline(0, color="white", linewidth=1, alpha=0.35)
    ax_edge.bar(
        df["timestamp_utc"],
        df["edge_yes_buy_up_sell_poly"],
        color=[edge_color if value > 0 else neg_color for value in df["edge_yes_buy_up_sell_poly"]],
        width=0.0008,
        alpha=0.9,
    )
    ax_edge.set_ylabel("Edge")
    ax_edge.set_xlabel("Timestamp UTC")
    ax_edge.set_title("Raw edge = Polymarket UP best bid - Kalshi YES ask")
    ax_edge.grid(True, axis="y", alpha=0.18)

    fig.autofmt_xdate()
    fig.tight_layout()
    output_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_png, dpi=180, bbox_inches="tight")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("/Users/max/Desktop/python_codes/polymarket-bot/data/btc_updown_15m/kxbtc_vs_polymarket_comparison.csv"),
        help="Path to the comparison CSV.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/Users/max/Desktop/python_codes/polymarket-bot/data/btc_updown_15m/kxbtc_vs_polymarket_diff.png"),
        help="Path for the output PNG.",
    )
    args = parser.parse_args()
    build_chart(args.input, args.output)
    print(args.output)


if __name__ == "__main__":
    main()
