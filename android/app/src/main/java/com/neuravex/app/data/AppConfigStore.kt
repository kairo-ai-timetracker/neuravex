package com.neuravex.app.data

import android.content.Context
import android.content.SharedPreferences

/** Non-sensitive local config (backend URL, account ID, polling interval).
 * Sensitive credentials never live here — see SecureCredentialStore. */
class AppConfigStore(context: Context) {
    private val prefs: SharedPreferences = context.getSharedPreferences("neuravex_config", Context.MODE_PRIVATE)

    var backendBaseUrl: String?
        get() = prefs.getString(KEY_BASE_URL, null)
        set(value) = prefs.edit().putString(KEY_BASE_URL, value).apply()

    var accountId: String?
        get() = prefs.getString(KEY_ACCOUNT_ID, null)
        set(value) = prefs.edit().putString(KEY_ACCOUNT_ID, value).apply()

    var pollingIntervalSeconds: Int
        get() = prefs.getInt(KEY_POLL_INTERVAL, 60)
        set(value) = prefs.edit().putInt(KEY_POLL_INTERVAL, value).apply()

    /**
     * The most recent per-asset wallet-balance diagnostic, written by
     * TradingExecutionService after every balance check and read by
     * DashboardScreen to display it persistently on-screen. This exists
     * specifically so the diagnosis doesn't depend on the user catching a
     * transient notification at the right moment — it's just always
     * there, showing the result of the last check whenever the dashboard
     * is open.
     */
    var lastBalanceDiagnostics: String?
        get() = prefs.getString(KEY_LAST_BALANCE_DIAGNOSTICS, null)
        set(value) = prefs.edit().putString(KEY_LAST_BALANCE_DIAGNOSTICS, value).apply()

    /**
     * The user's own custom Polygon token (symbol, contract address,
     * decimals) — set from Settings, persisted here so it survives app
     * restarts, and loaded into PolygonTokenRegistry.setCustomToken() on
     * every app start (see MainActivity). Unlike every other token in
     * PolygonTokenRegistry, this one is NOT independently verified by
     * this app; getting the address right is the user's own
     * responsibility. All three are null/empty together, or all three
     * are set together — see setCustomToken()/clearCustomToken() below.
     */
    var customTokenSymbol: String?
        get() = prefs.getString(KEY_CUSTOM_SYMBOL, null)
        private set(value) = prefs.edit().putString(KEY_CUSTOM_SYMBOL, value).apply()

    var customTokenAddress: String?
        get() = prefs.getString(KEY_CUSTOM_ADDRESS, null)
        private set(value) = prefs.edit().putString(KEY_CUSTOM_ADDRESS, value).apply()

    var customTokenDecimals: Int
        get() = prefs.getInt(KEY_CUSTOM_DECIMALS, 18)
        private set(value) = prefs.edit().putInt(KEY_CUSTOM_DECIMALS, value).apply()

    fun setCustomToken(symbol: String, address: String, decimals: Int) {
        customTokenSymbol = symbol
        customTokenAddress = address
        customTokenDecimals = decimals
    }

    fun clearCustomToken() {
        prefs.edit().remove(KEY_CUSTOM_SYMBOL).remove(KEY_CUSTOM_ADDRESS).remove(KEY_CUSTOM_DECIMALS).apply()
    }

    /** Re-applies the persisted custom token (if any) to
     * PolygonTokenRegistry — call once on app start, since the registry
     * itself holds no state across process restarts. */
    fun applyCustomTokenToRegistry() {
        val symbol = customTokenSymbol
        val address = customTokenAddress
        if (symbol != null && address != null) {
            PolygonTokenRegistry.setCustomToken(PolygonTokenRegistry.Token(symbol, address, customTokenDecimals))
        }
    }

    fun isOnboarded(): Boolean = backendBaseUrl != null && accountId != null

    companion object {
        private const val KEY_BASE_URL = "backend_base_url"
        private const val KEY_ACCOUNT_ID = "account_id"
        private const val KEY_POLL_INTERVAL = "poll_interval_seconds"
        private const val KEY_LAST_BALANCE_DIAGNOSTICS = "last_balance_diagnostics"
        private const val KEY_CUSTOM_SYMBOL = "custom_token_symbol"
        private const val KEY_CUSTOM_ADDRESS = "custom_token_address"
        private const val KEY_CUSTOM_DECIMALS = "custom_token_decimals"
    }
}
