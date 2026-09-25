package com.neuravex.app.data

/**
 * Token contract addresses on Polygon mainnet (chain ID 137).
 *
 * !! DO NOT add or change an address here without independently verifying
 * it against PolygonScan AND the token's own official documentation. !!
 * A wrong address is not a bug that fails loudly — a swap will simply
 * execute against whatever contract lives at that address (which may not
 * exist, may not be an ERC-20 at all, or may be a scam token squatting on
 * a similar-looking address), and there is no undo on-chain.
 *
 * Every address below was independently verified against PolygonScan at
 * the time it was added. Two well-known tokens were deliberately left out
 * rather than guessed: GHST (Aavegotchi) — its Polygon address is fine,
 * but Binance delisted GHST spot trading in Feb 2026, and this app's
 * price/candle data comes only from Binance's public API (see
 * PublicMarketDataExchange on the backend), so AI analysis for it would
 * simply never produce a decision; and GRT (The Graph) — two different
 * addresses turned up for it during verification, and rather than guess
 * which one is current, it was left out entirely.
 */
object PolygonTokenRegistry {

    data class Token(val symbol: String, val address: String, val decimals: Int)

    // Wrapped MATIC (used as "native" MATIC's ERC-20 wrapper for swaps).
    val WMATIC = Token("WMATIC", "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", 18)

    // Wrapped ETH, bridged to Polygon.
    val WETH = Token("WETH", "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619", 18)

    // Wrapped BTC, bridged to Polygon.
    val WBTC = Token("WBTC", "0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6", 8)

    // Native USDC (issued directly by Circle) — deliberately NOT the older
    // bridged "USDC.e" token, which is a different contract with a
    // different address.
    val USDC = Token("USDC", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6)

    // The older, PoS-bridged USDC ("USDC.e") — a separate contract from
    // native USDC above, from before Circle issued USDC directly on
    // Polygon. Verified against PolygonScan's own token page and Circle's
    // migration announcement (circle.com/blog/what-you-need-to-know-
    // native-usdc-on-polygon-pos), which lists both addresses side by
    // side. Deliberately NOT added to BUILT_IN/ALL below: this exists
    // purely so PolygonDexExecutionClient can read the balance for the
    // wallet's "Tokens" display (so it matches what a wallet app like
    // MetaMask shows), not to make it a second tradable "USDC-like"
    // symbol — NEURAVEX's cost basis, positions and P/L are all tracked
    // against the ONE quote currency (native USDC) already wired through
    // every strategy/risk calculation, and a second one would only
    // confuse that, never help it.
    val USDC_BRIDGED = Token("USDC.e", "0x2791bca1f2de4661ED88A30C99A7a9449Aa84174", 6)

    // (PoS-bridged) USDT — verified directly against its own PolygonScan
    // token page.
    val USDT = Token("USDT", "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", 6)

    // (PoS-bridged) DAI — same verification standard as above.
    val DAI = Token("DAI", "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063", 18)

    // ---- Additional tradable base assets (all against USDC) -----------
    // All 18 decimals unless noted; verified individually against
    // PolygonScan on the date each was added to this file.
    val LINK = Token("LINK", "0xb0897686c545045aFc77CF20eC7A532E3120E0F1", 18)
    val AAVE = Token("AAVE", "0xD6DF932A45C0f255f85145f286eA0b292B21C90B", 18)
    val UNI = Token("UNI", "0xb33EaAd8d922B1083446DC23f610c2567fB5180f", 18)
    val CRV = Token("CRV", "0x172370d5Cd63279eFa6d502DAB29171933a610AF", 18)
    val SAND = Token("SAND", "0xBbba073C31bF03b8ACf7c28EF0738DeCF3695683", 18)
    val MANA = Token("MANA", "0xA1c57f48F0Deb89f569dFbE6E2B7f46D33606fD4", 18)
    val SUSHI = Token("SUSHI", "0x0b3F868E0BE5597D5DB7fEB59E1CADBb0FdDa50a", 18)
    val ONE_INCH = Token("1INCH", "0x9c2C5fd7b07E95EE044DDeba0E97a665F142394F", 18)
    val COMP = Token("COMP", "0x8505B9d2254A7Ae468c0E9dD10Ccea3A837aef5c", 18)
    val BAL = Token("BAL", "0x9a71012B13CA4d3D0Cdc72A177DF3ef03b0E76A3", 18)

    // The verified, built-in universe. 13 tradable base assets (WMATIC,
    // WETH, WBTC, LINK, AAVE, UNI, CRV, SAND, MANA, SUSHI, 1INCH, COMP,
    // BAL) plus the two quote/stable currencies (USDC, USDT, DAI). A 14th,
    // fully user-defined token can be added at runtime via
    // setCustomToken() below — see Settings.
    private val BUILT_IN = listOf(
        WMATIC, WETH, WBTC, USDC, USDT, DAI,
        LINK, AAVE, UNI, CRV, SAND, MANA, SUSHI, ONE_INCH, COMP, BAL,
    )

    // The user's own custom token (symbol + contract address + decimals),
    // set from Settings. Unlike everything above, this one is NOT
    // independently verified by this app — the user is responsible for
    // getting the contract address right. null until the user defines
    // one. See CustomTokenStore for persistence across app restarts.
    private var custom: Token? = null

    fun setCustomToken(token: Token?) {
        custom = token
    }

    fun customToken(): Token? = custom

    val ALL: List<Token>
        get() = custom?.let { BUILT_IN + it } ?: BUILT_IN

    fun bySymbol(symbol: String): Token? = ALL.firstOrNull { it.symbol.equals(symbol, ignoreCase = true) }

    /** Splits "WETH/USDC" into the (base, quote) Token pair, or null if
     * either leg isn't in the verified registry above. */
    fun parsePair(pairSymbol: String): Pair<Token, Token>? {
        val parts = pairSymbol.split("/")
        if (parts.size != 2) return null
        val base = bySymbol(parts[0]) ?: return null
        val quote = bySymbol(parts[1]) ?: return null
        return base to quote
    }
}

/** Polygon network + known Uniswap V3 contract addresses. */
object PolygonNetwork {
    const val CHAIN_ID = 137L
    // PublicNode's endpoint: genuinely free, no API key required for any
    // method including eth_call, and widely used/maintained. Not Ankr's
    // public endpoint (previously used here) — its free tier turned out
    // to require an API key specifically for eth_call (contract calls),
    // which is exactly what every ERC20 balanceOf/Quoter lookup needs.
    // That requirement doesn't show up on simple methods like
    // eth_getBalance, which is why native-asset balance checks kept
    // working while every token balance failed with "-32000
    // Unauthorized" — a distinction only visible once the RPC error
    // message itself was surfaced in the diagnostics instead of being
    // discarded.
    const val DEFAULT_PUBLIC_RPC_URL = "https://polygon-bor-rpc.publicnode.com"

    const val UNISWAP_V3_SWAP_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
    const val UNISWAP_V3_QUOTER = "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"

    // 0.3% fee tier — the most broadly-liquid general-purpose Uniswap V3
    // tier for major pairs.
    const val DEFAULT_POOL_FEE_TIER = 3000
}
