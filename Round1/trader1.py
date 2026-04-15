from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict
import json


class Trader:

    # ── 仓位限制（Round 1 暂用80，提交前需确认） ──────────────────────
    POSITION_LIMITS = {
        "ASH_COATED_OSMIUM": 80,
        "INTARIAN_PEPPER_ROOT": 80,
    }

    # ══════════════════════════════════════════════════════════════════
    #  ASH_COATED_OSMIUM 参数
    #  性格：和Round 0的EMERALDS一样，fair value固定在10000
    #  Bot spread ≈ 16，我们在中间报价吃spread
    # ══════════════════════════════════════════════════════════════════
    ACO_FAIR   = 10000    # 公允价值
    ACO_OFFSET = 5        # 在fair value两侧各偏移5报价 → 我们的spread=10
    ACO_SKEW   = 0.15     # 库存偏移系数：每持有1单位，报价中心偏移0.15
    ACO_ORDER_QTY = 15    # 每侧挂单量

    # ══════════════════════════════════════════════════════════════════
    #  INTARIAN_PEPPER_ROOT 参数
    #  性格：完美线性上涨趋势，斜率≈0.1/step
    #  去趋势后残差仅±3，bot spread≈13
    #  策略：在线估计fair value + 做市
    # ══════════════════════════════════════════════════════════════════
    IPR_OFFSET = 4        # 在动态fair value两侧各偏移4报价
    IPR_SKEW   = 0.15     # 库存偏移系数
    IPR_ORDER_QTY = 15    # 每侧挂单量

    # ── 工具函数 ──────────────────────────────────────────────────────

    def _clamp_qty(self, product: str, position: int, desired_qty: int) -> int:
        """确保下单后不会超过仓位限制"""
        limit = self.POSITION_LIMITS.get(product, 80)
        if desired_qty > 0:  # 买单
            return min(desired_qty, limit - position)
        else:  # 卖单
            return max(desired_qty, -limit - position)

    def _get_mid(self, od: OrderDepth) -> float:
        """从OrderDepth计算mid price"""
        if od.buy_orders and od.sell_orders:
            return (max(od.buy_orders.keys()) + min(od.sell_orders.keys())) / 2.0
        elif od.buy_orders:
            return float(max(od.buy_orders.keys()))
        elif od.sell_orders:
            return float(min(od.sell_orders.keys()))
        return 0.0

    # ══════════════════════════════════════════════════════════════════
    #  ASH_COATED_OSMIUM 策略：固定fair value做市
    # ══════════════════════════════════════════════════════════════════

    def _trade_aco(self, od: OrderDepth, position: int) -> List[Order]:
        orders: List[Order] = []
        product = "ASH_COATED_OSMIUM"
        limit = self.POSITION_LIMITS[product]

        # ── 第一步：主动吃掉明显错价的bot挂单 ──
        # 如果bot的卖价低于fair value，直接买入（白送的钱）
        if od.sell_orders:
            for ask_px in sorted(od.sell_orders.keys()):
                if ask_px < self.ACO_FAIR:
                    ask_vol = -od.sell_orders[ask_px]  # 转为正数
                    qty = self._clamp_qty(product, position, ask_vol)
                    if qty > 0:
                        orders.append(Order(product, ask_px, qty))
                        position += qty

        # 如果bot的买价高于fair value，直接卖出
        if od.buy_orders:
            for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                if bid_px > self.ACO_FAIR:
                    bid_vol = od.buy_orders[bid_px]
                    qty = self._clamp_qty(product, position, -bid_vol)
                    if qty < 0:
                        orders.append(Order(product, bid_px, qty))
                        position += qty

        # ── 第二步：被动挂单在spread内部 ──
        # 根据当前仓位偏移报价中心
        skew = int(self.ACO_SKEW * position)
        bid_px = self.ACO_FAIR - self.ACO_OFFSET - skew
        ask_px = self.ACO_FAIR + self.ACO_OFFSET - skew

        buy_qty = self._clamp_qty(product, position, self.ACO_ORDER_QTY)
        if buy_qty > 0:
            orders.append(Order(product, bid_px, buy_qty))

        sell_qty = self._clamp_qty(product, position, -self.ACO_ORDER_QTY)
        if sell_qty < 0:
            orders.append(Order(product, ask_px, sell_qty))

        return orders

    # ══════════════════════════════════════════════════════════════════
    #  INTARIAN_PEPPER_ROOT 策略：动态fair value做市
    # ══════════════════════════════════════════════════════════════════

    def _trade_ipr(self, od: OrderDepth, position: int,
                   fair_value: float) -> List[Order]:
        orders: List[Order] = []
        product = "INTARIAN_PEPPER_ROOT"
        limit = self.POSITION_LIMITS[product]
        fv = round(fair_value)

        # ── 第一步：主动吃掉偏离fair value的bot挂单 ──
        if od.sell_orders:
            for ask_px in sorted(od.sell_orders.keys()):
                if ask_px < fv:
                    ask_vol = -od.sell_orders[ask_px]
                    qty = self._clamp_qty(product, position, ask_vol)
                    if qty > 0:
                        orders.append(Order(product, ask_px, qty))
                        position += qty

        if od.buy_orders:
            for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                if bid_px > fv:
                    bid_vol = od.buy_orders[bid_px]
                    qty = self._clamp_qty(product, position, -bid_vol)
                    if qty < 0:
                        orders.append(Order(product, bid_px, qty))
                        position += qty

        # ── 第二步：被动挂单在动态fair value两侧 ──
        skew = int(self.IPR_SKEW * position)
        bid_px = fv - self.IPR_OFFSET - skew
        ask_px = fv + self.IPR_OFFSET - skew

        buy_qty = self._clamp_qty(product, position, self.IPR_ORDER_QTY)
        if buy_qty > 0:
            orders.append(Order(product, bid_px, buy_qty))

        sell_qty = self._clamp_qty(product, position, -self.IPR_ORDER_QTY)
        if sell_qty < 0:
            orders.append(Order(product, ask_px, sell_qty))

        return orders

    # ══════════════════════════════════════════════════════════════════
    #  主入口
    # ══════════════════════════════════════════════════════════════════

    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

        # ── 恢复上一轮保存的状态 ──
        try:
            saved = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            saved = {}

        # INTARIAN_PEPPER_ROOT 的状态：
        # 记住第一次看到的mid price和对应的timestamp，用来做线性外推
        ipr_start_price = saved.get("ipr_start_price", None)
        ipr_start_ts = saved.get("ipr_start_ts", None)

        # ── 逐产品执行策略 ──
        for product, od in state.order_depths.items():
            position = state.position.get(product, 0)

            if product == "ASH_COATED_OSMIUM":
                result[product] = self._trade_aco(od, position)

            elif product == "INTARIAN_PEPPER_ROOT":
                mid = self._get_mid(od)

                # 初始化：记录起始价格和时间
                if ipr_start_price is None and mid > 0:
                    ipr_start_price = mid
                    ipr_start_ts = state.timestamp

                # 计算动态fair value：起始价 + 斜率 × 经过的步数
                if ipr_start_price is not None and ipr_start_ts is not None:
                    steps = (state.timestamp - ipr_start_ts) / 100.0
                    fair_value = ipr_start_price + 0.1 * steps
                else:
                    fair_value = mid  # 兜底

                result[product] = self._trade_ipr(od, position, fair_value)

        # ── 保存状态到下一轮 ──
        new_state = {
            "ipr_start_price": ipr_start_price,
            "ipr_start_ts": ipr_start_ts,
        }
        trader_data = json.dumps(new_state)

        conversions = 0
        return result, conversions, trader_data