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
data class AssetBalanceRequest(
    val symbol: String,
    val quantity: Double,
    val price_usd: Double,
)

@Serializable
data class ReportBalanceRequest(
    val equity: Double,
    val market_regime: String = "SIDEWAYS",
    // True only when `equity` covers the COMPLETE wallet (Polygon + Ethereum
    // mainnet). The backend only anchors the P/L baseline on such reports.
    val full_wallet: Boolean = false,
    // Per-asset breakdown so the backend can adopt any coin that's held but
    // not yet tracked as a position — see PolygonDexExecutionClient's
    // AssetBalance and the backend's _reconcile_untracked_positions.
    val assets: List<AssetBalanceRequest> = emptyList(),
)

@Serializable
data class PendingNotificationOut(
    val id: String,
    val title: String,
    val body: String,
    val created_at: String,
)

@Serializable
data class LoginRequest(val username: String, val password: String)

@Serializable
data class TokenResponse(val access_token: String, val refresh_token: String, val token_type: String = "bearer")

/** Body for POST /api/auth/refresh — trades a still-valid refresh token
 * for a brand-new access + refresh token pair, with no password involved.
 * See ApiClientFactory's Authenticator, which calls this automatically
 * the moment any request comes back 401. */
@Serializable
data class RefreshTokenRequest(val refresh_token: String)

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
/**
 * One token actually held in the wallet, purely for on-screen "Tokens"
 * list display (DashboardScreen) — the way a wallet app like MetaMask
 * shows what you own, across every network NEURAVEX can read: Polygon
 * (traded — PolygonDexExecutionClient) and Ethereum mainnet (view-only —
 * EthereumMainnetBalanceChecker). Deliberately NOT the same shape as
 * PolygonDexExecutionClient.AssetBalance, which is sent to the backend to
 * be adopted as a tradable position and therefore excludes native MATIC
 * and USDC on purpose (see that class's doc) — a display list has no such
 * restriction, so this one includes every nonzero, successfully-priced
 * asset with no exceptions. Never sent over the network.
 */
data class WalletHeldAsset(
    val symbol: String,
    val network: String, // "Polygon" or "Ethereum"
    val quantity: Double,
    val usdValue: Double,
)

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
