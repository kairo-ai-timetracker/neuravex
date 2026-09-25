package com.neuravex.app.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import androidx.core.app.NotificationCompat
import com.neuravex.app.MainActivity
import com.neuravex.app.R
import com.neuravex.app.data.ApiClientFactory
import com.neuravex.app.data.AppConfigStore
import com.neuravex.app.data.AssetBalanceRequest
import com.neuravex.app.data.BinanceExecutionClient
import com.neuravex.app.data.EthereumMainnetBalanceChecker
import com.neuravex.app.data.ExecutionRepository
import com.neuravex.app.data.NeuravexApi
import com.neuravex.app.data.PolygonDexExecutionClient
import com.neuravex.app.data.ReportBalanceRequest
import com.neuravex.app.security.SecureCredentialStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.util.concurrent.atomic.AtomicInteger

/**
 * Keeps polling the backend for approved trades and executes them with a
 * credential held only on this device, as long as the user has switched
 * the AI on (the account's `ai_enabled` setting — checked by asking the
 * backend on every poll, so this is the single source of truth, not a
 * local copy that could drift out of sync with what Settings shows).
 *
 * Why a foreground service and not just a background coroutine: Android
 * aggressively suspends background work (Doze, App Standby, and most OEM
 * skins add another layer on top of stock Android). A foreground service
 * with a persistent notification is the standard, supported way to keep
 * work like this alive — the notification is not optional decoration, it
 * is what the OS requires in exchange for not killing the process.
 *
 * This service NEVER changes what it executes based on anything other
 * than what the backend returned for THIS account — see
 * ExecutionRepository for the actual safety logic; this class is only the
 * scheduling loop around it. Which credential it uses (exchange key or
 * wallet key) is decided per-poll by the account's `execution_venue`
 * setting, never assumed or cached.
 *
 * The persistent notification also doubles as the visible diagnostic
 * channel for the wallet-balance check (see startLoopIfNeeded below) —
 * any failure there (a flaky public RPC, a quote that couldn't be
 * obtained for a very small balance, a missing wallet key) is surfaced as
 * notification text instead of being silently swallowed, since a
 * background service has no other user-visible way to report a problem.
 */
class TradingExecutionService : Service() {

    private val scope = CoroutineScope(SupervisorJob())
    private var loopJob: Job? = null
    private val alertNotificationCounter = AtomicInteger(2000)

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        createAlertNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, buildNotification("Watching for approved trades…"))
        startLoopIfNeeded()
        return START_STICKY
    }

    override fun onDestroy() {
        loopJob?.cancel()
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?) = null

    private fun startLoopIfNeeded() {
        if (loopJob?.isActive == true) return

        loopJob = scope.launch {
            val configStore = AppConfigStore(applicationContext)
            val credentialStore = SecureCredentialStore(applicationContext)

            while (true) {
                if (!configStore.isOnboarded()) {
                    updateNotification("Not set up yet")
                    delay(POLL_INTERVAL_WHEN_PAUSED_MS)
                    continue
                }

                try {
                    val api = ApiClientFactory.create(configStore.backendBaseUrl!!, credentialStore)
                    val accountId = configStore.accountId!!

                    val settingsResponse = api.getSettings(accountId)
                    val settings = settingsResponse.body()
                    if (!settingsResponse.isSuccessful || settings == null) {
                        updateNotification("Could not load settings")
                        delay(POLL_INTERVAL_WHEN_PAUSED_MS)
                        continue
                    }

                    // Best-effort — a failed check here must never disrupt
                    // the trade-execution loop below it (see the function's
                    // own try/catch).
                    checkPendingNotifications(api, accountId)

                    // Report the real wallet balance regardless of whether
                    // AI trading is on — same philosophy as dry-run
                    // decisions: you should be able to see the truth
                    // (your real money) before ever trusting the AI to
                    // act on it. This never touches PendingExecution.
                    //
                    // NOT done while simulating: reporting the real
                    // wallet here writes a PortfolioSnapshot — the exact
                    // same table the Celery-side simulation writes its
                    // fictional equity to (see tasks.py). Since this
                    // phone loop runs far more often (every ~15s) than a
                    // simulation tick (every 5 min), the real-wallet
                    // report was silently winning every time and masking
                    // the simulated numbers on the Dashboard — the
                    // Portfolio figure looked like it came from the
                    // simulation but was actually the real wallet the
                    // whole time. Skipping this block during simulation
                    // is what stops the two from fighting over the same
                    // row.
                    if (settings.execution_venue == "wallet" && !settings.paper_trading_enabled) {
                        val privateKey = credentialStore.getWalletPrivateKey()
                        if (privateKey == null) {
                            updateNotification("No wallet key found on this device")
                        } else {
                            val dexClient = PolygonDexExecutionClient(privateKeyHex = privateKey)
                            try {
                                val result = dexClient.getPortfolioValue()
                                // Same EVM address, so the same key that reads
                                // the Polygon wallet also reads it on Ethereum
                                // mainnet. A failed Ethereum lookup must NOT
                                // drop those holdings from the reported total —
                                // it instead marks the report as `full_wallet =
                                // false`, so the backend never anchors the P/L
                                // baseline on an incomplete number (see
                                // EthereumMainnetBalanceChecker.ValueResult and
                                // dashboard.report_balance on the server).
                                val ethResult = try {
                                    EthereumMainnetBalanceChecker(privateKey).getTotalUsd()
                                } catch (e: Exception) {
                                    null
                                }
                                val combinedTotal = result.totalUsdc + (ethResult?.totalUsd ?: 0.0)
                                val fullWallet = ethResult?.complete == true
                                val reportResponse = api.reportBalance(
                                    accountId,
                                    ReportBalanceRequest(
                                        equity = combinedTotal,
                                        full_wallet = fullWallet,
                                        assets = result.assets.map {
                                            AssetBalanceRequest(
                                                symbol = it.symbol, quantity = it.quantity, price_usd = it.priceUsd,
                                            )
                                        },
                                    ),
                                )
                                // Written unconditionally (success or
                                // $0.00) so the Dashboard always has the
                                // full picture of the last check, not just
                                // the failure case — this is the
                                // persistent counterpart to the
                                // notification text below, read by
                                // DashboardScreen so the user never has to
                                // catch a transient notification.
                                val diagnosticsLines = result.diagnostics +
                                    (ethResult?.diagnostics ?: listOf("Ethereum mainnet: check failed, not included in total"))
                                configStore.lastBalanceDiagnostics = diagnosticsLines.joinToString("\n")
                                // Polygon-only, structured, for the Dashboard's
                                // "Tokens" list — see AppConfigStore.lastHeldAssets
                                // for why Ethereum mainnet isn't included here too.
                                configStore.lastHeldAssets = result.heldAssets
                                if (reportResponse.isSuccessful) {
                                    val text = if (combinedTotal == 0.0 && diagnosticsLines.isNotEmpty()) {
                                        "Balance: $0.00 — ${diagnosticsLines.joinToString("; ")}"
                                    } else {
                                        "Balance: $%.2f (just updated)".format(combinedTotal)
                                    }
                                    updateNotification(text)
                                } else {
                                    updateNotification("Balance report rejected: HTTP ${reportResponse.code()}")
                                }
                            } catch (e: Exception) {
                                // Surfaced via the notification instead of
                                // silently swallowed — this is exactly the
                                // kind of failure (flaky public RPC, a
                                // quote that couldn't be obtained for a
                                // tiny balance) that was previously
                                // invisible.
                                configStore.lastBalanceDiagnostics = "Balance check threw an exception: ${e.javaClass.simpleName}: ${e.message}"
                                updateNotification("Balance check failed: ${e.javaClass.simpleName}: ${e.message}")
                            }
                        }
                    } else if (settings.paper_trading_enabled) {
                        // Real balance intentionally not checked here —
                        // see the comment above. The diagnostics panel
                        // would otherwise keep showing stale real-wallet
                        // data from before simulation was switched on.
                        configStore.lastBalanceDiagnostics = "Simulation mode is on — real wallet balance is not checked. Switch to Live in Settings to see it again."
                        configStore.lastHeldAssets = emptyList()
                        updateNotification("Simulation mode — watching AI decisions")
                    }

                    if (!settings.ai_enabled) {
                        updateNotification("AI trading is off")
                        delay(POLL_INTERVAL_WHEN_PAUSED_MS)
                        continue
                    }

                    val repository = ExecutionRepository(api, accountId)
                    val outcomes = when (settings.execution_venue) {
                        "wallet" -> {
                            val dexClient = buildDexClientOrNull(credentialStore)
                            repository.processPendingWallet(dexClient)
                        }
                        else -> {
                            val binanceClient = buildBinanceClientOrNull(credentialStore)
                            repository.processPending(binanceClient)
                        }
                    }

                    val executedCount = outcomes.count { it is ExecutionRepository.ExecutionOutcome.Executed }
                    val failedCount = outcomes.count { it is ExecutionRepository.ExecutionOutcome.Failed }
                    if (outcomes.isNotEmpty()) {
                        updateNotification(
                            when {
                                failedCount > 0 -> "$executedCount executed, $failedCount failed — check the app"
                                executedCount > 0 -> "$executedCount trade(s) executed"
                                else -> "Watching for approved trades…"
                            }
                        )
                    }
                } catch (e: Exception) {
                    // Network hiccups are expected and must never crash the
                    // service — just try again next interval.
                    updateNotification("Connection issue — retrying…")
                }

                delay(configStore.pollingIntervalSeconds * 1000L)
            }
        }
    }

    private suspend fun buildBinanceClientOrNull(store: SecureCredentialStore): BinanceExecutionClient? {
        val key = store.getApiKey() ?: return null
        val secret = store.getApiSecret() ?: return null
        return BinanceExecutionClient(apiKey = key, apiSecret = secret)
    }

    private suspend fun buildDexClientOrNull(store: SecureCredentialStore): PolygonDexExecutionClient? {
        val privateKey = store.getWalletPrivateKey() ?: return null
        return PolygonDexExecutionClient(privateKeyHex = privateKey)
    }

    /**
     * Polls the backend for one-off alerts (currently: portfolio moved by
     * €5 or more — see check_equity_movement in tasks.py) and shows each
     * as a normal Android notification, then acknowledges it so it's not
     * shown again next poll. Reuses this service's existing polling loop
     * rather than a separate WorkManager job, since this loop is already
     * the thing reliably surviving in the background (see the class
     * docstring on why a foreground service exists at all).
     */
    private suspend fun checkPendingNotifications(api: NeuravexApi, accountId: String) {
        try {
            val response = api.getPendingNotifications(accountId)
            val pending = response.body() ?: return
            for (n in pending) {
                showAlertNotification(n.title, n.body)
                try {
                    api.ackNotification(n.id)
                } catch (e: Exception) {
                    // If the ack itself fails, the same alert may be shown
                    // again next poll — harmless for a notification (unlike
                    // a trade), so not worth retry logic here.
                }
            }
        } catch (e: Exception) {
            // Best-effort alert channel — never worth interrupting the
            // main trade-execution loop for.
        }
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID, "NEURAVEX trading", NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = "Ongoing notification while auto-execution is watching for approved trades"
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun createAlertNotificationChannel() {
        val channel = NotificationChannel(
            ALERT_CHANNEL_ID, "NEURAVEX alerts", NotificationManager.IMPORTANCE_DEFAULT,
        ).apply {
            description = "One-off alerts, e.g. when your portfolio moves by a meaningful amount"
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun showAlertNotification(title: String, body: String) {
        val openAppIntent = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(this, ALERT_CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(body)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentIntent(openAppIntent)
            .setAutoCancel(true)
            .build()
        val manager = getSystemService(NotificationManager::class.java)
        // A fresh id per alert (not the ongoing service's fixed
        // NOTIFICATION_ID) so alerts stack in the tray instead of
        // overwriting each other or the persistent trading notification.
        manager.notify(alertNotificationCounter.incrementAndGet(), notification)
    }

    private fun buildNotification(text: String): Notification {
        val openAppIntent = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("NEURAVEX")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentIntent(openAppIntent)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(text: String) {
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(NOTIFICATION_ID, buildNotification(text))
    }

    companion object {
        private const val CHANNEL_ID = "neuravex_trading"
        private const val ALERT_CHANNEL_ID = "neuravex_alerts"
        private const val NOTIFICATION_ID = 1001
        private const val POLL_INTERVAL_WHEN_PAUSED_MS = 30_000L
    }
}
