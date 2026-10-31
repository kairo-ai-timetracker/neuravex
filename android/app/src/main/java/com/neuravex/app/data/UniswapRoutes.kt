package com.neuravex.app.data

import org.web3j.abi.FunctionEncoder
import org.web3j.abi.TypeReference
import org.web3j.abi.datatypes.Address
import org.web3j.abi.datatypes.DynamicBytes
import org.web3j.abi.datatypes.DynamicStruct
import org.web3j.abi.datatypes.Function
import org.web3j.abi.datatypes.generated.Uint256
import org.web3j.utils.Numeric
import java.math.BigInteger

/**
 * Which Uniswap V3 pools each coin trades through against native USDC.
 *
 * Measured on-chain (Quoter, USDC -> coin -> USDC for 2,500 USDC, 2026-10-31,
 * round-trip cost including pool fees): WETH 0.37%, LINK 0.63%, WMATIC 1.04%,
 * WBTC 1.12%, AAVE 1.22%, UNI 2.04%, LDO 4.37%. Of 104 coins that are both on
 * Polygon (Uniswap token list) and have Coinbase market data, these are the only
 * ones under 10% over a direct route or a route through WETH or WMATIC. The rest
 * have no pool or lose 80-100% to thin liquidity, so the AI doesn't buy them.
 */
object PolygonRoutes {
    /** USDC -[usdcFee]-> hub -[hubFee]-> coin, or a direct pool if hub is null. */
    private data class Route(val usdcFee: Int, val hub: PolygonTokenRegistry.Token? = null, val hubFee: Int? = null)

    private val ROUTES: Map<String, Route> by lazy {
        val weth = PolygonTokenRegistry.WETH
        mapOf(
            "WETH" to Route(500),
            "WMATIC" to Route(500),
            "WBTC" to Route(3000),
            "LINK" to Route(500, weth, 500),
            "AAVE" to Route(500, weth, 3000),
            "UNI" to Route(500, weth, 3000),
            "LDO" to Route(500, weth, 3000),
        )
    }

    val TRADABLE_SYMBOLS: Set<String> get() = ROUTES.keys

    data class Hops(val tokens: List<String>, val fees: List<Int>) {
        val isDirect: Boolean get() = fees.size == 1
    }

    /** The pools from [from] to [to]. A pair with no measured route falls back
     * to the 0.3% pool, so coins already held can still be valued and sold. */
    fun between(from: PolygonTokenRegistry.Token, to: PolygonTokenRegistry.Token): Hops {
        val usdc = PolygonTokenRegistry.USDC.address
        val coin = if (from.address == usdc) to else from
        val route = ROUTES[coin.symbol]
        if (route == null || (from.address != usdc && to.address != usdc)) {
            return Hops(listOf(from.address, to.address), listOf(PolygonNetwork.DEFAULT_POOL_FEE_TIER))
        }
        val fromUsdc = if (route.hub == null) {
            Hops(listOf(usdc, coin.address), listOf(route.usdcFee))
        } else {
            Hops(listOf(usdc, route.hub.address, coin.address), listOf(route.usdcFee, route.hubFee!!))
        }
        return if (from.address == usdc) fromUsdc else Hops(fromUsdc.tokens.reversed(), fromUsdc.fees.reversed())
    }
}

/** Encoding of Uniswap V3's multi-pool calls. */
object UniswapEncoding {
    /** address (20 bytes), then fee (3 bytes) + address for every further pool. */
    fun path(hops: PolygonRoutes.Hops): ByteArray {
        val hex = StringBuilder(Numeric.cleanHexPrefix(hops.tokens[0]))
        hops.fees.forEachIndexed { i, fee ->
            hex.append(fee.toString(16).padStart(6, '0')).append(Numeric.cleanHexPrefix(hops.tokens[i + 1]))
        }
        return Numeric.hexStringToByteArray(hex.toString())
    }

    /** exactInput / exactOutput share one struct shape; the two amounts are
     * (amountIn, amountOutMinimum) or (amountOut, amountInMaximum). */
    fun routerCall(name: String, path: ByteArray, recipient: String, deadline: BigInteger, amount: BigInteger, limit: BigInteger): Function =
        Function(
            name,
            listOf(DynamicStruct(DynamicBytes(path), Address(recipient), Uint256(deadline), Uint256(amount), Uint256(limit))),
            listOf(object : TypeReference<Uint256>() {}),
        )

    fun quoterCall(name: String, path: ByteArray, amount: BigInteger): Function =
        Function(name, listOf(DynamicBytes(path), Uint256(amount)), listOf(object : TypeReference<Uint256>() {}))

    fun encode(function: Function): String = FunctionEncoder.encode(function)
}

/**
 * Last check before a swap: the on-chain quote must be close to the market price
 * the bot decided on. The swap's slippage limit only protects against movement
 * after the quote, so a thin pool quoting a terrible price would otherwise go
 * through. A buy without a market price is refused; a sell without one goes
 * ahead, because a failed price lookup must not trap a position.
 */
object PriceGuard {
    const val MAX_DEVIATION = 0.05

    /** The reason to refuse, or null if the swap may go ahead. */
    fun refusal(side: String, quotedPrice: Double, marketPrice: Double?): String? {
        if (marketPrice == null || marketPrice <= 0.0) {
            return if (side == "buy") "Buy refused: no market price to check the quote against" else null
        }
        val tooFar = if (side == "buy") {
            quotedPrice > marketPrice * (1 + MAX_DEVIATION)
        } else {
            quotedPrice < marketPrice * (1 - MAX_DEVIATION)
        }
        return if (tooFar) "${side.replaceFirstChar { it.uppercase() }} refused: pool quotes $quotedPrice, market is $marketPrice" else null
    }
}
