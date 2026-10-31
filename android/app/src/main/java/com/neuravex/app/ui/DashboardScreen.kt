package com.neuravex.app.ui

import androidx.compose.animation.animateContentSize
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.sp
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
    var showSkippedDecisions by remember { mutableStateOf(false) }
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
            // BUG FIX: TradingExecutionService deliberately clears
            // configStore.lastHeldAssets in simulation mode (there is no
            // real wallet to check), which left the "Tokens" list below
            // permanently empty while simulating — the ONLY place that
            // still showed simulated holdings was the separate "Open
            // positions" card, in a plain qty@price format, not the
            // Tokens list the user actually wanted. Simulation already has
            // everything needed to fill that list itself: `positions`
            // (fetched above) is dashboard.py's own synthesized view of
            // AccountSettings.paper_balances/paper_positions — the exact
            // same data "Open positions" already reads — reshaped here
            // into the shared WalletHeldAsset rows, plus the simulated
            // USDC cash itself (a real wallet's Tokens list always
            // includes its USDC row too; the simulated one was simply
            // missing it).
            heldAssets = if (paperTradingEnabled) {
                val simulatedTokens = positions.map { p ->
                    WalletHeldAsset(
                        symbol = p.symbol.substringBefore("/"), network = "Simulatie",
                        quantity = p.quantity, usdValue = p.quantity * p.entry_price,
                    )
                }
                val simulatedCash = overview?.cash ?: 0.0
                if (simulatedCash > 0.0) {
                    simulatedTokens + WalletHeldAsset("USDC", "Simulatie", simulatedCash, simulatedCash)
                } else {
                    simulatedTokens
                }
            } else {
                configStore.lastHeldAssets
            }
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

    val botIsHolding = positions.isNotEmpty()
    val orbMood = when {
        errorMessage != null -> NeuravexColors.Amber
        (overview?.drawdown ?: 0.0) >= DRAWDOWN_STOP * 0.7 -> NeuravexColors.Loss
        botIsHolding -> NeuravexColors.Profit
        else -> NeuravexColors.VioletGlow
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = 20.dp, vertical = 24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column {
                    Text("NEURAVEX", style = MaterialTheme.typography.titleLarge, color = NeuravexColors.Silver, letterSpacing = 4.sp)
                    // Impossible to miss whether real money is at stake —
                    // confusing a simulated loss for a real one (or vice
                    // versa) is exactly what this badge exists to prevent.
                    ModePill(paperTradingEnabled)
                }
                TextButton(onClick = onOpenSettings) { Text("⚙  Settings", color = NeuravexColors.Silver) }
            }
        }

        errorMessage?.let { msg ->
            item {
                GlassCard(modifier = Modifier.fillMaxWidth(), glow = NeuravexColors.Amber) {
                    Text(msg, color = NeuravexColors.Amber, style = MaterialTheme.typography.bodySmall)
                }
            }
        }

        // `ov.equity` (Polygon) plus the view-only Ethereum-mainnet holdings,
        // so Portfolio matches a wallet app's total across every network —
        // see `otherNetworksUsd` above. Both are USD-equivalent; `rate`
        // converts to euros. Today's P/L stays Polygon-only: it's measured
        // against a Polygon-only baseline, and Ethereum swings aren't the
        // AI's doing. A simulation never mixes in anything real, and a live
        // wallet account's equity already includes Ethereum.
        val rate = usdToEurRate
        val backendAlreadyIncludesOtherNetworks =
            paperTradingEnabled || (executionVenue == "wallet" && !paperTradingEnabled)
        item {
            Column(modifier = Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
                NeuroOrb(mood = orbMood, active = botIsHolding) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text("PORTFOLIO", color = NeuravexColors.SilverDim, style = MaterialTheme.typography.labelSmall, letterSpacing = 2.sp)
                        val ov = overview
                        if (ov == null) {
                            Text("…", color = NeuravexColors.Silver, style = MaterialTheme.typography.headlineMedium)
                        } else {
                            val portfolioUsd = ov.equity + (if (backendAlreadyIncludesOtherNetworks) 0.0 else otherNetworksUsd)
                            CountingText(
                                value = portfolioUsd * (rate ?: 1.0),
                                format = { (if (rate != null) "€%,.2f" else "$%,.2f").format(it) },
                                color = NeuravexColors.Silver,
                                style = MaterialTheme.typography.headlineMedium,
                            )
                        }
                    }
                }
                Text(
                    botThought(errorMessage != null, positions.size, overview?.market_regime, overview?.drawdown ?: 0.0),
                    color = NeuravexColors.Silver,
                    style = MaterialTheme.typography.bodyMedium,
                    textAlign = TextAlign.Center,
                )
            }
        }

        overview?.let { ov ->
            item {
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    GlassCard(modifier = Modifier.weight(1f), glow = if (ov.daily_pnl >= 0) NeuravexColors.Profit else NeuravexColors.Loss) {
                        TileLabel("Today")
                        CountingText(
                            value = ov.daily_pnl * (rate ?: 1.0),
                            format = { "${if (it >= 0) "+" else "-"}${if (rate != null) "€" else "$"}%,.2f".format(kotlin.math.abs(it)) },
                            color = if (ov.daily_pnl >= 0) NeuravexColors.Profit else NeuravexColors.Loss,
                        )
                    }
                    GlassCard(modifier = Modifier.weight(1f), glow = NeuravexColors.Aurora) {
                        TileLabel("Market")
                        Text(friendlyRegime(ov.market_regime), color = NeuravexColors.Silver, style = MaterialTheme.typography.titleMedium)
                    }
                }
            }
            item {
                val used = (ov.drawdown / DRAWDOWN_STOP).toFloat()
                val gaugeColor = when {
                    used >= 0.7f -> NeuravexColors.Loss
                    used >= 0.4f -> NeuravexColors.Amber
                    else -> NeuravexColors.Profit
                }
                GlassCard(modifier = Modifier.fillMaxWidth(), glow = gaugeColor) {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                        ArcGauge(used, gaugeColor, Modifier.size(width = 84.dp, height = 46.dp))
                        Column {
                            Text("Down %.1f%% from the peak".format(ov.drawdown * 100), color = NeuravexColors.Silver)
                            Text(
                                "The bot sells everything and pauses at %.0f%%.".format(DRAWDOWN_STOP * 100),
                                color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall,
                            )
                        }
                    }
                }
            }
            if (!backendAlreadyIncludesOtherNetworks && !otherNetworksComplete && otherNetworkBalances.isNotEmpty()) {
                item {
                    Text(
                        "Portfolio may be understated — one or more Ethereum-mainnet balances or prices couldn't be read.",
                        color = NeuravexColors.Amber, style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        }

        // Everything actually held, the way a wallet app shows it. A real
        // Ethereum-mainnet holding has no place in a simulation's list. The
        // raw balance-check text stays behind a toggle for debugging.
        run {
            val allTokens = (heldAssets + (if (paperTradingEnabled) emptyList() else otherNetworkAssets))
                .sortedByDescending { it.usdValue }
            item { SectionTitle("What you hold") }
            item {
                GlassCard(modifier = Modifier.fillMaxWidth()) {
                    if (allTokens.isEmpty()) {
                        Text("Nothing found in the last wallet check yet.", color = NeuravexColors.SilverDim)
                    }
                    allTokens.forEach { asset -> TokenRow(asset, usdToEurRate) }
                    balanceDiagnostics?.let { diag ->
                        TextButton(onClick = { showBalanceDetails = !showBalanceDetails }) {
                            Text(if (showBalanceDetails) "Hide technical details" else "Technical details", color = NeuravexColors.SilverDim)
                        }
                        if (showBalanceDetails) {
                            diag.split("\n").forEach { line ->
                                Text(line, color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall)
                            }
                        }
                    }
                }
            }
        }

        item { SectionTitle("Open trades") }
        if (positions.isEmpty()) {
            item { Text("No open trades — waiting is a valid decision.", color = NeuravexColors.SilverDim) }
        } else {
            items(positions) { pos ->
                GlassCard(modifier = Modifier.fillMaxWidth(), glow = if (pos.side == "buy") NeuravexColors.Profit else NeuravexColors.Loss) {
                    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Text(pos.symbol, color = NeuravexColors.Silver, style = MaterialTheme.typography.titleMedium)
                        Text(pos.side.uppercase(), color = if (pos.side == "buy") NeuravexColors.Profit else NeuravexColors.Loss)
                    }
                    Text("${formatTokenQuantity(pos.quantity)} @ ${pos.entry_price}", color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall)
                }
            }
        }

        // Most decisions are "no trade"; those hide behind the switch so the
        // actual trades don't drown in them. The server keeps them all.
        val shownDecisions = if (showSkippedDecisions) decisions else decisions.filter { it.executed }
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                SectionTitle("Why it did what it did")
                TextButton(onClick = { showSkippedDecisions = !showSkippedDecisions }) {
                    Text(if (showSkippedDecisions) "Trades only" else "Show all", color = NeuravexColors.VioletGlow)
                }
            }
        }
        if (shownDecisions.isEmpty()) {
            item { Text("No trades in the last 24 hours.", color = NeuravexColors.SilverDim) }
        }
        items(shownDecisions) { d -> DecisionCard(d) }

        if (dailyStats.isNotEmpty()) {
            item { SectionTitle("Daily activity") }
            item {
                GlassCard(modifier = Modifier.fillMaxWidth()) {
                    dailyStats.forEach { day ->
                        Row(modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp), horizontalArrangement = Arrangement.SpaceBetween) {
                            Text(day.date, color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall)
                            Text(
                                "${day.executed_count} traded · ${day.skipped_count} skipped",
                                color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall,
                            )
                        }
                    }
                }
            }
        }
    }
}

/** The account's drawdown stop (config/neuravex.yaml, risk.max_drawdown). */
private const val DRAWDOWN_STOP = 0.10

/** What the bot is up to, in one plain sentence. */
private fun botThought(offline: Boolean, openTrades: Int, regime: String?, drawdown: Double): String = when {
    offline -> "I can't reach the server right now. I'll keep trying."
    drawdown >= DRAWDOWN_STOP -> "Safety stop reached. I've paused buying to protect what's left."
    openTrades == 1 -> "Holding 1 trade and watching its stop-loss."
    openTrades > 1 -> "Holding $openTrades trades and watching their stop-losses."
    regime == "CRASH" -> "The market is crashing. I'm staying out."
    regime == "BEAR" -> "The market is falling. I'm being picky about buying."
    regime == null -> "Waking up…"
    else -> "Watching the market for a good moment to buy."
}

private fun friendlyRegime(regime: String): String = when (regime) {
    "STRONG_BULL" -> "Rising fast"
    "BULL" -> "Rising"
    "SIDEWAYS" -> "Sideways"
    "BEAR" -> "Falling"
    "HIGH_VOLATILITY" -> "Wild swings"
    "CRASH" -> "Crashing"
    else -> regime
}

@Composable
private fun ModePill(simulation: Boolean) {
    val color = if (simulation) NeuravexColors.Aurora else NeuravexColors.Loss
    Text(
        if (simulation) "SIMULATION · no real funds" else "LIVE · real funds",
        color = color,
        style = MaterialTheme.typography.labelMedium,
        modifier = Modifier
            .padding(top = 4.dp)
            .background(color.copy(alpha = 0.14f), RoundedCornerShape(50))
            .padding(horizontal = 10.dp, vertical = 3.dp),
    )
}

@Composable
private fun SectionTitle(text: String) {
    Text(text, style = MaterialTheme.typography.titleMedium, color = NeuravexColors.Silver)
}

@Composable
private fun TileLabel(text: String) {
    Text(text, color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall)
}

@Composable
private fun DecisionCard(d: DecisionOut) {
    var expanded by remember { mutableStateOf(false) }
    val color = when (d.action) {
        "BUY" -> NeuravexColors.Profit
        "SELL" -> NeuravexColors.Loss
        else -> NeuravexColors.SilverDim
    }
    GlassCard(
        modifier = Modifier.fillMaxWidth().clickable { expanded = !expanded }.animateContentSize(),
        glow = color,
    ) {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(d.symbol, color = NeuravexColors.Silver, style = MaterialTheme.typography.titleMedium)
            Text(if (d.action == "NO_TRADE") "WAIT" else d.action, color = color)
        }
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(formatDecisionTimestamp(d.created_at), color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall)
            Text(if (expanded) "▲" else "why? ▼", color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall)
        }
        if (expanded) {
            d.reasons_for.forEach { Text("+ $it", color = NeuravexColors.Profit, style = MaterialTheme.typography.bodySmall) }
            d.reasons_against.forEach { Text("- $it", color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall) }
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

/** One row in the "Tokens" list — icon, symbol + network, value + quantity.
 * The icon has no real per-token logo (NEURAVEX has never needed one
 * before this), so it's a plain monogram circle; good enough to scan a
 * list at a glance, the same job a logo does here. */
@Composable
private fun TokenRow(asset: WalletHeldAsset, eurRate: Double?) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Box(
                modifier = Modifier.size(36.dp).background(
                    Brush.linearGradient(listOf(NeuravexColors.Violet, NeuravexColors.Aurora)), CircleShape,
                ),
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
            // Same USD-equivalent -> EUR conversion as the Portfolio card
            // above (see CurrencyConverter's doc) — this used to always
            // show a raw "$" dollar figure here while Portfolio showed the
            // converted "€" figure, so the same underlying balance looked
            // like two different amounts depending on which card you read.
            Text(
                if (eurRate != null) "€%.2f".format(asset.usdValue * eurRate) else "$%.2f".format(asset.usdValue),
                color = NeuravexColors.Silver, style = MaterialTheme.typography.bodyLarge,
            )
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
