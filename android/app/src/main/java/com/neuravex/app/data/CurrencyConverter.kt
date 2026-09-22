package com.neuravex.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Everything the backend reports (equity, cash, P/L) is a USD-equivalent
 * figure: USDC is a dollar stablecoin, and every other token on the
 * Dashboard is priced against USDC by PolygonDexExecutionClient /
 * EthereumMainnetBalanceChecker. The Dashboard previously showed those raw
 * dollar numbers with a "€" symbol — same digits, wrong currency, off by
 * whatever EUR/USD happens to be. This converts before display.
 *
 * Frankfurter (ECB reference rates, free, keyless) is used rather than a
 * crypto price API, since this is a plain fiat conversion. Cached for 15
 * minutes in memory — FX rates don't move fast enough to justify fetching
 * on every 15s poll — with the last successfully-fetched rate reused as
 * the fallback if a later fetch fails, so a brief network hiccup doesn't
 * suddenly change what's on screen.
 */
object CurrencyConverter {

    // Only used if this process has NEVER fetched a live rate yet AND the
    // very first fetch fails. Clearly approximate — replaced the moment a
    // live fetch succeeds.
    private const val FALLBACK_USD_TO_EUR = 0.92
    private const val CACHE_MS = 15 * 60_000L

    @Volatile private var cachedRate: Double? = null
    @Volatile private var cachedAtMs: Long = 0

    suspend fun getUsdToEurRate(): Double = withContext(Dispatchers.IO) {
        val now = System.currentTimeMillis()
        val cached = cachedRate
        if (cached != null && now - cachedAtMs < CACHE_MS) return@withContext cached
        try {
            val body = httpGet("https://api.frankfurter.app/latest?from=USD&to=EUR")
            val rate = JSONObject(body).getJSONObject("rates").getDouble("EUR")
            cachedRate = rate
            cachedAtMs = now
            rate
        } catch (e: Exception) {
            // Best-effort: reuse whatever was last fetched successfully,
            // however old, rather than the hardcoded fallback — a stale
            // real rate is closer to correct than a guess baked in at
            // build time.
            cached ?: FALLBACK_USD_TO_EUR
        }
    }

    suspend fun usdToEur(usd: Double): Double = usd * getUsdToEurRate()

    private fun httpGet(url: String): String {
        val connection = URL(url).openConnection() as HttpURLConnection
        try {
            connection.connectTimeout = 8_000
            connection.readTimeout = 8_000
            connection.requestMethod = "GET"
            if (connection.responseCode !in 200..299) error("HTTP ${connection.responseCode}")
            return connection.inputStream.bufferedReader().use { it.readText() }
        } finally {
            connection.disconnect()
        }
    }
}
