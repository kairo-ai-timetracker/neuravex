package com.neuravex.app.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.web3j.utils.Numeric
import java.math.BigInteger

class UniswapRoutesTest {
    private val usdc = PolygonTokenRegistry.USDC
    private val weth = PolygonTokenRegistry.WETH
    private val link = PolygonTokenRegistry.LINK

    @Test
    fun `direct coin uses its own pool, not the default tier`() {
        val hops = PolygonRoutes.between(usdc, weth)
        assertEquals(listOf(usdc.address, weth.address), hops.tokens)
        assertEquals(listOf(500), hops.fees)
    }

    @Test
    fun `hub route reverses for the sell side`() {
        val buy = PolygonRoutes.between(usdc, PolygonTokenRegistry.AAVE)
        assertEquals(listOf(usdc.address, weth.address, PolygonTokenRegistry.AAVE.address), buy.tokens)
        assertEquals(listOf(500, 3000), buy.fees)
        val sell = PolygonRoutes.between(PolygonTokenRegistry.AAVE, usdc)
        assertEquals(buy.tokens.reversed(), sell.tokens)
        assertEquals(listOf(3000, 500), sell.fees)
    }

    @Test
    fun `coin without a measured route falls back so it can still be sold`() {
        val hops = PolygonRoutes.between(PolygonTokenRegistry.SAND, usdc)
        assertEquals(listOf(3000), hops.fees)
        assertTrue(hops.isDirect)
    }

    @Test
    fun `path packs addresses and three byte fees`() {
        val path = Numeric.toHexStringNoPrefix(UniswapEncoding.path(PolygonRoutes.between(usdc, link)))
        val expected = usdc.address.removePrefix("0x") + "0001f4" + weth.address.removePrefix("0x") +
            "0001f4" + link.address.removePrefix("0x")
        assertEquals(expected.lowercase(), path)
        assertEquals(66 * 2, path.length)
    }

    @Test
    fun `router call has Uniswap's selector and struct layout`() {
        val recipient = "0x1111111111111111111111111111111111111111"
        val path = UniswapEncoding.path(PolygonRoutes.between(usdc, link))
        val encoded = UniswapEncoding.encode(
            UniswapEncoding.routerCall("exactInput", path, recipient, BigInteger.TEN, BigInteger.TWO, BigInteger.ONE)
        )
        assertTrue(encoded, encoded.startsWith("0xc04b8d59"))
        val words = encoded.removePrefix("0xc04b8d59").chunked(64)
        assertEquals(BigInteger.valueOf(0x20), BigInteger(words[0], 16))
        assertEquals(BigInteger.valueOf(0xa0), BigInteger(words[1], 16)) // offset of path inside the struct
        assertEquals(recipient.removePrefix("0x"), words[2].takeLast(40))
        assertEquals(BigInteger.TEN, BigInteger(words[3], 16))
        assertEquals(BigInteger.TWO, BigInteger(words[4], 16))
        assertEquals(BigInteger.ONE, BigInteger(words[5], 16))
        assertEquals(BigInteger.valueOf(66), BigInteger(words[6], 16))
        assertTrue(
            UniswapEncoding.encode(
                UniswapEncoding.routerCall("exactOutput", path, recipient, BigInteger.TEN, BigInteger.TWO, BigInteger.ONE)
            ).startsWith("0xf28c0498")
        )
    }

    @Test
    fun `quoter calls have the right selectors`() {
        val path = UniswapEncoding.path(PolygonRoutes.between(usdc, link))
        assertTrue(UniswapEncoding.encode(UniswapEncoding.quoterCall("quoteExactInput", path, BigInteger.ONE)).startsWith("0xcdca1753"))
        assertTrue(UniswapEncoding.encode(UniswapEncoding.quoterCall("quoteExactOutput", path, BigInteger.ONE)).startsWith("0x2f80bb1d"))
    }

    @Test
    fun `price guard refuses a bad quote in either direction`() {
        assertTrue(PriceGuard.refusal("buy", 106.0, 100.0) != null)
        assertTrue(PriceGuard.refusal("sell", 94.0, 100.0) != null)
        assertEquals(null, PriceGuard.refusal("buy", 104.0, 100.0))
        assertEquals(null, PriceGuard.refusal("sell", 96.0, 100.0))
        // A better price than the market is never a reason to refuse.
        assertEquals(null, PriceGuard.refusal("buy", 80.0, 100.0))
        assertEquals(null, PriceGuard.refusal("sell", 120.0, 100.0))
    }

    @Test
    fun `without a market price buys stop but sells continue`() {
        assertTrue(PriceGuard.refusal("buy", 100.0, null) != null)
        assertEquals(null, PriceGuard.refusal("sell", 100.0, null))
    }
}
