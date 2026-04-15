from datamodel import OrderDepth, UserId, TradingState, Order
from typing import List, Dict
import json


class Trader:

    POSITION_LIMIT = 80

    def bid(self):
        return 15

    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

        # ── Restore state ──────────────────────────────────────────
        trader_data = {}
        if state.traderData and state.traderData != "":
            try:
                trader_data = json.loads(state.traderData)
            except:
                trader_data = {}

        # ── INTARIAN_PEPPER_ROOT: Trend-following buy & hold ──────
        if "INTARIAN_PEPPER_ROOT" in state.order_depths:
            result["INTARIAN_PEPPER_ROOT"] = self.trade_pepper_root(state, trader_data)

        # ── ASH_COATED_OSMIUM: Market making ─────────────────────
        if "ASH_COATED_OSMIUM" in state.order_depths:
            result["ASH_COATED_OSMIUM"] = self.trade_ash(state, trader_data)

        trader_data_str = json.dumps(trader_data)
        conversions = 0
        return result, conversions, trader_data_str

    # ══════════════════════════════════════════════════════════════
    #  INTARIAN_PEPPER_ROOT
    #  Strategy: buy max position ASAP, hold forever.
    #  The price rises ~+1 per 1000 timestamp units (0.001/ts).
    #  Over a full day (ts 0→999900), price rises ~1000.
    #  Holding 80 units = 80,000 profit/day from trend alone.
    # ══════════════════════════════════════════════════════════════
    def trade_pepper_root(self, state: TradingState, trader_data: dict) -> List[Order]:
        product = "INTARIAN_PEPPER_ROOT"
        order_depth: OrderDepth = state.order_depths[product]
        orders: List[Order] = []

        position = state.position.get(product, 0)
        buy_capacity = self.POSITION_LIMIT - position  # how much more we can buy

        if buy_capacity <= 0:
            # Already at max long — just hold, no orders needed
            return orders

        # Compute fair value from mid price
        best_bid = max(order_depth.buy_orders.keys()) if order_depth.buy_orders else None
        best_ask = min(order_depth.sell_orders.keys()) if order_depth.sell_orders else None

        if best_ask is not None and best_bid is not None:
            fair_value = (best_bid + best_ask) / 2
        elif best_ask is not None:
            fair_value = best_ask - 6
        elif best_bid is not None:
            fair_value = best_bid + 6
        else:
            return orders

        # ── Aggressively buy: hit all sell orders at or below fair_value + margin ──
        # Since the price is always going UP, we're happy to pay up to fair_value + a few
        # The trend gain per step (~0.1 per 100ts) far exceeds any small overpay
        remaining_buy = buy_capacity

        if order_depth.sell_orders:
            # sell_orders has negative quantities; sorted ascending by price
            for ask_price in sorted(order_depth.sell_orders.keys()):
                if remaining_buy <= 0:
                    break
                # Buy aggressively: accept any ask up to fair_value + 3
                # (spread cost is trivial vs trend gain)
                if ask_price <= fair_value + 3:
                    ask_volume = -order_depth.sell_orders[ask_price]  # make positive
                    qty = min(ask_volume, remaining_buy)
                    orders.append(Order(product, ask_price, qty))
                    remaining_buy -= qty

        # ── Also place a passive buy order at bid+1 to catch any remaining capacity ──
        if remaining_buy > 0 and best_bid is not None:
            passive_bid = int(round(fair_value - 1))
            orders.append(Order(product, passive_bid, remaining_buy))

        return orders

    # ══════════════════════════════════════════════════════════════
    #  ASH_COATED_OSMIUM
    #  Strategy: market making around fair value (~10000).
    #  Bot spread is ~16 (bid ~9992, ask ~10008).
    #  We quote inside the bot spread to capture the spread.
    #  Inventory skew pushes quotes to mean-revert position to 0.
    # ══════════════════════════════════════════════════════════════
    def trade_ash(self, state: TradingState, trader_data: dict) -> List[Order]:
        product = "ASH_COATED_OSMIUM"
        order_depth: OrderDepth = state.order_depths[product]
        orders: List[Order] = []

        position = state.position.get(product, 0)

        best_bid = max(order_depth.buy_orders.keys()) if order_depth.buy_orders else None
        best_ask = min(order_depth.sell_orders.keys()) if order_depth.sell_orders else None

        if best_bid is None or best_ask is None:
            return orders

        mid_price = (best_bid + best_ask) / 2

        # ── Fair value: use 10000 as anchor, EWM for minor adjustments ──
        alpha = 0.15
        prev_fair = trader_data.get("ash_fair", None)
        if prev_fair is not None:
            fair_value = alpha * mid_price + (1 - alpha) * prev_fair
        else:
            fair_value = mid_price
        trader_data["ash_fair"] = fair_value

        # ── Inventory skew ──
        # Stronger skew to prevent position drift
        # At position ±40, skew shifts quotes by ±4 ticks
        skew = -position * 0.15

        # ── Quoting: two tiers of passive orders ──
        # Tier 1 (tight): ±4 from fair → spread 8, attracts more fills
        # Tier 2 (wide):  ±7 from fair → spread 14, still inside bot spread
        tier1_half = 4
        tier2_half = 7

        tier1_bid = int(round(fair_value + skew - tier1_half))
        tier1_ask = int(round(fair_value + skew + tier1_half))
        tier2_bid = int(round(fair_value + skew - tier2_half))
        tier2_ask = int(round(fair_value + skew + tier2_half))

        # ── Capacity ──
        max_buy = self.POSITION_LIMIT - position
        max_sell = self.POSITION_LIMIT + position

        # ── Step 1: Aggressively take mispriced orders ──
        remaining_buy = max_buy
        if order_depth.sell_orders:
            for ask_price in sorted(order_depth.sell_orders.keys()):
                if remaining_buy <= 0:
                    break
                if ask_price < fair_value - 1:
                    ask_volume = -order_depth.sell_orders[ask_price]
                    qty = min(ask_volume, remaining_buy)
                    orders.append(Order(product, ask_price, qty))
                    remaining_buy -= qty

        remaining_sell = max_sell
        if order_depth.buy_orders:
            for bid_price in sorted(order_depth.buy_orders.keys(), reverse=True):
                if remaining_sell <= 0:
                    break
                if bid_price > fair_value + 1:
                    bid_volume = order_depth.buy_orders[bid_price]
                    qty = min(bid_volume, remaining_sell)
                    orders.append(Order(product, bid_price, -qty))
                    remaining_sell -= qty

        # ── Step 2: Passive quotes (two tiers) ──
        # Tier 1: tight, small size
        t1_buy_qty = min(10, remaining_buy)
        if t1_buy_qty > 0:
            orders.append(Order(product, tier1_bid, t1_buy_qty))
            remaining_buy -= t1_buy_qty

        t1_sell_qty = min(10, remaining_sell)
        if t1_sell_qty > 0:
            orders.append(Order(product, tier1_ask, -t1_sell_qty))
            remaining_sell -= t1_sell_qty

        # Tier 2: wider, larger size
        t2_buy_qty = min(20, remaining_buy)
        if t2_buy_qty > 0:
            orders.append(Order(product, tier2_bid, t2_buy_qty))
            remaining_buy -= t2_buy_qty

        t2_sell_qty = min(20, remaining_sell)
        if t2_sell_qty > 0:
            orders.append(Order(product, tier2_ask, -t2_sell_qty))
            remaining_sell -= t2_sell_qty

        return orders
