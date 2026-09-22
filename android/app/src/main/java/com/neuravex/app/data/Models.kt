package com.neuravex.app.data

import kotlinx.serialization.Serializable

@Serializable
data class AccountOverview(
    val account_id: String,
    val equity: Double,
    val cash: Double,
    val daily_pnl: Double,
    val daily_pnl_pct: Double,
    val drawdown: Double,
    val market_regime: String,
    val open_positions_count: Int,
    val live_trading_enabled: Boolean,
)

@Serializable
data class PositionOut(
    val symbol: String,
    val side: String,
    val quantity: Double,
    val entry_price: Double,
    val current_price: Double? = null,
    val unrealized_pnl: Double? = null,
    val stop_loss: Double? = null,
    val take_profit: Double? = null,
    val opened_at: String,
)

@Serializable
data class DecisionOut(
    val symbol: String,
    val action: String,
    val confidence: Double,
    val final_score: Double,
    val reasons_for: List<String>,
    val reasons_against: List<String>,
    val entry: Double? = null,
    val stop_loss: Double? = null,
    val take_profit: Double? = null,
    val risk_reward: Double? = null,
    val created_at: String,
    val executed: Boolean = false,
)

@Serializable
data class DailyDecisionStats(
    val date: String,
    val executed_count: Int,
    val skipped_count: Int,
)

@Serializable
data class PendingExecutionOut(
    val id: String,
    val symbol: String,
    val side: String,
    val quantity: Double,
    val order_type: String,
    val limit_price: Double? = null,
    val stop_loss: Double? = null,
    val take_profit_1: Double? = null,
    val take_profit_2: Double? = null,
    val confidence: Double,
    val reasons_for: List<String>,
    val reasons_against: List<String>,
    val expires_at: String,
)

@Serializable
data class ReportExecutionRequest(
    val status: String,
    val exchange_order_id: String? = null,
    val average_fill_price: Double? = null,
    val filled_quantity: Double? = null,
    val failure_reason: String? = null,
)

@Serializable
data class AccountSettingsOut(
    val account_id: String,
    val symbols: List<String>,
    val min_balance_floor: Double? = null,
    val max_balance_target: Double? = null,
    val ai_enabled: Boolean,
    val execution_venue: String,
    val execution_mode: String,
    val live_trading_enabled: Boolean,
    val paper_trading_enabled: Boolean = true,
    val paper_starting_balance: Double = 1000.0,
    val paper_equity: Double = 1000.0,
)

@Serializable
data class AccountSettingsUpdate(
    val symbols: List<String>? = null,
    val min_balance_floor: Double? = null,
    val max_balance_target: Double? = null,
    val ai_enabled: Boolean? = null,
    val execution_venue: String? = null,
    val paper_trading_enabled: Boolean? = null,
    val paper_starting_balance: Double? = null,
)

@Serializable
data class ReportBalanceRequest(
    val equity: Double,
    val market_regime: String = "SIDEWAYS",
    // True only when `equity` covers the COMPLETE wallet (Polygon + Ethereum
    // mainnet). The backend only anchors the P/L baseline on such reports.
    val full_wallet: Boolean = false,
)

@Serializable
data class LoginRequest(val username: String, val password: String)

@Serializable
data class TokenResponse(val access_token: String, val token_type: String = "bearer")

/** Coins available when trading via a CEX (Binance) — kept from the
 * original CEX-only flow. */
object SupportedCoins {
    val ALL = listOf("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT")
}

/**
 * Coins available when trading on-chain via a wallet on Polygon. Every
 * base/quote token here maps to a contract address independently verified
 * against PolygonScan's own dedicated token page (see
 * PolygonTokenRegistry.kt) before being added. Do not add a symbol here
 * until its address has been verified the same way — a wrong token
 * address is not a bug that fails loudly, it is a swap that silently
 * sends funds somewhere unintended.
 */
object SupportedPolygonCoins {
    // Every tradable base asset in PolygonTokenRegistry, quoted in USDC.
    // Rebuilt dynamically (not a fixed list) so the user's custom 14th
    // token — set via Settings, see PolygonTokenRegistry.setCustomToken —
    // shows up here automatically once defined, with no separate list to
    // keep in sync.
    val ALL: List<String>
        get() = com.neuravex.app.data.PolygonTokenRegistry.ALL
            .filter { it.symbol != "USDC" && it.symbol != "USDT" && it.symbol != "DAI" }
            .map { "${it.symbol}/USDC" }
}
