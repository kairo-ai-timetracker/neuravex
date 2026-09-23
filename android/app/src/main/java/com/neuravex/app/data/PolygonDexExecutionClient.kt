package com.neuravex.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.web3j.abi.FunctionEncoder
import org.web3j.abi.FunctionReturnDecoder
import org.web3j.abi.TypeReference
import org.web3j.abi.datatypes.Address
import org.web3j.abi.datatypes.Function
import org.web3j.abi.datatypes.StaticStruct
import org.web3j.abi.datatypes.generated.Uint160
import org.web3j.abi.datatypes.generated.Uint24
import org.web3j.abi.datatypes.generated.Uint256
import org.web3j.crypto.Credentials
import org.web3j.crypto.RawTransaction
import org.web3j.crypto.TransactionEncoder
import org.web3j.protocol.Web3j
import org.web3j.protocol.core.DefaultBlockParameterName
import org.web3j.protocol.core.methods.request.Transaction
import org.web3j.protocol.core.methods.response.TransactionReceipt
import org.web3j.protocol.http.HttpService
import org.web3j.utils.Numeric
import java.math.BigDecimal
import java.math.BigInteger

/**
 * Executes swaps on Uniswap V3 (Polygon) using a wallet private key held
 * only on this device — it is never sent to the NEURAVEX backend or
 * anywhere else. See PolygonTokenRegistry.kt for the (independently
 * verified) token/contract addresses this relies on.
 *
 * BUY uses `exactOutputSingle` (the risk engine gives a target holding
 * size), SELL uses `exactInputSingle` (an amount of an existing position
 * to liquidate).
 */
class PolygonDexExecutionClient(
    privateKeyHex: String,
    rpcUrl: String = PolygonNetwork.DEFAULT_PUBLIC_RPC_URL,
) {
    data class ExecutionResult(
        val success: Boolean,
        val txHash: String? = null,
        val executedAmountIn: BigDecimal? = null,
        val executedAmountOut: BigDecimal? = null,
        val errorMessage: String? = null,
    )

    private val web3j: Web3j = Web3j.build(HttpService(rpcUrl))
    private val credentials: Credentials = Credentials.create(privateKeyHex.trim())

    val walletAddress: String get() = credentials.address

    suspend fun swap(pairSymbol: String, side: String, quantity: Double): ExecutionResult = withContext(Dispatchers.IO) {
        try {
            val pair = PolygonTokenRegistry.parsePair(pairSymbol)
                ?: return@withContext ExecutionResult(success = false, errorMessage = "Unknown or unverified token pair: $pairSymbol")
            val (base, quote) = pair

            val gasPrice = web3j.ethGasPrice().send().gasPrice
            if (gasPrice > MAX_GAS_PRICE_WEI) {
                return@withContext ExecutionResult(
                    success = false,
                    errorMessage = "Gas price abnormally high ($gasPrice wei) — skipping this trade rather than paying an outlier fee",
                )
            }

            when (side.lowercase()) {
                "buy" -> executeExactOutput(tokenIn = quote, tokenOut = base, amountOutHuman = quantity, gasPrice = gasPrice)
                "sell" -> executeExactInput(tokenIn = base, tokenOut = quote, amountInHuman = quantity, gasPrice = gasPrice)
                else -> ExecutionResult(success = false, errorMessage = "Unknown side: $side")
            }
        } catch (e: Exception) {
            ExecutionResult(success = false, errorMessage = "DEX execution error: ${e.javaClass.simpleName}: ${e.message}")
        }
    }

    private fun executeExactInput(
        tokenIn: PolygonTokenRegistry.Token,
        tokenOut: PolygonTokenRegistry.Token,
        amountInHuman: Double,
        gasPrice: BigInteger,
    ): ExecutionResult {
        val amountIn = toRawAmount(amountInHuman, tokenIn.decimals)

        val balance = erc20BalanceOf(tokenIn.address)
        if (balance < amountIn) {
            return ExecutionResult(
                success = false,
                errorMessage = "Insufficient ${tokenIn.symbol}: have ${fromRawAmount(balance, tokenIn.decimals)}, need $amountInHuman",
            )
        }

        val quotedAmountOut = quoteExactInputSingle(tokenIn.address, tokenOut.address, amountIn)
            ?: return ExecutionResult(success = false, errorMessage = "No quote available for ${tokenIn.symbol}/${tokenOut.symbol} — pool may lack liquidity")
        val amountOutMinimum = applySlippage(quotedAmountOut, reduce = true)

        ensureApproval(tokenIn.address, amountIn, gasPrice)?.let { return it }

        val deadline = deadlineFromNow()
        val params = StaticStruct(
            Address(tokenIn.address), Address(tokenOut.address),
            Uint24(BigInteger.valueOf(PolygonNetwork.DEFAULT_POOL_FEE_TIER.toLong())),
            Address(walletAddress), Uint256(deadline), Uint256(amountIn), Uint256(amountOutMinimum), Uint160(BigInteger.ZERO),
        )
        val function = Function("exactInputSingle", listOf(params), listOf(object : TypeReference<Uint256>() {}))

        val txHash = try {
            sendContractTransaction(PolygonNetwork.UNISWAP_V3_SWAP_ROUTER, function, gasPrice, GAS_LIMIT_SWAP)
        } catch (e: Exception) {
            return ExecutionResult(success = false, errorMessage = "Swap transaction rejected: ${e.message}")
        }
        val receipt = waitForReceipt(txHash)
            ?: return ExecutionResult(success = false, txHash = txHash, errorMessage = "Timed out waiting for on-chain confirmation")
        if (receipt.status != "0x1") {
            return ExecutionResult(success = false, txHash = txHash, errorMessage = "Transaction reverted on-chain")
        }
        return ExecutionResult(
            success = true, txHash = txHash,
            executedAmountIn = fromRawAmount(amountIn, tokenIn.decimals),
            executedAmountOut = fromRawAmount(quotedAmountOut, tokenOut.decimals),
        )
    }

    private fun executeExactOutput(
        tokenIn: PolygonTokenRegistry.Token,
        tokenOut: PolygonTokenRegistry.Token,
        amountOutHuman: Double,
        gasPrice: BigInteger,
    ): ExecutionResult {
        val amountOut = toRawAmount(amountOutHuman, tokenOut.decimals)

        val quotedAmountIn = quoteExactOutputSingle(tokenIn.address, tokenOut.address, amountOut)
            ?: return ExecutionResult(success = false, errorMessage = "No quote available for ${tokenIn.symbol}/${tokenOut.symbol} — pool may lack liquidity")
        val amountInMaximum = applySlippage(quotedAmountIn, reduce = false)

        val balance = erc20BalanceOf(tokenIn.address)
        if (balance < amountInMaximum) {
            return ExecutionResult(
                success = false,
                errorMessage = "Insufficient ${tokenIn.symbol}: have ${fromRawAmount(balance, tokenIn.decimals)}, " +
                    "would need up to ${fromRawAmount(amountInMaximum, tokenIn.decimals)}",
            )
        }

        ensureApproval(tokenIn.address, amountInMaximum, gasPrice)?.let { return it }

        val deadline = deadlineFromNow()
        val params = StaticStruct(
            Address(tokenIn.address), Address(tokenOut.address),
            Uint24(BigInteger.valueOf(PolygonNetwork.DEFAULT_POOL_FEE_TIER.toLong())),
            Address(walletAddress), Uint256(deadline), Uint256(amountOut), Uint256(amountInMaximum), Uint160(BigInteger.ZERO),
        )
        val function = Function("exactOutputSingle", listOf(params), listOf(object : TypeReference<Uint256>() {}))

        val txHash = try {
            sendContractTransaction(PolygonNetwork.UNISWAP_V3_SWAP_ROUTER, function, gasPrice, GAS_LIMIT_SWAP)
        } catch (e: Exception) {
            return ExecutionResult(success = false, errorMessage = "Swap transaction rejected: ${e.message}")
        }
        val receipt = waitForReceipt(txHash)
            ?: return ExecutionResult(success = false, txHash = txHash, errorMessage = "Timed out waiting for on-chain confirmation")
        if (receipt.status != "0x1") {
            return ExecutionResult(success = false, txHash = txHash, errorMessage = "Transaction reverted on-chain")
        }
        return ExecutionResult(
            success = true, txHash = txHash,
            executedAmountIn = fromRawAmount(quotedAmountIn, tokenIn.decimals),
            executedAmountOut = fromRawAmount(amountOut, tokenOut.decimals),
        )
    }

    private fun quoteExactInputSingle(tokenIn: String, tokenOut: String, amountIn: BigInteger): BigInteger? {
        val function = Function(
            "quoteExactInputSingle",
            listOf(
                Address(tokenIn), Address(tokenOut),
                Uint24(BigInteger.valueOf(PolygonNetwork.DEFAULT_POOL_FEE_TIER.toLong())),
                Uint256(amountIn), Uint160(BigInteger.ZERO),
            ),
            listOf(object : TypeReference<Uint256>() {}),
        )
        return callAndDecodeUint256(PolygonNetwork.UNISWAP_V3_QUOTER, function)
    }

    private fun quoteExactOutputSingle(tokenIn: String, tokenOut: String, amountOut: BigInteger): BigInteger? {
        val function = Function(
            "quoteExactOutputSingle",
            listOf(
                Address(tokenIn), Address(tokenOut),
                Uint24(BigInteger.valueOf(PolygonNetwork.DEFAULT_POOL_FEE_TIER.toLong())),
                Uint256(amountOut), Uint160(BigInteger.ZERO),
            ),
            listOf(object : TypeReference<Uint256>() {}),
        )
        return callAndDecodeUint256(PolygonNetwork.UNISWAP_V3_QUOTER, function)
    }

    private fun callAndDecodeUint256(contractAddress: String, function: Function): BigInteger? {
        val encoded = FunctionEncoder.encode(function)
        val response = web3j.ethCall(
            Transaction.createEthCallTransaction(walletAddress, contractAddress, encoded),
            DefaultBlockParameterName.LATEST,
        ).send()
        if (response.hasError()) {
            // Propagate the RPC provider's own error message instead of
            // discarding it — this used to just return null here, which
            // is exactly what made "every eth_call is failing" and "the
            // request itself is malformed" indistinguishable in the
            // diagnostics from every other kind of failure. Whatever
            // Ankr/the node actually says (rate limited, unsupported
            // method, bad params, ...) now shows up verbatim in the next
            // wallet balance check.
            throw IllegalStateException("RPC error: ${response.error?.code} ${response.error?.message}")
        }
        val decoded = FunctionReturnDecoder.decode(response.value, function.outputParameters)
        if (decoded.isEmpty()) return null
        return (decoded[0] as Uint256).value
    }

    private fun erc20BalanceOf(tokenAddress: String): BigInteger {
        val function = Function("balanceOf", listOf(Address(walletAddress)), listOf(object : TypeReference<Uint256>() {}))
        // No `?: BigInteger.ZERO` fallback here on purpose. That fallback
        // used to silently turn a FAILED RPC call into a reported balance
        // of exactly zero — indistinguishable from a real zero balance,
        // and exactly what made the USDC "$0" line in the diagnostics
        // misleading: the RPC call was failing, not returning a real
        // zero, but the failure was being masked before it ever reached
        // the diagnostic. Throwing here lets every caller (both the
        // portfolio-value diagnostics and the pre-swap balance checks in
        // executeExactInput/executeExactOutput) see and report the real
        // failure instead of silently treating "I don't know" as "zero."
        return callAndDecodeUint256(tokenAddress, function)
            ?: throw IllegalStateException("RPC call for balanceOf returned no usable result (node error or malformed response)")
    }

    private fun erc20Allowance(tokenAddress: String, spender: String): BigInteger {
        val function = Function(
            "allowance", listOf(Address(walletAddress), Address(spender)), listOf(object : TypeReference<Uint256>() {}),
        )
        return callAndDecodeUint256(tokenAddress, function) ?: BigInteger.ZERO
    }

    /** Approves EXACTLY the amount this one trade needs — never an
     * unlimited/standing allowance. */
    private fun ensureApproval(tokenAddress: String, amountNeeded: BigInteger, gasPrice: BigInteger): ExecutionResult? {
        val current = erc20Allowance(tokenAddress, PolygonNetwork.UNISWAP_V3_SWAP_ROUTER)
        if (current >= amountNeeded) return null

        val function = Function(
            "approve", listOf(Address(PolygonNetwork.UNISWAP_V3_SWAP_ROUTER), Uint256(amountNeeded)), emptyList(),
        )
        val txHash = try {
            sendContractTransaction(tokenAddress, function, gasPrice, GAS_LIMIT_APPROVE)
        } catch (e: Exception) {
            return ExecutionResult(success = false, errorMessage = "Approval transaction rejected: ${e.message}")
        }
        val receipt = waitForReceipt(txHash)
            ?: return ExecutionResult(success = false, txHash = txHash, errorMessage = "Approval did not confirm in time")
        if (receipt.status != "0x1") {
            return ExecutionResult(success = false, txHash = txHash, errorMessage = "Approval transaction reverted on-chain")
        }
        return null
    }

    private fun sendContractTransaction(contractAddress: String, function: Function, gasPrice: BigInteger, gasLimit: Long): String {
        val encodedFunction = FunctionEncoder.encode(function)
        val nonce = web3j.ethGetTransactionCount(walletAddress, DefaultBlockParameterName.PENDING).send().transactionCount
        val rawTransaction = RawTransaction.createTransaction(
            nonce, gasPrice, BigInteger.valueOf(gasLimit), contractAddress, BigInteger.ZERO, encodedFunction,
        )
        val signedMessage = TransactionEncoder.signMessage(rawTransaction, PolygonNetwork.CHAIN_ID, credentials)
        val response = web3j.ethSendRawTransaction(Numeric.toHexString(signedMessage)).send()
        if (response.hasError()) {
            throw IllegalStateException(response.error?.message ?: "unknown RPC error")
        }
        return response.transactionHash
    }

    private fun waitForReceipt(txHash: String): TransactionReceipt? {
        repeat(RECEIPT_POLL_MAX_ATTEMPTS) {
            Thread.sleep(RECEIPT_POLL_INTERVAL_MS)
            val receipt = web3j.ethGetTransactionReceipt(txHash).send().transactionReceipt.orElse(null)
            if (receipt != null) return receipt
        }
        return null
    }

    private fun toRawAmount(humanAmount: Double, decimals: Int): BigInteger =
        BigDecimal.valueOf(humanAmount).multiply(BigDecimal.TEN.pow(decimals)).toBigInteger()

    private fun fromRawAmount(rawAmount: BigInteger, decimals: Int): BigDecimal =
        BigDecimal(rawAmount).divide(BigDecimal.TEN.pow(decimals))

    private fun applySlippage(amount: BigInteger, reduce: Boolean): BigInteger {
        val adjustment = amount.multiply(BigInteger.valueOf(SLIPPAGE_BPS)).divide(BigInteger.valueOf(10_000))
        return if (reduce) amount.subtract(adjustment) else amount.add(adjustment)
    }

    private fun deadlineFromNow(): BigInteger =
        BigInteger.valueOf(System.currentTimeMillis() / 1000 + DEADLINE_SECONDS)

    /**
     * eth_getBalance for the native asset (MATIC/POL), tolerant of a
     * known RPC quirk: several Ethereum-compatible nodes (including some
     * responses seen from public Polygon endpoints) return the bare
     * string "0x" for a zero balance instead of the more common "0x0".
     * Both mean exactly zero per the JSON-RPC spec's "leading zeroes
     * stripped" quantity encoding, but web3j's decoder enforces at least
     * one hex digit after "0x" and throws MessageDecodingException on the
     * bare form — which, before this fix, was indistinguishable from a
     * genuine RPC failure in the diagnostics. Caught and treated as zero
     * here specifically (and only) for that one exception type; any
     * other failure still propagates as a real error.
     */
    private fun getNativeBalanceOrZero(): BigInteger {
        return try {
            web3j.ethGetBalance(walletAddress, DefaultBlockParameterName.LATEST).send().balance
        } catch (e: Exception) {
            // Matched by message rather than by catching a specific
            // web3j exception type: the exact fully-qualified class name
            // for this exception differs between web3j versions/artifacts
            // and isn't worth hard-coding. Checking the message for the
            // known "0x" quantity-format complaint is what actually
            // identifies this one specific, safe-to-treat-as-zero case;
            // anything else re-throws unchanged so it's still reported as
            // a real failure in the diagnostics rather than silently
            // swallowed.
            if (e.message?.contains("0x[0-9a-fA-F]+") == true) {
                BigInteger.ZERO
            } else {
                throw e
            }
        }
    }

    // ---- Portfolio value reporting -----------------------------------
    /**
     * Result of a portfolio-value check: the computed USDC total, plus one
     * diagnostic line per asset explaining what happened for it —
     * "skipped (zero balance)", the actual quote/balance value used, or
     * the specific exception if that asset's lookup failed. Replaces the
     * previous silent try/catch-and-ignore version, whose only visible
     * symptom was a flat $0.00 with no way to tell whether that meant
     * "wallet is genuinely empty" or "every RPC call failed silently."
     */
    /** One held ERC-20 asset, structured (unlike `diagnostics`, which is
     * human-readable text only) — this is what lets the backend actually
     * know WHICH coins are held and at what price, so it can adopt them as
     * tracked positions (see ReportBalanceRequest.assets / the backend's
     * _reconcile_untracked_positions). Native MATIC is intentionally not
     * included here — it has no PolygonTokenRegistry entry to buy/sell
     * against, so the backend has nothing to track it as; WMATIC (the
     * wrapped, tradable form) is what gets reported instead. */
    data class AssetBalance(val symbol: String, val quantity: Double, val priceUsd: Double)

    data class PortfolioValueResult(
        val totalUsdc: Double,
        val diagnostics: List<String>,
        val assets: List<AssetBalance> = emptyList(),
    )

    suspend fun getPortfolioValue(): PortfolioValueResult = withContext(Dispatchers.IO) {
        var total = BigDecimal.ZERO
        val diagnostics = mutableListOf<String>()
        val assets = mutableListOf<AssetBalance>()

        try {
            val nativeBalanceWei = getNativeBalanceOrZero()
            if (nativeBalanceWei > BigInteger.ZERO) {
                val quoted = quoteExactInputSingle(PolygonTokenRegistry.WMATIC.address, PolygonTokenRegistry.USDC.address, nativeBalanceWei)
                if (quoted != null) {
                    val usdValue = fromRawAmount(quoted, PolygonTokenRegistry.USDC.decimals)
                    total = total.add(usdValue)
                    diagnostics += "MATIC (native): ${fromRawAmount(nativeBalanceWei, 18)} -> \$$usdValue"
                } else {
                    diagnostics += "MATIC (native): balance=${fromRawAmount(nativeBalanceWei, 18)}, quote FAILED (no pool liquidity or RPC returned nothing)"
                }
            } else {
                diagnostics += "MATIC (native): zero balance, skipped"
            }
        } catch (e: Exception) {
            diagnostics += "MATIC (native): lookup FAILED — ${e.javaClass.simpleName}: ${e.message}"
        }

        for (token in PolygonTokenRegistry.ALL) {
            if (token.symbol == "USDC") continue
            try {
                val balance = erc20BalanceOf(token.address)
                if (balance <= BigInteger.ZERO) {
                    diagnostics += "${token.symbol}: zero balance, skipped"
                    continue
                }
                if (token.symbol == "USDT" || token.symbol == "DAI") {
                    val usdValue = fromRawAmount(balance, token.decimals)
                    total = total.add(usdValue)
                    val qty = fromRawAmount(balance, token.decimals)
                    diagnostics += "${token.symbol}: $qty -> \$$usdValue (1:1 stablecoin)"
                    assets += AssetBalance(token.symbol, qty.toDouble(), 1.0)
                } else {
                    val quoted = quoteExactInputSingle(token.address, PolygonTokenRegistry.USDC.address, balance)
                    if (quoted != null) {
                        val usdValue = fromRawAmount(quoted, PolygonTokenRegistry.USDC.decimals)
                        total = total.add(usdValue)
                        val qty = fromRawAmount(balance, token.decimals)
                        diagnostics += "${token.symbol}: $qty -> \$$usdValue"
                        val qtyDouble = qty.toDouble()
                        if (qtyDouble > 0.0) {
                            assets += AssetBalance(token.symbol, qtyDouble, usdValue.toDouble() / qtyDouble)
                        }
                    } else {
                        diagnostics += "${token.symbol}: balance=${fromRawAmount(balance, token.decimals)}, quote FAILED (no pool liquidity or RPC returned nothing)"
                    }
                }
            } catch (e: Exception) {
                diagnostics += "${token.symbol}: lookup FAILED — ${e.javaClass.simpleName}: ${e.message}"
            }
        }

        try {
            val usdcBalance = erc20BalanceOf(PolygonTokenRegistry.USDC.address)
            val usdValue = fromRawAmount(usdcBalance, PolygonTokenRegistry.USDC.decimals)
            total = total.add(usdValue)
            diagnostics += "USDC: \$$usdValue"
        } catch (e: Exception) {
            diagnostics += "USDC: lookup FAILED — ${e.javaClass.simpleName}: ${e.message}"
        }

        PortfolioValueResult(totalUsdc = total.toDouble(), diagnostics = diagnostics, assets = assets)
    }

    /** Backward-compatible wrapper — returns only the total, for callers
     * that don't need the per-asset breakdown. */
    suspend fun getTotalPortfolioValueInUsdc(): Double = getPortfolioValue().totalUsdc

    companion object {
        private const val SLIPPAGE_BPS = 100L
        private val MAX_GAS_PRICE_WEI: BigInteger = BigInteger.valueOf(1000).multiply(BigInteger.TEN.pow(9))
        private const val GAS_LIMIT_APPROVE = 80_000L
        private const val GAS_LIMIT_SWAP = 300_000L
        private const val RECEIPT_POLL_INTERVAL_MS = 3_000L
        private const val RECEIPT_POLL_MAX_ATTEMPTS = 40
        private const val DEADLINE_SECONDS = 300L
    }
}
