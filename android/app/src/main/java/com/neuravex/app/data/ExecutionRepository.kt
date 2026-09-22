package com.neuravex.app.data

import java.time.Instant
import java.time.format.DateTimeParseException

class ExecutionRepository(private val api: NeuravexApi, private val accountId: String) {

    sealed class ExecutionOutcome {
        data class Executed(val symbol: String, val quantity: Double, val price: Double?) : ExecutionOutcome()
        data class Failed(val symbol: String, val reason: String) : ExecutionOutcome()
        data class Skipped(val symbol: String, val reason: String) : ExecutionOutcome()
    }

    suspend fun processPending(binanceClient: BinanceExecutionClient?): List<ExecutionOutcome> {
        if (binanceClient == null) {
            return listOf(ExecutionOutcome.Skipped("*", "No exchange credentials configured on this device"))
        }

        val response = api.getPendingExecutions(accountId)
        if (!response.isSuccessful) {
            return listOf(ExecutionOutcome.Skipped("*", "Could not reach backend: HTTP ${response.code()}"))
        }
        val pending = response.body().orEmpty()
        val results = mutableListOf<ExecutionOutcome>()

        for (item in pending) {
            results += processOne(item, binanceClient)
        }
        return results
    }

    suspend fun processPendingWallet(dexClient: PolygonDexExecutionClient?): List<ExecutionOutcome> {
        if (dexClient == null) {
            return listOf(ExecutionOutcome.Skipped("*", "No wallet key configured on this device"))
        }

        val response = api.getPendingExecutions(accountId)
        if (!response.isSuccessful) {
            return listOf(ExecutionOutcome.Skipped("*", "Could not reach backend: HTTP ${response.code()}"))
        }
        val pending = response.body().orEmpty()
        val results = mutableListOf<ExecutionOutcome>()

        for (item in pending) {
            results += processOneWallet(item, dexClient)
        }
        return results
    }

    private suspend fun processOne(item: PendingExecutionOut, client: BinanceExecutionClient): ExecutionOutcome {
        if (isExpired(item.expires_at)) {
            return ExecutionOutcome.Skipped(item.symbol, "Expired before this device picked it up — missed by design, not executed late")
        }

        val claim = api.claimExecution(item.id)
        if (!claim.isSuccessful) {
            return ExecutionOutcome.Skipped(item.symbol, "Not claimed (HTTP ${claim.code()}) — already handled or expired")
        }

        val binanceSymbol = item.symbol.replace("/", "")
        val result = client.placeMarketOrder(binanceSymbol, item.side, item.quantity)

        return if (result.success) {
            api.reportExecution(
                item.id,
                ReportExecutionRequest(
                    status = "executed",
                    exchange_order_id = result.exchangeOrderId,
                    average_fill_price = result.averageFillPrice,
                    filled_quantity = result.filledQuantity,
                ),
            )
            ExecutionOutcome.Executed(item.symbol, result.filledQuantity ?: item.quantity, result.averageFillPrice)
        } else {
            api.reportExecution(
                item.id,
                ReportExecutionRequest(status = "failed", failure_reason = result.errorMessage),
            )
            ExecutionOutcome.Failed(item.symbol, result.errorMessage ?: "Unknown execution error")
        }
    }

    private suspend fun processOneWallet(item: PendingExecutionOut, client: PolygonDexExecutionClient): ExecutionOutcome {
        if (isExpired(item.expires_at)) {
            return ExecutionOutcome.Skipped(item.symbol, "Expired before this device picked it up — missed by design, not executed late")
        }

        val claim = api.claimExecution(item.id)
        if (!claim.isSuccessful) {
            return ExecutionOutcome.Skipped(item.symbol, "Not claimed (HTTP ${claim.code()}) — already handled or expired")
        }

        val result = client.swap(item.symbol, item.side, item.quantity)

        return if (result.success) {
            // For a BUY, amountIn is the quote asset spent (USDC) and
            // amountOut is the base asset received — so the token amount
            // is executedAmountOut and price (USDC per token) is
            // amountIn/amountOut. For a SELL it's the other way around:
            // amountIn is the base asset actually sold and amountOut is
            // the USDC received, so the token amount is executedAmountIn
            // and price is amountOut/amountIn. Previously this always
            // used the BUY-shaped math, which for a sell reported the
            // USDC proceeds as the "quantity" and an inverted (tokens per
            // USDC, not USDC per token) number as the "price" — harmless
            // while nothing server-side read these fields, but wrong the
            // moment report-execution started using them to update the
            // account's open position (see backend's
            // execution.py:_apply_fill_to_position).
            val filledQty = if (item.side.lowercase() == "sell") {
                result.executedAmountIn?.toDouble() ?: item.quantity
            } else {
                result.executedAmountOut?.toDouble() ?: item.quantity
            }
            val fillPrice = computeEffectivePrice(result, item.side)
            api.reportExecution(
                item.id,
                ReportExecutionRequest(
                    status = "executed",
                    exchange_order_id = result.txHash,
                    average_fill_price = fillPrice,
                    filled_quantity = filledQty,
                ),
            )
            ExecutionOutcome.Executed(item.symbol, filledQty, fillPrice)
        } else {
            api.reportExecution(
                item.id,
                ReportExecutionRequest(status = "failed", failure_reason = result.errorMessage),
            )
            ExecutionOutcome.Failed(item.symbol, result.errorMessage ?: "Unknown execution error")
        }
    }

    private fun computeEffectivePrice(result: PolygonDexExecutionClient.ExecutionResult, side: String): Double? {
        val amountIn = result.executedAmountIn?.toDouble() ?: return null
        val amountOut = result.executedAmountOut?.toDouble() ?: return null
        return if (side.lowercase() == "sell") {
            if (amountIn == 0.0) null else amountOut / amountIn // USDC received per token sold
        } else {
            if (amountOut == 0.0) null else amountIn / amountOut // USDC spent per token bought
        }
    }

    private fun isExpired(expiresAtIso: String): Boolean {
        return try {
            Instant.parse(if (expiresAtIso.endsWith("Z")) expiresAtIso else "${expiresAtIso}Z").isBefore(Instant.now())
        } catch (e: DateTimeParseException) {
            false
        }
    }
}
