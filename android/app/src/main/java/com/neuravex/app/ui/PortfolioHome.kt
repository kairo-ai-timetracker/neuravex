package com.neuravex.app.ui

import androidx.compose.animation.animateColorAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.neuravex.app.data.ApiClientFactory
import com.neuravex.app.data.AppConfigStore
import com.neuravex.app.data.PortfolioOut
import com.neuravex.app.security.SecureCredentialStore
import kotlinx.coroutines.delay

private fun marketLabel(market: String) = when (market) {
    "metals" -> "Metals"
    "stocks" -> "Stocks"
    else -> "Crypto"
}

/**
 * One tab per market, each with its own portfolio. Every tab shows its result
 * since the start, so the markets can be compared at a glance. If the server
 * can't list the portfolios, the crypto dashboard still works on its own.
 */
@Composable
fun PortfolioHome(
    configStore: AppConfigStore,
    selectedMarket: String,
    onSelectMarket: (String) -> Unit,
    onOpenSettings: (PortfolioOut?) -> Unit,
) {
    val context = LocalContext.current
    val api = remember { ApiClientFactory.create(configStore.backendBaseUrl!!, SecureCredentialStore(context)) }
    var portfolios by remember { mutableStateOf<List<PortfolioOut>>(emptyList()) }
    var returns by remember { mutableStateOf<Map<String, Double>>(emptyMap()) }

    LaunchedEffect(Unit) {
        while (true) {
            try {
                if (portfolios.isEmpty()) {
                    val response = api.ensurePortfolios(configStore.accountId!!)
                    if (response.isSuccessful) portfolios = response.body().orEmpty()
                }
                returns = portfolios.mapNotNull { p ->
                    api.getOverview(p.account_id).body()?.let { p.market to it.daily_pnl_pct }
                }.toMap()
            } catch (e: Exception) {
                // The tabs just keep their last numbers until the next round.
            }
            delay(30_000)
        }
    }

    val current = portfolios.firstOrNull { it.market == selectedMarket }
    Column(modifier = Modifier.fillMaxSize()) {
        Box(modifier = Modifier.weight(1f)) {
            key(current?.account_id ?: configStore.accountId) {
                DashboardScreen(
                    configStore = configStore,
                    accountId = current?.account_id ?: configStore.accountId!!,
                    market = current?.market ?: "crypto",
                    onOpenSettings = { onOpenSettings(current) },
                )
            }
        }
        if (portfolios.isNotEmpty()) {
            Row(
                modifier = Modifier.fillMaxWidth().navigationBarsPadding().padding(horizontal = 12.dp, vertical = 8.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                portfolios.forEach { p ->
                    MarketTab(
                        label = marketLabel(p.market),
                        result = returns[p.market],
                        selected = p.market == selectedMarket,
                        onClick = { onSelectMarket(p.market) },
                        modifier = Modifier.weight(1f),
                    )
                }
            }
        }
    }
}

@Composable
private fun MarketTab(label: String, result: Double?, selected: Boolean, onClick: () -> Unit, modifier: Modifier) {
    val edge by animateColorAsState(if (selected) NeuravexColors.VioletGlow else NeuravexColors.SilverDim.copy(alpha = 0.25f), label = "tab")
    val shape = RoundedCornerShape(16.dp)
    Column(
        modifier = modifier
            .background(if (selected) NeuravexColors.Violet.copy(alpha = 0.25f) else NeuravexColors.SurfaceRaised.copy(alpha = 0.8f), shape)
            .border(1.dp, edge, shape)
            .clickable(onClick = onClick)
            .padding(vertical = 10.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(label, color = NeuravexColors.Silver, style = MaterialTheme.typography.labelLarge, textAlign = TextAlign.Center)
        Text(
            result?.let { "%+.1f%%".format(it * 100) } ?: "…",
            color = when {
                result == null || result == 0.0 -> NeuravexColors.SilverDim
                result > 0 -> NeuravexColors.Profit
                else -> NeuravexColors.Loss
            },
            style = MaterialTheme.typography.bodySmall,
        )
    }
}
