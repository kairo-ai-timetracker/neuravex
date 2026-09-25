package com.neuravex.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Divider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.neuravex.app.data.AccountOverview
import com.neuravex.app.data.ApiClientFactory
import com.neuravex.app.data.AppConfigStore
import com.neuravex.app.data.CurrencyConverter
import com.neuravex.app.data.DecisionOut
import com.neuravex.app.data.DailyDecisionStats
import com.neuravex.app.data.EthereumMainnetBalanceChecker
import com.neuravex.app.data.PositionOut
import com.neuravex.app.data.WalletHeldAsset
import com.neuravex.app.security.SecureCredentialStore
import com.neuravex.app.service.TradingExecutionService
import kotlinx.coroutines.delay

@Composable
fun DashboardScreen(configStore: AppConfigStore, onOpenSettings: () -> Unit) {
    val context = LocalContext.current
    val credentialStore = remember { SecureCredentialStore(context) }
    val api = remember { ApiClientFactory.create(configStore.backendBaseUrl!!, credentialStore) }
    val accountId = configStore.accountId!!

    var overview by remember { mutableStateOf<AccountOverview?>(null) }
    var positions by remember { mutableStateOf<List<PositionOut>>(emptyList()) }
    var decisions by remember { mutableStateOf<List<DecisionOut>>(emptyList()) }
    var errorMessage by remember { mutableStateOf<String?>(null) }
    // Read from AppConfigStore on the same poll cycle as everything else
    // below — TradingExecutionService (a separate background process)
    // writes this after every balance check, so polling is how this
    // screen picks up its latest value; there's no live/reactive channel
    // between the two. This is deliberately always visible here, not
    // tucked inside a notification the user has to catch at the right
    // moment.
    var balanceDiagnostics by remember { mutableStateOf<String?>(configStore.lastBalanceDiagnostics) }
    // Structured, per-token version of the above — see
    // AppConfigStore.lastHeldAssets. This is what the "Tokens" list below
    // renders; balanceDiagnostics stays around only as the raw, collapsed
    // "Details" text for diagnosing a failed/partial check.
    var heldAssets by remember { mutableStateOf(configStore.lastHeldAssets) }
    var showBalanceDetails by remember { mutableStateOf(false) }
    // Fetched alongside `overview` on the same poll cycle, purely to show
    // whether what's on screen is simulated or real — see the badge in
    // the header below. Nothing else on this screen currently branches on
    // it; PortfolioSnapshot rows (what `overview` reads from) already
    // look identical whether they came from a simulated tick or a real
    // wallet report, by design (see backend/app/core/tasks.py), so
    // without this badge the two would be visually indistinguishable.
    var paperTradingEnabled by remember { mutableStateOf(true) }
    // In LIVE mode with a wallet venue, TradingExecutionService already
    // folds Ethereum-mainnet holdings into the total it reports to the
    // backend as `equity` (see its report-balance call) — so `ov.equity`
    // itself is already the combined Polygon+Ethereum figure in that case.
    // Tracked here so the Portfolio calculation below adds
    // `otherNetworksUsd` ONLY when the backend hasn't already included it
    // (simulation, or a non-wallet venue) — otherwise Ethereum gets
    // counted twice, inflating Portfolio well past what the wallet
    // actually holds.
    var executionVenue by remember { mutableStateOf<String?>(null) }
    var dailyStats by remember { mutableStateOf<List<DailyDecisionStats>>(emptyList()) }
    // View-only — never traded, never gates any AI decision. See
    // EthereumMainnetBalanceChecker's class doc for why this is kept
    // completely separate from the Polygon side.
    var otherNetworkBalances by remember { mutableStateOf<Map<String, java.math.BigDecimal>>(emptyMap()) }
    // Same holdings as otherNetworkBalances above, but already priced and
    // in the shared WalletHeldAsset shape — feeds the unified "Tokens"
    // list below alongside the Polygon side (heldAssets).
    var otherNetworkAssets by remember { mutableStateOf<List<WalletHeldAsset>>(emptyList()) }
    // USD value of the above, folded into the "Portfolio" headline figure
    // so it matches what a wallet app shows across every network — see
    // EthereumMainnetBalanceChecker.getTotalUsd(). `otherNetworksComplete`
    // is false whenever any lookup or price failed, so the Dashboard can
    // say so instead of silently understating the total.
    var otherNetworksUsd by remember { mutableStateOf(0.0) }
    var otherNetworksComplete by remember { mutableStateOf(true) }
    // Everything the backend reports is USD-equivalent (USDC is a dollar
    // stablecoin) — this is what actually converts the headline figures to
    // euros. See CurrencyConverter's doc for why a live rate is used
    // instead of a hardcoded one.
    var usdToEurRate by remember { mutableStateOf<Double?>(null) }

    // Start the background execution/balance-reporting service whenever
    // the dashboard is visible — see TradingExecutionService's own
    // documentation for why this needs to be a foreground service.
    DisposableEffect(Unit) {
        val intent = android.content.Intent(context, TradingExecutionService::class.java)
        context.startForegroundService(intent)
        onDispose { }
    }

    LaunchedEffect(Unit) {
        while (true) {
            try {
                val overviewResponse = api.getOverview(accountId)
                if (overviewResponse.isSuccessful) {
                    overview = overviewResponse.body()
                    errorMessage = null
                } else {
                    errorMessage = "Overview request failed: HTTP ${overviewResponse.code()} — ${overviewResponse.errorBody()?.string()}"
                }
                val positionsResponse = api.getPositions(accountId)
                if (positionsResponse.isSuccessful) positions = positionsResponse.body().orEmpty()
                val decisionsResponse = api.getDecisions(accountId)
                if (decisionsResponse.isSuccessful) decisions = decisionsResponse.body().orEmpty()
                val dailyStatsResponse = api.getDailyStats(accountId)
                if (dailyStatsResponse.isSuccessful) dailyStats = dailyStatsResponse.body().orEmpty()
                val settingsResponse = api.getSettings(accountId)
                if (settingsResponse.isSuccessful) {
                    settingsResponse.body()?.let {
                        paperTradingEnabled = it.paper_trading_enabled
                        executionVenue = it.execution_venue
                    }
                }
            } catch (e: Exception) {
                errorMessage = "Could not reach the NEURAVEX server: ${e.javaClass.simpleName}: ${e.message}"
            }
            // Cached internally for 15 minutes, so calling this every 15s
            // poll is cheap — see CurrencyConverter.
            try {
                usdToEurRate = CurrencyConverter.getUsdToEurRate()
            } catch (e: Exception) {
                // Keep showing the last known rate rather than blanking it.
            }
            balanceDiagnostics = configStore.lastBalanceDiagnostics
            heldAssets = configStore.lastHeldAssets
            delay(15_000)
        }
    }

    // Separate, much slower loop for the Ethereum-mainnet view-only check
    // — this never gates anything and rarely changes, so polling it every
    // 15s like the main loop above would just be unnecessary load on a
    // public RPC. Only runs at all if a wallet key exists on this device.
    LaunchedEffect(Unit) {
        while (true) {
            val privateKey = credentialStore.getWalletPrivateKey()
            if (privateKey != null) {
                try {
                    val checker = EthereumMainnetBalanceChecker(privateKey)
                    otherNetworkBalances = checker.getBalances()
                    val valueResult = checker.getTotalUsd()
                    otherNetworksUsd = valueResult.totalUsd
                    otherNetworksComplete = valueResult.complete
                    otherNetworkAssets = valueResult.heldAssets
                } catch (e: Exception) {
                    // View-only — a failed check here just means this
                    // section stays empty/stale for one cycle, never an
                    // error worth alarming the user with the way a
                    // failed Polygon check is.
                }
            }
            delay(120_000)
        }
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        item {
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                androidx.compose.foundation.layout.Column {
                    Text("NEURAVEX", style = MaterialTheme.typography.headlineMedium, color = NeuravexColors.Silver)
                    Text("AI Trading. Beyond Human.", color = NeuravexColors.VioletGlow)
                    // Impossible to miss whether real money is at stake —
                    // deliberately more prominent than a subtitle would
                    // be, since confusing a simulated loss for a real one
                    // (or vice versa) is exactly the kind of mistake this
                    // badge exists to prevent.
                    Text(
                        if (paperTradingEnabled) "SIMULATION — no real funds" else "LIVE — real funds",
                        color = if (paperTradingEnabled) NeuravexColors.SilverDim else NeuravexColors.Loss,
                        style = MaterialTheme.typography.labelMedium,
                    )
                }
                TextButton(onClick = onOpenSettings) { Text("Settings") }
            }
        }

        errorMessage?.let { msg ->
            item { Text(msg, color = NeuravexColors.Loss) }
        }

        overview?.let { ov ->
            // `ov.equity` (Polygon) plus the view-only Ethereum-mainnet
            // holdings, so Portfolio matches a wallet app's total across
            // every network — see the `otherNetworksUsd` comment above.
            // Both are USD-equivalent; `rate` converts the whole thing to
            // euros. Today's P/L is left Polygon-only on purpose: it's
            // measured against `live_start_equity` (a Polygon-only
            // baseline), so folding in Ethereum/SHIB price swings would
            // attribute holdings the AI never touches to its performance.
            val rate = usdToEurRate
            // See `executionVenue`'s comment above: don't add Ethereum a
            // second time when the backend's own `equity` already
            // includes it.
            val backendAlreadyIncludesOtherNetworks = executionVenue == "wallet" && !paperTradingEnabled
            item {
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    val portfolioUsd = ov.equity + (if (backendAlreadyIncludesOtherNetworks) 0.0 else otherNetworksUsd)
                    StatCard(
                        "Portfolio",
                        if (rate != null) "€%.2f".format(portfolioUsd * rate) else "$%.2f".format(portfolioUsd),
                        NeuravexColors.Silver,
                    )
                    StatCard(
                        "Today's P/L",
                        if (rate != null) {
                            "${if (ov.daily_pnl >= 0) "+" else ""}€%.2f".format(ov.daily_pnl * rate)
                        } else {
                            "${if (ov.daily_pnl >= 0) "+" else ""}$%.2f".format(ov.daily_pnl)
                        },
                        if (ov.daily_pnl >= 0) NeuravexColors.Profit else NeuravexColors.Loss,
                    )
                }
            }
            if (!backendAlreadyIncludesOtherNetworks && !otherNetworksComplete && otherNetworkBalances.isNotEmpty()) {
                item {
                    Text(
                        "Portfolio may be understated — one or more Ethereum-mainnet balances or prices couldn't be read.",
                        color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
            item {
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    StatCard("Drawdown", "%.1f%%".format(ov.drawdown * 100), NeuravexColors.Silver)
                    StatCard("Regime", ov.market_regime, NeuravexColors.Silver)
                }
            }
        }

        // Unified "Tokens" list — everything actually held, across both
        // networks NEURAVEX can read (Polygon, traded; Ethereum mainnet,
        // view-only — see WalletHeldAsset's doc in Models.kt), rendered
        // the way a wallet app like MetaMask shows what you own: one row
        // per token, icon + name, value + quantity. Replaces the old raw
        // diagnostic-text dump, which is still available (unabridged, for
        // debugging a failed/partial check) behind the "Show details"
        // toggle below.
        run {
            val allTokens = (heldAssets + otherNetworkAssets).sortedByDescending { it.usdValue }
            item { Text("Tokens", style = MaterialTheme.typography.titleMedium, color = NeuravexColors.Silver) }
            if (allTokens.isEmpty()) {
                item { Text("No tokens found in the last wallet check yet.", color = NeuravexColors.SilverDim) }
            } else {
                item {
                    Card(colors = CardDefaults.cardColors(containerColor = NeuravexColors.SurfaceRaised)) {
                        androidx.compose.foundation.layout.Column(modifier = Modifier.padding(vertical = 4.dp)) {
                            allTokens.forEachIndexed { index, asset ->
                                TokenRow(asset)
                                if (index != allTokens.lastIndex) Divider()
                            }
                        }
                    }
                }
            }
            balanceDiagnostics?.let { diag ->
                item {
                    androidx.compose.foundation.layout.Column {
                        TextButton(onClick = { showBalanceDetails = !showBalanceDetails }) {
                            Text(if (showBalanceDetails) "Hide balance-check details" else "Show balance-check details")
                        }
                        if (showBalanceDetails) {
                            androidx.compose.foundation.layout.Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                diag.split("\n").forEach { line ->
                                    Text(line, color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall)
                                }
                            }
                        }
                    }
                }
            }
        }

        item { Divider() }

        item { Text("Open positions", style = MaterialTheme.typography.titleMedium, color = NeuravexColors.Silver) }
        if (positions.isEmpty()) {
            item { Text("No open positions — \"no trade\" is a valid decision.", color = NeuravexColors.SilverDim) }
        } else {
            items(positions) { pos ->
                Card(colors = CardDefaults.cardColors(containerColor = NeuravexColors.SurfaceRaised)) {
                    Column2 {
                        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                            Text(pos.symbol, color = NeuravexColors.Silver)
                            Text(pos.side.uppercase(), color = if (pos.side == "buy") NeuravexColors.Profit else NeuravexColors.Loss)
                        }
                        Text("Qty ${pos.quantity} @ ${pos.entry_price}", color = NeuravexColors.SilverDim)
                    }
                }
            }
        }

        item { Divider() }

        if (dailyStats.isNotEmpty()) {
            item { Text("Daily activity", style = MaterialTheme.typography.titleMedium, color = NeuravexColors.Silver) }
            items(dailyStats) { day ->
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text(day.date, color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall)
                    Text(
                        "${day.executed_count} executed, ${day.skipped_count} skipped",
                        color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
            item { Divider() }
        }

        item {
            Text(
                "Why did NEURAVEX decide this? (last 24h — the server keeps every decision permanently for future analysis)",
                style = MaterialTheme.typography.titleMedium, color = NeuravexColors.Silver,
            )
        }
        items(decisions) { d ->
            Card(colors = CardDefaults.cardColors(containerColor = NeuravexColors.SurfaceRaised)) {
                Column2 {
                    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Text(d.symbol, color = NeuravexColors.Silver)
                        Text(
                            d.action,
                            color = when (d.action) {
                                "BUY" -> NeuravexColors.Profit
                                "SELL" -> NeuravexColors.Loss
                                else -> NeuravexColors.SilverDim
                            },
                        )
                    }
                    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Text(formatDecisionTimestamp(d.created_at), color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall)
                        Text(
                            if (d.executed) "Executed" else "Skipped",
                            color = if (d.executed) NeuravexColors.Profit else NeuravexColors.SilverDim,
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                    d.reasons_for.forEach { Text("+ $it", color = NeuravexColors.Profit, style = MaterialTheme.typography.bodySmall) }
                    d.reasons_against.forEach { Text("- $it", color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall) }
                }
            }
        }
    }
}

/** Server timestamps are UTC ISO-8601 (e.g. "2026-09-20T19:40:50.957445"
 * — naive, no offset, since the backend always writes datetime.utcnow()).
 * This is a lightweight display-only formatter (date + hour:minute), not
 * a timezone conversion — good enough for "when did this happen" at a
 * glance without pulling in a date library for it. */
private fun formatDecisionTimestamp(iso: String): String {
    return try {
        val datePart = iso.substringBefore("T")
        val timePart = iso.substringAfter("T").take(5)
        "$datePart $timePart"
    } catch (e: Exception) {
        iso
    }
}

@Composable
private fun Column2(content: @Composable androidx.compose.foundation.layout.ColumnScope.() -> Unit) {
    androidx.compose.foundation.layout.Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp), content = content)
}

@Composable
private fun StatCard(label: String, value: String, valueColor: androidx.compose.ui.graphics.Color) {
    Card(colors = CardDefaults.cardColors(containerColor = NeuravexColors.SurfaceRaised)) {
        Column2 {
            Text(label, color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall)
            Text(value, color = valueColor, style = MaterialTheme.typography.headlineSmall)
        }
    }
}

/** One row in the "Tokens" list — icon, symbol + network, value + quantity.
 * The icon has no real per-token logo (NEURAVEX has never needed one
 * before this), so it's a plain monogram circle; good enough to scan a
 * list at a glance, the same job a logo does here. */
@Composable
private fun TokenRow(asset: WalletHeldAsset) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Box(
                modifier = Modifier.size(36.dp).background(NeuravexColors.VioletGlow, CircleShape),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    asset.symbol.take(1).uppercase(),
                    color = NeuravexColors.Silver,
                    style = MaterialTheme.typography.labelLarge,
                )
            }
            androidx.compose.foundation.layout.Column {
                Text(asset.symbol, color = NeuravexColors.Silver, style = MaterialTheme.typography.bodyLarge)
                Text(asset.network, color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall)
            }
        }
        androidx.compose.foundation.layout.Column(horizontalAlignment = Alignment.End) {
            Text("$%.2f".format(asset.usdValue), color = NeuravexColors.Silver, style = MaterialTheme.typography.bodyLarge)
            Text(formatTokenQuantity(asset.quantity), color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall)
        }
    }
}

/** Up to 6 decimals, trailing zeros trimmed — e.g. 124.14284926491537
 * shows as "124.142849", and a whole number like 5.0 shows as "5". */
private fun formatTokenQuantity(quantity: Double): String {
    val formatted = "%.6f".format(quantity).trimEnd('0').trimEnd('.')
    return formatted.ifEmpty { "0" }
}
