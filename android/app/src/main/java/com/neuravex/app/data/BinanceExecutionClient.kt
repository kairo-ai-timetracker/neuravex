package com.neuravex.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.security.InvalidKeyException
import java.security.NoSuchAlgorithmException
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * Places market orders directly against Binance's REST API, using an
 * API key/secret held only on this device. The NEURAVEX backend never
 * sees this credential — it only ever sees the risk engine's plan (see
 * PendingExecutionOut) and the fill result reported back after.
 */
class BinanceExecutionClient(private val apiKey: String, private val apiSecret: String) {

    data class ExecutionResult(
        val success: Boolean,
        val exchangeOrderId: String? = null,
        val averageFillPrice: Double? = null,
        val filledQuantity: Double? = null,
        val errorMessage: String? = null,
    )

    private val client = OkHttpClient()
    private val baseUrl = "https://api.binance.com"

    suspend fun placeMarketOrder(symbol: String, side: String, quantity: Double): ExecutionResult = withContext(Dispatchers.IO) {
        try {
            val timestamp = System.currentTimeMillis()
            val params = "symbol=$symbol&side=${side.uppercase()}&type=MARKET&quantity=$quantity&timestamp=$timestamp"
            val signature = sign(params)
            val url = "$baseUrl/api/v3/order?$params&signature=$signature"

            val request = Request.Builder()
                .url(url)
                .addHeader("X-MBX-APIKEY", apiKey)
                .post(okhttp3.RequestBody.create(null, ByteArray(0)))
                .build()

            client.newCall(request).execute().use { response ->
                val body = response.body?.string() ?: ""
                if (!response.isSuccessful) {
                    return@withContext ExecutionResult(success = false, errorMessage = "HTTP ${response.code}: $body")
                }
                // Minimal parsing — a production build would use kotlinx.serialization here too.
                val orderIdMatch = Regex("\"orderId\":(\\d+)").find(body)
                val fillPriceMatch = Regex("\"price\":\"([0-9.]+)\"").find(body)
                val execQtyMatch = Regex("\"executedQty\":\"([0-9.]+)\"").find(body)
                ExecutionResult(
                    success = true,
                    exchangeOrderId = orderIdMatch?.groupValues?.get(1),
                    averageFillPrice = fillPriceMatch?.groupValues?.get(1)?.toDoubleOrNull(),
                    filledQuantity = execQtyMatch?.groupValues?.get(1)?.toDoubleOrNull() ?: quantity,
                )
            }
        } catch (e: Exception) {
            ExecutionResult(success = false, errorMessage = "${e.javaClass.simpleName}: ${e.message}")
        }
    }

    private fun sign(data: String): String {
        try {
            val mac = Mac.getInstance("HmacSHA256")
            mac.init(SecretKeySpec(apiSecret.toByteArray(), "HmacSHA256"))
            return mac.doFinal(data.toByteArray()).joinToString("") { "%02x".format(it) }
        } catch (e: NoSuchAlgorithmException) {
            throw IllegalStateException("HmacSHA256 not available", e)
        } catch (e: InvalidKeyException) {
            throw IllegalStateException("Invalid API secret", e)
        }
    }
}
