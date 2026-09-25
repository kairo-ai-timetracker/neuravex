package com.neuravex.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.web3j.abi.FunctionEncoder
import org.web3j.abi.FunctionReturnDecoder
import org.web3j.abi.TypeReference
import org.web3j.abi.datatypes.Address
import org.web3j.abi.datatypes.Function
import org.web3j.abi.datatypes.generated.Uint256
import org.web3j.protocol.Web3j
import org.web3j.protocol.core.DefaultBlockParameterName
import org.web3j.protocol.core.methods.request.Transaction
import org.web3j.protocol.http.HttpService
import java.math.BigDecimal
import java.math.BigInteger

/**
 * Read-only balance check for Ethereum mainnet — a completely separate,
 * much smaller sibling of PolygonDexExecutionClient. This exists purely
 * to answer "what do I own", not to trade: NEURAVEX's AI, risk engine,
 * and execution logic are all scoped to Polygon only (see the safety
 * architecture), and this class has no create_order/swap capability at
 * all — it can only look, never act. Kept on its own RPC connection
 * (Ethereum mainnet, not Polygon) since the two chains share nothing —
 * different gas token, different contract addresses, different network.
 *
 * Same verification standard as PolygonTokenRegistry: every address
 * below was independently checked against Etherscan before being added.
 */
class EthereumMainnetBalanceChecker(privateKeyHex: String) {

    data class Token(val symbol: String, val address: String, val decimals: Int)

    companion object {
        // PublicNode's Ethereum mainnet endpoint — same provider already
        // used for Polygon (see PolygonTokenRegistry), chosen for the
        // same reason: genuinely free, no API key required for eth_call.
        const val RPC_URL = "https://ethereum-rpc.publicnode.com"

        // Verified against Etherscan.
        val SHIB = Token("SHIB", "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE", 18)

        val KNOWN_TOKENS = listOf(SHIB)
    }

    private val web3j = Web3j.build(HttpService(RPC_URL))
    private val walletAddress: String = org.web3j.crypto.Credentials.create(privateKeyHex.trim()).address

    /**
     * Native ETH + known-token balances. Unlike the Polygon side, this
     * does NOT attempt to price anything via an on-chain Quoter (no DEX
     * integration exists here at all, by design) — it reports raw
     * balances only. Pricing for the Dashboard's combined "other
     * networks" figure comes from a simple, separate price lookup the
     * caller supplies, not from this class.
     */
    suspend fun getBalances(): Map<String, BigDecimal> = getBalancesChecked().balances

    /**
     * Balances plus the names of any lookups that FAILED. A missing entry in
     * `balances` is ambiguous on its own (failed vs. genuinely zero) — the
     * Portfolio total must only count a reading as complete when `failed`
     * is empty, otherwise a transient RPC error would silently understate
     * the wallet (the same trap as the old "€0,00" balance bug).
     */
    data class CheckedBalances(val balances: Map<String, BigDecimal>, val failed: List<String>)

    /**
     * Value of everything on Ethereum mainnet in USD. `complete` is true
     * only if every balance lookup succeeded AND every non-zero balance
     * could be priced. `heldAssets` is the same WalletHeldAsset shape
     * PolygonDexExecutionClient produces (see its doc / Models.kt) —
     * purely for the Dashboard's on-screen "Tokens" list, never sent
     * anywhere.
     */
    data class ValueResult(
        val totalUsd: Double,
        val complete: Boolean,
        val diagnostics: List<String>,
        val heldAssets: List<WalletHeldAsset> = emptyList(),
    )

    suspend fun getTotalUsd(): ValueResult {
        val checked = getBalancesChecked()
        val held = checked.balances.filterValues { it.signum() > 0 }
        val prices = if (held.isEmpty()) emptyMap() else UsdPriceLookup.getUsdPrices(held.keys)
        var total = 0.0
        val diagnostics = mutableListOf<String>()
        val heldAssets = mutableListOf<WalletHeldAsset>()
        var complete = checked.failed.isEmpty()
        for ((symbol, amount) in checked.balances) {
            if (amount.signum() <= 0) {
                diagnostics += "Ethereum $symbol: zero balance"
                continue
            }
            val price = prices[symbol]
            if (price == null) {
                complete = false
                diagnostics += "Ethereum $symbol: $amount -> no price available"
            } else {
                val usd = amount.toDouble() * price
                total += usd
                diagnostics += "Ethereum $symbol: $amount -> \$%.6f".format(usd)
                heldAssets += WalletHeldAsset(symbol, "Ethereum", amount.toDouble(), usd)
            }
        }
        checked.failed.forEach { diagnostics += "Ethereum $it: balance lookup FAILED" }
        return ValueResult(total, complete, diagnostics, heldAssets)
    }

    suspend fun getBalancesChecked(): CheckedBalances = withContext(Dispatchers.IO) {
        val result = mutableMapOf<String, BigDecimal>()
        val failed = mutableListOf<String>()
        try {
            val weiBalance = web3j.ethGetBalance(walletAddress, DefaultBlockParameterName.LATEST).send().balance
            result["ETH"] = BigDecimal(weiBalance).divide(BigDecimal.TEN.pow(18))
        } catch (e: Exception) {
            failed += "ETH"
            // Best-effort, view-only: a failed lookup here just means
            // that one figure is missing from the "other networks"
            // display, never a reason to alarm the user the way a
            // failed Polygon balance check does (nothing here gates any
            // trading decision).
        }
        for (token in KNOWN_TOKENS) {
            try {
                val function = Function("balanceOf", listOf(Address(walletAddress)), listOf(object : TypeReference<Uint256>() {}))
                val encoded = FunctionEncoder.encode(function)
                val response = web3j.ethCall(
                    Transaction.createEthCallTransaction(walletAddress, token.address, encoded),
                    DefaultBlockParameterName.LATEST,
                ).send()
                var ok = false
                if (!response.hasError()) {
                    val decoded = FunctionReturnDecoder.decode(response.value, function.outputParameters)
                    if (decoded.isNotEmpty()) {
                        val raw = (decoded[0] as Uint256).value
                        result[token.symbol] = BigDecimal(raw).divide(BigDecimal.TEN.pow(token.decimals))
                        ok = true
                    }
                }
                if (!ok) failed += token.symbol
            } catch (e: Exception) {
                failed += token.symbol
                // Same best-effort reasoning as above.
            }
        }
        CheckedBalances(result, failed)
    }
}
