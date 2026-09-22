package com.neuravex.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Minimal, read-only USD price lookup for the view-only "other networks"
 * holdings (ETH, SHIB). Deliberately separate from the Polygon side, which
 * prices tokens through an on-chain Quoter — there is no DEX integration on
 * Ethereum mainnet by design, so a plain public price feed is used instead.
 *
 * Binance's public ticker is tried first (keyless, same source the backend
 * already uses for market data); CoinGecko is the fallback. A symbol with no
 * price is simply missing from the result — callers must treat that as
 * "incomplete", never as a price of zero.
 */
object UsdPriceLookup {

    private data class Ids(val binancePair: String, val coingeckoId: String)

    private val IDS = mapOf(
        "ETH" to Ids("ETHUSDT", "ethereum"),
        "SHIB" to Ids("SHIBUSDT", "shiba-inu"),
    )

    suspend fun getUsdPrices(symbols: Collection<String>): Map<String, Double> = withContext(Dispatchers.IO) {
        val prices = mutableMapOf<String, Double>()
        for (symbol in symbols) {
            val ids = IDS[symbol] ?: continue
            try {
                val body = httpGet("https://api.binance.com/api/v3/ticker/price?symbol=${ids.binancePair}")
                val price = JSONObject(body).getString("price").toDouble()
                if (price > 0.0) prices[symbol] = price
            } catch (e: Exception) {
                // fall through to CoinGecko below
            }
        }
        val missing = symbols.filter { it in IDS && it !in prices }
        if (missing.isNotEmpty()) {
            try {
                val ids = missing.joinToString(",") { IDS.getValue(it).coingeckoId }
                val json = JSONObject(httpGet("https://api.coingecko.com/api/v3/simple/price?ids=$ids&vs_currencies=usd"))
                for (symbol in missing) {
                    val entry = json.optJSONObject(IDS.getValue(symbol).coingeckoId) ?: continue
                    val price = entry.optDouble("usd", 0.0)
                    if (price > 0.0) prices[symbol] = price
                }
            } catch (e: Exception) {
                // best effort — callers see the gap as "incomplete"
            }
        }
        prices
    }

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
