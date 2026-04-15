from datamodel import OrderDepth, UserId, TradingState, Order
from typing import List, Dict
import json
import numpy as np


class Trader:

    POSITION_LIMIT = 80
    WARMUP_STEPS = 50
    SLOPE_THRESHOLD = 0.0003

    def bid(self):
        return 15

    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

        trader_data = {}
        if state.traderData and state.traderData != "":
            try:
                trader_data = json.loads(state.traderData)
            except:
                trader_data = {}

        if "INTARIAN_PEPPER_ROOT" in state.order_depths:
            result["INTARIAN_PEPPER_ROOT"] = self.trade_pepper_root(state, trader_data)

        if "ASH_COATED_OSMIUM" in state.order_depths:
            result["ASH_COATED_OSMIUM"] = self.trade_ash(state, trader_data)

        trader_data_str = json.dumps(trader_data)
        conversions = 0
        return result, conversions, trader_data_str

    # ══════════════════════════════════════════════════════════════
    #  INTARIAN_PEPPER_ROOT — Adaptive trend-following
    #
    #  Phase 1 (warmup, first ~50 steps):
    #    Collect mid prices, estimate slope via linear regression.
    #    If early signal is strong (>2x threshold), start building.
    #
    #  Phase 2:
    #    UP   (slope > +0.0003) → max long, hold
    #    DOWN (slope < -0.0003) → max short, hold
    #    FLAT (|slope| < 0.0003) → market-make
    #
    #  Slope is continuously re-estimated from last 200 obs.
    # ══════════════════════════════════════════════════════════════
    def trade_pepper_root(self, state: TradingState, trader_data: dict) -> List[Order]:
        product = "INTARIAN_PEPPER_ROOT"
        order_depth: OrderDepth = state.order_depths[product]
        orders: List[Order] = []

        position = state.position.get(product, 0)

        best_bid = max(order_depth.buy_orders.keys()) if order_depth.buy_orders else None
        best_ask = min(order_depth.sell_orders.keys()) if order_depth.sell_orders else None

        if best_bid is None or best_ask is None:
            return orders

        mid_price = (best_bid + best_ask) / 2
        timestamp = state.timestamp

        # ── Collect observations ──
        obs = trader_data.get("pepper_obs", [])
        obs.append([timestamp, mid_price])
        if len(obs) > 200:
            obs = obs[-200:]
        trader_data["pepper_obs"] = obs

        # ── Estimate slope via linear regression ──
        n = len(obs)
        slope = trader_data.get("pepper_slope", 0.0)

        if n >= 20:
            ts_arr = np.array([o[0] for o in obs], dtype=float)
            pr_arr = np.array([o[1] for o in obs], dtype=float)
            slope = float(np.polyfit(ts_arr, pr_arr, 1)[0])
            trader_data["pepper_slope"] = slope

        # ── Determine regime ──
        if n < self.WARMUP_STEPS:
            if n >= 30 and abs(slope) > self.SLOPE_THRESHOLD * 3:
                regime = "EARLY_SIGNAL"
            else:
                regime = "WARMUP"
        elif slope > self.SLOPE_THRESHOLD:
            regime = "UP"
        elif slope < -self.SLOPE_THRESHOLD:
            regime = "DOWN"
        else:
            regime = "FLAT"

        # ── Execute ──
        if regime == "WARMUP":
            pass  # No trades, just observe

        elif regime == "EARLY_SIGNAL":
            target = 20 if slope > 0 else -20
            orders = self._move_toward_target(product, order_depth, position, target, mid_price)

        elif regime == "UP":
            orders = self._aggressive_trend(product, order_depth, position, mid_price, direction=1)

        elif regime == "DOWN":
            orders = self._aggressive_trend(product, order_depth, position, mid_price, direction=-1)

        else:  # FLAT
            orders = self._market_make_generic(product, order_depth, position, mid_price)

        return orders

    def _aggressive_trend(self, product, order_depth, position, mid_price, direction):
        """Build max position in the trend direction."""
        orders = []

        if direction > 0:
            capacity = self.POSITION_LIMIT - position
            if capacity <= 0:
                return orders

            remaining = capacity

            # Hit all asks aggressively
            if order_depth.sell_orders:
                for ask_price in sorted(order_depth.sell_orders.keys()):
                    if remaining <= 0:
                        break
                    if ask_price <= mid_price + 3:
                        vol = -order_depth.sell_orders[ask_price]
                        qty = min(vol, remaining)
                        orders.append(Order(product, ask_price, qty))
                        remaining -= qty

            # Passive bid for remainder
            if remaining > 0:
                orders.append(Order(product, int(round(mid_price - 1)), remaining))

        else:  # direction < 0
            capacity = self.POSITION_LIMIT + position
            if capacity <= 0:
                return orders

            remaining = capacity

            # Hit all bids aggressively
            if order_depth.buy_orders:
                for bid_price in sorted(order_depth.buy_orders.keys(), reverse=True):
                    if remaining <= 0:
                        break
                    if bid_price >= mid_price - 3:
                        vol = order_depth.buy_orders[bid_price]
                        qty = min(vol, remaining)
                        orders.append(Order(product, bid_price, -qty))
                        remaining -= qty

            # Passive ask for remainder
            if remaining > 0:
                orders.append(Order(product, int(round(mid_price + 1)), -remaining))

        return orders

    def _move_toward_target(self, product, order_depth, position, target_pos, mid_price):
        """Gradually move position toward target (warmup phase)."""
        orders = []
        diff = target_pos - position

        if diff > 0 and order_depth.sell_orders:
            best_ask = min(order_depth.sell_orders.keys())
            if best_ask <= mid_price + 2:
                vol = -order_depth.sell_orders[best_ask]
                qty = min(min(diff, 15), vol)
                if qty > 0:
                    orders.append(Order(product, best_ask, qty))

        elif diff < 0 and order_depth.buy_orders:
            best_bid = max(order_depth.buy_orders.keys())
            if best_bid >= mid_price - 2:
                vol = order_depth.buy_orders[best_bid]
                qty = min(min(-diff, 15), vol)
                if qty > 0:
                    orders.append(Order(product, best_bid, -qty))

        return orders

    def _market_make_generic(self, product, order_depth, position, mid_price):
        """Market-make when no trend is detected."""
        orders = []
        half_spread = 5
        skew = -position * 0.15

        our_bid = int(round(mid_price + skew - half_spread))
        our_ask = int(round(mid_price + skew + half_spread))

        max_buy = self.POSITION_LIMIT - position
        max_sell = self.POSITION_LIMIT + position

        bid_qty = min(15, max_buy)
        if bid_qty > 0:
            orders.append(Order(product, our_bid, bid_qty))

        ask_qty = min(15, max_sell)
        if ask_qty > 0:
            orders.append(Order(product, our_ask, -ask_qty))

        return orders

    # ══════════════════════════════════════════════════════════════
    #  ASH_COATED_OSMIUM — Market making
    #
    #  Fair value ~10000, bot spread ~16.
    #  Two-tier quoting inside the spread + aggressive taking.
    #  Inventory skew to keep position near zero.
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

        # ── EWM fair value ──
        alpha = 0.15
        prev_fair = trader_data.get("ash_fair", None)
        if prev_fair is not None:
            fair_value = alpha * mid_price + (1 - alpha) * prev_fair
        else:
            fair_value = mid_price
        trader_data["ash_fair"] = fair_value

        # ── Inventory skew ──
        skew = -position * 0.15

        # ── Two-tier quoting ──
        tier1_bid = int(round(fair_value + skew - 4))
        tier1_ask = int(round(fair_value + skew + 4))
        tier2_bid = int(round(fair_value + skew - 7))
        tier2_ask = int(round(fair_value + skew + 7))

        max_buy = self.POSITION_LIMIT - position
        max_sell = self.POSITION_LIMIT + position

        # ── Aggressive: take mispriced orders ──
        remaining_buy = max_buy
        if order_depth.sell_orders:
            for ask_price in sorted(order_depth.sell_orders.keys()):
                if remaining_buy <= 0:
                    break
                if ask_price < fair_value - 1:
                    vol = -order_depth.sell_orders[ask_price]
                    qty = min(vol, remaining_buy)
                    orders.append(Order(product, ask_price, qty))
                    remaining_buy -= qty

        remaining_sell = max_sell
        if order_depth.buy_orders:
            for bid_price in sorted(order_depth.buy_orders.keys(), reverse=True):
                if remaining_sell <= 0:
                    break
                if bid_price > fair_value + 1:
                    vol = order_depth.buy_orders[bid_price]
                    qty = min(vol, remaining_sell)
                    orders.append(Order(product, bid_price, -qty))
                    remaining_sell -= qty

        # ── Passive: tier 1 (tight) ──
        t1_buy = min(10, remaining_buy)
        if t1_buy > 0:
            orders.append(Order(product, tier1_bid, t1_buy))
            remaining_buy -= t1_buy

        t1_sell = min(10, remaining_sell)
        if t1_sell > 0:
            orders.append(Order(product, tier1_ask, -t1_sell))
            remaining_sell -= t1_sell

        # ── Passive: tier 2 (wide) ──
        t2_buy = min(20, remaining_buy)
        if t2_buy > 0:
            orders.append(Order(product, tier2_bid, t2_buy))
            remaining_buy -= t2_buy

        t2_sell = min(20, remaining_sell)
        if t2_sell > 0:
            orders.append(Order(product, tier2_ask, -t2_sell))
            remaining_sell -= t2_sell

        return orders
