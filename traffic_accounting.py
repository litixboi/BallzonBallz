import logging
import time
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger("TrafficAccounting")


class TrafficAccountingEngine:
    """Production-grade accounting engine enforcing strict 2x usage multiplier accounting
    on high-cost routes (e.g. domestic bridge Inbound 2, dedicated US egress, residential worker cascades).
    Accurately computes synthetic weighted consumption, eliminates negative number overflows,
    and commands 3X-UI to disable clients immediately when purchased plan quota is exhausted."""

    def __init__(self, bridge_multiplier: float = 2.0, primary_multiplier: float = 1.0):
        self.bridge_multiplier = bridge_multiplier
        self.primary_multiplier = primary_multiplier

    def calculate_weighted_consumption(
        self,
        purchased_volume_gb: float,
        primary_client: Optional[Dict[str, Any]] = None,
        bridge_client: Optional[Dict[str, Any]] = None,
        order_multiplier: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Compute exact weighted bandwidth usage across both primary and secondary bridge routes.

        Args:
            purchased_volume_gb: Total plan volume purchased in GB.
            primary_client: Dict from 3X-UI for primary Germany instance.
            bridge_client: Dict from 3X-UI for secondary Domestic bridge instance.
            order_multiplier: Optional custom multiplier on the order (e.g. 2.0 for premium plans).

        Returns:
            Dict containing raw bytes, weighted bytes, used GB, remaining GB, consumed percentage,
            and quota status flags.
        """
        purchased_bytes = int(purchased_volume_gb * 1024 * 1024 * 1024)

        # Primary panel raw bytes
        p_up = primary_client.get("up", 0) if primary_client else 0
        p_down = primary_client.get("down", 0) if primary_client else 0
        p_raw_bytes = max(0, p_up + p_down)

        # Secondary bridge panel raw bytes
        b_up = bridge_client.get("up", 0) if bridge_client else 0
        b_down = bridge_client.get("down", 0) if bridge_client else 0
        b_raw_bytes = max(0, b_up + b_down)

        # Multiplier determination
        p_mult = order_multiplier if (order_multiplier and order_multiplier > 1.0) else self.primary_multiplier
        b_mult = self.bridge_multiplier

        # Weighted calculation: bridge traffic is strictly doubled (2x)
        weighted_p_bytes = int(p_raw_bytes * p_mult)
        weighted_b_bytes = int(b_raw_bytes * b_mult)
        total_weighted_bytes = weighted_p_bytes + weighted_b_bytes

        total_raw_bytes = p_raw_bytes + b_raw_bytes
        used_gb = total_weighted_bytes / (1024 ** 3)
        raw_used_gb = total_raw_bytes / (1024 ** 3)

        # Zero-underflow protection
        remaining_bytes = max(0, purchased_bytes - total_weighted_bytes)
        remaining_gb = max(0.0, remaining_bytes / (1024 ** 3))

        consumed_pct = min(100.0, (total_weighted_bytes / purchased_bytes * 100.0)) if purchased_bytes > 0 else 100.0
        is_depleted = total_weighted_bytes >= purchased_bytes

        return {
            "purchased_volume_gb": purchased_volume_gb,
            "purchased_bytes": purchased_bytes,
            "primary_raw_bytes": p_raw_bytes,
            "bridge_raw_bytes": b_raw_bytes,
            "total_raw_bytes": total_raw_bytes,
            "raw_used_gb": round(raw_used_gb, 2),
            "weighted_bytes": total_weighted_bytes,
            "used_gb": round(used_gb, 2),
            "remaining_bytes": remaining_bytes,
            "remaining_gb": round(remaining_gb, 2),
            "consumed_pct": round(consumed_pct, 1),
            "is_depleted": is_depleted,
            "is_2x_active": (b_mult > 1.0 and b_raw_bytes > 0) or (p_mult > 1.0),
            "primary_multiplier": p_mult,
            "bridge_multiplier": b_mult,
        }

    def check_and_enforce_quota(
        self,
        email: str,
        order: Dict[str, Any],
        primary_client: Optional[Dict[str, Any]] = None,
        bridge_client: Optional[Dict[str, Any]] = None,
        primary_panel_mgr=None,
        bridge_panel_mgr=None,
    ) -> Dict[str, Any]:
        """Evaluate weighted usage and automatically disable client if synthetic 2x quota is reached."""
        plan_vol = float(order.get("volume_gb", 30))
        order_mult = float(order.get("multiplier", 1.0))
        calc = self.calculate_weighted_consumption(plan_vol, primary_client, bridge_client, order_mult)

        enforced = False
        if calc["is_depleted"]:
            logger.warning(
                "Client '%s' (Order %s) reached 100%% weighted quota (Used: %.2fGB / Plan: %.0fGB). Enforcing cutoff...",
                email,
                order.get("order_id"),
                calc["used_gb"],
                plan_vol,
            )
            # Disable on Primary Panel
            if primary_panel_mgr:
                try:
                    primary_panel_mgr.set_client_enabled(email, False)
                    enforced = True
                except Exception as e:
                    logger.error("Failed to disable client %s on primary panel: %s", email, e)

            # Disable on Secondary Bridge Panel
            if bridge_panel_mgr:
                try:
                    bridge_panel_mgr.set_client_enabled(email, False)
                    enforced = True
                except Exception as e:
                    logger.error("Failed to disable client %s on bridge panel: %s", email, e)

        calc["enforced"] = enforced
        return calc

    def format_client_usage_display(
        self,
        order: Dict[str, Any],
        primary_client: Optional[Dict[str, Any]] = None,
        bridge_client: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generate human-readable display lines for Telegram /orders interface with 2x notice."""
        plan_vol = float(order.get("volume_gb", 30))
        order_mult = float(order.get("multiplier", 1.0))
        dur_days = order.get("duration_days", 30)
        dur_map = {30: "۱ ماهه", 90: "۳ ماهه", 180: "۶ ماهه"}
        dur_str = dur_map.get(dur_days, f"{dur_days} روز")

        if not primary_client and not bridge_client:
            return {
                "has_stats": False,
                "lines": [
                    f"📊 <b>حجم کل:</b> {plan_vol:.0f} گیگابایت | {dur_str}",
                ],
            }

        calc = self.calculate_weighted_consumption(plan_vol, primary_client, bridge_client, order_mult)

        # Calculate days left from expiryTime
        exp_ms = 0
        if primary_client:
            exp_ms = primary_client.get("expiryTime", 0)
        elif bridge_client:
            exp_ms = bridge_client.get("expiryTime", 0)

        if exp_ms > 0:
            days_left = max(0, int((exp_ms - time.time() * 1000) / (86400 * 1000)))
            exp_str = f"~{days_left} روز باقیمانده"
        else:
            exp_str = dur_str

        lines = []
        if calc["is_depleted"]:
            lines.append(f"⛔ <b>وضعیت ساب:</b> ترافیک پلن به اتمام رسیده است (100% مصرف)")
        else:
            lines.append(f"📊 <b>حجم مصرفی:</b> {calc['used_gb']:.2f} گیگ از {plan_vol:.0f} گیگ ({calc['consumed_pct']:.1f}%)")
            lines.append(f"📈 <b>حجم باقیمانده:</b> {calc['remaining_gb']:.2f} گیگابایت")

        if calc["is_2x_active"]:
            lines.append("⚡ <i>ضریب مصرف ۲ برابری روی سرورهای اختصاصی/بریج لحاظ شده است.</i>")

        lines.append(f"⏳ <b>اعتبار زمانی:</b> {exp_str}")

        return {
            "has_stats": True,
            "calc": calc,
            "lines": lines,
            "days_left": days_left if exp_ms > 0 else dur_days,
            "consumed_pct": calc["consumed_pct"],
            "is_depleted": calc["is_depleted"],
        }


# Global singleton instance
accounting_engine = TrafficAccountingEngine(bridge_multiplier=2.0, primary_multiplier=1.0)
